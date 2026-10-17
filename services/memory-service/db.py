"""数据库连接与迁移（Phase 1）

连接：
- connect(db_path)：统一的连接入口。记忆服务、提取脚本、以后的 worker 都用它。
  等锁最多 10 秒（sqlite3 默认 5 秒）；库本身是 WAL 模式（一写多读，迁移时确保开启）。

迁移（方案 10.4 节）：
- migrations/ 目录下的 NNN_名字.py，每个文件提供 up(conn)。记忆服务启动时、提取脚本每次运行时都会调用 migrate()，
  按编号执行还没执行过的迁移，执行记录在 schema_migrations 表里。
- 规则：只加不删——不删列、不改列的含义，旧代码在新库上仍然能运行。
- 每个迁移在一个事务里执行（SQLite 的建表、加列也能回滚），失败就整体回滚、启动报错，不会留下半截结构。
- 已有数据的库执行新迁移前，先用 SQLite 的备份接口复制一份到 data/sqlite/backups/（只保留最近 10 份）。
- 两个进程同时启动也不会重复执行：拿到写锁以后会再查一次这个版本是不是已经被别人执行过。
"""
import importlib.util
import re
import sqlite3
from pathlib import Path

import timeutil

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
BUSY_TIMEOUT_SEC = 10.0
KEEP_BACKUPS = 10
_FILE_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.py$")


def connect(db_path, timeout=BUSY_TIMEOUT_SEC):
    """统一的数据库连接"""
    return sqlite3.connect(str(db_path), timeout=timeout)


# ---------- 迁移里常用的小工具 ----------

def table_exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def add_column(conn, table, col, decl):
    """列不存在才加（SQLite 的 ADD COLUMN 没有 IF NOT EXISTS）。返回是否真的加了。"""
    if col in columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return True


# ---------- 迁移执行器 ----------

class Migration:
    def __init__(self, version, name, path):
        self.version, self.name, self.path = version, name, path
        self._module = None

    def up(self, conn):
        if self._module is None:
            spec = importlib.util.spec_from_file_location(f"migration_{self.version:03d}", self.path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not hasattr(module, "up"):
                raise RuntimeError(f"迁移 {self.path.name} 没有 up(conn) 函数")
            self._module = module
        self._module.up(conn)

    @property
    def label(self):
        return f"{self.version:03d}_{self.name}"


def discover(migrations_dir=MIGRATIONS_DIR):
    found = []
    for p in sorted(Path(migrations_dir).glob("*.py")):
        m = _FILE_RE.match(p.name)
        if m:
            found.append(Migration(int(m.group(1)), m.group(2), p))
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"迁移编号重复：{versions}")
    return found


def backup(db_path, tag):
    """用 SQLite 的备份接口复制一份库（WAL 模式下也是一致的快照）；只保留最近 KEEP_BACKUPS 份同类备份。"""
    db_path = Path(db_path)
    bdir = db_path.parent / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = timeutil.utc_now().strftime("%Y%m%d-%H%M%S")
    dest_path = bdir / f"{db_path.stem}.{tag}.{stamp}.db"
    src = connect(db_path)
    dst = sqlite3.connect(str(dest_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    olds = sorted(bdir.glob(f"{db_path.stem}.pre_migration_*.db"))
    for old in olds[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return dest_path


def migrate(db_path, migrations_dir=MIGRATIONS_DIR, log=print):
    """执行还没执行过的迁移，返回这次执行了的版本号列表。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.isolation_level = None   # 自己管事务：BEGIN IMMEDIATE / COMMIT / ROLLBACK
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL")   # 持久化在库文件里，一次设置即可
        except sqlite3.OperationalError as e:
            log(f"[MIGRATE] 设置 WAL 失败（不影响运行）：{e}")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        pending = [m for m in discover(migrations_dir) if m.version not in done]
        if not pending:
            return []
        has_data = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT IN ('schema_migrations', 'sqlite_sequence')"
        ).fetchone()[0] > 0
        if has_data:
            path = backup(db_path, f"pre_migration_{pending[0].version:03d}")
            log(f"[MIGRATE] 执行迁移前已备份：{path.name}")
        applied = []
        for m in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 拿到写锁后再查一次：另一个进程可能刚刚执行过
                if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (m.version,)).fetchone():
                    conn.execute("COMMIT")
                    continue
                m.up(conn)
                if not conn.in_transaction:
                    raise RuntimeError(f"迁移 {m.label} 在 up() 里提前提交了事务（不要调用 commit / executescript）")
                conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                             (m.version, m.name, timeutil.utc_now_str()))
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                    log(f"[MIGRATE] 迁移 {m.label} 失败，已回滚")
                else:
                    log(f"[MIGRATE] 迁移 {m.label} 失败（事务已被提前提交，无法回滚，请检查库结构）")
                raise
            applied.append(m.version)
            log(f"[MIGRATE] 已执行 {m.label}")
        return applied
    finally:
        conn.close()


def current_version(db_path):
    conn = connect(db_path)
    try:
        if not table_exists(conn, "schema_migrations"):
            return 0
        return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()
