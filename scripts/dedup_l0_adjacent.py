#!/usr/bin/env python3
"""一次性清洗：删除 L0 相邻重复消息。

背景：Kelivo 在含工具调用的轮次里，会把历史消息拆成多条（tool_use-only
assistant、tool_result user 归一化后为空），空消息占 msg_idx 下标导致真实
消息 msg_idx 在轮次间漂移；且同一句 assistant 回复会被重复发回。save_conversation
的 dedup 按 (conv_id, msg_idx) 匹配，位置漂移后失配 → 同内容重复 insert。

清洗规则（保守，仅去相邻重复）：
  每个 conv 内、active L0 按 msg_idx 升序，若某行与「紧邻的前一条 active 行」
  role 相同且 content「忽略空白后」相同 → 标 superseded（保留最早那条）。
  空白不敏感：工具调用把一轮 assistant 拆成多块，Kelivo 重发时块间空白(\n)可能
  不一致，导致同一句回复两条只差空白，需一并去重。
  不同时刻说的相同内容（中间隔着别的消息）不相邻 → 保留，不误删。

软删（status='superseded'），可逆。默认 dry-run，加 --apply 才真正写库。
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "sqlite" / "memory.db"


def find_adjacent_dups(conn):
    """返回需软删的行列表：[(dup_id, keep_id, conv_id, role, content_prefix), ...]"""
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, conv_id, msg_idx, role, content FROM l0_messages "
        "WHERE status='active' ORDER BY conv_id, msg_idx, id"
    ).fetchall()
    def key(s):
        # 空白不敏感比较键（与网关 server.js dedupKey 一致）
        return "".join((s or "").split())

    to_delete = []
    prev = {}  # conv_id -> (id, role, content_key) 上一条保留的 active 行
    for rid, conv_id, msg_idx, role, content in rows:
        p = prev.get(conv_id)
        if p and p[1] == role and p[2] == key(content):
            # 与紧邻前一条相同 → 当前这条是重复，删它，保留前一条(更早)
            to_delete.append((rid, p[0], conv_id, role, (content or "")[:24]))
            # prev 不更新：连续 N 条相同只保留第一条，后续都删
        else:
            prev[conv_id] = (rid, role, key(content))
    return to_delete


def main():
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(str(DB))
    dups = find_adjacent_dups(conn)

    print(f"{'[APPLY]' if apply else '[DRY-RUN]'} 检测到 {len(dups)} 条相邻重复：")
    for dup_id, keep_id, conv_id, role, prefix in dups:
        print(f"  删 id={dup_id} (保留 id={keep_id}) conv={conv_id[:16]} "
              f"{role} content={prefix!r}")

    if not dups:
        print("无相邻重复，无需清洗。")
        conn.close()
        return

    if apply:
        c = conn.cursor()
        ids = [d[0] for d in dups]
        c.executemany("UPDATE l0_messages SET status='superseded' WHERE id=?",
                      [(i,) for i in ids])
        conn.commit()
        print(f"\n已软删 {len(ids)} 条：{ids}")
        # 提示：被删行若 extracted=1，其 L1 索引可能同样重复，需另行处理
        c.execute(
            "SELECT id, extracted FROM l0_messages WHERE id IN (%s)"
            % ",".join("?" * len(ids)), ids)
        ext1 = [r[0] for r in c.fetchall() if r[1] == 1]
        if ext1:
            print(f"注意：其中 {len(ext1)} 条 extracted=1 (曾提取过 L1)："
                  f"{ext1}，对应 L1 可能也重复，建议后续核查。")
    else:
        print("\n未写库（dry-run）。确认无误后加 --apply 执行。")
    conn.close()


if __name__ == "__main__":
    main()
