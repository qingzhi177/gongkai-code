"""L0 原文的手动修改（app/l0_edit.py）的测试：临时库 + 编出来的数据，不碰真实记忆库，不调用模型。

运行：python3 tests/manual/test_l0_edit.py
（最后一部分「前端带着改之前的原文来存记录」要导入 main.py，缺依赖时自动跳过；在 VPS 的 venv 里会跑。）
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="l0edittest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)

import db  # noqa: E402
from app import config  # noqa: E402
from app import branches as B  # noqa: E402
from app import l0_edit as L  # noqa: E402

PASS = FAIL = 0
T = "2026-10-16 12:00:00"
KAO_OLD = "•͈ᴗ⁃͈ ✧"
KAO_NEW = "•͈ᴗ•͈ ✧"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


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


def msg(conv, idx, role, content, extracted=1, status="active", ts=T):
    return run("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status, extracted) VALUES (?, ?, ?, ?, ?, 'pluvia', ?, ?)",
               (conv, idx, role, content, ts, status, extracted))


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)

        print("\n[1] 找出改了哪几段")
        segs = L.segments("好呀 那就这样——" + KAO_OLD + "\n走吧.", "好呀 那就这样——" + KAO_NEW + "\n走吧.")
        check("一处改动，带着前后文", len(segs) == 1 and segs[0][0][0].endswith("⁃͈ ✧\n走吧.") and "•͈ ✧" in segs[0][0][1], segs)
        t, n = L.apply("你说「就这样——" + KAO_OLD + "」", segs)
        check("引用里有这一段：换掉", n == 1 and KAO_NEW in t, t)
        t, n = L.apply("别的话里也有 ⁃ 这个字", segs)
        check("只有单独一个字对得上：不改", n == 0)
        segs2 = L.segments("窗外���亮的一盏灯", "窗外透亮的一盏灯")
        check("乱码换成正确的字", L.apply("照片里是窗外���亮的一盏灯。", segs2)[0] == "照片里是窗外透亮的一盏灯。")
        segs3 = L.segments("今天去了海边，好开心", "今天去了海边，很开心！")
        check("几处改动都能换", L.apply("你说「今天去了海边，好开心」", segs3)[0] == "你说「今天去了海边，很开心！」", segs3)

        print("\n[2] 只改字：原文改掉，引用过的地方一起改")
        u1 = msg("A", 0, "user", "早呀")
        a1 = msg("A", 1, "assistant", "早！今天想做什么")
        u2 = msg("A", 2, "user", "好呀 那就这样——" + KAO_OLD + "\n走吧.")
        other = msg("B", 0, "user", "完全不相关的一句话")
        l1 = run("INSERT INTO l1_memories (content, quote, source_msg_id, conv_id, client, event_type, ts, status) VALUES (?, ?, ?, 'A', 'pluvia', 'fact', ?, 'active')",
                 ("你会用颜文字表示要保密", "就这样——" + KAO_OLD, u1, T))
        l1b = run("INSERT INTO l1_memories (content, quote, conv_id, client, event_type, ts, status) VALUES ('别的记忆', '完全不相关的一句话', 'B', 'pluvia', 'fact', ?, 'active')", (T,))
        ep = run("INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, mood, topics, refs, status, version, created_at) "
                 "VALUES ('2026-10-16', 'A', ?, ?, '保密', ?, ?, '开心', '[]', ?, 'active', 1, ?)",
                 (T, T, "你说「就这样——" + KAO_OLD + "」。", "你说「就这样——" + KAO_OLD + "」。[#3]", json.dumps([u1, a1, u2]), T))
        ch = run("INSERT INTO chapters (n, start_day, status, sections, version, created_at, updated_at) VALUES (1, '2026-10-16', 'current', ?, 1, ?, ?)",
                 (json.dumps([{"title": "开头", "body": "你说「就这样——" + KAO_OLD + "」。", "refs": [ep]}], ensure_ascii=False), T, T))
        st = run("INSERT INTO relationship_states (day, lines, refs, signals, source_key, status, version, created_at) VALUES ('2026-10-16', ?, '[]', '{}', 'k', 'active', 1, ?)",
                 (json.dumps([{"key": "close", "text": "你说「就这样——" + KAO_OLD + "」", "with_refs": "你说「就这样——" + KAO_OLD + "」[#1]"}], ensure_ascii=False), T))
        st_old = run("INSERT INTO relationship_states (day, lines, refs, signals, source_key, status, version, created_at) VALUES ('2026-10-15', ?, '[]', '{}', 'k0', 'superseded', 0, ?)",
                     (json.dumps([{"key": "close", "text": "你说「就这样——" + KAO_OLD + "」"}], ensure_ascii=False), T))
        lp = run("INSERT INTO open_loops (kind, text, owner, status, created_ts, created_quote, created_at, updated_at) VALUES ('plan', '保密的事', 'user', 'open', ?, ?, ?, ?)",
                 (T, "就这样——" + KAO_OLD, T, T))
        r = L.edit(u2, "好呀 那就这样——" + KAO_NEW + "\n走吧.", "fix")
        check("原文改掉了", q("SELECT content FROM l0_messages WHERE id=?", (u2,))[0][0] == "好呀 那就这样——" + KAO_NEW + "\n走吧.")
        check("记忆的原话、片段、章节、关系状态、未完的事都一起改了", r["synced"] == {"l1_memories": 1, "episodes": 1, "chapters": 1,
                                                                    "relationship_states": 1, "open_loops": 1}, r)
        check("记忆的原话", q("SELECT quote FROM l1_memories WHERE id=?", (l1,))[0][0] == "就这样——" + KAO_NEW)
        check("片段的正文和带出处的正文", all(KAO_NEW in x and KAO_OLD not in x for x in q("SELECT body, body_with_refs FROM episodes WHERE id=?", (ep,))[0]))
        sec = json.loads(q("SELECT sections FROM chapters WHERE id=?", (ch,))[0][0])
        check("章节（JSON 里的字也改了，JSON 还是好的）", KAO_NEW in sec[0]["body"] and sec[0]["refs"] == [ep])
        lines = json.loads(q("SELECT lines FROM relationship_states WHERE id=?", (st,))[0][0])
        check("关系状态现在这一版改了，旧版本不动", KAO_NEW in lines[0]["text"] and KAO_NEW in lines[0]["with_refs"]
              and KAO_OLD in q("SELECT lines FROM relationship_states WHERE id=?", (st_old,))[0][0])
        check("未完的事的原话", q("SELECT created_quote FROM open_loops WHERE id=?", (lp,))[0][0] == "就这样——" + KAO_NEW)
        check("不相关的不动；只改了原话，不用重算向量", q("SELECT quote FROM l1_memories WHERE id=?", (l1b,))[0][0] == "完全不相关的一句话"
              and r["l1_changed"] == [])
        e = L.history(u2)
        check("修改记录：改之前、改之后、改法、连带改了几处", len(e) == 1 and e[0]["old_content"].endswith(KAO_OLD + "\n走吧.") and e[0]["mode"] == "fix"
              and e[0]["synced"]["l1_memories"] == 1, e)
        check("改了几次", L.counts(config.get_conn(), [u2, u1]) == {u2: 1})
        run("UPDATE l1_memories SET content=? WHERE id=?", ("你说过「就这样——" + KAO_NEW + "」", l1))
        r = L.edit(u2, "好呀 那就这样——" + KAO_OLD + "\n走吧.", "fix")
        check("改回去：引用的地方也改回去；记忆的内容改了要重算向量", r["l1_changed"] == [l1]
              and q("SELECT quote FROM l1_memories WHERE id=?", (l1,))[0][0] == "就这样——" + KAO_OLD, r)
        check("改回去也记一笔", len(L.history(u2)) == 2)

        print("\n[3] 不能改的情况")
        for name, fn in [("内容没有变", lambda: L.edit(u2, "好呀 那就这样——" + KAO_OLD + "\n走吧.")), ("空的", lambda: L.edit(u2, "  \n")),
                         ("没有这条", lambda: L.edit(99999, "x")), ("改法不对", lambda: L.edit(u2, "x", "magic"))]:
            try:
                fn()
                check(f"{name}：报错", False)
            except ValueError:
                check(f"{name}：报错", True)
        old_ver = msg("A", 1, "assistant", "早！今天想干嘛", status="superseded")
        try:
            L.edit(old_ver, "随便改改")
            check("被换下来的旧版本：不能改", False)
        except ValueError as ex:
            check("被换下来的旧版本：不能改", "不是现在的版本" in str(ex))

        print("\n[4] 意思改了：重新提取")
        calls = []

        def cascade(cur, conv_id, ids):
            calls.append((conv_id, ids))
            cur.execute("UPDATE l0_messages SET extracted=0 WHERE conv_id=?", (conv_id,))
            return [l1]
        r = L.edit(a1, "早！今天想去海边吗", "reextract", cascade=cascade)
        check("那一批的记忆作废、重新提取", calls == [("A", [a1])] and r["l1_purged"] == [l1]
              and q("SELECT extracted FROM l0_messages WHERE id=?", (a1,))[0][0] == 0, (calls, r))
        jobs_ = q("SELECT dedupe_key, payload FROM pipeline_jobs WHERE kind='settle_day'")
        check("那一天的每日片段整天重新整理", r["resettle"] == ["2026-10-16"] and jobs_ and json.loads(jobs_[0][1]).get("force") is True, (r, jobs_))
        check("重新提取时不去改引用（片段会重新整理）", r["synced"] == {})
        calls.clear()
        n_id = msg("A", 3, "user", "还没提取过的一句", extracted=0)
        L.edit(n_id, "还没提取过的一句话", "reextract", cascade=cascade)
        check("还没提取过的消息：不用作废记忆", calls == [])

        print("\n[5] 前端带着改之前的原文来时，认得是同一条")
        conn = config.get_conn()
        al = L.aliases(conn, "A")
        check("改之前的比较键 → 现在的", al.get(L.cmp_key("assistant", "早！今天想做什么")) == L.cmp_key("assistant", "早！今天想去海边吗"), al)
        check("别的会话没有", L.aliases(conn, "B") == {})
        b = B.register(conn, "BR", {"conversation_id": "A"}, [("user", "早呀"), ("assistant", "早！今天想做什么")], T)
        check("分支会话：前端带的是改之前的原文，也算重合", b and b["shared"] == 2, b)
        check("之后存记录时也认", B.shared_len(conn, "BR", [("user", "早呀"), ("assistant", "早！今天想做什么"), ("user", "新的")]) == 2)
        conn.close()

        print("\n[6] 存记录时的序列对齐（要导入 main.py）")
        try:
            import main as M
        except Exception as ex:      # 本地缺 fastapi 之类的依赖
            print(f"  - 跳过：导入 main.py 失败（{type(ex).__name__}）")
            M = None
        if M:
            conv = "S"
            ids = [msg(conv, i, role, text) for i, (role, text) in enumerate([("user", "你好"), ("assistant", "你好呀 (ᗜ֊ᗜ)"), ("user", "今天下雨了")])]
            L.edit(ids[1], "你好呀 (ᗜˬᗜ)", "fix")
            req = M.SaveRequest(conv_id=conv, client="pluvia", messages=[
                {"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀 (ᗜ֊ᗜ)"},
                {"role": "user", "content": "今天下雨了"}, {"role": "assistant", "content": "记得带伞"}])
            res = asyncio.run(M.save_conversation(req))
            rows = q("SELECT id, content, status FROM l0_messages WHERE conv_id=? ORDER BY msg_idx, id", (conv,))
            active = [(i, c) for i, c, s in rows if s == "active"]
            check("前端带着改之前的原文来：改过的那条不被冲掉，也不重复存，只多存新的一条",
                  res.get("saved") == 1 and [c for _, c in active] == ["你好", "你好呀 (ᗜˬᗜ)", "今天下雨了", "记得带伞"]
                  and active[1][0] == ids[1], (res, rows))
            req2 = M.SaveRequest(conv_id=conv, client="pluvia", messages=[
                {"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀 (ᗜˬᗜ)"},
                {"role": "user", "content": "今天下雨了"}, {"role": "assistant", "content": "记得带伞"}])
            res2 = asyncio.run(M.save_conversation(req2))
            check("前端后来也换成了改过的样子：照样认得", res2.get("saved") == 0
                  and q("SELECT COUNT(*) FROM l0_messages WHERE conv_id=? AND status='active'", (conv,))[0][0] == 4, res2)
            req3 = M.SaveRequest(conv_id=conv, client="pluvia", messages=[
                {"role": "user", "content": "你好"}, {"role": "assistant", "content": "完全重新生成的一句"},
                {"role": "user", "content": "今天下雨了"}, {"role": "assistant", "content": "记得带伞"}])
            asyncio.run(M.save_conversation(req3))
            check("真的重新生成了：照常换成新版本", q("SELECT status FROM l0_messages WHERE id=?", (ids[1],))[0][0] == "superseded")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
