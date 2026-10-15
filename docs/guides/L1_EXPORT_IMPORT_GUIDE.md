# L1 记忆导出导入功能 - 使用指南

## ✅ 已完成

### Git 提交
- **Commit**: `a19d666`
- **改动**: 2 文件，+386 行，-4 行
- **origin (私有)** - ✓ 已推送
- **public (公开)** - ✓ 已推送

### 服务状态
- ✅ Memory Service 已重启运行正常

---

## 🎯 功能概述

### 修复：feel 导出导入
**问题**：原来只保留 11 个字段，和弦情绪锚点（anchor_json）会丢失  
**修复**：现在保留完整 16 个字段，包括：
- ✅ anchor_json（和弦情绪锚点）
- ✅ display_count, last_shown_at, visibility, supersedes

### 新增：L1 记忆导出导入

#### 完整版（推荐）
- 导出 L1 + L0 上下文
- 导入时智能重映射 source_msg_id
- 保持记忆与原文的关联

#### 简化版
- 只导出 L1 内容
- 不保留 L0 关联（source_msg_id=NULL）
- 适合跨系统迁移

---

## 📖 API 使用指南

### 1. 导出 L1 记忆

#### 完整版导出（带 L0 上下文）
```bash
# 导出所有 L1
curl http://localhost:8001/export/l1?mode=full > l1_full.json

# 只导出核心记忆
curl "http://localhost:8001/export/l1?mode=full&is_core=1" > l1_core.json

# 只导出 feel 类型
curl "http://localhost:8001/export/l1?mode=full&event_type=feel" > l1_feel.json
```

**返回格式**：
```json
{
  "status": "ok",
  "format": "l1-with-context",
  "version": 1,
  "exported_at": "2026-09-04T15:00:00",
  "l1_memories": [
    {
      "id": 1,
      "content": "用户喜欢早上写代码",
      "source_msg_id": 150,
      "anchor_json": "{...}",
      ...
    }
  ],
  "l0_context": {
    "150": {
      "anchor_msg_idx": 149,
      "conv_id": "xxx",
      "context": [...]  // 前后各2条 L0 消息
    }
  }
}
```

#### 简化版导出（只 L1）
```bash
# 导出所有 L1（不保留 L0 关联）
curl http://localhost:8001/export/l1?mode=simple > l1_simple.json

# 只导出核心记忆
curl "http://localhost:8001/export/l1?mode=simple&is_core=1" > l1_core_simple.json
```

**返回格式**：
```json
{
  "status": "ok",
  "format": "l1-simple",
  "memories": [
    {
      "id": 1,
      "content": "用户喜欢早上写代码",
      "source_msg_id": 150,
      "anchor_json": "{...}",
      ...
    }
  ]
}
```

### 2. 导入 L1 记忆

#### 完整版导入（自动重映射）
```bash
# 从文件导入
curl -X POST http://localhost:8001/import/l1 \
  -H "Content-Type: application/json" \
  -d @l1_full.json
```

**返回统计**：
```json
{
  "status": "ok",
  "imported": 10,   // 成功导入数量
  "remapped": 8,    // 成功重映射 source_msg_id
  "orphaned": 2,    // 找不到对应 L0（变成孤立 L1）
  "skipped": 5      // 因重复跳过
}
```

**重映射逻辑**：
- 按 conv_id + msg_idx(±5) + content 匹配
- 找到对应的 L0 → 更新 source_msg_id
- 找不到 → source_msg_id=NULL（不影响导入）

#### 简化版导入
```bash
# 从文件导入
curl -X POST http://localhost:8001/import/l1 \
  -H "Content-Type: application/json" \
  -d @l1_simple.json
```

**返回统计**：
```json
{
  "status": "ok",
  "imported": 10
}
```

---

## 🎬 使用场景

### 场景 1：重新导入对话后保留 L1（你的需求）

**正确流程**（针对将来使用）：

```bash
# 1. 清空记忆库 + 重新导入 Kelivo 备份
#    （L0 顺序正确，msg_idx 正确）

# 2. 重新提取 L1
POST /extract/bulk
# （L1 与 L0 关联正确）

# 3. 现在可以导出 L1 备份（为将来保留）
curl "http://localhost:8001/export/l1?mode=full" > l1_backup.json

# 4. 将来如果再需要重新导入对话
#    a. 导出 L1
curl "http://localhost:8001/export/l1?mode=full" > l1_before_reimport.json

#    b. 清空 + 重新导入对话

#    c. 导入 L1（自动重映射）
curl -X POST http://localhost:8001/import/l1 \
  -H "Content-Type: application/json" \
  -d @l1_before_reimport.json
```

**为什么现在不用？**
- 当前 L0 顺序已错乱（bug4 问题）
- 即使保留映射也是错的关联
- 建议：先重新导入对话 → 重新提取 L1 → 之后再用导出导入功能

---

### 场景 2：只保留核心记忆 + feel

```bash
# 1. 导出核心 L1 和 feel
curl "http://localhost:8001/export/l1?mode=full&is_core=1" > l1_core.json
curl "http://localhost:8001/export/feel" > feel.json

# 2. 清空 + 重新导入对话

# 3. 导入核心 L1（自动重映射）
curl -X POST http://localhost:8001/import/l1 \
  -H "Content-Type: application/json" \
  -d @l1_core.json

# 4. 导入 feel（现在支持 anchor_json）
curl -X POST http://localhost:8001/import/feel \
  -H "Content-Type: application/json" \
  -d '{"items": [...]}' # 从 feel.json 提取

# 5. 重新提取其他 L1
POST /extract/bulk?skip_existing=true
```

---

### 场景 3：跨系统迁移记忆

```bash
# 在旧系统导出（简化版，不依赖 L0）
curl "http://localhost:8001/export/l1?mode=simple" > l1_migrate.json

# 在新系统导入
curl -X POST http://new-system:8001/import/l1 \
  -H "Content-Type: application/json" \
  -d @l1_migrate.json
```

---

## 🔍 筛选参数

### event_type 筛选
```bash
# 只导出特定类型
curl "http://localhost:8001/export/l1?event_type=preference_change"
curl "http://localhost:8001/export/l1?event_type=relationship"
curl "http://localhost:8001/export/l1?event_type=feel"
```

可用类型：
- `preference_change` - 偏好变化
- `fact` - 事实
- `event` - 事件
- `plan` - 计划
- `relationship` - 关系
- `feel` - 感受
- `general` - 一般

### is_core 筛选
```bash
# 只导出核心记忆
curl "http://localhost:8001/export/l1?is_core=1"

# 只导出非核心记忆
curl "http://localhost:8001/export/l1?is_core=0"
```

### 组合筛选
```bash
# 核心 + feel
curl "http://localhost:8001/export/l1?is_core=1&event_type=feel"

# 核心 + 完整版
curl "http://localhost:8001/export/l1?is_core=1&mode=full"
```

---

## 📊 重映射成功率

### 影响因素

**成功重映射的条件**：
1. ✅ conv_id 相同
2. ✅ msg_idx 在 ±5 范围内
3. ✅ content 完全一致（忽略换行）

**可能失败的情况**：
- ❌ 消息被编辑过（内容变化）
- ❌ msg_idx 偏移超过 5
- ❌ 消息被删除

**预期成功率**：
- 未编辑的消息：~95%+
- 编辑过的消息：~0%
- 总体：~80-90%

### 失败时的处理

- 找不到对应 L0 → source_msg_id=NULL（变成孤立 L1）
- 孤立 L1 不影响导入，只是无法回溯原文
- `get_l0_context()` 会返回空（无上下文）

---

## 💡 最佳实践

### 1. 定期备份 L1
```bash
# 每周导出一次（完整版）
curl "http://localhost:8001/export/l1?mode=full" > "l1_backup_$(date +%Y%m%d).json"
```

### 2. 重新导入对话前导出
```bash
# 导出所有 L1（防止丢失）
curl "http://localhost:8001/export/l1?mode=full" > l1_before_reimport.json

# 导出 feel（额外保险）
curl "http://localhost:8001/export/feel" > feel_backup.json
```

### 3. 导入后检查统计
```bash
# 查看重映射结果
{
  "imported": 198,  // 总共导入了多少
  "remapped": 180,  // 成功重映射了多少（90%+）
  "orphaned": 18,   // 变成孤立 L1 了多少
  "skipped": 0      // 因重复跳过了多少
}
```

如果 `orphaned` 比例过高（>20%），可能需要检查：
- L0 是否正确导入
- msg_idx 是否正确
- content 是否被修改

---

## 🐛 故障排查

### 问题 1：导入时 remapped=0（全是 orphaned）

**可能原因**：
- L0 数据库为空
- conv_id 不匹配
- msg_idx 全部错位

**解决**：
- 先确认 L0 已正确导入
- 检查 conv_id 是否一致
- 使用简化版（放弃关联）

### 问题 2：导入后 skipped 很多

**原因**：按 content + event_type 去重，已有相同记忆

**解决**：
- 正常现象，不需要处理
- 如果想强制覆盖，先删除旧记忆

### 问题 3：anchor_json 丢失

**原因**：使用旧版 feel 导入

**解决**：
- 确认已重启服务（应用新代码）
- 重新导出 + 导入

---

## 📚 相关文档

- `feel_export_import_analysis.md` - 完整技术分析
- `l1_preservation_analysis.md` - L1 保留方案分析

---

**功能完成时间**: 2026-09-04 15:56  
**实施人员**: Claude (Opus 4.8)  
**状态**: ✅ 已部署，可以使用

---

## 🚀 下一步（针对你的需求）

### 推荐流程

1. **现在不用导出** （当前 L0 顺序错乱，导出也是错的）

2. **清空记忆库**
```sql
DELETE FROM l0_messages;
DELETE FROM l1_memories;
DELETE FROM thinking_records;
```

3. **重新导入 Kelivo 备份**
```bash
# Dashboard 或 API
POST /import/kelivo-backup
```

4. **重新提取 L1**
```bash
POST /extract/bulk
```

5. **之后就可以使用 L1 导出导入了**
```bash
# 定期备份
curl "http://localhost:8001/export/l1?mode=full" > l1_backup.json

# 将来需要时导入
curl -X POST http://localhost:8001/import/l1 \
  -d @l1_backup.json
```

---

**准备好重新导入对话了吗？** 🎯
