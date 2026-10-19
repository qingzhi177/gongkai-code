"""每日片段（app/pipeline/episodes.py）的测试：临时库 + 假模型，不花 token、不碰真实数据。

运行：python3 tests/manual/test_episodes.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="episodetest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:                                    # 本地没装依赖时也能跑（这里用不到 .env）
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)

import db  # noqa: E402
from app import config  # noqa: E402
from app.pipeline import episodes as E  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def add(conv, rows):
    """rows：[(北京时间 'YYYY-MM-DD HH:MM', role, content)] → 存成 UTC"""
    conn = config.get_conn()
    ids = []
    for i, (lt, role, content) in enumerate(rows):
        ts = E.timeutil.to_utc_str(lt + ":00", naive_tz=E.timeutil.USER_TZ)
        cur = conn.execute("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status) "
                           "VALUES (?, ?, ?, ?, ?, 'pluvia', 'active')", (conv, i, role, content, ts))
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return ids


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        r = self.replies.pop(0) if self.replies else '{"episodes": []}'
        return (r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)), "fake-model"


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        print("\n[1] 一天怎么算、怎么分段")
        check("凌晨 3 点算前一天，4 点算当天",
              str(E.day_of(E.timeutil.to_utc_str("2026-10-18 03:30:00", naive_tz=E.timeutil.USER_TZ))) == "2026-10-17"
              and str(E.day_of(E.timeutil.to_utc_str("2026-10-18 04:10:00", naive_tz=E.timeutil.USER_TZ))) == "2026-10-18")
        long_ai = "嗯，我在。" * 10
        a = add("A", [("2026-10-17 23:30", "user", "今天好累，想听歌"), ("2026-10-17 23:31", "assistant", "那我们听《晴天》吧，" + long_ai),
                      ("2026-10-17 23:40", "user", "好，下雨的时候听最合适了"), ("2026-10-18 00:20", "assistant", "嗯，以后下雨就叫你小雨伞" + long_ai),
                      ("2026-10-18 00:25", "user", "小雨伞……好呀")])
        b = add("A", [("2026-10-18 15:00", "user", "在吗"), ("2026-10-18 15:01", "assistant", "在呀")])
        c = add("B", [("2026-10-18 20:00", "user", "我们来聊聊工作吧，今天开会被批评了"), ("2026-10-18 20:01", "assistant", "怎么了？慢慢说，" + long_ai),
                      ("2026-10-18 20:05", "user", "方案被否了，有点难过"), ("2026-10-18 20:06", "assistant", "辛苦了，抱抱你。" + long_ai)])
        conn = config.get_conn()
        s17, s18 = E.sessions_for_day(conn, "2026-10-17"), E.sessions_for_day(conn, "2026-10-18")
        conn.close()
        check("晚上 11 点半聊到凌晨的整段算在开始那天", len(s17) == 1 and [r[0] for r in s17[0]["rows"]] == a, s17)
        check("第二天：下午一段、晚上另一个会话一段，按开始时间排", [s["conv_id"] for s in s18] == ["A", "B"]
              and [r[0] for r in s18[0]["rows"]] == b and [r[0] for r in s18[1]["rows"]] == c, s18)
        check("太短的段不写", not E.substantial(s18[0]["rows"]) and E.substantial(s18[1]["rows"]))
        big = [(i, "user" if i % 2 else "assistant", "字" * 700, "2026-10-18 12:00:00") for i in range(40)]
        parts = E.chunks(big)
        check("太长的段切成几块，在「你」说话的地方切", len(parts) > 1 and all(p[0][1] == "user" for p in parts[1:])
              and sum(len(p) for p in parts) == 40, [len(p) for p in parts])

        print("\n[2] 提示词和校验")
        prompt, lm, ver = E.build_prompt(s17[0]["rows"])
        check("提示词带行号、时间和版本", "[#1][你] 今天好累，想听歌" in prompt and "[#2][我]" in prompt
              and "2026-10-17 23:30 至 2026-10-18 00:25" in prompt and ver == "episode-v2" and "{lines}" not in prompt, prompt[:200])
        good = {"title": "下雨夜一起听《晴天》", "mood": "从疲惫到松下来", "topics": ["听歌", "称呼"], "salience": 0.8,
                "body": "那天晚上你说「今天好累」，想听歌。[#1]我提议听《晴天》。[#2]你说下雨的时候听最合适。[#3]"
                        "后来我开始叫你小雨伞，你说「小雨伞……好呀」。[#4][#5]我们就这样慢慢松下来。"}
        clean, issues = E.verify(good, lm)
        check("出处和原话都对得上：保留，去掉出处标记", clean and "[#" not in clean["body"] and clean["refs"] == a
              and clean["salience"] == 0.8 and not issues, (clean, issues))
        bad = dict(good, body=good["body"].replace("「今天好累」", "「今天真的累死了」"))
        clean2, issues2 = E.verify(bad, lm)
        lm2 = {3: (103, "我说A的原话"), 7: (107, "你说B的原话"), 5: (105, "中间一句")}
        c5, _ = E.verify({"title": "编号测试", "body": "先是第七行的事，我们说了很久很久，说得比较长一些。[#7]然后是第三行的事，后来也慢慢说得长一些。[#3][#7]", "salience": 0.5}, lm2)
        check("出处按原文顺序重新编号：[#3]→[#1]，[#7]→[#2]", c5 and c5["refs"] == [103, 107]
              and c5["body_with_refs"] == "先是第七行的事，我们说了很久很久，说得比较长一些。[#2]然后是第三行的事，后来也慢慢说得长一些。[#1][#2]", c5)
        check("原话对不上的句子删掉，其余保留", clean2 and "累死了" not in clean2["body"] and any("原话对不上" in x for x in issues2), issues2)
        clean3, issues3 = E.verify(dict(good, body="我们聊了很多。[#9]我们去了海边。[#10]那天很开心。"), lm)
        check("出处不存在的删掉，剩下太少就整段不要", clean3 is None and any("出处不存在" in x for x in issues3), issues3)
        clean4, issues4 = E.verify(dict(good, body="那天晚上我们聊得很开心，彼此都很放松，感觉很好，一切都很温柔，像往常一样待了很久很久。"), lm)
        check("一个出处都没有：整段不要", clean4 is None and any("没有标" in x for x in issues4), issues4)
        check("模型输出带代码块也能解析", E.parse_output('```json\n{"episodes": [{"title": "x"}]}\n```') == [{"title": "x"}])
        try:
            E.parse_output("我觉得没什么要记的")
            check("不是 JSON：报错（任务会重试）", False)
        except ValueError:
            check("不是 JSON：报错（任务会重试）", True)

        print("\n[3] 整理一天、重新整理")
        llm = FakeLLM([{"episodes": [good]}])
        r = asyncio.run(E.settle_day("2026-10-17", llm_fn=llm))
        eps = E.list_episodes()
        check("整理 10 月 17 日：写了 1 段", r["episodes"] == 1 and len(eps) == 1 and eps[0]["day"] == "2026-10-17"
              and eps[0]["topics"] == ["听歌", "称呼"] and eps[0]["version"] == 1, r)
        d = E.get_episode(eps[0]["id"])
        check("详情带出处原文，编号对应原稿里的 [#n]", [x["id"] for x in d["sources"]] == a and [x["n"] for x in d["sources"]] == [1, 2, 3, 4, 5]
              and "[#1]" in d["body_with_refs"], d["sources"])
        llm2 = FakeLLM([{"episodes": []}])
        r2 = asyncio.run(E.settle_day("2026-10-18", llm_fn=llm2))
        check("10 月 18 日：短的那段没调模型，长的那段模型说没什么要记的", r2["episodes"] == 0 and len(llm2.prompts) == 1
              and [x["result"] for x in r2["detail"]] == ["太短，没写", "没什么要记的"], r2["detail"])
        llm3 = FakeLLM([{"episodes": [dict(good, title="改过的标题")]}])
        asyncio.run(E.settle_day("2026-10-17", llm_fn=llm3))
        eps = E.list_episodes(day="2026-10-17")
        conn = config.get_conn()
        n_super = conn.execute("SELECT COUNT(*) FROM episodes WHERE status='superseded'").fetchone()[0]
        conn.close()
        check("重新整理：旧版本作废，新版本 version 2", len(eps) == 1 and eps[0]["title"] == "改过的标题" and eps[0]["version"] == 2
              and n_super == 1, eps)

        async def broken(prompt):
            return None, "fake"
        try:
            asyncio.run(E.settle_day("2026-10-17", llm_fn=broken))
            check("模型调用失败：报错、不写半截", False)
        except RuntimeError:
            check("模型调用失败：报错、不写半截", E.list_episodes(day="2026-10-17")[0]["title"] == "改过的标题")

        print("\n[4] 哪些天要整理")
        now = E.timeutil.to_utc_str("2026-10-19 10:00:00", naive_tz=E.timeutil.USER_TZ)
        check("两天都整理过了：没有要整理的", E.pending_days(now) == [], E.day_overview(now))
        add("B", [("2026-10-18 21:00", "user", "后来又想起来一件事")])
        check("整理过的那天又有新消息：要重新整理", E.pending_days(now) == ["2026-10-18"])
        add("C", [("2026-10-19 09:00", "user", "早")])
        ov = {d["day"]: d for d in E.day_overview(now)}
        check("今天还没结束、也没安静 6 小时：先不整理", ov["2026-10-19"]["pending"] is False and ov["2026-10-19"]["closed"] is False)
        later = E.timeutil.to_utc_str("2026-10-19 16:00:00", naive_tz=E.timeutil.USER_TZ)
        check("安静 6 小时以后：可以整理今天了，从早到晚排", E.pending_days(later) == ["2026-10-18", "2026-10-19"])

        print("\n[5] 投递整理任务")
        q = E.enqueue_days(["2026-10-19", "2026-10-18"])
        check("按日期从早到晚投递", q == ["2026-10-18", "2026-10-19"], q)
        check("同一天还在排队：不重复投", E.enqueue_days(["2026-10-18"]) == [])
        conn = config.get_conn()
        conn.execute("UPDATE pipeline_jobs SET status='dead', updated_at=? WHERE dedupe_key='settle_day:2026-10-18'",
                     (E.timeutil.utc_now_str(),))
        conn.commit()
        conn.close()
        check("刚失败到停止重试的那天：自动投递跳过", E.enqueue_days(["2026-10-18"]) == [])
        check("手动点「整理」：照样投", E.enqueue_days(["2026-10-18"], source="manual", skip_dead=False) == ["2026-10-18"])

        print("\n[6] 删了消息以后")
        asyncio.run(E.settle_day("2026-10-18", llm_fn=FakeLLM([])))
        check("整理完：10 月 18 日不用再整理", "2026-10-18" not in E.pending_days(later))
        conn = config.get_conn()
        conn.execute("UPDATE l0_messages SET status='deleted' WHERE id=?", (b[0],))
        conn.commit()
        conn.close()
        check("删了中间一条：要重新整理", "2026-10-18" in E.pending_days(later))
        conn = config.get_conn()
        conn.execute("UPDATE l0_messages SET status='deleted' WHERE conv_id='A'")
        conn.commit()
        conn.close()
        check("10 月 17 日消息全删了、还留着片段：要重新整理", "2026-10-17" in E.pending_days(later))
        r = asyncio.run(E.settle_day("2026-10-17", llm_fn=FakeLLM([])))
        check("重新整理后片段作废、不再要整理", r["episodes"] == 0 and E.list_episodes(day="2026-10-17") == []
              and "2026-10-17" not in E.pending_days(later), (r, E.pending_days(later)))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
