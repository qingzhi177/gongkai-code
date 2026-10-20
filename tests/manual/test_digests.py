"""日摘、周摘、近七天摘要（app/pipeline/digests.py）的测试：临时库 + 假模型，不花 token、不碰真实数据。

运行：python3 tests/manual/test_digests.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="digesttest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:                                    # 本地没装依赖时也能跑（这里用不到 .env）
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
try:
    import httpx  # noqa: F401
except ImportError:                                    # 设置模块会间接导入召回模块；这里不发网络请求
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=None, Timeout=None, Limits=None)
try:
    import jieba  # noqa: F401
except ImportError:
    sys.modules["jieba"] = types.SimpleNamespace(setLogLevel=lambda *a: None, lcut=lambda t, **k: list(t),
                                                 cut_for_search=lambda t: list(t), initialize=lambda: None)

import db  # noqa: E402
from app import config  # noqa: E402
from app.pipeline import digests as D  # noqa: E402

PASS = FAIL = 0


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
        r = self.replies.pop(0) if self.replies else "{}"
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


def add_episode(day, title, body, l0_text, hour=21):
    ts = f"{day} {hour - 8:02d}:00:00"             # 北京时间 hour 点（存 UTC）
    mid = run("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status) VALUES ('c', 0, 'user', ?, ?, 'pluvia', 'active')",
              (l0_text, ts))
    return run("INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, mood, topics, refs, salience, "
               "status, version, issues, model, prompt_version, created_at) VALUES (?, 'c', ?, ?, ?, ?, ?, '平静', '[]', ?, 0.5, 'active', 1, "
               "'[]', 'fake', 'episode-v2', ?)", (day, ts, ts, title, body, body, json.dumps([mid]), ts))


def jobs_of(kind):
    return [json.loads(r[0]) for r in q("SELECT payload FROM pipeline_jobs WHERE kind=? AND status='queued' ORDER BY id", (kind,))]


def clear_jobs():
    run("DELETE FROM pipeline_jobs")


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        now = "2026-10-21 04:00:00"                    # 北京时间 10 月 21 日（周三）中午
        print("\n[1] 窗口和日期")
        start, today = D.window(now)
        check("近七天：10 月 15 日到 21 日", str(start) == "2026-10-15" and str(today) == "2026-10-21", (start, today))
        check("日期标签带星期", D._label("2026-10-19") == "10月19日（周一）")
        check("没聊天的日子连起来写", D._gaps("2026-10-15", "2026-10-21", {"2026-10-16", "2026-10-19"})
              == "10月15日（周四）、10月17日（周六）到10月18日（周日）、10月20日（周二）到10月21日（周三）")

        print("\n[2] 日摘")
        e1 = add_episode("2026-10-16", "下雨夜听歌", "晚上下雨，你说「今天想听点安静的」，我们一起听了一首老歌。", "今天想听点安静的", hour=21)
        e2 = add_episode("2026-10-16", "睡前说晚安", "睡前你说「明天要早起」，我们互道晚安。", "明天要早起，晚安啦", hour=23)
        llm = FakeLLM([{"lines": ["晚上下雨，你说「今天想听点安静的」，我们一起听了一首老歌。[#1]",
                                  "睡前你说要早起，我们互道晚安。[#2]", "这一句没有出处。", "你说「今天心情很好」。[#1]"]}])
        r = asyncio.run(D.digest_day("2026-10-16", llm_fn=llm, now=now))
        row = q("SELECT body, body_with_refs, refs, status, version FROM digests WHERE kind='day'")[0]
        check("日摘：两行通过、没出处和原话对不上的删掉", r["status"] == "ok" and r["lines"] == 2 and row[0].count("\n") == 1
              and "[#" not in row[0] and json.loads(row[2]) == [e1, e2] and row[3] == "active", (r, row))
        check("提示词带日期标签和时段", "10月16日（周五）" in llm.prompts[0] and "[#1] 2026年10月16日晚上《下雨夜听歌》" in llm.prompts[0], llm.prompts[0][:300])
        check("那天在近七天窗口里：投递近七天摘要；那周（12 日～18 日）已经结束：投递周摘",
              jobs_of("digest_recent") == [{}] and jobs_of("digest_week") == [{"week": "2026-10-12"}], (jobs_of("digest_recent"), jobs_of("digest_week")))
        again = FakeLLM([])
        check("片段没变：不重写", asyncio.run(D.digest_day("2026-10-16", llm_fn=again, now=now))["status"] == "skipped" and not again.prompts)
        lines, used, iss = D.verify_lines(["后来你说「明天要早起」。[#2]", "一开始下雨了。[#1]"], {1: e1, 2: e2},
                                          {e1: "今天想听点安静的", e2: "明天要早起，晚安啦"})
        check("出处按片段的时间顺序编号（和片段、卷一样）", lines == ["后来你说「明天要早起」。[#2]", "一开始下雨了。[#1]"] and used == [e1, e2], (lines, used))

        print("\n[3] 写不出来、片段没了")
        e3 = add_episode("2026-10-17", "一件小事", "你说今天随便聊聊。", "随便聊聊", hour=20)
        bad = {"lines": ["我们聊了很多。", "很开心。"]}
        r3 = asyncio.run(D.digest_day("2026-10-17", llm_fn=FakeLLM([bad, bad]), now=now))
        check("两次都没出处：记一条「没通过」，不发布", r3["status"] == "rejected"
              and q("SELECT status FROM digests WHERE kind='day' AND period_start='2026-10-17'")[0][0] == "rejected")
        conn = config.get_conn()
        check("没通过的那天不算待更新（输入不变就不再重试）", "2026-10-17" not in D.stale_days(conn))
        conn.close()
        run("UPDATE episodes SET status='superseded' WHERE id=?", (e3,))
        run("UPDATE episodes SET status='superseded' WHERE id=?", (e2,))
        conn = config.get_conn()
        check("10 月 16 日少了一个片段：要重写", "2026-10-16" in D.stale_days(conn), D.stale_days(conn))
        conn.close()
        r4 = asyncio.run(D.digest_day("2026-10-16", llm_fn=FakeLLM([{"lines": ["晚上下雨，我们一起听了一首老歌。[#1]"]}]), now=now))
        check("重写：版本 2，旧版本作废", r4["version"] == 2 and q("SELECT COUNT(*) FROM digests WHERE kind='day' AND period_start='2026-10-16' "
                                                                  "AND status='active'")[0][0] == 1)
        run("UPDATE episodes SET status='active' WHERE id=?", (e2,))

        print("\n[4] 近七天摘要")
        e4 = add_episode("2026-10-19", "周一的晚饭", "你说晚饭吃了「热乎乎的面」，很满足。", "晚饭吃了热乎乎的面", hour=19)
        asyncio.run(D.digest_day("2026-10-16", llm_fn=FakeLLM([{"lines": ["晚上我们一起听了一首老歌。[#1]", "睡前你说「明天要早起」。[#2]"]}]), now=now))
        asyncio.run(D.digest_day("2026-10-19", llm_fn=FakeLLM([{"lines": ["傍晚你说晚饭吃了「热乎乎的面」。[#1]"]}]), now=now))
        clear_jobs()
        rl = FakeLLM([{"body": "10 月 16 日晚上我们一起听了一首老歌，你说「明天要早起」。[#1]10 月 17 日和 18 日没有聊天。"
                               "10 月 19 日傍晚你说晚饭吃了「热乎乎的面」。[#2]"}])
        rr = asyncio.run(D.digest_recent(llm_fn=rl, now=now))
        rec = q("SELECT body, refs, period_start, period_end FROM digests WHERE kind='recent' AND status='active'")[0]
        check("近七天：用了两天的日摘，出处是日摘", rr["status"] == "ok" and rr["days"] == 2 and len(json.loads(rec[1])) == 2
              and rec[2] == "recent" and rec[3] == "2026-10-15~2026-10-21", (rr, rec))
        check("提示词：今天、窗口、没聊天的日子都写清楚（今天还没结束，不算没聊天）", "今天是 10月21日（周三）" in rl.prompts[0] and "10月15日（周四） 到 10月21日（周三）" in rl.prompts[0]
              and "10月17日（周六）到10月18日（周日）、10月20日（周二）\n" in rl.prompts[0] and "今天还没结束" in rl.prompts[0], rl.prompts[0][:600])
        check("没变：不重写", asyncio.run(D.digest_recent(llm_fn=FakeLLM([]), now=now))["status"] == "skipped")
        later = "2026-10-24 04:00:00"                  # 10 月 24 日：窗口挪到 18 日～24 日
        conn = config.get_conn()
        check("窗口往前挪了：要重写", D.recent_stale(conn, later))
        conn.close()
        r_far = asyncio.run(D.digest_recent(llm_fn=FakeLLM([]), now="2026-11-05 04:00:00"))
        body = q("SELECT body FROM digests WHERE kind='recent' AND status='active'")[0][0]
        check("七天里一天都没聊：直接写「没有聊天」，不调模型", r_far.get("empty") and body == "最近七天（10月30日（周五）到11月5日（周四））没有聊天。", body)

        print("\n[5] 周摘")
        clear_jobs()
        conn = config.get_conn()
        weeks = D.stale_weeks(conn, now)
        conn.close()
        check("10 月 12 日那周已经结束、有日摘：要写周摘；这周（19 日起）还没结束：不写", weeks == ["2026-10-12"], weeks)
        wl = FakeLLM([{"body": "这周我们只在 10 月 16 日聊了一个晚上，一起听了一首老歌，你说「明天要早起」。[#1]"}])
        rw = asyncio.run(D.digest_week("2026-10-14", llm_fn=wl))
        wrow = q("SELECT period_start, period_end, status FROM digests WHERE kind='week'")[0]
        check("周摘：按周一算，写到周日", rw["status"] == "ok" and wrow == ("2026-10-12", "2026-10-18", "active"), (rw, wrow))
        check("周摘提示词带没聊天的日子", "10月12日（周一）到10月15日（周四）" in wl.prompts[0] and "10月17日（周六）到10月18日（周日）" in wl.prompts[0], wl.prompts[0][:400])
        bad_w = FakeLLM([{"body": "这周你说「我从来没说过这句」，很开心很开心很开心很开心很开心很开心。[#1]"},
                         {"body": "这周你说「我从来没说过这句」，很开心很开心很开心很开心很开心很开心。[#1]"}])
        run("UPDATE digests SET source_key='old' WHERE kind='week'")
        rbw = asyncio.run(D.digest_week("2026-10-12", llm_fn=bad_w))
        check("周摘原话对不上：重写一次，还不行就不发布、保留旧版本", rbw["status"] == "rejected" and len(bad_w.prompts) == 2
              and q("SELECT COUNT(*) FROM digests WHERE kind='week' AND status='active'")[0][0] == 1, rbw)

        print("\n[6] 投递顺序")
        clear_jobs()
        e5 = add_episode("2026-10-20", "周二的一首歌", "你分享了一首歌，说「单曲循环一下午」。", "单曲循环一下午", hour=15)
        out = D.enqueue_stale(now=now)
        check("有日摘要写：先只投递日摘（周摘和近七天等日摘写完再说）", out == ["digest_day:2026-10-20"], out)
        sk = asyncio.run(D.digest_recent(llm_fn=FakeLLM([]), now=now))
        check("窗口里还有日摘没写完：近七天先跳过", sk["status"] == "skipped" and "没写完" in sk["message"], sk)
        asyncio.run(D.digest_day("2026-10-20", llm_fn=FakeLLM([{"lines": ["下午你说这首歌要「单曲循环一下午」。[#1]"]}]), now=now))
        clear_jobs()
        conn = config.get_conn()
        stale = D.stale_days(conn)
        conn.close()
        out2 = D.enqueue_stale(now=now)
        check("日摘都写好了：投递近七天", stale == [] and "digest_recent" in out2, (stale, out2))
        d = D.get_digest(q("SELECT id FROM digests WHERE kind='day' AND period_start='2026-10-20' AND status='active'")[0][0])
        check("日摘详情：出处是片段", d["sources"][0]["title"] == "周二的一首歌" and d["sources"][0]["n"] == 1, d["sources"])
        ov = D.overview(now=now)
        check("总览：近七天、日摘、周摘、待更新、没通过的", ov["recent"] and len(ov["days"]) == 3 and len(ov["weeks"]) == 1
              and ov["pending"]["recent"] is True and any(f["kind"] == "day" for f in ov["failed"]), ov["pending"])

        print("\n[7] 出错")
        add_episode("2026-10-21", "又一天", "你说今天也很好。", "今天也很好", hour=10)
        try:
            asyncio.run(D.digest_day("2026-10-21", llm_fn=FakeLLM([None]), now=now))
            check("模型没返回：报错（任务会重试）、不写", False)
        except RuntimeError:
            check("模型没返回：报错（任务会重试）、不写", not q("SELECT 1 FROM digests WHERE period_start='2026-10-21'"))
        check("设置有默认值", D.cfg()["enabled"] is True)
        _ = e4, e5
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
