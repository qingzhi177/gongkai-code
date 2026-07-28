#!/usr/bin/env python3
"""一次性清理：历史孤儿 L1（问题1）。

孤儿定义：active L1，但其所属 conv_id 的 active L0 数量为 0
—— 即整个来源对话的 L0 已全部 superseded/删除，这条 L1 索引的对话内容已不存在，
却仍 active、仍会被 recall 搜到。多为联动逻辑（修复6/7/单条删除）存在之前，
删对话或版本切换遗留的。

处理：标 L1 status='superseded'（软删，可逆）+ 从 ChromaDB 删向量 l1_{id}。
默认 dry-run，加 --apply 才写库 + 删向量。

安全性：
  - 判据是「整个 conv 无 active L0」，比「批次区间无 active L0」更强，不会误删
    仍有活跃来源的 L1。
  - 若某 conv 仍有 active L0（内容还在），其 L1 不在清理范围。
"""
import os
import sys
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "sqlite" / "memory.db"


def load_chroma():
    """返回 l1_collection，失败返回 None（dry-run 不需要）。"""
    try:
        import chromadb
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent /
                    "services" / "memory-service" / ".env")
        cli = chromadb.HttpClient(host=os.getenv("CHROMA_HOST", "localhost"),
                                  port=int(os.getenv("CHROMA_PORT", "8000")))
        return cli.get_or_create_collection(name="l1_memories")
    except Exception as e:
        print(f"[warn] 连接 ChromaDB 失败: {e}")
        return None


def find_orphans(conn):
    """返回 [(l1_id, conv_id, content_prefix), ...]：真孤儿 L1。

    孤儿判据（三者同时满足）：
      1. status='active'
      2. conv_id 非空（排除 client='ai_self' 的 AI 自述感受——它们 conv_id=''、
         本就无对应 L0，不是孤儿，删了会毁掉 AI 的自述记忆库）
      3. 该 conv_id 的 active L0 数量为 0（来源对话已整体消失）
    额外用 client!='ai_self' 双保险。
    """
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, conv_id, client, substr(content,1,36) FROM l1_memories "
        "WHERE status='active' AND conv_id IS NOT NULL AND conv_id != ''"
    ).fetchall()
    orphans = []
    for l1_id, conv_id, client, prefix in rows:
        if client == 'ai_self':
            continue
        n = c.execute(
            "SELECT COUNT(*) FROM l0_messages WHERE conv_id=? AND status='active'",
            (conv_id,)).fetchone()[0]
        if n == 0:
            orphans.append((l1_id, conv_id, prefix))
    return orphans


def main():
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(str(DB))
    orphans = find_orphans(conn)

    print(f"{'[APPLY]' if apply else '[DRY-RUN]'} 检测到 {len(orphans)} 条孤儿 L1"
          f"（所属 conv 无 active L0）：")
    for l1_id, conv_id, prefix in orphans:
        print(f"  L1 id={l1_id} conv={conv_id[:22]} content={prefix!r}")

    if not orphans:
        print("无孤儿 L1，无需清理。")
        conn.close()
        return

    if apply:
        ids = [o[0] for o in orphans]
        c = conn.cursor()
        c.executemany("UPDATE l1_memories SET status='superseded' WHERE id=?",
                      [(i,) for i in ids])
        conn.commit()
        print(f"\n已软删 {len(ids)} 条 L1。")
        col = load_chroma()
        if col is not None:
            try:
                col.delete(ids=[f"l1_{i}" for i in ids])
                print(f"已从 ChromaDB 删除 {len(ids)} 条向量。")
            except Exception as e:
                print(f"[warn] ChromaDB 删除失败: {e}（L1 已软删，向量可重跑本脚本清理）")
    else:
        print("\n未写库（dry-run）。确认无误后加 --apply 执行。")
    conn.close()


if __name__ == "__main__":
    main()
