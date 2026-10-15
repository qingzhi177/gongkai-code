# L1 记忆保留方案分析

## 📊 当前数据状态

### 数据库概况
- **L0 消息总数**: 1,601 条 (status='active')
- **L1 记忆总数**: 198 条 (status='active')
- **会话数量**: 1 个 (conv_id: `0a3b59be-991c-4e65-abce-58e27868600b`)

### L0 消息特征
- **id 范围**: 4 ~ 1916 (说明有删除/superseded)
- **msg_idx 范围**: 0 ~ 1617
- **来源**: 当前是通过 Gateway 实时存储（用数组下标当 msg_idx，有 bug）

### L1 记忆特征
- **有 source_msg_id**: 192 条 (97%)
- **无 source_msg_id**: 6 条 (3%)
- **关联有效性**: 192 条中只有 **26 条**能匹配到当前 active 的 L0 消息
- **关联失效**: **166 条** L1 记忆的 source_msg_id 指向已不存在或 superseded 的 L0

### 💥 核心问题
**L1 记忆的 source_msg_id 是 L0 的主键 `id`，不是 `msg_idx`！**

重新导入 Kelivo 备份时：
1. 旧的 L0 消息会被删除或标记为 superseded
2. 新导入的 L0 消息会获得**全新的 id**（从当前最大 id 继续递增）
3. 现有 L1 的 source_msg_id 会**全部失效**（指向不存在的 id）

---

## 🎯 你的需求分析

### 你想要的
1. ✅ **重新导入 Kelivo 备份** → 修复 L0 顺序混乱（msg_idx 正确）
2. ✅ **保留 feel 文件** → 已支持（导入时可选保留）
3. ❓ **保留其他 L1 记忆** → 核心难点

### 为什么保留 L1 很困难

#### 问题 1：L1 的 source_msg_id 会失效
```sql
-- 现有 L1 记忆
id=1, content="用户喜欢早上写代码", source_msg_id=150

-- 现有 L0 消息
id=150, msg_idx=149, content="我喜欢早上写代码"

-- 重新导入后
-- 旧 L0 (id=150) 被删除或 superseded
-- 新 L0 (id=2000, msg_idx=149, content="我喜欢早上写代码")  ← 全新的 id！

-- L1 记忆的 source_msg_id=150 失效 ❌
```

#### 问题 2：内容匹配不可靠
即使通过内容匹配（`content LIKE '%我喜欢早上写代码%'`）重新关联，也会有问题：
- 编辑过的消息内容已变化，匹配不上
- 相似内容可能匹配到错误的消息
- 性能问题（198条L1 × 1601条L0 = 316,998 次比对）

#### 问题 3：msg_idx 也不稳定
旧数据的 msg_idx 是错的（bug4 问题），不能用来做映射。

---

## 🛠️ 解决方案

### 方案 A：智能重映射（推荐，80%+ 成功率）

**核心思路**：导入时建立"旧id → 新id"映射表，批量更新 L1 的 source_msg_id

#### 实现步骤

1. **导入前备份 L1**
```python
# 导出当前 L1 记忆到 JSON
SELECT id, content, quote, source_msg_id, conv_id, event_type, tags, 
       valence, arousal, is_core, access_count, ts, anchor_json,
       display_count, last_shown_at, visibility, supersedes
FROM l1_memories 
WHERE status='active'
```

2. **导入时记录映射关系**
```python
# kelivo_backup_importer.py 修改
old_to_new_id_map = {}  # {旧L0.id: 新L0.id}

# 在 _import_conv() 中插入 L0 消息后
for idx, m in enumerate(messages):
    # ... 现有逻辑 ...
    cur = mem_conn.execute("INSERT INTO l0_messages ...")
    new_id = cur.lastrowid
    
    # 尝试找到旧的对应消息
    old_msg = mem_conn.execute(
        "SELECT id FROM l0_messages_backup 
         WHERE conv_id=? AND msg_idx=? AND role=? 
         AND REPLACE(content, '\n', '') = REPLACE(?, '\n', '')",
        (cid, idx, m['role'], m['content'])
    ).fetchone()
    
    if old_msg:
        old_to_new_id_map[old_msg[0]] = new_id
```

3. **导入后批量更新 L1**
```python
for old_id, new_id in old_to_new_id_map.items():
    mem_conn.execute(
        "UPDATE l1_memories SET source_msg_id=? WHERE source_msg_id=?",
        (new_id, old_id)
    )
```

#### 优点
- 自动化程度高
- 保留大部分 L1 记忆（能匹配上的）
- 无需手动操作

#### 缺点
- 编辑过的消息可能匹配不上（~20%）
- 需要修改导入器代码
- 匹配失败的 L1 会变成"孤儿"（source_msg_id 指向不存在的 id）

---

### 方案 B：导出/清理/重导/重提取（最干净，但工作量大）

#### 步骤

1. **导出 feel 类型的 L1**
```bash
python scripts/export_l1_feel.py > backup_feel.json
```

2. **清空整个记忆库**
```sql
DELETE FROM l0_messages;
DELETE FROM l1_memories;
DELETE FROM thinking_records;
-- 重置自增 id
DELETE FROM sqlite_sequence WHERE name IN ('l0_messages', 'l1_memories');
```

3. **重新导入 Kelivo 备份**
```bash
# L0 顺序正确，id 从 1 开始
```

4. **重新提取所有 L1**
```bash
# 让 AI 重新分析对话，提取记忆
POST /extract/bulk
```

5. **恢复 feel 类型的 L1**
```bash
# 从 backup_feel.json 导入，匹配新的 source_msg_id
```

#### 优点
- 最干净，完全解决 id 混乱问题
- L0 和 L1 完全重新对齐
- 无遗留问题

#### 缺点
- **工作量巨大**：重新提取 1600+ 条消息的 L1 记忆
- **API 成本高**：可能需要几百次 LLM 调用
- **时间长**：可能需要几小时
- **内容可能变化**：AI 重新提取的记忆可能跟之前不完全一样

---

### 方案 C：保留核心 L1，其他重提取（折中方案）

#### 步骤

1. **导出核心 L1** (is_core=1 或 event_type='feel')
```sql
SELECT * FROM l1_memories 
WHERE status='active' AND (is_core=1 OR event_type='feel')
-- 假设有 ~30 条核心记忆
```

2. **重新导入 Kelivo 备份**（会话 id 相同）

3. **手动重映射核心 L1**
```python
# 对于每条核心 L1：
# - 用 quote 或 content 关键词在新 L0 中搜索
# - 找到最匹配的新 L0 消息
# - 更新 source_msg_id
```

4. **其他 L1 用自动提取补充**
```bash
POST /extract/bulk?skip_existing=true
# 只提取那些还没有 L1 记忆的 L0 区间
```

#### 优点
- 保留最重要的记忆（feel、核心标记）
- 工作量适中（只手动处理 ~30 条）
- API 成本可控

#### 缺点
- 核心 L1 需要手动重映射（但数量少）
- 非核心 L1 会丢失（重新提取的可能不同）

---

### 方案 D：不保留 L1，只保留 feel 文件（最简单）

#### 步骤

1. **确认 feel 文件已备份**
```bash
ls -lh data/profile/feel.json
```

2. **清空记忆库**
```sql
DELETE FROM l0_messages;
DELETE FROM l1_memories;
```

3. **重新导入 Kelivo 备份**

4. **重新导入 feel**
```bash
# Dashboard 导入 feel.json
# 或者导入时指定 --keep-feel
```

5. **重新提取所有 L1**
```bash
POST /extract/bulk
```

#### 优点
- 最简单，无需复杂映射
- 数据最干净
- feel（情感记录）不会丢失

#### 缺点
- 所有非 feel 类型的 L1 记忆会丢失
- 重新提取的 L1 可能跟之前不完全一样

---

## 🤔 方案对比

| 方案 | L1保留率 | 工作量 | API成本 | 风险 | 推荐度 |
|------|---------|--------|---------|------|--------|
| A. 智能重映射 | ~80% | 中 | 低 | 中（映射失败） | ⭐⭐⭐⭐ |
| B. 完全重提取 | 0%→100%* | 极高 | 极高 | 低 | ⭐⭐ |
| C. 核心保留 | 15%手动+70%重提取 | 中高 | 中 | 低 | ⭐⭐⭐ |
| D. 只保留feel | feel保留 | 低 | 高 | 低 | ⭐⭐⭐⭐⭐ |

*重提取的内容可能跟原来不同

---

## 💡 我的建议

### 如果你的 L1 记忆很重要（比如包含很多手动标注的核心记忆）
→ **选择方案 A（智能重映射）**
- 我可以帮你写脚本实现自动映射
- 大部分 L1 能保留
- 失败的可以手动补救

### 如果你主要关心 feel，其他 L1 可以重新提取
→ **选择方案 D（最简单）**
- 导入时保留 feel
- 其他 L1 让 AI 重新提取
- 数据最干净

### 如果你想兼顾效率和质量
→ **选择方案 C（核心保留）**
- 手动保留 is_core=1 的记忆
- 自动重提取其他记忆
- 折中方案

---

## 🔧 关于会话 ID

### Q: Kelivo 同一个窗口导入，会话 id 一样吗？
**A: 是的！** 

Kelivo 备份中的 `conversation_rows.id` 就是会话的唯一标识。你当前的会话 id 是：
```
0a3b59be-991c-4e65-abce-58e27868600b
```

重新导入这个会话时：
- ✅ `conv_id` 保持不变
- ❌ L0 消息的 `id` 会全部变化（主键自增）
- ✅ L0 消息的 `msg_idx` 会正确（按真实顺序 0,1,2,3...）

---

## 📝 下一步

### 请告诉我
1. **你更倾向哪个方案？** (A/B/C/D)
2. **你的 L1 记忆中，哪些是最重要的？**
   - 手动标注的核心记忆？
   - feel 类型？
   - 特定 event_type？
3. **你能接受的工作量和 API 成本？**

根据你的选择，我可以：
- 写自动化脚本
- 修改导入器支持映射
- 给出详细的操作步骤

---

**分析完成时间**: 2026-09-04
**当前状态**: 等待用户选择方案
