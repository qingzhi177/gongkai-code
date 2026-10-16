"""一次性迁移：把 ChromaDB 里的 L1 向量复制到 SQLite vectors 表（Phase 1）

用法（ChromaDB 容器还在运行时执行）：
    cd ~/memory-system/services/memory-service
    venv/bin/python ../../scripts/migrate_chroma_to_sqlite.py

- 只复制 SQLite 里仍为 active 的 L1 对应的向量（孤儿向量丢弃）
- 已存在于 vectors 表的不覆盖
- 复制完后，缺失的向量由记忆服务的定时对账（POST /index/reconcile）自动重新嵌入
"""
import os
import sqlite3
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from vector_store import VectorCollection  # noqa: E402

DATA_DIR = os.getenv("DATA_DIR", os.path.expanduser("~/memory-system/data"))
SQLITE_PATH = os.path.join(DATA_DIR, "sqlite", "memory.db")


def main():
    import chromadb
    client = chromadb.HttpClient(host=os.getenv("CHROMA_HOST", "localhost"), port=int(os.getenv("CHROMA_PORT", 8000)))
    col = client.get_collection("l1_memories")
    conn = sqlite3.connect(SQLITE_PATH)
    active = {f"l1_{r[0]}": r[1] for r in conn.execute("SELECT id, content FROM l1_memories WHERE status='active'")}
    conn.close()
    store = VectorCollection(SQLITE_PATH, owner_type="l1")
    existing = store.existing_ids()
    todo = [i for i in active if i not in existing]
    copied = 0
    for start in range(0, len(todo), 100):
        batch = todo[start:start + 100]
        got = col.get(ids=batch, include=["embeddings"])
        ids = list(got.get("ids") or [])
        embs = got.get("embeddings")
        if embs is None or len(ids) == 0:
            continue
        store.add(ids=ids, embeddings=[list(e) for e in embs], documents=[active[i] for i in ids])
        copied += len(ids)
    missing = len(todo) - copied
    print(f"Chroma 向量总数 {col.count()}；SQLite active L1 {len(active)} 条；"
          f"复制 {copied} 条；已存在 {len(existing)} 条；仍缺 {missing} 条（交给对账重新嵌入）")


if __name__ == "__main__":
    main()
