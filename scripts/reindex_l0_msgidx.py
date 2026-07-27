#!/usr/bin/env python3
"""一次性迁移：把活跃对话的 active L0 的 msg_idx 重编号为连续 0,1,2,...

背景：网关根治前，msg_idx 用「含空消息的原始数组下标」，工具轮次的空消息占位
导致 msg_idx 稀疏（如 0,1,4,5,...）。网关根治后改为「清洗序列的连续下标」。
存量活跃对话的稀疏 msg_idx 与新规则的连续下标错位 → 下一轮 dedup 误判成版本
切换、重复 insert。本脚本把存量 active L0 重编号成连续，与新网关对齐。

安全性：
  - 只改 active 行的 msg_idx（superseded 是历史、dedup 不查，保持原样）
  - 按 (msg_idx, id) 升序重编号，新号 <= 旧号，UPDATE 时目标槽必已腾空，无冲突
  - msg_idx 只是排序号；L1.source_msg_id 引用的是 L0.id 而非 msg_idx，不受影响
  - 已连续(从0无空洞)的对话跳过

默认 dry-run，加 --apply 才写库。
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "sqlite" / "memory.db"


def plan(conn):
    """返回 {conv_id: [(l0_id, old_idx, new_idx), ...]}，只含需要改的对话。"""
    c = conn.cursor()
    convs = [r[0] for r in c.execute(
        "SELECT DISTINCT conv_id FROM l0_messages WHERE status='active'").fetchall()]
    result = {}
    for conv in convs:
        rows = c.execute(
            "SELECT id, msg_idx FROM l0_messages "
            "WHERE conv_id=? AND status='active' ORDER BY msg_idx, id",
            (conv,)).fetchall()
        changes = []
        for new_idx, (l0_id, old_idx) in enumerate(rows):
            if old_idx != new_idx:
                changes.append((l0_id, old_idx, new_idx))
        if changes:
            result[conv] = changes
    return result


def main():
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(str(DB))
    changes_by_conv = plan(conn)

    total = sum(len(v) for v in changes_by_conv.values())
    print(f"{'[APPLY]' if apply else '[DRY-RUN]'} {len(changes_by_conv)} 个对话需重编号，"
          f"共 {total} 行 msg_idx 变更：")
    for conv, changes in changes_by_conv.items():
        print(f"  conv={conv[:20]} ({len(changes)} 行):")
        for l0_id, old_idx, new_idx in changes:
            print(f"     id={l0_id}  msg_idx {old_idx} → {new_idx}")

    if not changes_by_conv:
        print("所有活跃对话 msg_idx 已连续，无需迁移。")
        conn.close()
        return

    if apply:
        c = conn.cursor()
        for conv, changes in changes_by_conv.items():
            # 升序应用（新号<=旧号，无冲突）
            for l0_id, old_idx, new_idx in sorted(changes, key=lambda x: x[1]):
                c.execute("UPDATE l0_messages SET msg_idx=? WHERE id=?", (new_idx, l0_id))
        conn.commit()
        print(f"\n已重编号 {total} 行。")
    else:
        print("\n未写库（dry-run）。确认无误后加 --apply 执行。")
    conn.close()


if __name__ == "__main__":
    main()
