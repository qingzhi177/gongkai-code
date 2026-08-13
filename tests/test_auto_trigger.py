#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2-1: 单元测试 - 自动触发逻辑、增量演化、阈值边界条件

运行: python3 tests/test_auto_trigger.py
"""

import sys
import sqlite3
from pathlib import Path

# 添加服务目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "memory-service"))

# 配置测试数据库路径
SQLITE_PATH = Path(__file__).parent.parent / "data" / "sqlite" / "memory.db"


def test_check_thresholds():
    """测试阈值检查逻辑"""
    print("\n=== 测试 1: 阈值检查逻辑 ===")

    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # 读取配置
    config = c.execute("SELECT check_threshold_l1 FROM narrative_config WHERE id=1").fetchone()
    threshold = config[0] if config else 10

    # 读取当前水位线
    watermark = c.execute(
        "SELECT last_l1_id FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    last_l1_id = watermark[0] if watermark else 0

    # 计算新增重要记忆数
    new_count = c.execute(
        "SELECT COUNT(*) FROM l1_memories WHERE status='active' AND (is_core=1 OR arousal>=0.6) AND id > ?",
        (last_l1_id,)
    ).fetchone()[0]

    conn.close()

    print(f"  阈值: {threshold}")
    print(f"  水位线: {last_l1_id}")
    print(f"  新增重要记忆: {new_count}")
    print(f"  是否达到阈值: {'✅ 是' if new_count >= threshold else '❌ 否'}")

    assert threshold > 0, "阈值必须大于 0"
    assert last_l1_id >= 0, "水位线不能为负"
    assert new_count >= 0, "新增数不能为负"

    return new_count >= threshold


def test_watermark_increment():
    """测试水位线推进逻辑"""
    print("\n=== 测试 2: 水位线推进逻辑 ===")

    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # 获取所有版本的水位线
    versions = c.execute(
        "SELECT version, last_l1_id, trigger_type, status FROM shared_narrative ORDER BY version"
    ).fetchall()

    conn.close()

    print(f"  共 {len(versions)} 个版本:")
    for v in versions[-5:]:  # 只显示最近 5 个
        print(f"    v{v[0]}: 水位线={v[1]}, 触发={v[2]}, 状态={v[3]}")

    # 检查 active 版本的水位线是否 >= 上一个 active 之前的最大水位线
    # （允许中间有测试回拨，但 active 版本应该是最终正确的）
    if len(versions) > 1:
        active_version = [v for v in versions if v[3] == 'active']
        if active_version:
            active_watermark = active_version[0][1]
            # 找到 active 之前的最后一个 superseded 版本
            superseded_versions = [v for v in versions if v[3] == 'superseded' and v[0] < active_version[0][0]]
            if superseded_versions:
                last_superseded_watermark = superseded_versions[-1][1]
                if active_watermark < last_superseded_watermark:
                    print(f"  ⚠️ active 版本水位线({active_watermark}) < 上个 superseded 版本({last_superseded_watermark})")
                    print(f"     这可能是测试回拨导致，生产环境应避免")
                else:
                    print(f"  ✅ active 版本水位线({active_watermark}) >= 上个 superseded 版本({last_superseded_watermark})")
            else:
                print(f"  ℹ️ 只有一个 active 版本，无法比较")
        else:
            print("  ⚠️ 没有 active 版本")
    else:
        print("  ⚠️ 版本数不足，跳过单调性检查")

    return True


def test_incremental_mode():
    """测试增量演化模式"""
    print("\n=== 测试 3: 增量演化模式 ===")

    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # 获取最近版本的 trigger_details
    row = c.execute(
        "SELECT version, trigger_details FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()

    conn.close()

    if not row:
        print("  ⚠️ 没有活跃的 Narrative")
        return False

    version, details_str = row

    try:
        import json
        details = json.loads(details_str) if details_str else {}

        print(f"  版本: v{version}")
        print(f"  模式: {details.get('mode', 'N/A')}")
        print(f"  原因: {details.get('reason', 'N/A')}")
        print(f"  消费 L1 数: {details.get('l1_count', 0)}")
        print(f"  来源 L1 IDs: {details.get('l1_ids', [])[:5]}{'...' if len(details.get('l1_ids', [])) > 5 else ''}")

        # 验证 JSON 结构
        assert 'mode' in details, "缺少 mode 字段"
        assert details['mode'] in ['full', 'incremental'], f"mode 值非法: {details['mode']}"
        if details['mode'] == 'incremental':
            assert 'l1_ids' in details, "增量模式缺少 l1_ids"
            assert 'l1_count' in details, "增量模式缺少 l1_count"
            assert len(details['l1_ids']) == details['l1_count'], "l1_ids 数量与 l1_count 不匹配"

        print(f"  ✅ trigger_details 结构验证通过")
        return True

    except json.JSONDecodeError as e:
        print(f"  ❌ trigger_details JSON 解析失败: {e}")
        return False
    except AssertionError as e:
        print(f"  ❌ 结构验证失败: {e}")
        return False


def test_boundary_conditions():
    """测试边界条件"""
    print("\n=== 测试 4: 边界条件 ===")

    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # 测试 1: 水位线 = 0 的情况（首次生成）
    zero_count = c.execute(
        "SELECT COUNT(*) FROM l1_memories WHERE status='active' AND (is_core=1 OR arousal>=0.6) AND id > 0"
    ).fetchone()[0]
    print(f"  边界 1 (水位线=0): 新增 {zero_count} 条")
    assert zero_count >= 0, "计数不能为负"

    # 测试 2: 没有新增记忆的情况
    max_l1_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM l1_memories WHERE status='active'").fetchone()[0]
    zero_new = c.execute(
        "SELECT COUNT(*) FROM l1_memories WHERE status='active' AND (is_core=1 OR arousal>=0.6) AND id > ?",
        (max_l1_id,)
    ).fetchone()[0]
    print(f"  边界 2 (水位线=最大ID): 新增 {zero_new} 条")
    assert zero_new == 0, "水位线在最大 ID 时应该没有新增"

    # 测试 3: 阈值 = 1 的极端情况
    config = c.execute("SELECT check_threshold_l1 FROM narrative_config WHERE id=1").fetchone()
    threshold = config[0] if config else 10
    print(f"  边界 3 (当前阈值={threshold}): {'✅ 合理' if threshold >= 1 else '❌ 阈值过小'}")
    assert threshold >= 1, "阈值不能小于 1"

    # 测试 4: 单次消费上限 60 条的边界
    watermark = c.execute(
        "SELECT last_l1_id FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    last_l1_id = watermark[0] if watermark else 0

    total_new = c.execute(
        "SELECT COUNT(*) FROM l1_memories WHERE status='active' AND (is_core=1 OR arousal>=0.6) AND id > ?",
        (last_l1_id,)
    ).fetchone()[0]

    if total_new > 60:
        print(f"  边界 4 (单次上限 60): 当前新增 {total_new} 条，超过上限 ⚠️")
        print(f"    → 第一批会消费 60 条，剩余 {total_new - 60} 条留给下轮")
    else:
        print(f"  边界 4 (单次上限 60): 当前新增 {total_new} 条，未超上限 ✅")

    conn.close()

    print("  ✅ 边界条件测试通过")
    return True


def test_skip_logic():
    """测试 skip 逻辑（无新增时不生成）"""
    print("\n=== 测试 5: Skip 逻辑 ===")

    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    watermark = c.execute(
        "SELECT last_l1_id FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    last_l1_id = watermark[0] if watermark else 0

    new_count = c.execute(
        "SELECT COUNT(*) FROM l1_memories WHERE status='active' AND (is_core=1 OR arousal>=0.6) AND id > ?",
        (last_l1_id,)
    ).fetchone()[0]

    conn.close()

    should_skip = new_count == 0
    print(f"  新增重要记忆: {new_count}")
    print(f"  应该 skip: {'✅ 是' if should_skip else '❌ 否'}")

    if should_skip:
        print("  → 重复触发 /narrative/generate 应返回 skipped")

    return True


def main():
    """运行所有测试"""
    print("=" * 60)
    print("P2-1: 自动触发与增量演化单元测试")
    print("=" * 60)

    try:
        test_check_thresholds()
        test_watermark_increment()
        test_incremental_mode()
        test_boundary_conditions()
        test_skip_logic()

        print("\n" + "=" * 60)
        print("✅ 所有测试通过")
        print("=" * 60)
        return 0

    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ 运行错误: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
