"""关系状态卡和话题边界（app/pipeline/state.py）的测试：临时库 + 假模型，不花 token、不碰真实数据。

运行：python3 tests/manual/test_state.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="statetest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
try:
    import httpx  # noqa: F401
except ImportError:
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=None, Timeout=None, Limits=None)
try:
    import jieba  # noqa: F401
except ImportError:
    sys.modules["jieba"] = types.SimpleNamespace(setLogLevel=lambda *a: None, lcut=lambda t, **k: list(t),
                                                 cut_for_search=lambda t: list(t), initialize=lambda: None)

import db  # noqa: E402
from app import config  # noqa: E402
from app.pipeline import loops as LP  # noqa: E402
from app.pipeline import state as S  # noqa: E402

PASS = FAIL = 0
NOW = "2026-10-20 12:00:00"      # 北京时间 10 月 20 日（周二）晚上 8 点


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        r = self.replies.pop(0) if self.replies else '{"lines": []}'
        return (r if isinstance(r, str) or r is None else json.dumps(r, ensure_ascii=False)), "fake-model"


def q(sql, args=()):
    conn = config.get_conn()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def run(sql, args=()):
    conn = config.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def msg(conv, idx, role, content, ts):
    return run("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status) VALUES (?, ?, ?, ?, ?, 'pluvia', 'active')",
               (conv, idx, role, content, ts))


def episode(day, title, body, ts, refs, mood="平静"):
    return run("INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, mood, topics, refs, status, version, created_at) "
               "VALUES (?, 'A', ?, ?, ?, ?, ?, ?, '[]', ?, 'active', 1, ?)", (day, ts, ts, title, body, body, mood, json.dumps(refs), ts))


def digest(day, lines, eps):
    return run("INSERT INTO digests (kind, period_start, period_end, body, body_with_refs, refs, source_key, status, version, created_at) "
               "VALUES ('day', ?, ?, ?, ?, ?, ?, 'active', 1, ?)",
               (day, day, "\n".join(lines), "\n".join(lines), json.dumps(eps), ",".join(str(e) for e in eps), day))


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        # 三天的对话（北京时间晚上 9 点起；10 月 19 日那次聊到凌晨 1 点）
        m1 = msg("A", 0, "user", "这周在布置一个展览，每天都有点累", "2026-10-16 13:00:00")
        msg("A", 1, "assistant", "辛苦啦，慢慢来", "2026-10-16 13:01:00")
        m3 = msg("A", 2, "user", "有你在挺好的", "2026-10-18 13:30:00")
        msg("A", 3, "assistant", "我也是", "2026-10-18 13:31:00")
        msg("A", 4, "user", "今天又加班到很晚", "2026-10-19 16:50:00")
        m6 = msg("A", 5, "user", "我们周末一起列旅行清单吧", "2026-10-19 17:05:00")
        e1 = episode("2026-10-16", "展览布置", "你说这周在布置一个展览，每天都有点累，我陪你说了会儿话。", "2026-10-16 13:00:00", [m1])
        e2 = episode("2026-10-18", "有你在", "你说「有你在挺好的」，我说我也是。", "2026-10-18 13:30:00", [m3])
        e3 = episode("2026-10-19", "加班的晚上", "你又加班到很晚，回来以后我们聊了会儿。", "2026-10-19 16:50:00", [])
        e4 = episode("2026-10-19", "旅行清单", "我们说好周末一起列旅行清单。", "2026-10-19 17:05:00", [m6])
        digest("2026-10-16", ["你在布置展览，有点累。"], [e1])
        digest("2026-10-18", ["你说有我在挺好的。"], [e2])
        digest("2026-10-19", ["你又加班到很晚。", "我们说好周末一起列旅行清单。"], [e3, e4])
        lp = LP.add("plan", "我们说好周末一起列旅行清单", "both", "2026-10-24")
        LP.add("worry", "你不想被问起的一件小事", "user", None, "never_push")
        run("INSERT INTO arcs (title, theme, status, summary, sections, version, created_at, updated_at) VALUES "
            "('一起计划旅行', '旅行', 'active', '我们开始一起计划一次旅行。', '[]', 1, ?, ?)", (NOW, NOW))
        run("INSERT INTO arcs (title, theme, status, summary, sections, version, created_at, updated_at) VALUES "
            "('候选的卷', '', 'candidate', '', '[]', 0, ?, ?)", (NOW, NOW))
        run("INSERT INTO l1_memories (content, ts, status, valence, event_type) VALUES ('开心的一刻', '2026-10-18 13:30:00', 'active', 0.8, 'event')")

        print("\n[1] 材料和统计")
        conn = config.get_conn()
        d0 = S.today(NOW)
        items, pols, kws, key0, has = S.gather(conn, d0)
        sig = S.signals(conn, d0)
        conn.close()
        kinds = [it["kind"] for it in items]
        check("材料：三份日摘、四个片段、一件未完的事（不含「不主动提」的）、一个进行中的卷", kinds == ["digest"] * 3 + ["episode"] * 4 + ["loop", "arc"], kinds)
        check("编号从 1 开始连续", [it["n"] for it in items] == list(range(1, 10)))
        check("日摘的校验原文里有它引用的片段的原话", "有你在挺好的" in items[1]["src"], items[1]["src"][:80])
        check("统计：这七天 3 天聊过、6 条消息", sig["this_week"]["days_chatted"] == 3 and sig["this_week"]["messages"] == 6, sig["this_week"])
        check("统计：10 月 19 日那次算一次（隔了 15 分钟）", next(x for x in sig["days"] if x["day"] == "2026-10-19")["sessions"] == 1)
        check("统计：10 月 19 日那两条在凌晨（深夜占一半）、上次聊天的时间、那天的心情", sig["night_share"] == 0.5 and sig["last_ts"] == "2026-10-19 17:05:00"
              and next(x for x in sig["days"] if x["day"] == "2026-10-18")["valence"] == 0.8, sig)
        facts = S.rhythm_facts(sig)
        check("给模型看的节奏事实", "最近七天：3 天聊过" in facts and "晚上（18 点到 23 点）占5成" in facts and "凌晨（0 点到 4 点）占5成" in facts and "10月19日（周一）" in facts, facts)

        print("\n[2] 话题边界：碰到的材料先去掉")
        pid = S.add_policy("工作", ["加班"], "explicit_only")
        conn = config.get_conn()
        items2, pols, kws, key1, _ = S.gather(conn, d0)
        conn.close()
        texts = "\n".join(it["text"] for it in items2)
        check("碰到「加班」的日摘那一行和片段都去掉了", "加班" not in texts and "旅行清单" in texts, texts)
        check("加了话题边界，材料的指纹就变了", key1 != key0)
        for name, fn in [("重名", lambda: S.add_policy("工作", [], "excluded")), ("没有这种边界", lambda: S.add_policy("学习", [], "maybe")),
                         ("话题名太长", lambda: S.add_policy("这是一个特别特别特别特别特别特别长的话题名字", [], "excluded")),
                         ("没有这个话题", lambda: S.delete_policy(999))]:
            try:
                fn()
                check(f"{name}：报错", False)
            except ValueError:
                check(f"{name}：报错", True)
        S.edit_policy(pid, keywords="加班、出差")
        check("修改关键词（顿号分开的也行）", S.policies()[0]["keywords"] == ["加班", "出差"])

        print("\n[3] 写卡片：校验、重写一次")
        good = {"lines": [
            {"key": "rhythm", "text": "这周我们聊得比上周多，你多在晚上来。"},
            {"key": "weather", "text": "你这周在布置展览，有点累。[#1][#4]"},
            {"key": "ongoing", "text": "我们说好周末一起列旅行清单。[#7][#8]"},
            {"key": "close", "text": "你说「有你在挺好的」。[#2]"}]}
        bad = {"lines": [
            {"key": "rhythm", "text": "这周聊了 3 天。"},
            {"key": "weather", "text": "你最近有点累。"},
            {"key": "ongoing", "text": "我们在计划旅行。[#99]"},
            {"key": "close", "text": "你说「我好想你」。[#2]"},
            {"key": "mood", "text": "随便"}]}
        llm = FakeLLM([bad, good])
        r = asyncio.run(S.refresh(llm_fn=llm, now=NOW))
        check("第一次全没通过 → 带着问题重写 → 通过", r["status"] == "ok" and len(llm.prompts) == 2 and "上一次写的没通过校验" in llm.prompts[1], r)
        check("提示词里有今天的日期、节奏事实，没有轻放话题的内容", "2026年10月20日（周二）" in llm.prompts[0] and "最近七天：3 天聊过" in llm.prompts[0]
              and "加班" not in llm.prompts[0])
        cur = S.overview(NOW)["current"]
        lines = {x["key"]: x for x in cur["lines"]}
        check("四行都在，按固定顺序", [x["key"] for x in cur["lines"]] == ["rhythm", "weather", "ongoing", "close"])
        check("出处重新编号（按第一次出现的顺序）", lines["weather"]["with_refs"] == "你这周在布置展览，有点累。[#1][#2]"
              and lines["ongoing"]["refs"] == [3, 4] and lines["close"]["refs"] == [5], cur["lines"])
        check("出处对应的材料", [x["kind"] for x in cur["refs"]] == ["digest", "episode", "loop", "arc", "digest"] and cur["refs"][2]["id"] == lp, cur["refs"])
        check("正文去掉了出处标记", lines["close"]["text"] == "你说「有你在挺好的」。")
        iss = " ".join(cur["issues"])
        check("没通过的原因记下来了（写了数字 / 没标出处 / 出处不存在 / 原话对不上 / 没有这一行）",
              all(x in iss for x in ("写了具体数字", "没有标出处", "出处不存在", "原话对不上", "没有这一行")), cur["issues"])
        card = S.overview("2026-10-20 03:05:00")["card"]
        check("卡片的样子：四行 + 轻放 + 上次聊天（现算）", card.startswith("【我们最近】\n· 节奏：这周我们聊得比上周多") and "· 轻放：工作——你不主动提，我就不提。" in card
              and card.endswith("· 上次聊天：大约 10 小时前。"), card)
        check("卡片不到 600 字", len(card) <= 600)

        print("\n[4] 什么时候重写")
        again = FakeLLM([])
        check("材料没变：不调模型", asyncio.run(S.refresh(llm_fn=again, now=NOW))["status"] == "skipped" and not again.prompts)
        check("材料没变：不算过时", S.is_stale(NOW) is False)
        r = asyncio.run(S.refresh(llm_fn=FakeLLM([good]), now=NOW, force=True))
        check("你点「现在重写」：不管变没变都重写", r["status"] == "ok" and r["version"] == 2)
        LP.close(lp, "done", "列好了")
        check("未完的事了结了 → 材料变了", S.is_stale(NOW) is True)
        r = asyncio.run(S.refresh(llm_fn=FakeLLM([{"lines": [{"key": "weather", "text": "你很好。"}]}, {"lines": []}]), now=NOW))
        check("两次都没通过：记一条「没通过」，现在用的还是上一版", r["status"] == "rejected"
              and S.overview(NOW)["current"]["version"] == 2 and q("SELECT status FROM relationship_states ORDER BY id DESC LIMIT 1")[0][0] == "rejected")
        check("没通过的那一版也记着材料的指纹：不会反复重试", S.is_stale(NOW) is False
              and asyncio.run(S.refresh(llm_fn=FakeLLM([]), now=NOW))["status"] == "skipped")
        run("UPDATE digests SET source_key='x' WHERE period_start='2026-10-19'")
        check("日摘还没写完：先不写（写完会再来），也不算过时", asyncio.run(S.refresh(llm_fn=FakeLLM([]), now=NOW, force=False))["status"] == "skipped"
              and S.is_stale(NOW) is False)
        run("UPDATE digests SET source_key=? WHERE period_start='2026-10-19'", (f"{e3},{e4}",))
        r = asyncio.run(S.refresh(llm_fn=FakeLLM([]), now="2026-11-30 12:00:00"))
        last = q("SELECT lines, status FROM relationship_states ORDER BY id DESC LIMIT 1")[0]
        check("七天里一天都没聊：不调模型，直接写「最近七天我们没有聊天」", r.get("empty") and "最近七天我们没有聊天" in last[0] and last[1] == "active", r)
        check("这时上次聊天是六个星期以前", S.overview("2026-11-30 12:00:00")["card"].endswith("· 上次聊天：大约 6 个星期前。"))

        print("\n[5] 碰到轻放话题的行直接去掉")
        S.delete_policy(pid)
        S.add_policy("旅行", [], "excluded")
        llm = FakeLLM([{"lines": [{"key": "weather", "text": "你这周在布置展览。[#1]"},
                                  {"key": "ongoing", "text": "我们在计划旅行。[#1]"}]}])
        r = asyncio.run(S.refresh(llm_fn=llm, now=NOW))
        cur = S.overview(NOW)["current"]
        check("材料里没有碰到「旅行」的", "旅行" not in llm.prompts[0].split("【材料】")[1], llm.prompts[0][-500:])
        check("写出来碰到「旅行」的那行去掉了", [x["key"] for x in cur["lines"]] == ["weather"] and any("轻放" in i for i in cur["issues"]), cur)
        check("话题名本身就是关键词（没填关键词时）", S.light_keywords(S.policies()) == ["旅行"])

        print("\n[6] 其他")
        check("上次聊天的说法", S.gap_phrase("2026-10-20 11:30:00", NOW) == "不到一小时前" and S.gap_phrase("2026-10-19 12:00:00", NOW) == "大约一天前"
              and S.gap_phrase("2026-10-10 12:00:00", NOW) == "大约 10 天前" and S.gap_phrase("2026-09-20 12:00:00", NOW) == "大约 4 个星期前"
              and S.gap_phrase(None, NOW) is None)
        check("投递任务（排着队时不重复投）", S.enqueue() is True and S.enqueue() is False)
        ov = S.overview(NOW)
        check("总览：卡片、统计、话题边界、历史、开关", ov["card"] and ov["signals"]["days"] and ov["policies"] and len(ov["history"]) >= 4 and ov["enabled"] is True)
        v = S.get_version(ov["history"][-1]["id"])
        check("看某一版和它的出处", v and v["version"] == 1 and len(v["sources"]) == 5 and v["sources"][0]["title"] == "10月16日（周五）的日摘", v and v["sources"][:1])
        check("设置有默认值", S.cfg()["enabled"] is True)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
