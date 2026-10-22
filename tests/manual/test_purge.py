"""清空所有数据（app/purge.py、POST /admin/purge_all）和召回对比定格（app/recall/traces.py freeze()）的测试：
临时库 + 编出来的数据，不碰真实记忆库，不调用模型。

运行：python3 tests/manual/test_purge.py
（最后一部分走一遍 main.py 的接口，缺依赖时自动跳过；在 VPS 的 venv 里会跑。）
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

TMP = Path(tempfile.mkdtemp(prefix="purgetest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)

import db  # noqa: E402
from app import config  # noqa: E402
from app import purge as P  # noqa: E402
from app.recall import golden as G  # noqa: E402
from app.recall import traces as T  # noqa: E402

PASS = FAIL = 0
NOW = "2026-10-20 12:00:00"


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


def fill_row(conn, table):
    """给一张表随便塞一行：没有默认值的 NOT NULL 列按类型填个值（只为了看清空时有没有漏表）"""
    cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    names, vals = [], []
    single_pk = sum(1 for c in cols if c[5]) == 1
    for _, name, typ, notnull, default, pk in cols:
        if pk and single_pk and (typ or "").upper() == "INTEGER":      # 自增编号，不用填
            continue
        if notnull and default is None or pk:
            names.append(name)
            t = (typ or "").upper()
            vals.append(1 if "INT" in t else 0.5 if "REAL" in t else f"{table}-{name}")
    if names:
        conn.execute(f'INSERT INTO "{table}" ({", ".join(names)}) VALUES ({", ".join("?" * len(vals))})', vals)
    else:
        conn.execute(f'INSERT INTO "{table}" DEFAULT VALUES')


def purge():
    conn = config.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        res = P.purge(conn)
        conn.commit()
        return res
    finally:
        conn.close()


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)

        print("\n[1] 保留的表、清空的表")
        conn = config.get_conn()
        try:
            all_tables = P.tables(conn)
            clear = P.to_clear(conn)
        finally:
            conn.close()
        for t in ("l0_messages", "l1_memories", "thinking_records", "vectors", "episodes", "digests", "settle_days", "arcs",
                  "arc_materials", "arc_routing", "lexicon", "lexicon_scans", "open_loops", "loop_events", "loops_scans",
                  "relationship_states", "chapters", "chapter_versions", "chapter_episodes", "chapter_proposals",
                  "shared_narrative", "recent_summary", "narrative_state", "pipeline_jobs", "l0_edits", "conv_branches"):
            if t in all_tables and t not in clear:
                check(f"{t} 要清空", False)
        check("各层的表都在清空名单里（以前漏掉的每日片段、叙事卷、共同语汇、未完的事、关系状态、章节也在）",
              {"episodes", "arcs", "lexicon", "open_loops", "relationship_states", "chapters", "digests"} <= set(clear), clear)
        check("配置、设置、召回对比不清", not ({"providers", "model_configs", "active_config", "app_settings", "recall_traces",
                                              "recall_labels", "injections", "schema_migrations"} & set(clear)))
        check("关键词索引的表不直接删（由 text_index 重建）", not any(t.startswith("l1_fts") for t in clear))

        print("\n[2] 每张要清的表都塞一行，清空以后都是空的；保留的表不动")
        conn = config.get_conn()
        try:
            for t in clear:
                fill_row(conn, t)
            conn.commit()
        finally:
            conn.close()
        run("DELETE FROM l1_memories")
        run("DELETE FROM sqlite_sequence WHERE name='l1_memories'")
        m1 = run("INSERT INTO l1_memories (content, ts, status, event_type, author) VALUES ('你说喜欢下雨天听歌', ?, 'active', 'preference', 'user')", (NOW,))
        m2 = run("INSERT INTO l1_memories (content, ts, status, event_type, author) VALUES ('我们约好周末一起看海', ?, 'active', 'plan', 'ai')", (NOW,))
        dec = {"candidates": [{"id": m1, "rerank": 0.8}, {"id": m2, "rerank": 0.4}], "picks": [{"id": m1, "tier": "strong"}], "gate": "ok"}
        tr = run("INSERT INTO recall_traces (turn_key, backend, session_id, client, mode, query, user_text, decision, legacy, rerank, created_at) "
                 "VALUES ('k1', 'pluvia', 's1', 'pluvia', 'shadow', '下雨', '今天下雨了', ?, ?, '{\"ok\": true}', ?)",
                 (json.dumps(dec), json.dumps({"picked": [str(m2)], "gate": "ok"}), NOW))
        G.set_label(tr, m1, "should")
        before = {t: q(f'SELECT COUNT(*) FROM "{t}"')[0][0] for t in ("recall_traces", "recall_labels", "schema_migrations")}
        res = purge()
        left = [t for t in clear if q(f'SELECT COUNT(*) FROM "{t}"')[0][0]]
        check("要清的表都空了", not left, left)
        check("返回每张表删了几行", res["cleared"].get("l1_memories") == 2 and res["cleared"].get("episodes") == 1, res["cleared"])
        after = {t: q(f'SELECT COUNT(*) FROM "{t}"')[0][0] for t in before}
        check("召回对比、标注、迁移记录都还在", after == before, (before, after))

        print("\n[3] 召回对比：清空前定格，以后看的还是当时的记忆")
        check("定格了一轮", res["frozen"] == 1, res)
        t = T.get_trace(tr)
        check("详情里的记忆是定格下来的内容（记忆表已经空了）", t["snippets"].get(str(m1), {}).get("content") == "你说喜欢下雨天听歌"
              and t["snippets"].get(str(m2), {}).get("author") == "ai" and t["frozen_at"], t.get("snippets"))
        check("详情不带整个 frozen（只给定格时间）", "frozen" not in t)
        n1 = run("INSERT INTO l1_memories (content, ts, status) VALUES ('新导入的一条记忆', ?, 'active')", (NOW,))
        check("编号不重置：新记忆接着往后编，不会撞上旧编号", n1 > m2, (n1, m2))
        run("INSERT INTO l1_memories (id, content, ts, status) VALUES (?, '故意占用旧编号的新记忆', ?, 'active')", (m1, NOW))
        lst = [x for x in T.list_traces() if x["id"] == tr][0]
        check("列表里：标着清空前，旧版和新版选中的记忆都是当时的内容（就算编号被新记忆占了）", lst["frozen"] is True
              and lst["new"][0]["content"] == "你说喜欢下雨天听歌"[:40] and lst["legacy"]["picked"][0]["content"] == "我们约好周末一起看海", lst)
        check("详情里也不会显示成新记忆", T.get_trace(tr)["snippets"][str(m1)]["content"] == "你说喜欢下雨天听歌")
        ex = G.export()["items"][0]
        check("导出黄金集：标注的记忆用定格的内容", ex["should"] == [{"id": m1, "content": "你说喜欢下雨天听歌"}], ex)
        cal = G.calibration()
        check("门槛校准照样能用（只用存下来的精排分）", cal["counts"]["should"] == 1, cal["counts"])
        tr2 = run("INSERT INTO recall_traces (turn_key, backend, session_id, client, mode, query, user_text, decision, created_at) "
                  "VALUES ('k2', 'pluvia', 's1', 'pluvia', 'shadow', '新的', '新的一轮', ?, ?)",
                  (json.dumps({"candidates": [{"id": n1}], "picks": [{"id": n1}]}), NOW))
        x2 = [x for x in T.list_traces() if x["id"] == tr2][0]
        check("清空以后的新一轮照常去记忆表里取", x2["frozen"] is False and x2["new"][0]["content"] == "新导入的一条记忆", x2)
        at = T.get_trace(tr)["frozen_at"]
        res2 = purge()
        check("再清空一次：已经定格的不再动，只定格新的那一轮", res2["frozen"] == 1 and T.get_trace(tr)["frozen_at"] == at
              and T.get_trace(tr2)["snippets"][str(n1)]["content"] == "新导入的一条记忆")

        print("\n[4] 记忆已经清掉了：可以从备份库定格")
        bak = TMP / "backup.db"
        b = sqlite3.connect(bak)
        b.execute("CREATE TABLE l1_memories (id INTEGER PRIMARY KEY, content TEXT, ts TEXT, event_type TEXT, author TEXT)")
        b.execute("INSERT INTO l1_memories VALUES (7, '备份里的一条旧记忆', ?, 'event', 'user')", (NOW,))
        b.commit()
        b.close()
        tr3 = run("INSERT INTO recall_traces (turn_key, backend, session_id, client, mode, query, user_text, decision, created_at) "
                  "VALUES ('k3', 'pluvia', 's1', 'pluvia', 'shadow', '旧的', '旧的一轮', ?, ?)",
                  (json.dumps({"candidates": [{"id": 7}], "picks": [{"id": 7}]}), NOW))
        conn = config.get_conn()
        src = sqlite3.connect(f"file:{bak}?mode=ro", uri=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            n = T.freeze(conn, source=src)
            conn.commit()
        finally:
            src.close()
            conn.close()
        check("用备份库定格：内容是备份里的", n == 1 and T.get_trace(tr3)["snippets"]["7"]["content"] == "备份里的一条旧记忆")

        print("\n[5] 走一遍接口（要导入 main.py）")
        try:
            import main as M
        except Exception as ex:      # 本地缺 fastapi 之类的依赖
            print(f"  - 跳过：导入 main.py 失败（{type(ex).__name__}）")
            M = None
        if M:
            prof = Path(os.environ["DATA_DIR"]) / "profile"
            prof.mkdir(parents=True, exist_ok=True)
            (prof / "user.md").write_text("画像", encoding="utf-8")
            run("INSERT INTO episodes (day, conv_id, title, body, body_with_refs, status, version, created_at) "
                "VALUES ('2026-10-20', 'c', '片段', '正文', '正文', 'active', 1, ?)", (NOW,))
            check("确认文本不对：不清", asyncio.run(M.purge_all_data({"confirmation": "no"}))["status"] == "error"
                  and q("SELECT COUNT(*) FROM episodes")[0][0] == 1)
            r = asyncio.run(M.purge_all_data({"confirmation": "CONFIRM DELETE"}))
            check("清空成功：返回清了哪些表", r["status"] == "ok" and r["cleared"].get("episodes") == 1, r)
            check("画像文件放在 DATA_DIR 下的也清掉了", not (prof / "user.md").exists())
            check("召回对比还在", q("SELECT COUNT(*) FROM recall_traces")[0][0] == 3)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
