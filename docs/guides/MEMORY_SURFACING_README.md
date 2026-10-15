# 记忆浮现机制 - 快速上手指南

## 🎯 核心变化

### 从"数据库查询"到"偶尔想起"

**旧版**：每轮固定展示 8 条结构化记忆清单  
**新版**：30% 概率浮现 0-2 条自然语言念头

## 🚀 启动服务

```bash
# 1. Memory Service（会自动执行数据库迁移）
cd services/memory-service
source venv/bin/activate
python main.py

# 2. Gateway
cd services/gateway
npm start
```

## 📊 核心参数（可调整）

在 `services/gateway/server.js` 顶部：

```javascript
const SURFACE_PROBABILITY = 0.3;          // 30% 概率开放回忆
const MIN_RELEVANCE = 0.15;               // 相关度阈值
const COOLDOWN_HOURS = 24;                // 24 小时冷却
const SERENDIPITY_PROBABILITY = 0.05;    // 5% 无由来浮现
const SHOW_COUNT_WEIGHTS = [
  { value: 0, weight: 0.2 },  // 20% 不显示
  { value: 1, weight: 0.6 },  // 60% 显示 1 条
  { value: 2, weight: 0.2 }   // 20% 显示 2 条
];
```

## 🔍 验证数据库迁移

```bash
sqlite3 data/sqlite/memory.db "PRAGMA table_info(l1_memories);" | grep -E "display_count|last_shown_at|visibility"
```

预期输出：
```
display_count|INTEGER|0|0|0
last_shown_at|INTEGER|0||0
visibility|REAL|0|1.0|0
```

## 📝 格式对比

### 旧格式（结构化清单）
```
[相关记忆目录]
- [2024-08-15 preference_change] 用户喜欢在早上写代码
⭐ [2024-07-20 relationship] 我们聊过关于深度学习的话题
```

### 新格式（自然语言念头）
```
（似乎在某个夏天，你说过更喜欢早上写代码来着）

（⭐ 去年夏天，我们聊过一次关于深度学习的事——那次聊得挺深入的）
```

## 🎛️ 渐进式部署

如果想先保持旧行为观察，可以临时调整参数：

```javascript
// 阶段 1：保持旧行为
const SURFACE_PROBABILITY = 1.0;    // 100% 展示
const MIN_RELEVANCE = 0.0;          // 无过滤

// 阶段 2：逐步降低
const SURFACE_PROBABILITY = 0.5;    // 50% 展示

// 阶段 3：最终目标
const SURFACE_PROBABILITY = 0.3;    // 30% 展示
```

## 🧪 测试 API

```bash
chmod +x test_new_apis.sh
./test_new_apis.sh
```

## 📚 详细文档

- `implementation_report.md` - 完整实施报告
- `test_memory_surfacing.md` - 测试清单

## 🔑 关键设计原则

> **这段代码最后会让 AI 呈现出什么样的性格？**

✅ 像人那样偶尔想起一件小事  
❌ 而不是总把过去翻出来摆在桌上

---

**实施日期**：2026-08-30  
**状态**：✅ 已完成，数据库迁移成功
