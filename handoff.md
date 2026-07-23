# 交接文件 handoff.md

> 写于 2026-07-23，记录 Memory System v1.3 当前状态与下一步待办。
> 本文件供新上下文窗口接手开发使用。

---

## 一、系统架构概览

```
用户(Kelivo前端) ──HTTPS──▶ nginx ──▶ 网关 Gateway (Node.js:3000)
                                          │
                    ┌─────────────────────┤
                    ▼                     ▼
          记忆服务 MemoryService     上游 LLM API
          (Python FastAPI:8001)    （中转站/Anthropic）
                    │
          ┌─────────┼─────────┐
          ▼         ▼         ▼
       SQLite   ChromaDB   L2 人格画像
       (L0+L1)  (向量库)    (MD文件)
                :8000
```

### 三层记忆

| 层 | 存储 | 内容 | 提取方式 |
|----|------|------|---------|
| L0 | SQLite `l0_messages` | 对话原文（带版本管理、status字段） | 每轮对话后异步保存 |
| L1 | SQLite `l1_memories` + ChromaDB | 记忆索引（BM25+向量RRF融合检索） | cron每10分钟 `extract_l1.py` |
| L2 | `/data/profile/about_*.md` | 人格画像（用户/AI/我们） | AI主动调 `update_profile` 工具 |

### 关键文件位置

```
services/
  gateway/server.js          # Node.js 网关（流式SSE、工具执行、缓存、路由）
  memory-service/main.py     # FastAPI 记忆服务（所有 /api 接口）
  memory-service/extract_l1.py  # L1提取脚本（cron驱动）
  dashboard/index.html       # Dashboard 前端（单文件静态页）
data/
  sqlite/memory.db           # L0+L1 数据
  profile/about_*.md         # L2 人格画像
```

---

## 二、已完成功能（第一、二阶段）

### 第一阶段（需求1）
| 功能 | 说明 |
|------|------|
| L0 存储 | 对话原文 SQLite，带版本管理（内容变化时旧行 status=superseded，新行插入） |
| L1 索引 | DeepSeek 提取，BM25+向量 RRF 融合检索，意图路由 |
| L2 画像 | about_user/about_ai/about_us，AI 主动调 update_profile 更新 |
| recall/feel/update_profile | AI 自主调用工具，网关内部执行 |
| Dashboard 基础版 | L0/L1/画像浏览、导入、统计 |
| 自动备份 | backup.sh → GitHub |

### 第二阶段（需求2）
| 功能编号 | 说明 |
|---------|------|
| 功能1 | 流式 SSE 输出（前端实时逐字显示） |
| 功能2 | 思维链透传（Anthropic thinking 块，前端折叠区渲染） |
| 功能3 | 工具调用前端可见（tool_use 卡片，**流式有，非流式跳过**） |
| 功能4 | Token usage 透传（流式/非流式，跨工具轮次累加） |
| 功能5 | Prompt caching（system 稳定前缀带 cache_control，动态 memoryMenu 注入 user 消息） |
| 功能6 | Dashboard 多供应商配置（CRUD + 热重载 + Fernet 加密 API Key） |
| 功能7 | 联网搜索 bocha_search（博查 API，工具格式 OpenAI→Anthropic 自动转换） |

---

## 三、需求3 完成进度

### ✅ 已完成

#### 修复1：L1 提取视角改为 AI 第一人称
- **文件**：`services/memory-service/extract_l1.py`
- **改动**：在 `EXTRACT_PROMPT` "核心规则"前插入视角说明段
- **效果**：新提取的 L1 记忆有明确主语（"她说…""我觉得…""我们一起…"）；旧记忆不变

#### bug1：流式工具调用 token 显示 0
- **文件**：`services/gateway/server.js`
- **根因**：网关发 `tool_use` 卡片给前端 → Kelivo 创建 loading 工具块 → 因为 Kelivo 没有该工具的 `onToolCall` 永不解除 → `hasLoadingTool` 恒真 → 阻断 token 归属 → 显示 0
- **三处修复**（详见下文技术细节）：
  1. `toolResolve`：卡片后补发 `web_search_tool_result` 解除 loading
  2. 惰性 `message_start`：延迟到拿到真实 input usage 再发
  3. 回环短路返回缓存的真实 usage（`rememberUsage/computeConvId`）

#### bug2：非流式工具调用透传 → **有意跳过**
- **原因**：Kelivo 非流式解析器遇到 `tool_use` 块且 `onToolCall != null` 时会进工具分支执行+`continue` 循环，但该分支**不 yield 最终文本**，导致正文丢失。用户基本只用流式，故跳过，非流式保持现状。

#### 修复2：feel 来源区分
- **文件**：`services/memory-service/main.py`、`services/dashboard/index.html`
- **发现问题**：需求说"已有 client=ai_self"，实际只有 ChromaDB metadata 有，SQLite 的 client 字段是 NULL
- **改动**：
  1. `save_feel` INSERT 补 `client='ai_self'`、`conv_id=''`
  2. 一次性迁移 39 条历史 AI 自述 feel 为 `client='ai_self'`
  3. Dashboard 加"感受·AI自述"/"感受·对话提取"子按钮（`loadFeel` 函数）

### ⏳ 待做（修复3~8）

| 修复 | 文件 | 说明 |
|------|------|------|
| 修复3 | `main.py` + `index.html` | `/l1/list` 的 `limit` 默认值 50→500/1000；Dashboard 前端 `loadL1` url 里 `limit=100`→`1000` |
| 修复4 | `main.py` + `index.html` | L1 内容手动编辑：Dashboard 加"编辑"按钮，`PUT /l1/{id}` 支持更新 `content`（同时重新生成 embedding 更新 ChromaDB） |
| 修复5 | `extract_l1.py` | L1 时间戳用 L0 原文时间：`save_l1` 里用 `source_msg_id` 查 L0 的 `ts`，fallback 到 `datetime.now()` |
| 修复6 | `main.py` | 删除对话联动清除 L1+向量：`DELETE /l0/conversation/{conv_id}` 加上标记 L1 superseded + ChromaDB 删向量 |
| 修复7 | `server.js` | L0 版本切换联动 L1：`save_conversation` diff 检测到版本切换（旧行 superseded）时，也标记旧 L0 对应的 L1 superseded、删 ChromaDB 向量、新 L0 的 extracted=0 |
| 修复8 | `main.py` | **和修复6几乎完全相同**，需求文档重复了。做完修复6即可，修复8 可直接打勾 ✅ |

---

## 四、技术细节

### 4.1 System Prompt 结构（功能5 Prompt Caching）

```js
// server.js ~line 800
const systemBlocks = [
  { type: 'text', text: systemPrefix, cache_control: { type: 'ephemeral' } }
];
// systemPrefix = 角色说明 + L2画像 + 工具说明（稳定，可缓存）
// memoryMenu（动态检索结果）注入到最新 user 消息前，不放 system
```

**注意**：system 前缀必须稳定才能命中缓存；动态内容（memoryMenu）只能注入 user 消息，否则每轮都会让缓存失效。

### 4.2 工具调用透传（功能3）

**流式（已完整实现）**：
```
上游返回 tool_use (stop=tool_use)
  → emitter.toolUseCard()   # 发 tool_use 块给前端（Kelivo 显示卡片，loading）
  → executeTool()           # 网关内部执行
  → emitter.toolResolve()   # 发 web_search_tool_result 块（解除 loading，让 token 正常显示）
  → 下一轮上游请求（带 tool_result）
  → 最终文本流回前端
  → emitter.finish() 发 message_delta(真实 usage) + message_stop
```

**非流式（跳过）**：只返回最终文本，不透传工具过程。原因见 bug2 说明。

### 4.3 Kelivo 回环处理

Kelivo 看到 `tool_use` 卡片后会再发一个 `tool_result` 请求（回环）：
- 网关通过 `isToolLoopback()` 检测（最后一条 user 消息全是 `tool_result` 块且无真实文本）
- 检测到后短路返回空 `end_turn`，**不再调用上游**，**不存记忆**
- `computeConvId(req)` 复用同一个 conv_id，`lastUsageByConv` 缓存真实 usage，回环时返回，避免 Kelivo 用 0 覆盖 token 显示

### 4.4 工具格式转换

所有工具（TOOLS 数组是 OpenAI 格式，Kelivo 发来的是 Anthropic 原生格式）：
```js
// toAnthropicTool() 统一转换为 Anthropic 原生格式
const allTools = [...(req.body.tools || []), ...TOOLS].map(toAnthropicTool);
```

### 4.5 多供应商配置热重载

- SQLite `providers` + `active_config` 表存配置，API Key 用 Fernet 加密
- Dashboard 保存后调 `/config/active` → 记忆服务通知网关 `POST /reload-config` → 网关更新 `activeConfig` 内存变量，无需重启

### 4.6 L1 提取流程

```
cron (每10分钟) → extract_l1.py
  → 查 l0_messages WHERE extracted=0 AND status='active' LIMIT 50
  → 按 conv_id 分组
  → 每组对话文本 → DeepSeek API 提取 → JSON 数组
  → save_l1(): 存 SQLite l1_memories + ChromaDB 向量
  → 标记 l0_messages.extracted=1
```

### 4.7 ChromaDB 向量 ID 规则

L1 向量 ID 格式：`l1_{sqlite_id}`，如 `l1_42`。删 ChromaDB 向量时必须用此格式。

---

## 五、下一步待办（修复3~8 详细说明）

### 修复3：L1 列表 limit 100→1000

**backend** `main.py:626`：
```python
# 将 limit: int = 50 改为 1000
async def get_l1_list(event_type: Optional[str] = None, limit: int = 1000, offset: int = 0):
```

**frontend** `index.html` `loadL1` 函数：
```js
// 将 'limit=100' 改为 'limit=1000'
var url = API + '/l1/list?limit=1000';
```

可同时在 Dashboard 加分页（前端 slice，每页 50 条），避免一次渲染太多 DOM 卡顿。

---

### 修复4：L1 内容手动编辑

**backend** `main.py`：
1. `UpdateL1Request` 模型加 `content: Optional[str] = None`
2. `PUT /l1/{memory_id}` 的 `update_l1` 函数：
   - `content` 有变化时：UPDATE SQLite content
   - 同时调 `get_embedding(new_content)` 重新生成向量
   - ChromaDB `update(ids=[f"l1_{memory_id}"], embeddings=[new_embedding], documents=[new_content])`

**frontend** `index.html` `renderL1` 函数：
- 每条 L1 加"编辑"按钮，`onclick="editL1Content(m.id, m.content)"`
- `editL1Content` 函数：`prompt()` 弹框显示当前 content，保存时调 `fetch PUT /l1/{id}` 传 `{content: newContent}`

---

### 修复5：L1 时间戳用 L0 原文时间

**文件**：`extract_l1.py`，`save_l1` 函数：

```python
def save_l1(memory, conv_id, client, source_id):
    # 在函数开头查 L0 的 ts
    l0_ts = None
    if source_id:
        conn_ts = sqlite3.connect(str(SQLITE_PATH))
        try:
            row = conn_ts.execute('SELECT ts FROM l0_messages WHERE id=?', (source_id,)).fetchone()
            if row and row[0]:
                l0_ts = row[0]
        finally:
            conn_ts.close()
    ts = l0_ts or datetime.now().isoformat()
    
    # INSERT 时用 ts 替换 DEFAULT CURRENT_TIMESTAMP
    c.execute('INSERT INTO l1_memories (..., ts) VALUES (?, ...)', (..., ts))
    
    # ChromaDB metadata 里也用同一个 ts
    metadatas=[{"ts": ts, ...}]
```

**注意**：`save_l1` 目前 `ts` 字段用数据库 DEFAULT，需要在 INSERT 里显式传入。

---

### 修复6+8：删除对话时联动清除 L1+向量

**文件**：`main.py`，`DELETE /l0/conversation/{conv_id}`：

```python
@app.delete("/l0/conversation/{conv_id}")
async def delete_l0_conversation(conv_id: str):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    # 1. 查出该 conv_id 下所有 active L1 的 id
    c.execute("SELECT id FROM l1_memories WHERE conv_id=? AND status='active'", (conv_id,))
    l1_ids = [row[0] for row in c.fetchall()]
    
    # 2. 标记 L0 superseded（已有）
    c.execute("UPDATE l0_messages SET status='superseded' WHERE conv_id=?", (conv_id,))
    
    # 3. 标记 L1 superseded
    c.execute("UPDATE l1_memories SET status='superseded' WHERE conv_id=?", (conv_id,))
    conn.commit()
    conn.close()
    
    # 4. 从 ChromaDB 删除对应向量
    if l1_ids:
        chroma_ids = [f"l1_{i}" for i in l1_ids]
        try:
            l1_collection.delete(ids=chroma_ids)
        except Exception as e:
            print(f"ChromaDB 删除失败: {e}")
    return {"status": "ok"}
```

**修复8** 和修复6 描述完全相同（需求文档写重复了），做完修复6 = 修复8 也完成。

---

### 修复7：L0 版本切换时联动 L1

**文件**：`main.py`，`save_conversation` 函数中版本切换处：

```python
# 找到版本切换的那段（已有的逻辑）
if existing:
    if existing[1] == content:
        continue
    # 旧 L0 标记 superseded（已有）
    old_l0_id = existing[0]
    c.execute('UPDATE l0_messages SET status=? WHERE id=?', ('superseded', old_l0_id))
    
    # --- 新增：旧 L0 对应的 L1 也清理 ---
    # 查出以该 l0 id 为 source_msg_id 的 L1
    c.execute("SELECT id FROM l1_memories WHERE source_msg_id=? AND status='active'", (old_l0_id,))
    old_l1_ids = [row[0] for row in c.fetchall()]
    if old_l1_ids:
        c.execute(
            f"UPDATE l1_memories SET status='superseded' WHERE source_msg_id=? AND status='active'",
            (old_l0_id,)
        )
        # 用 list: chroma_ids
        try:
            l1_collection.delete(ids=[f"l1_{i}" for i in old_l1_ids])
        except Exception as e:
            print(f"ChromaDB 版本清理失败: {e}")
    # --- 新增结束 ---
```

**注意**：`save_conversation` 里用的是同步 sqlite，但 `l1_collection` 是 ChromaDB 的 HttpClient（同步版）。`main.py` 里已经 `from chromadb` 引入了。

---

## 六、操作注意事项

### 重启命令（已配置免密 sudo）
```bash
sudo systemctl restart memory-gateway    # 重启网关
sudo systemctl restart memory-service    # 重启记忆服务
```

### 每个修复完成后
```bash
git add <改动文件>
git commit -m "修复X: ..."
./push-all.sh   # 同步推送到私有仓库(origin) + 公开仓库(public)
```

### .env 文件
- **不改现有 key**（`DEEPSEEK_API_KEY`、`ALIBABA_API_KEY`、`CLAUDE_API_KEY`、`BOCHA_API_KEY`、`CONFIG_SECRET_KEY`）
- 两个 .env：`services/memory-service/.env` 和 `services/gateway/.env`

### ChromaDB 操作（需在 memory-service venv 里）
```bash
cd services/memory-service && source venv/bin/activate
python -c "import chromadb; ..."
```

### SQLite 路径
```
data/sqlite/memory.db
```

### Dashboard
静态文件，nginx 直接 serve `services/dashboard/index.html`。
改完 index.html 后**浏览器 Ctrl+Shift+R 强制刷新**（绕开缓存）。

### L1 删除约定
用 `status='superseded'` 软删除，不物理删行。ChromaDB 向量用 `l1_collection.delete(ids=[...])` 删。

---

## 七、git 近期提交记录

```
ee10716 修复2: feel 来源区分（AI自述 vs 对话提取）
3f9148c 修复bug1: 流式工具调用 token 显示 0
f7415c3 修复1: L1 提取视角改为 AI 第一人称
6b1a8eb 文档: README 更新第二阶段功能
9fbc896 功能7: 网络搜索工具(博查API)
```

---

*本文件由 Claude Opus 4.8 生成于 2026-07-23*
