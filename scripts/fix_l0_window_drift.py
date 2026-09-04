#!/usr/bin/env python3
"""
Bug4 历史数据修复脚本：修复因滑动窗口大小变化导致的 L0 msg_idx 错位

问题：旧版代码用 enumerate(req.messages) 的下标直接当 msg_idx，导致窗口大小变化后：
- 早期历史消息首次出现时 msg_idx 较小，覆盖了已有的不相关消息
- Dashboard 按 id ASC 排序，导致这些消息显示在最后（id 最大）
- msg_idx 不再反映真实对话顺序

修复策略：
- 方案 A（推荐）：从 Kelivo 备份重新导入，使用真实的 message_order
- 方案 B（兜底）：按 id ASC 重新赋值连续的 msg_idx（不能完全恢复真实顺序，但至少不再混乱）

用法：
  python scripts/fix_l0_window_drift.py              # dry-run，只显示会改什么
  python scripts/fix_l0_window_drift.py --apply      # 真正写库
  python scripts/fix_l0_window_drift.py --conv-id xxx --apply  # 只修复指定对话
"""

import sqlite3
import sys
import argparse
from pathlib import Path

# 数据库路径
REPO_ROOT = Path(__file__).parent.parent
SQLITE_PATH = REPO_ROOT / "data" / "sqlite" / "memory.db"


def fix_conv_msg_idx(conn, conv_id, dry_run=True):
    """方案 B：按 id ASC 重新赋值连续的 msg_idx（兜底方案）

    注意：这只是"不再比现在更乱"的兜底方案，不能百分百恢复真实顺序，
    因为部分行的 id 顺序本身已经是错的（窗口扩大后首次写入的历史消息，id 最大）。

    推荐先尝试方案 A：从 Kelivo 备份重新导入。
    """
    c = conn.cursor()

    # 1. 查出当前 active 且无 group_id 的行（按 id ASC）
    rows = c.execute(
        "SELECT id, msg_idx FROM l0_messages "
        "WHERE conv_id=? AND status='active' AND (group_id IS NULL OR group_id='') "
        "ORDER BY id ASC",
        (conv_id,)
    ).fetchall()

    if not rows:
        return 0, 0

    # 2. 检查是否有 msg_idx 错位（不连续、有重复、或顺序不对）
    current_indices = [r[1] for r in rows]
    expected_indices = list(range(len(rows)))

    needs_fix = False
    if current_indices != expected_indices:
        needs_fix = True

    if not needs_fix:
        return 0, len(rows)

    # 3. 重新赋值 msg_idx = 0, 1, 2, ...
    updates = []
    for new_idx, (row_id, old_idx) in enumerate(rows):
        if new_idx != old_idx:
            updates.append((new_idx, row_id))

    if dry_run:
        print(f"  会更新 {len(updates)} 条消息的 msg_idx（总共 {len(rows)} 条）")
        if len(updates) <= 10:
            for new_idx, row_id in updates[:10]:
                print(f"    id={row_id}: msg_idx -> {new_idx}")
        else:
            for new_idx, row_id in updates[:5]:
                print(f"    id={row_id}: msg_idx -> {new_idx}")
            print(f"    ... 还有 {len(updates) - 5} 条")
    else:
        for new_idx, row_id in updates:
            c.execute("UPDATE l0_messages SET msg_idx=? WHERE id=?", (new_idx, row_id))
        print(f"  ✓ 已更新 {len(updates)} 条消息的 msg_idx")

    return len(updates), len(rows)


def main():
    parser = argparse.ArgumentParser(description="修复 L0 msg_idx 错位问题")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--conv-id", help="只修复指定 conv_id（默认修复所有）")
    args = parser.parse_args()

    if not SQLITE_PATH.exists():
        print(f"❌ 数据库不存在: {SQLITE_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # 获取需要修复的 conv_id 列表
    if args.conv_id:
        conv_ids = [args.conv_id]
    else:
        rows = c.execute(
            "SELECT DISTINCT conv_id FROM l0_messages "
            "WHERE status='active' AND (group_id IS NULL OR group_id='')"
        ).fetchall()
        conv_ids = [r[0] for r in rows]

    print(f"{'=' * 60}")
    print(f"Bug4 历史数据修复 - {'DRY RUN' if not args.apply else 'APPLY'}")
    print(f"{'=' * 60}")
    print(f"方案 B（兜底）：按 id ASC 重新赋值 msg_idx")
    print(f"")
    print(f"⚠️  注意：这只能保证 msg_idx 连续，不能完全恢复真实对话顺序")
    print(f"   （因为部分消息的 id 顺序本身就是错的）")
    print(f"")
    print(f"✅ 推荐方案：从 Kelivo 备份重新导入，使用真实的 message_order")
    print(f"")
    print(f"待处理对话数: {len(conv_ids)}")
    print(f"")

    total_updated = 0
    total_rows = 0

    for i, conv_id in enumerate(conv_ids, 1):
        print(f"[{i}/{len(conv_ids)}] conv_id={conv_id}")
        updated, rows_count = fix_conv_msg_idx(conn, conv_id, dry_run=not args.apply)
        total_updated += updated
        total_rows += rows_count

    print(f"")
    print(f"{'=' * 60}")
    print(f"总计: {total_updated} 条需要更新 / {total_rows} 条总消息")

    if args.apply:
        conn.commit()
        print(f"✅ 已提交数据库修改")
    else:
        print(f"")
        print(f"💡 这是 dry-run，没有修改数据库")
        print(f"   确认无误后，加 --apply 参数执行真正的修复")

    conn.close()
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
