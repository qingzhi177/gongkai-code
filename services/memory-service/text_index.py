"""L1 关键词检索：SQLite FTS5 + jieba 预分词（Phase 1：替代每次查询都全量重建的 BM25）

为什么：
- 以前每次 /search 都把全部 L1 读出来、jieba 分词、重建 BM25Okapi，规模到上万条时每轮多出数百毫秒以上。
- SQLite 自带的 unicode61 分词器不会切分中文（一整串汉字算一个词），trigram 分词器又搜不到两个字的词，
  所以入库前先用 jieba 分好词、用空格连起来，再交给 FTS5 建索引；查询时同样先分词。

一致性（不依赖每个写入点都记得更新索引）：
- l1_memories 上的触发器在任何增删改后递增 index_meta('l1').version（纯 SQL，跨进程有效）；
- 检索前 / 定时任务里调用 reconcile()：版本有变化就按「id + 内容哈希」对账，只重建变化的那几条。
"""
import hashlib
import re
import sqlite3
import threading

import jieba

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS l1_fts USING fts5(seg, l1_id UNINDEXED, tokenize = 'unicode61');
CREATE TABLE IF NOT EXISTS l1_fts_state (l1_id INTEGER PRIMARY KEY, text_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS index_meta (name TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO index_meta (name, version) VALUES ('l1', 0);
CREATE TRIGGER IF NOT EXISTS trg_l1_ver_ins AFTER INSERT ON l1_memories
BEGIN UPDATE index_meta SET version = version + 1 WHERE name = 'l1'; END;
CREATE TRIGGER IF NOT EXISTS trg_l1_ver_del AFTER DELETE ON l1_memories
BEGIN UPDATE index_meta SET version = version + 1 WHERE name = 'l1'; END;
CREATE TRIGGER IF NOT EXISTS trg_l1_ver_upd AFTER UPDATE OF content, quote, tags, status ON l1_memories
BEGIN UPDATE index_meta SET version = version + 1 WHERE name = 'l1'; END;
"""

# 单字虚词/代词：OR 查询里会匹配到几乎所有记忆，去掉
STOP_TOKENS = set("的 了 吗 呢 啊 呀 吧 嘛 哦 噢 嗯 哈 是 在 和 与 就 都 也 很 还 有 这 那 我 你 他 她 它 们 个 着 过 被 把 给 对 让 又 才 么 啦 喔 诶".split())
_PUNCT_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)

_lock = threading.Lock()
_reconciled_version = None


def _hash(*parts):
    return hashlib.sha1("\x1f".join(p or "" for p in parts).encode("utf-8")).hexdigest()


def tokenize(text):
    toks = []
    for t in jieba.cut(text or ""):
        t = t.strip().lower()
        if not t or _PUNCT_RE.match(t) or t in STOP_TOKENS:
            continue
        toks.append(t)
    return toks


def ensure_schema(db_path):
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _index_text(content, quote, tags):
    # 内容 + 原句 + 标签一起建索引（标签是 JSON 数组字符串，分词时符号会被过滤掉）
    return " ".join(tokenize(" ".join(x for x in (content, quote, tags) if x)))


def reconcile(db_path, force=False):
    """按 id + 内容哈希对账，增量更新 FTS。返回 (新增/更新数, 删除数)；没有变化返回 (0, 0)。"""
    global _reconciled_version
    with _lock:
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            ver = conn.execute("SELECT version FROM index_meta WHERE name = 'l1'").fetchone()
            ver = ver[0] if ver else 0
            if not force and ver == _reconciled_version:
                return (0, 0)
            active = {}
            for lid, content, quote, tags in conn.execute(
                    "SELECT id, content, quote, tags FROM l1_memories WHERE status = 'active'"):
                active[lid] = (_hash(content, quote, tags), content, quote, tags)
            state = dict(conn.execute("SELECT l1_id, text_hash FROM l1_fts_state").fetchall())
            to_remove = [lid for lid, h in state.items() if lid not in active or active[lid][0] != h]
            to_add = [lid for lid, v in active.items() if state.get(lid) != v[0]]
            for lid in to_remove:
                conn.execute("DELETE FROM l1_fts WHERE l1_id = ?", (lid,))
                conn.execute("DELETE FROM l1_fts_state WHERE l1_id = ?", (lid,))
            for lid in to_add:
                h, content, quote, tags = active[lid]
                conn.execute("INSERT INTO l1_fts (seg, l1_id) VALUES (?, ?)", (_index_text(content, quote, tags), lid))
                conn.execute("INSERT OR REPLACE INTO l1_fts_state (l1_id, text_hash) VALUES (?, ?)", (lid, h))
            conn.commit()
            _reconciled_version = ver
            return (len(to_add), len(to_remove))
        finally:
            conn.close()


def search(db_path, query, limit=30):
    """返回 [(l1_id, score)]，score 越大越相关（已把 FTS5 的 bm25 取负）。"""
    toks = list(dict.fromkeys(tokenize(query)))[:24]
    if not toks:
        return []
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in toks)
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        rows = conn.execute(
            "SELECT l1_id, bm25(l1_fts) AS s FROM l1_fts WHERE l1_fts MATCH ? ORDER BY s LIMIT ?",
            (match, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[FTS] 查询失败: {e}")
        return []
    finally:
        conn.close()
    return [(int(lid), -float(s)) for lid, s in rows]
