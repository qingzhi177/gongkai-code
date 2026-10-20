"""未完的事（app/pipeline/loops.py）的测试：临时库 + 假模型，不花 token、不碰真实数据。

运行：python3 tests/manual/test_loops.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="loopstest_"))
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
from app import settings as app_settings  # noqa: E402
from app.pipeline import episodes as E  # noqa: E402
from app.pipeline import loops as L  # noqa: E402

PASS = FAIL = 0
FILL = "，我一直都在这里陪着你，慢慢说就好，不着急，想说什么都可以告诉我。"
NOW = "2026-10-20 12:00:00"      # 北京时间 10 月 20 日晚上 8 点


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
        r = self.replies.pop(0) if self.replies else '{"new": [], "closed": [], "mentioned": []}'
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


def day_chat(day, conv, msgs, hour=21):
    """一天的一段对话（北京时间 hour 点开始，每条隔一分钟），并标记这天已经整理过"""
    for i, (role, content) in enumerate(msgs):
        ts = f"{day} {hour - 8:02d}:{i:02d}:00"
        run("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status) VALUES (?, ?, ?, ?, ?, 'pluvia', 'active')",
            (conv, i, role, content, ts))
    run("INSERT OR REPLACE INTO settle_days (day, status, last_l0_id, messages, sessions, episodes, detail, version, settled_at) "
        "VALUES (?, 'done', 0, 0, 1, 0, '[]', 1, ?)", (day, day))


def loop(lid):
    conn = config.get_conn()
    try:
        return L._get(conn, lid)
    finally:
        conn.close()


def events(lid):
    return [r[0] for r in q("SELECT event FROM loop_events WHERE loop_id=? ORDER BY id", (lid,))]


def add_loop(kind, text, owner="user", due=None, policy="when_relevant", seen="2026-10-20 02:00:00"):
    lid = L.add(kind, text, owner, due, policy)
    run("UPDATE open_loops SET created_ts=?, last_mentioned_at=? WHERE id=?", (seen, seen, lid))
    return lid


def first_part(day):
    conn = config.get_conn()
    try:
        return E.chunks(E.sessions_for_day(conn, day)[0]["rows"])[0]
    finally:
        conn.close()


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        day_chat("2026-10-16", "A", [("user", "周三我要去面试，有点紧张" + FILL),
                                     ("assistant", "别怕，你准备得很好。等你回来，下次给你讲完那个灯塔的故事" + FILL),
                                     ("user", "好呀" + FILL), ("assistant", "晚安" + FILL)])
        day_chat("2026-10-19", "A", [("user", "面试过了！第一个就想告诉你" + FILL), ("assistant", "太好了，恭喜你" + FILL),
                                     ("user", "对了，灯塔的故事还没讲完呢" + FILL), ("assistant", "记着呢" + FILL)])

        print("\n[1] 扫描：新的事、了结、又提起（按时间顺序，后面的块能看到前面留下的事）")
        conn = config.get_conn()
        check("两天各一块没扫过", len(L.unscanned(conn)) == 2)
        conn.close()
        llm = FakeLLM([
            {"new": [{"kind": "waiting", "text": "你在等周三面试的结果", "owner": "user", "due": "2026-10-21", "lines": [1], "quote": "周三我要去面试"},
                     {"kind": "promise", "text": "我答应下次给你讲完灯塔的故事", "owner": "ai", "due": None, "lines": [2],
                      "quote": "下次给你讲完那个灯塔的故事"},
                     {"kind": "plan", "text": "原话对不上的一条事情", "lines": [3], "quote": "这句话不存在"},
                     {"kind": "plan", "text": "超过三件的第四件事情", "lines": [3], "quote": "好呀"}],
             "closed": [], "mentioned": []},
            {"new": [], "closed": [{"loop": "L1", "status": "done", "result": "面试过了，你第一时间来告诉我", "lines": [1], "quote": "面试过了"}],
             "mentioned": [{"loop": "L2", "lines": [3], "quote": "灯塔的故事还没讲完"}, {"loop": "L1", "lines": [1], "quote": "面试过了"}]},
        ])
        r = asyncio.run(L.scan(llm_fn=llm, now=NOW))
        check("扫了两块", r["chunks"] == 2 and not r["more"], r)
        check("第一块的提示词：还没有未完的事、带日期和星期", "（没有）" in llm.prompts[0] and "2026-10-16（周五）" in llm.prompts[0], llm.prompts[0][-600:])
        check("新的事：两件通过，原话对不上的不收，第四件不看", r["new"] == ["你在等周三面试的结果", "我答应下次给你讲完灯塔的故事"], r["new"])
        check("说明里写了原话对不上", any("原话对不上" in n for n in r["notes"]), r["notes"])
        l1, l2 = loop(1), loop(2)
        check("L1：在等的结果、软期限、原话、出处", l1["kind"] == "waiting" and l1["due_at"] == "2026-10-21" and l1["created_quote"] == "周三我要去面试"
              and l1["created_l0_id"] == 1 and l1["created_ts"] == "2026-10-16 13:00:00", l1)
        check("L2：我答应的事、没有期限", l2["owner"] == "ai" and l2["due_at"] is None and l2["follow_up_policy"] == "when_relevant", l2)
        check("第二块的提示词里有前面留下的事（带软期限）", "[L1]" in llm.prompts[1] and "软期限 2026-10-21" in llm.prompts[1] and "[L2]" in llm.prompts[1],
              llm.prompts[1][:800])
        l1 = loop(1)
        check("L1 有了下文：标成有结果了，记下后续和原话", l1["status"] == "done" and l1["closed_text"] == "面试过了，你第一时间来告诉我"
              and l1["closed_quote"] == "面试过了" and l1["closed_by"] == "scan" and l1["closed_ts"] == "2026-10-19 13:00:00", l1)
        l2 = loop(2)
        check("L2 又被提起：次数和时间", l2["mention_count"] == 1 and l2["last_mentioned_at"] == "2026-10-19 13:02:00", l2)
        check("同一块里了结过的不再算又提起", loop(1)["mention_count"] == 0)
        check("经过：出现 → 有结果了；出现 → 又提起", events(1) == ["created", "done"] and events(2) == ["created", "mentioned"], (events(1), events(2)))
        check("每块只扫一次", q("SELECT COUNT(*) FROM loops_scans")[0][0] == 2 and L.pending_chunks() == 0)
        again = FakeLLM([])
        check("再扫一次：没有要扫的，不调模型", asyncio.run(L.scan(llm_fn=again, now=NOW))["chunks"] == 0 and not again.prompts)

        print("\n[2] 校验：原话、编号、太像的当成又提起、行号")
        day_chat("2026-10-20", "B", [("user", "那个灯塔的故事你还记得吗" + FILL), ("assistant", "记得呀，下次一定讲完" + FILL),
                                     ("user", "好呀，还有下周六我们一起去看展吧" + FILL), ("assistant", "好，下周六一起去看展" + FILL)])
        llm = FakeLLM([{
            "closed": [{"loop": "L2", "status": "done", "result": "讲完了", "lines": [2], "quote": "已经讲完了"},
                       {"loop": "L99", "status": "done", "result": "?", "lines": [1], "quote": "灯塔"},
                       {"loop": "L1", "status": "done", "result": "已经了结的", "lines": [1], "quote": "灯塔"}],
            "new": [{"kind": "promise", "text": "我答应下次给你讲完那个灯塔故事", "owner": "ai", "lines": [2], "quote": "下次一定讲完"},
                    {"kind": "plan", "text": "我们说好下周六一起去看展", "owner": "both", "due": "2026-10-24", "lines": [3, 4], "quote": "下周六一起去看展"},
                    {"kind": "worry", "text": "行号不对的一条事情", "lines": [9], "quote": "灯塔"}],
            "mentioned": []}])
        r = asyncio.run(L.scan(llm_fn=llm, now=NOW))
        check("已经了结的事不给模型看", "[L1]" not in llm.prompts[0] and "[L2]" in llm.prompts[0], llm.prompts[0][:600])
        check("原话对不上：不了结", loop(2)["status"] == "open" and any("L2：原话对不上" in n for n in r["notes"]), r["notes"])
        check("没有这件事 / 已经了结的：不收", sum("没有这件还没了结的事" in n for n in r["notes"]) == 2, r["notes"])
        check("和 L2 太像：当成又提起，不新建", loop(2)["mention_count"] == 2 and any("和 L2 是同一件事" in n for n in r["notes"]), r["notes"])
        ids = [x[0] for x in q("SELECT id FROM open_loops WHERE text='我们说好下周六一起去看展'")]
        check("原话可以在标的任何一行里找到（这里在第 4 行）", len(ids) == 1 and loop(ids[0])["created_l0_id"] == q(
            "SELECT id FROM l0_messages WHERE conv_id='B' AND msg_idx=3")[0][0] and loop(ids[0])["due_at"] == "2026-10-24", ids)
        show = ids[0]
        check("行号不对的不收", any("没有标出处的行号" in n for n in r["notes"]), r["notes"])
        conn = config.get_conn()
        try:
            data = {"new": [{"kind": "plan", "text": "期限写错的一件事情", "due": "2026-13-40", "lines": [1], "quote": "灯塔"},
                            {"kind": "hobby", "text": "种类不对的一件事情", "lines": [1], "quote": "灯塔"},
                            {"kind": "plan", "text": "太远", "lines": [1], "quote": "灯塔"}]}
            conn.execute("BEGIN IMMEDIATE")
            out = L.apply_scan(conn, data, first_part("2026-10-20"))
            conn.rollback()
        finally:
            conn.close()
        check("期限写错就当没说；种类不对、太短的不收", len(out["new"]) == 1 and any("种类不对" in n for n in out["notes"])
              and any("太短" in n for n in out["notes"]), out)
        check("软期限的范围", L.parse_due("2026-10-15", E.to_day("2026-10-20")) is None and L.parse_due("2026-10-19", E.to_day("2026-10-20")) == "2026-10-19"
              and L.parse_due("2028-01-01", E.to_day("2026-10-20")) is None and L.parse_due(None, E.to_day("2026-10-20")) is None)

        print("\n[3] 只给模型看这段对话之前就有的事（以后导入旧聊天时，旧的对话不会去了结后来的事）")
        run("UPDATE open_loops SET created_ts='2026-10-25 00:00:00' WHERE id=?", (show,))
        conn = config.get_conn()
        try:
            vis = [x["id"] for x in L.visible_open(conn, first_part("2026-10-16"))]
            vis2 = [x["id"] for x in L.visible_open(conn, first_part("2026-10-20"))]
        finally:
            conn.close()
        check("10 月 16 日的对话看不到后来才有的事（只看得到那段里出现的 L2）", vis == [2], vis)
        check("10 月 20 日的对话看得到 L2", 2 in vis2 and show not in vis2, vis2)
        run("UPDATE open_loops SET created_ts='2026-10-20 13:03:00' WHERE id=?", (show,))

        print("\n[4] 开窗预览")
        a = add_loop("waiting", "你在等快递送到新家", due="2026-10-22")
        b = add_loop("plan", "我们说好周三一起听新专辑", owner="both", due="2026-10-21", policy="when_due")
        c = add_loop("worry", "你不想被问起的一件小事", policy="never_push")
        d = add_loop("ritual_due", "我们约好每周二一起列一周计划", owner="both", due="2026-10-20", policy="when_due")
        p = L.pack(now=NOW)
        check("顺序：到期的 → 快到期的 → 最近提起的；「不主动提」和没到期的「到期时才提」不列", [x["id"] for x in p] == [d, a, show, 2], p)
        check("一行里写着期限还有几天", "软期限是今天（10月20日）" in p[0]["line"] and "软期限 10月22日，还有 2 天" in p[1]["line"], p[:2])
        check("一行的样子", p[3]["line"] == "我答应下次给你讲完灯塔的故事（答应的事，10月16日说的）", p[3]["line"])
        check("明天到期", "软期限是明天（10月21日）" in L.line(loop(b), E.to_day("2026-10-20")))
        check("「到期时才提」到期当天才列", b in [x["id"] for x in L.pack(now="2026-10-21 12:00:00")])
        app_settings.update("loops", {"pack_size": 2})
        check("开窗最多列几件可以调", len(L.pack(now=NOW)) == 2)
        app_settings.reset("loops", ["pack_size"])

        print("\n[5] 自动过期")
        e1 = add_loop("waiting", "过了期限没再提的一件事", due="2026-10-01", seen="2026-09-28 02:00:00")
        e2 = add_loop("waiting", "过了期限但后来又提起的事", due="2026-10-01", seen="2026-10-05 02:00:00")
        e3 = add_loop("unfinished_topic", "很久没再提的一个话题", seen="2026-08-01 02:00:00")
        e4 = add_loop("unfinished_topic", "最近还在提的一个话题", seen="2026-10-01 02:00:00")
        e5 = add_loop("waiting", "期限刚过不久的一件事", due="2026-10-10", seen="2026-09-28 02:00:00")
        exp = L.expire(now=NOW)
        check("过了期限 14 天以上、之后没再提的过期；没有期限 45 天以上没提的过期", sorted(exp) == sorted([e1, e3]), exp)
        check("后来又提起的、最近还在提的、期限刚过不久的不过期", all(loop(x)["status"] == "open" for x in (e2, e4, e5)))
        check("过期的记下原因", loop(e1)["status"] == "expired" and loop(e1)["closed_by"] == "auto"
              and "软期限 2026-10-01" in q("SELECT detail FROM loop_events WHERE loop_id=? AND event='expired'", (e1,))[0][0])
        L.reopen(e3)
        check("重新打开算刚提起过，不会马上又过期", loop(e3)["status"] == "open" and e3 not in L.expire(now=NOW) and events(e3)[-1] == "reopened")

        print("\n[6] 你在 Dashboard 上的操作")
        L.close(a, "done", "快递昨天就到了")
        x = loop(a)
        check("标「有结果了」，记下后续", x["status"] == "done" and x["closed_text"] == "快递昨天就到了" and x["closed_by"] == "dashboard", x)
        try:
            L.close(a, "done")
            check("已经了结的不能再了结", False)
        except ValueError as err:
            check("已经了结的不能再了结", "有结果了" in str(err), err)
        L.reopen(a)
        x = loop(a)
        check("重新打开：清掉了结的记录", x["status"] == "open" and x["closed_text"] is None and x["closed_by"] is None, x)
        L.close(a, "dropped")
        check("标「不用再管了」", loop(a)["status"] == "dropped")
        L.edit(2, policy="never_push")
        x = loop(2)
        check("改跟进方式：标成改过，经过里写着改了什么", x["follow_up_policy"] == "never_push" and x["edited"] == 1
              and q("SELECT detail FROM loop_events WHERE loop_id=2 AND event='edited'")[0][0] == "改了跟进方式", x)
        check("「不主动提」的不在开窗预览里", 2 not in [y["id"] for y in L.pack(now=NOW)])
        L.edit(show, due="")
        check("清空期限", loop(show)["due_at"] is None)
        n_events = len(events(show))
        L.edit(show, text=loop(show)["text"])
        check("没有改动就不记", len(events(show)) == n_events)
        for name, fn in [("种类不对", lambda: L.edit(show, kind="hobby")), ("期限格式不对", lambda: L.add("plan", "格式不对的一件事", due="10月21日")),
                         ("太短", lambda: L.add("plan", "短")), ("跟进方式不对", lambda: L.edit(show, policy="always")),
                         ("谁的事不对", lambda: L.add("plan", "谁的事写错了的事", owner="cat")), ("没写内容", lambda: L.add("plan", "")),
                         ("没有这件事", lambda: L.close(9999))]:
            try:
                fn()
                check(f"{name}：报错", False)
            except ValueError:
                check(f"{name}：报错", True)
        lid = L.add("plan", "我们说好一起做一张旅行清单", "both", "2026-11-01", "when_due")
        x = loop(lid)
        check("自己加一件", x["source"] == "dashboard" and x["edited"] == 1 and x["due_at"] == "2026-11-01" and x["follow_up_policy"] == "when_due", x)

        print("\n[7] 改过的事，扫描不再自动改期限；没改过的会改")
        day_chat("2026-10-21", "C", [("user", "看展改到下下周六了" + FILL), ("assistant", "好，那就下下周六" + FILL),
                                     ("user", "灯塔的故事也不急" + FILL), ("assistant", "嗯嗯" + FILL)])
        run("UPDATE open_loops SET due_at='2026-10-24', edited=0 WHERE id=?", (show,))
        asyncio.run(L.scan(llm_fn=FakeLLM([{"new": [], "closed": [], "mentioned": [
            {"loop": f"L{show}", "due": "2026-10-31", "lines": [1], "quote": "看展改到下下周六"},
            {"loop": "L2", "due": "2026-11-30", "lines": [3], "quote": "灯塔的故事也不急"}]}]), now=NOW))
        check("没改过的：期限跟着改，经过里写着", loop(show)["due_at"] == "2026-10-31"
              and q("SELECT detail FROM loop_events WHERE loop_id=? AND event='mentioned' ORDER BY id DESC", (show,))[0][0] == "软期限改成 2026-10-31")
        check("你改过的：期限不动", loop(2)["due_at"] is None and loop(2)["mention_count"] == 3, loop(2))

        print("\n[8] 查看")
        ov = L.overview(now=NOW)
        check("总览：进行中、了结了的、数量、开窗预览、扫描进度", len(ov["open"]) == ov["counts"]["open"] and ov["counts"]["done"] == 1
              and ov["counts"]["dropped"] == 1 and ov["counts"]["expired"] == 1 and ov["pending_chunks"] == 0 and ov["pack"], ov["counts"])
        check("进行中的按期限排（有期限的在前）", [x["due_at"] for x in ov["open"]][:3] == ["2026-10-01", "2026-10-10", "2026-10-20"],
              [x["due_at"] for x in ov["open"]])
        dt = L.detail(1)
        check("详情：经过带原话", [e["event"] for e in dt["events"]] == ["created", "done"] and dt["events"][1]["quote"] == "面试过了"
              and dt["events"][1]["detail"] == "面试过了，你第一时间来告诉我", dt["events"])

        print("\n[9] 分批和出错")
        day_chat("2026-10-22", "D", [("user", "新的一天" + FILL), ("assistant", "早安" + FILL), ("user", "嗯" + FILL), ("assistant", "好" + FILL)])
        day_chat("2026-10-23", "D", [("user", "又一天" + FILL), ("assistant", "早" + FILL), ("user", "嗯" + FILL), ("assistant", "好" + FILL)])
        try:
            asyncio.run(L.scan(llm_fn=FakeLLM([None]), now=NOW))
            check("模型没返回：报错（任务会重试），这块不算扫过", False)
        except RuntimeError:
            check("模型没返回：报错（任务会重试），这块不算扫过", L.pending_chunks() == 2)
        try:
            asyncio.run(L.scan(llm_fn=FakeLLM(["我想想……"]), now=NOW))
            check("模型没输出 JSON：报错，这块不算扫过", False)
        except ValueError:
            check("模型没输出 JSON：报错，这块不算扫过", L.pending_chunks() == 2)
        run("INSERT INTO pipeline_jobs (kind, dedupe_key, payload, status, attempts, max_attempts, next_run_at, source, created_at, updated_at) "
            "VALUES ('loops_scan', 'loops_scan', '{}', 'running', 1, 4, '2026-10-20 00:00:00', 'auto', '2026-10-20 00:00:00', '2026-10-20 00:00:00')")
        r3 = asyncio.run(L.scan(llm_fn=FakeLLM([]), now=NOW, limit=1))
        check("一次只扫一块、还有剩下的：再投一个任务（自己正在跑也投得进去）", r3["chunks"] == 1 and r3["more"] is True
              and q("SELECT COUNT(*) FROM pipeline_jobs WHERE kind='loops_scan' AND status='queued'")[0][0] == 1, r3)
        run("UPDATE pipeline_jobs SET status='dead', updated_at=? WHERE kind='loops_scan'", (L._now(),))
        check("刚失败过（比如没余额了）：自动投递先停一停", L.enqueue_scan() is False)
        check("在 Dashboard 上点「现在扫描」照样投", L.enqueue_scan(source="dashboard") is True)
        run("UPDATE pipeline_jobs SET status='dead', updated_at='2026-01-01 00:00:00' WHERE kind='loops_scan'")
        check("失败是很久以前的：照常自动投递", L.enqueue_scan() is True)
        c0 = L.cfg()
        check("设置有默认值", c0["enabled"] is True and c0["expire_after_days"] == 14 and c0["stale_days"] == 45 and c0["pack_size"] == 5, c0)
        check("「不主动提」的事也在（只是不列进开窗预览）", loop(c)["status"] == "open")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
