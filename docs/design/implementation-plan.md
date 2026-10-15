# Shared Narrative & Dashboard 扩展实现方案

## 一、数据库设计

### 1. Shared Narrative 表
```sql
CREATE TABLE shared_narrative (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,              -- 叙事内容（markdown格式）
    version INTEGER DEFAULT 1,           -- 版本号
    trigger_type TEXT,                   -- 触发类型：manual/auto_l1/auto_threshold
    trigger_details TEXT,                -- 触发详情（JSON）
    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active'         -- active/superseded
)
```

### 2. Recent Summary 表
```sql
CREATE TABLE recent_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,              -- 摘要内容
    period_start DATETIME,              -- 时间段起始
    period_end DATETIME,                -- 时间段结束
    msg_count INTEGER,                  -- 覆盖消息数
    token_count INTEGER,                -- Token数（估算）
    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active'
)
```

### 3. Narrative Config 表（更新检查策略配置）
```sql
CREATE TABLE narrative_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- 单行配置
    auto_update_enabled INTEGER DEFAULT 1,   -- 是否启用自动更新
    check_threshold_turns INTEGER DEFAULT 50, -- 对话轮数阈值
    check_threshold_l1 INTEGER DEFAULT 10,    -- 高重要性L1累计阈值
    summary_max_turns INTEGER DEFAULT 100,    -- 摘要最大轮数
    summary_max_tokens INTEGER DEFAULT 50000  -- 摘要最大Token
)
```

### 4. 扩展 model_configs 表（支持不同模块独立配置）
```sql
-- 已有 providers 表，新增用途字段的配置关联表
CREATE TABLE model_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT UNIQUE NOT NULL,        -- chat/l1_extract/embedding/narrative/summary
    provider_id INTEGER,                 -- 关联 providers.id
    model_name TEXT,                     -- 具体模型名
    enabled INTEGER DEFAULT 1,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP
)
```

## 二、API 端点设计

### Shared Narrative API

#### GET /narrative/current
获取当前活跃的 Shared Narrative
- 返回：`{version, content, ts, trigger_type}`

#### POST /narrative/generate
手动触发生成 Shared Narrative
- 请求体：`{trigger_type: "manual", force: true}`
- 逻辑：
  1. 读取现有 narrative
  2. 获取最近的重要 L1 记忆
  3. 调用 LLM 生成新 narrative
  4. 旧版本标记为 superseded
  5. 保存新版本
- 返回：新生成的 narrative

#### GET /narrative/history
获取历史版本
- 参数：`?limit=10`
- 返回：版本列表

#### PUT /narrative/{id}
编辑 Narrative 内容
- 请求体：`{content: "..."}`

### Recent Summary API

#### GET /summary/current
获取当前 Recent Summary

#### POST /summary/generate
手动触发生成摘要
- 逻辑：基于最近 N 轮对话生成摘要

#### GET /summary/config
获取摘要配置

#### PUT /summary/config
更新摘要配置
- 请求体：`{max_turns, max_tokens}`

### L1 Extraction Status API

#### GET /l1/extraction_status
获取 L1 提取状态
- 返回：
```json
{
  "total_messages": 12500,
  "extracted_messages": 11200,
  "pending_messages": 1300,
  "batches": [
    {"time_range": "2026-08-01", "msg_range": "12000-12500", "status": "extracted"},
    {"time_range": "2026-08-12", "msg_range": "12501-13000", "status": "pending"}
  ]
}
```

#### POST /l1/extract_now
手动触发 L1 提取
- 请求体：`{batch_id: optional}`
- 调用现有 extract_l1.py 脚本

### Pipeline Status API

#### GET /pipeline/status
获取整个记忆管线状态
- 返回：
```json
{
  "l0": {"total": 12500, "percent": 100},
  "l1": {"extracted": 10000, "pending": 2500, "percent": 80},
  "embedding": {"count": 10000, "last_update": "2026-08-13 12:00"},
  "narrative": {"version": 3, "last_update": "2026-08-10", "pending": false}
}
```

### Model Config API (扩展现有)

#### GET /config/models
获取所有模块的模型配置
- 返回：
```json
{
  "chat": {"provider": "中转站A", "model": "claude-opus-4-6"},
  "l1_extract": {"provider": "中转站B", "model": "deepseek-chat"},
  "embedding": {"provider": "阿里云", "model": "text-embedding-v3"},
  "narrative": {"provider": "中转站A", "model": "claude-sonnet-4-5"},
  "summary": {"provider": "中转站A", "model": "claude-sonnet-4-5"}
}
```

#### PUT /config/models/{purpose}
更新特定模块的模型配置
- purpose: chat/l1_extract/embedding/narrative/summary
- 请求体：`{provider_id, model_name}`

## 三、Dashboard 页面设计

### 新增 Tab：
1. **Memory Extraction**（记忆提取）
2. **Pipeline Status**（管线状态）
3. **Shared Narrative**（共同经历）
4. **Recent Summary**（近期摘要）
5. **Model Config**（扩展现有配置页面）

### Memory Extraction 页面布局
```
┌─ 提取状态统计 ─┐
│ 总消息: 12500  │
│ 已提取: 11200  │
│ 待处理: 1300   │
└────────────────┘

┌─ 批次列表 ─────────────────────┐
│ 时间       消息范围      状态   │
│ 08-01  12000-12500  [已提取]   │
│ 08-12  12501-13000  [待提取] [立即提取]│
└─────────────────────────────────┘
```

### Pipeline Status 页面（月相进度条）
使用 Canvas 绘制月相样式的进度指示器：
- L0: 满月（100%）
- L1: 渐亏月（80%）
- Embedding: 满月（100%）
- Narrative: 上弦月（60%）

### Shared Narrative 页面
```
┌─ 当前版本 (v3) ─────────────┐
│ 最后更新: 2026-08-10 15:30  │
│ 触发方式: 手动触发           │
│ [查看历史版本] [立即更新] [编辑]│
└──────────────────────────────┘

┌─ 叙事内容 ─────────────────┐
│ ### 2026年5月20日-5月22日  │
│ 我们的对话始于...          │
│ ...                       │
└──────────────────────────────┘
```

### Model Config 扩展
在现有配置页面新增"模块配置"部分：
```
┌─ 主聊天模型 ──────────┐
│ 供应商: [中转站A ▼]   │
│ 模型: [claude-opus-4-6 ▼]│
└──────────────────────┘

┌─ L1 记忆提取 ─────────┐
│ 供应商: [中转站B ▼]   │
│ 模型: [deepseek-chat ▼]│
└──────────────────────┘

┌─ 向量嵌入 ───────────┐
│ 供应商: [阿里云 ▼]    │
│ 模型: [text-embedding-v3]│
└──────────────────────┘

┌─ 共同经历叙事 ────────┐
│ 供应商: [中转站A ▼]   │
│ 模型: [claude-sonnet-4-5 ▼]│
└──────────────────────┘

┌─ 近期摘要 ───────────┐
│ 供应商: [中转站A ▼]   │
│ 模型: [claude-sonnet-4-5 ▼]│
└──────────────────────┘
```

## 四、实现顺序

### 第一步：数据库 Schema（不破坏现有数据）
- 添加新表
- 添加迁移逻辑

### 第二步：Shared Narrative 基础功能
- API 实现
- 生成逻辑（调用 LLM）
- 测试生成

### 第三步：Recent Summary 功能
- API 实现
- 滚动更新逻辑

### 第四步：Model Config 扩展
- 扩展后端 API
- Dashboard UI

### 第五步：Pipeline Status
- 状态汇总 API
- 月相进度条 UI

### 第六步：Memory Extraction
- 提取状态 API
- Dashboard UI

### 第七步：整合测试
- 端到端测试
- 日志优化

## 五、注意事项

1. **API Key 安全**：使用现有的 Fernet 加密方式
2. **热重载**：模型配置变更后通知网关重新加载
3. **向后兼容**：不破坏现有 API
4. **增量实现**：每个功能完成后 commit + push
5. **日志**：关键操作添加日志便于调试
