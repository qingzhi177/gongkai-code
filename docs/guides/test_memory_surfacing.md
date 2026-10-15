# 记忆浮现机制测试报告

## 已完成的改动

### Memory Service (main.py)

1. **数据库字段迁移**（已自动执行）：
   ```
   ✓ display_count INTEGER DEFAULT 0     - 展示计数
   ✓ last_shown_at INTEGER                - 最后展示时间戳（毫秒）
   ✓ visibility REAL DEFAULT 1.0          - 可见度（0.0-1.0）
   ✓ supersedes INTEGER                   - 修正关系（指向被修正的旧 feel id）
   ```

2. **删除自动计数逻辑**：
   - `search_memories()` 不再自动更新 `access_count`
   - 改由 Gateway 在 recall 工具调用时显式更新

3. **新增 6 个接口**：
   - `POST /memories/increment_access_count` - recall 工具调用计数
   - `POST /memories/increment_display_count` - 展示计数
   - `POST /memories/update_last_shown` - 更新最后展示时间
   - `PUT /l1/{l1_id}/visibility` - 更新可见度（软隐藏）
   - `POST /search_serendipity` - 无由来浮现查询

### Gateway (server.js)

1. **配置参数**（文件顶部）：
   ```javascript
   SURFACE_PROBABILITY = 0.3          // 30% 概率开放回忆
   MIN_RELEVANCE = 0.15               // RRF 分数阈值
   COOLDOWN_HOURS = 24                // 24 小时冷却
   SERENDIPITY_PROBABILITY = 0.05     // 5% 概率无由来浮现
   SERENDIPITY_COOLDOWN_DAYS = 7      // 7 天冷却
   SHOW_COUNT_WEIGHTS                 // 0/1/2 条的概率分布
   ```

2. **recall 工具描述更新**：
   ```
   旧："搜索记忆库中的详细信息。当目录里的摘要不够用、需要看原文细节时使用。"
   新："当你隐约觉得某个话题以前聊过、或想确认某个细节时，用这个工具主动回忆。
       不需要确定记忆一定存在才搜——凭感觉去找也可以。目录里没显示的，不代表记忆库里没有。"
   ```

3. **systemPrefix 新增引导文案**：
   ```
   记忆系统偶尔会浮现一些过去的片段——这些不是你必须处理的信息，而是偶然想起的痕迹。
   这是我们曾经共同经历过的一点痕迹。它们可能与当前话题有关，也可能无关。
   不要刻意复述它们，也不要让它们限制你对当下的理解。只有在它们真的相关、真的有助于理解当前交流时，才自然地用上。
   记忆片段只是部分提醒，不代表所有相关记忆都已列出。如果你觉得某个话题以前聊过但没看到相关片段，可以用 recall 去找。
   ```

4. **完全重写 getMemoryMenu()**：
   - 随机门控（30% 概率）
   - 相关度过滤（score >= 0.15）
   - 冷却过滤（24 小时）
   - 加权随机抽取（0-2 条）
   - 无由来浮现通道（5% 概率，独立）
   - 自然语言格式（括号包裹 + 模糊时间）

5. **新增 6 个辅助函数**：
   - `relevanceDrivenSurfacing()` - 主通道
   - `serendipitousSurfacing()` - 并行通道
   - `weightedRandomChoice()` - 加权随机选择
   - `weightedSample()` - 加权采样
   - `formatMemoriesAsThoughts()` - 格式化为念头
   - `toFuzzyTime()` - 时间模糊化
   - `updateDisplayMetrics()` - 更新展示指标

6. **呈现格式变化**：
   ```
   旧格式（结构化清单）：
   [相关记忆目录]
   - [2024-08-15 preference_change] 用户喜欢在早上写代码
   ⭐ [2024-07-20 relationship] 我们聊过关于深度学习的话题
   
   新格式（自然语言念头）：
   （似乎在某个夏天，你说过更喜欢早上写代码来着）
   
   （⭐ 去年夏天，我们聊过一次关于深度学习的事——那次聊得挺深入的）
   ```

7. **时间粒度映射**：
   - 0-1 天 → "今天早些时候"
   - 1-3 天 → "前两天"
   - 3-7 天 → "最近"
   - 7-14 天 → "前阵子"
   - 14-30 天 → "不久之前"
   - 30-365 天 → "春天/夏天/秋天/冬天"（按月份映射到 12 个季节词）
   - 365+ 天 → "去年春天" / "2年前的夏天"

8. **recall 工具执行增强**：
   - 成功检索后调用 `/memories/increment_access_count`
   - 区分"显式调用"和"被动展示"

## 设计原则验证

### ✓ 符合《记忆理念工程化》
1. recall 语义从"二级查询"变为"意图驱动的联想"
2. 引导文案明确"目录不是全部"
3. 新增 visibility 字段为 Forget 机制预留空间
4. access_count 和 display_count 分离统计
5. systemPrefix 引导文案进入 prompt cache

### ✓ 符合《记忆呈现与随机触发》
1. 随机门控实现"偶尔浮现"
2. 冷却机制避免短期重复
3. 无由来浮现通道独立并行
4. 格式去结构化、去表格感
5. 引导文案不预设关系（"共同经历的痕迹" vs "你的朋友说过"）

## 向后兼容性

✓ 新字段有默认值，旧数据自动兼容
✓ 可以通过调整概率参数渐进式生效（先设 SURFACE_PROBABILITY=1.0 保持旧行为）
✓ 删除旧计数逻辑不影响现有数据，只影响新的统计口径

## 测试清单

### 基础功能测试
- [ ] Memory Service 启动成功
- [ ] Gateway 启动成功
- [ ] 数据库新字段存在（display_count, last_shown_at, visibility, supersedes）
- [ ] 旧数据读取正常

### 记忆浮现测试
- [ ] 记忆浮现概率约 30%（统计 50 轮对话）
- [ ] 相关度低的记忆被过滤
- [ ] 展示条数符合分布（0:20%, 1:60%, 2:20%）
- [ ] 冷却机制生效（同一条记忆 24 小时内不重复）
- [ ] 无由来浮现约 5% 触发（独立于主通道）
- [ ] 高情感强度记忆更容易无由来浮现

### 格式化测试
- [ ] 时间模糊化正确（不同日期范围）
- [ ] 自然语言格式无字段标签
- [ ] 括号包裹营造"念头"感
- [ ] 核心记忆 ⭐ 保留

### 工具调用测试
- [ ] recall 工具成功后 access_count +1
- [ ] 展示记忆后 display_count +1
- [ ] last_shown_at 更新为最新时间戳

### Prompt Caching 测试
- [ ] systemPrefix 变化后缓存更新
- [ ] 随机逻辑不影响缓存命中
- [ ] Cache hit metrics 正常

## 性能影响评估

**候选池扩大**：8 条 → 20 条（检索开销 +150%）
**展示率下降**：100% → 30%（总体调用量 -70%）
**综合影响**：预计总体性能略有提升（展示率降低的收益大于候选池扩大的开销）

## 后续优化方向

1. **可配置化**：如果需要频繁调整概率参数，可以做成 Dashboard 配置项
2. **自动衰减**：基于 display_count 和时间自动调整 visibility
3. **Feel 修正机制**：补全 supersedes 的前端交互（"修正之前的感受"）
4. **A/B 测试**：对比新旧格式的 LLM 行为差异

## 启动命令

```bash
# Memory Service
cd /home/qingzhi/memory-system/services/memory-service
source venv/bin/activate
python main.py

# Gateway
cd /home/qingzhi/memory-system/services/gateway
npm start
```

## 检查数据库迁移

```bash
sqlite3 /home/qingzhi/memory-system/data/sqlite/memory.db "PRAGMA table_info(l1_memories);" | grep -E "display_count|last_shown_at|visibility|supersedes"
```

预期输出：
```
display_count|INTEGER|0|0|0
last_shown_at|INTEGER|0||0
visibility|REAL|0|1.0|0
supersedes|INTEGER|0||0
```
