"""SQLite 向量存储（Phase 1：替代 ChromaDB）

为什么：
- 以前向量和元数据在 ChromaDB 里另存一份，和 SQLite 互不同步（is_core / last_shown_at 等），是好几个 bug 的根源；
  ChromaDB 容器还要单独占内存、单独备份（之前甚至没挂对数据目录）。
- 个人规模（几千到几万条）下，numpy 暴力点积只要几毫秒，不需要专门的向量数据库。

设计：
- 向量以 float32 BLOB（已归一化）存在记忆库同一个 SQLite 文件的 vectors 表里 → 单一真相源，跟着 backup.sh 一起备份。
- 进程内缓存一份矩阵；任何进程写入/删除向量都会递增 vector_meta.version，
  其他进程（记忆服务 / cron 提取）检索时发现版本变化就重新加载。
- VectorCollection 提供与原 chromadb Collection 兼容的 add / update / delete / get / query / count，
  main.py、extract_l1.py 里原来调用 l1_collection 的地方无需逐个改写。
"""
import hashlib
import sqlite3
import threading

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    owner_type TEXT NOT NULL,             -- 'l1'（以后还会有 episode / arc 等）
    owner_id   TEXT NOT NULL,             -- 与原 Chroma id 一致，如 'l1_123'
    model      TEXT NOT NULL DEFAULT '',
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,             -- float32，已归一化
    text_hash  TEXT,                      -- 嵌入文本的 sha1，内容没变就不用重算
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (owner_type, owner_id)
);
CREATE TABLE IF NOT EXISTS vector_meta (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO vector_meta (id, version) VALUES (1, 0);
"""


def text_hash(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def ensure_schema(db_path):
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _normalize(vec):
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class VectorCollection:
    """与 chromadb Collection 接口兼容的 SQLite 向量集合"""

    def __init__(self, db_path, owner_type="l1", model="text-embedding-v3"):
        self.db_path = str(db_path)
        self.owner_type = owner_type
        self.model = model
        self._lock = threading.Lock()
        self._loaded_version = None
        self._ids = []
        self._mat = None
        ensure_schema(self.db_path)

    # ---------- 内部 ----------
    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=10)

    @staticmethod
    def _bump(conn):
        conn.execute("UPDATE vector_meta SET version = version + 1 WHERE id = 1")

    def _current_version(self, conn):
        row = conn.execute("SELECT version FROM vector_meta WHERE id = 1").fetchone()
        return row[0] if row else 0

    def _ensure_loaded(self):
        with self._lock:
            conn = self._conn()
            try:
                ver = self._current_version(conn)
                if ver == self._loaded_version and self._mat is not None:
                    return
                rows = conn.execute(
                    "SELECT owner_id, dim, vec FROM vectors WHERE owner_type = ?", (self.owner_type,)
                ).fetchall()
            finally:
                conn.close()
            ids, vecs = [], []
            dim = None
            for oid, d, blob in rows:
                v = np.frombuffer(blob, dtype=np.float32)
                if dim is None:
                    dim = v.shape[0]
                if v.shape[0] != dim:      # 维度不一致（换过模型）的跳过，等重嵌入
                    continue
                ids.append(oid)
                vecs.append(v)
            self._ids = ids
            self._mat = np.vstack(vecs) if vecs else np.zeros((0, 1), dtype=np.float32)
            self._loaded_version = ver

    # ---------- chromadb 兼容接口 ----------
    def add(self, ids, embeddings, documents=None, metadatas=None):
        """新增或覆盖（upsert）。metadatas 忽略：元数据以 SQLite 的业务表为准。"""
        if not ids:
            return
        conn = self._conn()
        try:
            for k, (oid, emb) in enumerate(zip(ids, embeddings)):
                v = _normalize(emb)
                th = text_hash(documents[k]) if documents and k < len(documents) and documents[k] is not None else None
                conn.execute(
                    "INSERT OR REPLACE INTO vectors (owner_type, owner_id, model, dim, vec, text_hash, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                    (self.owner_type, str(oid), self.model, int(v.shape[0]), v.tobytes(), th),
                )
            self._bump(conn)
            conn.commit()
        finally:
            conn.close()

    upsert = add

    def update(self, ids, embeddings=None, documents=None, metadatas=None):
        """只更新元数据时什么都不用做（元数据在 SQLite 业务表里）；带 embeddings 时等同 upsert。"""
        if embeddings is None:
            return
        self.add(ids, embeddings, documents)

    def delete(self, ids=None, where=None):
        if not ids:
            return
        conn = self._conn()
        try:
            conn.executemany(
                "DELETE FROM vectors WHERE owner_type = ? AND owner_id = ?",
                [(self.owner_type, str(i)) for i in ids],
            )
            self._bump(conn)
            conn.commit()
        finally:
            conn.close()

    def reset(self):
        """清空本集合（用于 /admin/purge_all）"""
        conn = self._conn()
        try:
            conn.execute("DELETE FROM vectors WHERE owner_type = ?", (self.owner_type,))
            self._bump(conn)
            conn.commit()
        finally:
            conn.close()

    def count(self):
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM vectors WHERE owner_type = ?", (self.owner_type,)).fetchone()[0]
        finally:
            conn.close()

    def existing_ids(self):
        conn = self._conn()
        try:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT owner_id, text_hash FROM vectors WHERE owner_type = ?", (self.owner_type,))}
        finally:
            conn.close()

    def get(self, ids=None, include=None):
        ids = [str(i) for i in (ids or [])]
        out_ids, embs = [], []
        if ids:
            conn = self._conn()
            try:
                q = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"SELECT owner_id, vec FROM vectors WHERE owner_type = ? AND owner_id IN ({q})",
                    [self.owner_type] + ids,
                ).fetchall()
            finally:
                conn.close()
            found = {oid: np.frombuffer(blob, dtype=np.float32) for oid, blob in rows}
            for i in ids:
                if i in found:
                    out_ids.append(i)
                    embs.append(found[i])
        return {
            "ids": out_ids,
            "embeddings": embs,
            "documents": [None] * len(out_ids),
            "metadatas": [{} for _ in out_ids],
        }

    def query(self, query_embeddings, n_results=10, include=None):
        """余弦相似度 Top-K。distances = 1 - 余弦相似度（越小越近），另附 similarities。"""
        self._ensure_loaded()
        results = {"ids": [], "distances": [], "similarities": [], "documents": [], "metadatas": []}
        for qe in query_embeddings:
            if self._mat is None or len(self._ids) == 0:
                for key in results:
                    results[key].append([])
                continue
            q = _normalize(qe)
            if q.shape[0] != self._mat.shape[1]:
                for key in results:
                    results[key].append([])
                continue
            sims = self._mat @ q
            k = min(int(n_results), len(self._ids))
            idx = np.argpartition(-sims, k - 1)[:k]
            idx = idx[np.argsort(-sims[idx])]
            results["ids"].append([self._ids[i] for i in idx])
            results["similarities"].append([float(sims[i]) for i in idx])
            results["distances"].append([float(1.0 - sims[i]) for i in idx])
            results["documents"].append([None] * k)
            results["metadatas"].append([{} for _ in range(k)])
        return results
