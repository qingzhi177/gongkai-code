"""迁移框架测试（db.py + migrations/）

用法（在 services/memory-service 目录下）：
    venv/bin/python ../../tests/manual/test_migrations.py                 # 只用临时库
    venv/bin/python ../../tests/manual/test_migrations.py --real ../../data/sqlite/memory.db
        # 额外在「真实库的副本」上跑一遍（只读打开原库复制，原库不会被改动）

全部在临时目录里进行，跑完自动删除。
"""
import argparse
import multiprocessing
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE_DIR = HERE.parent.parent / "services" / "memory-service"
sys.path.insert(0, str(SERVICE_DIR))

import db  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def quiet(*a, **k):
    pass


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def indexes(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}


def row_counts(conn):
    out = {}
    for t in sorted(tables(conn)):
        try:
            out[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = None   # 例如 FTS5 虚拟表在没有加载分词器时不可读
    return out


def table_sql(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table','trigger') AND name != 'schema_migrations'")}


EXPECTED_TABLES = {
    "l0_messages", "l1_memories", "providers", "active_config", "shared_narrative", "recent_summary",
    "narrative_config", "model_configs", "narrative_state", "custom_prompts", "conv_settings",
    "thinking_records", "l0_deleted_marks", "schema_migrations",
}
EXPECTED_COLS = {
    "l0_messages": {"group_id", "version", "group_anchor", "extract_attempts", "extract_error", "extracted"},
    "l1_memories": {"anchor_json", "display_count", "last_shown_at", "visibility", "supersedes",
                    "event_msg_id", "evidence_json", "evidence_status", "author"},
    "providers": {"role", "api_format"},
    "shared_narrative": {"last_l1_id"},
    "recent_summary": {"last_l0_id"},
    "thinking_records": {"full_reply"},
}


def all_versions():
    return [m.version for m in db.discover()]


# ---------- 1. 全新空库 ----------
def test_fresh(tmp):
    print("\n[1] 全新空库")
    p = tmp / "fresh" / "memory.db"
    applied = db.migrate(p, log=quiet)
    check("执行了全部迁移", applied == all_versions(), applied)
    conn = sqlite3.connect(p)
    check("表齐全", EXPECTED_TABLES <= tables(conn), EXPECTED_TABLES - tables(conn))
    for t, cols in EXPECTED_COLS.items():
        have = set(db.columns(conn, t))
        check(f"{t} 的列齐全", cols <= have, cols - have)
    check("narrative_config 默认行存在", conn.execute("SELECT COUNT(*) FROM narrative_config WHERE id=1").fetchone()[0] == 1)
    check("conv_settings 默认行存在",
          conn.execute("SELECT COUNT(*) FROM conv_settings WHERE id='__default__'").fetchone()[0] == 1)
    check("索引已建", {"idx_l0_conv", "idx_l0_extract", "idx_l1_status"} <= indexes(conn), indexes(conn))
    check("WAL 模式", conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
    rows = conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version").fetchall()
    check("执行记录带 UTC 时间", all(len(r[2]) == 19 for r in rows), rows)
    conn.close()
    check("空库不做备份", not (p.parent / "backups").exists())
    check("再跑一次是空操作", db.migrate(p, log=quiet) == [])
    check("current_version", db.current_version(p) == max(all_versions()))


# ---------- 2. 旧库（有数据、没有 schema_migrations）----------
def test_existing_old_style(tmp):
    print("\n[2] 有数据的旧库（模拟升级前：只有一部分列）")
    p = tmp / "old" / "memory.db"
    p.parent.mkdir(parents=True)
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE l0_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT NOT NULL, "
                 "msg_idx INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, "
                 "ts DATETIME DEFAULT CURRENT_TIMESTAMP, source TEXT DEFAULT 'live', client TEXT DEFAULT 'unknown', "
                 "status TEXT DEFAULT 'active', extracted INTEGER DEFAULT 0)")
    conn.executemany("INSERT INTO l0_messages (conv_id, msg_idx, role, content) VALUES (?,?,?,?)",
                     [("c1", i, "user" if i % 2 == 0 else "assistant", f"消息{i}") for i in range(20)])
    conn.commit()
    conn.close()
    applied = db.migrate(p, log=quiet)
    check("执行了全部迁移", applied == all_versions(), applied)
    conn = sqlite3.connect(p)
    check("原有 20 行还在", conn.execute("SELECT COUNT(*) FROM l0_messages").fetchone()[0] == 20)
    check("缺的列补上了", {"group_id", "extract_attempts"} <= set(db.columns(conn, "l0_messages")))
    check("新列取默认值", conn.execute("SELECT COUNT(*) FROM l0_messages WHERE extract_attempts=0").fetchone()[0] == 20)
    conn.close()
    backups = list((p.parent / "backups").glob("memory.pre_migration_001.*.db"))
    check("迁移前做了备份", len(backups) == 1, backups)
    if backups:
        b = sqlite3.connect(backups[0])
        check("备份里是迁移前的样子", "group_id" not in db.columns(b, "l0_messages")
              and b.execute("SELECT COUNT(*) FROM l0_messages").fetchone()[0] == 20)
        b.close()


# ---------- 3. 失败回滚 ----------
BROKEN = '''
from db import add_column
def up(conn):
    conn.execute("CREATE TABLE broken_new (id INTEGER PRIMARY KEY)")
    add_column(conn, "l0_messages", "broken_col", "TEXT")
    conn.execute("UPDATE l0_messages SET content = 'X'")
    raise RuntimeError("故意失败")
'''
FIXED = '''
from db import add_column
def up(conn):
    add_column(conn, "l0_messages", "fixed_col", "TEXT")
'''
COMMITS = '''
def up(conn):
    conn.execute("CREATE TABLE sneaky (id INTEGER)")
    conn.execute("COMMIT")
'''


def test_failure(tmp):
    print("\n[3] 迁移失败时整体回滚")
    mdir = tmp / "mig_fail"
    shutil.copytree(db.MIGRATIONS_DIR, mdir, ignore=shutil.ignore_patterns("__pycache__"))
    p = tmp / "fail" / "memory.db"
    db.migrate(p, migrations_dir=mdir, log=quiet)
    conn = sqlite3.connect(p)
    conn.execute("INSERT INTO l0_messages (conv_id, msg_idx, role, content) VALUES ('c', 0, 'user', '原文')")
    conn.commit()
    conn.close()
    (mdir / "900_broken.py").write_text(BROKEN, encoding="utf-8")
    raised = False
    try:
        db.migrate(p, migrations_dir=mdir, log=quiet)
    except RuntimeError as e:
        raised = "故意失败" in str(e)
    check("失败会抛出异常（启动会报错）", raised)
    conn = sqlite3.connect(p)
    check("新表没有留下", "broken_new" not in tables(conn))
    check("新列没有留下", "broken_col" not in db.columns(conn, "l0_messages"))
    check("数据没有被改", conn.execute("SELECT content FROM l0_messages").fetchone()[0] == "原文")
    check("没有执行记录", conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=900").fetchone()[0] == 0)
    conn.close()
    (mdir / "900_broken.py").write_text(FIXED, encoding="utf-8")
    check("修好后能正常执行", db.migrate(p, migrations_dir=mdir, log=quiet) == [900])

    (mdir / "901_commits.py").write_text(COMMITS, encoding="utf-8")
    raised = False
    try:
        db.migrate(p, migrations_dir=mdir, log=quiet)
    except RuntimeError as e:
        raised = "提前提交" in str(e)
    check("up() 里提前提交会被发现", raised)
    conn = sqlite3.connect(p)
    check("提前提交的迁移不记为已执行",
          conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=901").fetchone()[0] == 0)
    conn.close()

    (mdir / "901_commits.py").unlink()
    (mdir / "900_dup.py").write_text(FIXED, encoding="utf-8")
    raised = False
    try:
        db.discover(mdir)
    except RuntimeError:
        raised = True
    check("编号重复会报错", raised)
    (mdir / "900_dup.py").unlink()
    (mdir / "notes.py").write_text("x = 1", encoding="utf-8")
    check("不符合命名的文件被忽略", all(m.name != "notes" for m in db.discover(mdir)))


# ---------- 4. 两个进程同时启动 ----------
SLOW = '''
import time
def up(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS run_log (pid INTEGER)")
    import os
    conn.execute("INSERT INTO run_log VALUES (?)", (os.getpid(),))
    time.sleep(1.0)
'''


def _worker(p, mdir, q):
    sys.path.insert(0, str(SERVICE_DIR))
    import db as _db
    try:
        q.put(("ok", _db.migrate(p, migrations_dir=mdir, log=quiet)))
    except Exception as e:  # noqa: BLE001
        q.put(("err", repr(e)))


def test_concurrent(tmp):
    print("\n[4] 两个进程同时执行迁移")
    mdir = tmp / "mig_conc"
    mdir.mkdir()
    (mdir / "001_slow.py").write_text(SLOW, encoding="utf-8")
    p = tmp / "conc" / "memory.db"
    p.parent.mkdir(parents=True)
    q = multiprocessing.Queue()
    procs = [multiprocessing.Process(target=_worker, args=(p, mdir, q)) for _ in range(2)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(30)
    results = [q.get(timeout=5) for _ in procs]
    check("两个进程都没出错", all(r[0] == "ok" for r in results), results)
    check("只有一个进程真正执行了", sorted(len(r[1]) for r in results) == [0, 1], results)
    conn = sqlite3.connect(p)
    check("迁移内容只执行一次", conn.execute("SELECT COUNT(*) FROM run_log").fetchone()[0] == 1)
    conn.close()


# ---------- 5. 真实库的副本 ----------
def test_real_copy(tmp, real):
    print(f"\n[5] 真实库的副本（{real.name}）")
    p = tmp / "real" / "memory.db"
    p.parent.mkdir(parents=True)
    src = sqlite3.connect(f"file:{real}?mode=ro", uri=True)
    dst = sqlite3.connect(p)
    src.backup(dst)
    dst.close()
    src.close()
    conn = sqlite3.connect(p)
    before_counts, before_sql = row_counts(conn), table_sql(conn)
    already = db.table_exists(conn, "schema_migrations")
    conn.close()
    applied = db.migrate(p, log=quiet)
    if already:
        print(f"  （副本里已有 schema_migrations，这次执行：{applied}）")
    else:
        check("执行了全部迁移", applied == all_versions(), applied)
    conn = sqlite3.connect(p)
    after_counts, after_sql = row_counts(conn), table_sql(conn)
    after_counts.pop("schema_migrations", None)
    changed = {t: (before_counts.get(t), after_counts.get(t)) for t in after_counts
               if before_counts.get(t) != after_counts.get(t)}
    check("每张表行数不变", not changed, changed)
    diff = [n for n in after_sql if before_sql.get(n) != after_sql[n]]
    check("表结构（建表语句）没有变化——001 对现有库是空操作", not diff, diff)
    check("索引已建", {"idx_l0_conv", "idx_l1_conv"} <= indexes(conn), indexes(conn))
    check("版本号", db.current_version(p) == max(all_versions()))
    l0 = before_counts.get("l0_messages")
    l1 = before_counts.get("l1_memories")
    print(f"  （L0 {l0} 行，L1 {l1} 行）")
    conn.close()
    if not already:
        check("迁移前做了备份", len(list((p.parent / "backups").glob("memory.pre_migration_*.db"))) == 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", help="真实库路径（只读打开，复制后测试）")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="migtest_"))
    try:
        test_fresh(tmp)
        test_existing_old_style(tmp)
        test_failure(tmp)
        test_concurrent(tmp)
        if args.real:
            test_real_copy(tmp, Path(args.real).resolve())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
