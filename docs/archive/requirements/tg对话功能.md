# 网关侧实现：TG ↔ Kelivo 会话同步（API 架构，不使用 Claude 订阅）

> 背景：现有记忆系统网关（services/gateway/server.js，端口 3000）+ 记忆服务
> （services/memory-service/main.py，端口 8001）。目标：让 TG bot（自搭桥，conversation_id=
> `tg_<chat_id>`）与 Kelivo（X-Conversation-Id header）共用同一会话上下文——同一 conv_id →
> 同一份 L0 原始消息流；TG 对话经过网关获得记忆注入；两端通过增量接口同步消息。
> 本文件只做**网关侧**改动；Kelivo 客户端改动、TG bot 本体另行进行。

## 改动文件清单

1. `services/gateway/server.js` —— /v1/models、组装模式、per-conv 串行队列、effort 注入、/api 薄代理
2. `services/memory-service/main.py` —— conv_settings 表+API、/conversations、/conversations/{cid}/messages 增量接口

不改：.env、Dashboard、extract_l1.py、docker-compose.yml。

## 关键约定

- 不改 .env 里现有 key（可以新增）
- 不修改 Kelivo 请求的 messages 透传逻辑（组装模式是独立分支，只对 tg-bot 生效）
- 保持现有 isToolLoopback / 工具内部执行行为不变（工具路由是后续任务）
- SQLite 存 UTC，显示注意 +8（但dashboard显示好像会加上8小时，根据具体的数据存储逻辑判断是否转换）
- 每完成一个任务 commit + push；重启：`sudo systemctl restart memory-gateway` / `sudo systemctl restart memory-service`

---

## 任务 A：网关加 /v1/models（OpenAI/Anthropic 双格式）

文件：`services/gateway/server.js`，在 `/health` 路由附近新增：

```js
// ============ 模型列表（codex/TG 桥走 OpenAI 格式，Claude Code 走 Anthropic 格式） ============
let modelsCache = [];
let modelsCacheTs = 0;
const MODELS_TTL_MS = 60 * 1000;

async function refreshModelsCache(force = false) {
  if (!force && modelsCache.length && Date.now() - modelsCacheTs < MODELS_TTL_MS) return;
  const list = [];
  const seen = new Set();
  try {
    const res = await axios.get(`${MEMORY_SERVICE_URL}/config/providers`, { timeout: 5000 });
    for (const p of res.data.providers || []) {
      for (const m of p.models || []) {
        if (m && !seen.has(m)) { seen.add(m); list.push(m); }
      }
    }
    const active = res.data.active;
    if (active && active.model && !seen.has(active.model)) { seen.add(active.model); list.push(active.model); }
    for (const m of [process.env.DEFAULT_MODEL, 'claude-sonnet-4-5', 'deepseek-chat']) {
      if (m && !seen.has(m)) { seen.add(m); list.push(m); }
    }
  } catch (e) {
    console.error('[MODELS] 刷新失败:', e.message);
    if (modelsCache.length) return;
  }
  modelsCache = list;
  modelsCacheTs = Date.now();
  console.log('[MODELS] 模型列表:', modelsCache.join(', '));
}

app.get('/v1/models', async (req, res) => {
  await refreshModelsCache();
  if (req.headers['anthropic-version']) {
    return res.json({
      data: modelsCache.map(id => ({ type: 'model', id, display_name: id, created_at: new Date(0).toISOString() }))
    });
  }
  res.json({ object: 'list', data: modelsCache.map(id => ({ id, object: 'model', created: 0, owned_by: 'gateway' })) });
});

app.get('/v1/models/:modelId', async (req, res) => {
  await refreshModelsCache();
  const id = req.params.modelId;
  if (!modelsCache.includes(id)) {
    return res.status(404).json({ error: { message: `Model '${id}' not found`, type: 'invalid_request_error', code: 'model_not_found' } });
  }
  res.json({ id, object: 'model', created: 0, owned_by: 'gateway' });
});

app.get('/models', async (req, res) => { req.url = '/v1/models'; app.handle(req, res); });
```

在 `loadActiveConfig()` 末尾加 `await refreshModelsCache(true);`；`/reload-config` handler 里同样加；`app.listen` 启动回调里调用一次 `refreshModelsCache()`。

## 任务 B：记忆服务新增会话配置与同步接口

文件：`services/memory-service/main.py`

### B1. init_db() 里新增表（在 custom_prompts 建表附近）

```python
c.execute('''CREATE TABLE IF NOT EXISTS conv_settings (
    id TEXT PRIMARY KEY,          -- conv_id；'__default__' 为全局默认
    window_mode TEXT DEFAULT 'all',   -- all | n | tokens
    window_n INTEGER DEFAULT 50,      -- window_mode=n 时的条数
    window_tokens INTEGER DEFAULT 8000, -- window_mode=tokens 时的 token 预算（粗略 char/4）
    effort TEXT DEFAULT 'medium',     -- low | medium | high（output_config.effort）
    model TEXT DEFAULT '',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)''')
c.execute("INSERT OR IGNORE INTO conv_settings (id) VALUES ('__default__')")
```

### B2. 新增 API（加在 config 相关接口附近）

```python
class ConvSettingsUpdate(BaseModel):
    window_mode: Optional[str] = None
    window_n: Optional[int] = None
    window_tokens: Optional[int] = None
    effort: Optional[str] = None
    model: Optional[str] = None

def _get_conv_settings(c, cid: str) -> dict:
    row = c.execute("SELECT * FROM conv_settings WHERE id=?", (cid,)).fetchone()
    if not row:
        row = c.execute("SELECT * FROM conv_settings WHERE id='__default__'").fetchone()
    if not row:
        return {"window_mode": "all", "window_n": 50, "window_tokens": 8000, "effort": "medium", "model": ""}
    return {"window_mode": row[1], "window_n": row[2], "window_tokens": row[3], "effort": row[4], "model": row[5]}

@app.get("/conv/{cid}/settings")
async def get_conv_settings(cid: str):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    s = _get_conv_settings(c, cid)
    conn.close()
    return {"conv_id": cid, **s}

@app.put("/conv/{cid}/settings")
async def put_conv_settings(cid: str, req: ConvSettingsUpdate):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    cur = _get_conv_settings(c, cid)
    for k in ("window_mode", "window_n", "window_tokens", "effort", "model"):
        v = getattr(req, k)
        if v is not None:
            cur[k] = v
    c.execute(
        "INSERT INTO conv_settings (id, window_mode, window_n, window_tokens, effort, model, updated_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "window_mode=excluded.window_mode, window_n=excluded.window_n, "
        "window_tokens=excluded.window_tokens, effort=excluded.effort, "
        "model=excluded.model, updated_at=excluded.updated_at",
        (cid, cur["window_mode"], cur["window_n"], cur["window_tokens"], cur["effort"], cur["model"]))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/conv/defaults")
async def get_conv_defaults():
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    s = _get_conv_settings(c, "__default__")
    conn.close()
    return s

@app.put("/conv/defaults")
async def put_conv_defaults(req: ConvSettingsUpdate):
    return await put_conv_settings("__default__", req)
```

### B3. 会话列表 + 增量消息接口

```python
@app.get("/conversations")
async def list_conversations():
    """列出所有有 L0 的会话（按最近活跃排序）"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    rows = c.execute(
        "SELECT conv_id, COUNT(*), MAX(ts) FROM l0_messages "
        "WHERE status='active' GROUP BY conv_id ORDER BY MAX(ts) DESC"
    ).fetchall()
    conn.close()
    return {"conversations": [
        {"conv_id": r[0], "msg_count": r[1], "last_ts": r[2]} for r in rows
    ]}

@app.get("/conversations/{cid}/messages")
async def conversation_messages(cid: str, after_id: int = 0, limit: int = 200):
    """增量拉取：返回 id > after_id 的消息（升序）。origin 推导：assistant→ai，user→client。"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, role, content, ts, client FROM l0_messages "
        "WHERE conv_id=? AND status='active' AND id>? ORDER BY id ASC LIMIT ?",
        (cid, after_id, limit)).fetchall()
    conn.close()
    return {"conv_id": cid, "next_after_id": rows[-1][0] if rows else after_id, "messages": [
        {"id": r[0], "role": r[1], "content": r[2], "ts": r[3],
         "origin": "ai" if r[1] == "assistant" else (r[4] or "unknown")}
        for r in rows
    ]}
```

> 说明：origin 复用现有 client 列（Kelivo 保存时 client='kelivo'，TG 桥保存时 client='tg-bot'），
> 不需要加列、不需要动 save_conversation 的写入逻辑。

## 任务 C：网关 per-conv 串行队列

文件：`services/gateway/server.js`，在工具定义附近新增：

```js
// ============ per-conv 串行队列：同一 conv_id 的请求排队执行，防止上下文错乱 ============
const convQueues = new Map();
function enqueueConv(convId, task) {
  const prev = convQueues.get(convId) || Promise.resolve();
  const next = prev.then(task, task);
  convQueues.set(convId, next.catch(() => {}));
  return next;
}
```

然后在 `/v1/chat/completions` handler **最外层**（try 之前）把主体包进队列：
把现有 `app.post('/v1/chat/completions', async (req, res) => { try { ... } catch ... })` 改为：

```js
app.post('/v1/chat/completions', (req, res) => {
  const cid = resolveConvId(req);
  enqueueConv(cid, async () => {
    try {
      await chatCompletionsMain(req, res);
    } catch (e) {
      console.error('网关错误:', e.response?.data || e.message);
      if (res.headersSent) { try { res.end(); } catch (_) {} return; }
      res.status(500).json({ error: { message: e.response?.data?.error?.message || e.message, type: 'gateway_error' } });
    }
  });
});

// 原 handler 主体整体移入此函数（内容不变）
async function chatCompletionsMain(req, res) { ... }
```

> 注意：把原来的 `app.post(...)` 主体搬进 `chatCompletionsMain` 时，内部的 `return` 行为保持不变；
> 流式分支已经自己 catch 流错误，非流式错误由外层统一兜底。

## 任务 D：网关组装模式（TG 桥专用分支）

文件：`services/gateway/server.js`。在 `/v1/chat/completions` 队列回调里、调用
`chatCompletionsMain` **之前**插入：

```js
const clientName = (req.headers['x-client-name'] || '').toLowerCase();
if (clientName.includes('tg')) {
  return enqueueConv(resolveConvId(req), () => handleTgBotAssembled(req, res));
}
```

新增函数（放在 chatCompletionsMain 附近）：

```js
// TG 桥专用：只发最新一条 user 消息 + conversation_id，网关从 L0 组装历史（按 conv_settings 窗口）
async function handleTgBotAssembled(req, res) {
  try {
    const convId = req.body.conversation_id || resolveConvId(req);
    const msgs = req.body.messages || [];
    // 取最后一条文本 user 消息作为本轮输入
    let currentText = '';
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role !== 'user') continue;
      const t = typeof m.content === 'string' ? m.content : '';
      if (t.trim()) { currentText = t; break; }
    }
    if (!currentText) {
      return res.status(400).json({ error: { message: 'no user text', type: 'invalid_request_error' } });
    }

    // 1) 会话设置（窗口 + effort + model）
    let settings = { window_mode: 'all', window_n: 50, window_tokens: 8000, effort: 'medium', model: '' };
    try {
      const s = await axios.get(`${MEMORY_SERVICE_URL}/conv/${encodeURIComponent(convId)}/settings`, { timeout: 3000 });
      settings = { ...settings, ...(s.data || {}) };
    } catch (e) { /* 用默认 */ }

    // 2) 从 L0 拉历史
    let history = [];
    try {
      const h = await axios.get(`${MEMORY_SERVICE_URL}/conversations/${encodeURIComponent(convId)}/messages?after_id=0&limit=500`, { timeout: 5000 });
      history = (h.data && h.data.messages) || [];
    } catch (e) { /* 新会话无历史 */ }

    // 3) 按窗口截断历史（不包含本轮消息）
    let trimmed = history;
    if (settings.window_mode === 'n' && settings.window_n > 0) {
      trimmed = trimmed.slice(-settings.window_n);
    } else if (settings.window_mode === 'tokens' && settings.window_tokens > 0) {
      let acc = 0;
      trimmed = [];
      for (let i = history.length - 1; i >= 0; i--) {
        const cost = Math.ceil((history[i].content || '').length / 4);
        if (acc + cost > settings.window_tokens) break;
        acc += cost;
        trimmed.unshift(history[i]);
      }
    }

    // 4) 组装 messages：历史（含此前 AI 回复）+ 本轮 user
    const assembled = trimmed.map(h => ({ role: h.role, content: h.content }));
    assembled.push({ role: 'user', content: currentText });
    req.body.messages = assembled;
    req.body.conversation_id = convId;

    // 5) effort/thinking：客户端没显式传才用会话配置注入
    if (!req.body.output_config && !req.body.thinking && settings.effort) {
      req.body.output_config = { effort: settings.effort };
    }
    if (settings.model && !req.body.model) req.body.model = settings.model;

    // 6) 走原主流程（记忆注入、上游转发、save_conversation 都在里面；client 自动取 x-client-name='tg-bot'）
    await chatCompletionsMain(req, res);
  } catch (e) {
    console.error('[TG] 组装模式错误:', e.message);
    if (!res.headersSent) {
      res.status(500).json({ error: { message: e.message, type: 'gateway_error' } });
    } else { try { res.end(); } catch (_) {} }
  }
}
```

> 说明：组装模式不注入网关工具（TG 桥纯聊天）；记忆注入（memoryMenu 等）在
> chatCompletionsMain 内照常生效；`resolveConvId` 对 tg 请求会优先 body.conversation_id。

## 任务 E：effort 注入（非组装模式也生效）

文件：`services/gateway/server.js`。在 `chatCompletionsMain` 里取到 `convIdForMenu` 之后、
构造 systemBlocks 之前插入：

```js
// effort 注入：Kelivo 显式传了就不动；没传时按会话配置（默认 medium）
if (!req.body.output_config && !req.body.thinking) {
  try {
    const s = await axios.get(`${MEMORY_SERVICE_URL}/conv/${encodeURIComponent(convIdForMenu)}/settings`, { timeout: 3000 });
    if (s.data && s.data.effort) req.body.output_config = { effort: s.data.effort };
  } catch (e) { /* 忽略 */ }
}
```

## 任务 F：/api 薄代理（让 Kelivo/TG 桥只面对网关一个入口，可选但推荐）

文件：`services/gateway/server.js`，在路由注册末尾（app.listen 之前）新增：

```js
// ============ /api 薄代理：转发到记忆服务的同步/配置接口 ============
app.all('/api/*', async (req, res) => {
  const target = req.originalUrl.replace(/^\/api/, MEMORY_SERVICE_URL);
  try {
    const upstream = await axios({
      method: req.method,
      url: target,
      data: ['GET', 'HEAD'].includes(req.method) ? undefined : req.body,
      params: req.query,
      timeout: 15000,
      responseType: 'json',
    });
    res.status(upstream.status).json(upstream.data);
  } catch (e) {
    const status = e.response?.status || 502;
    res.status(status).json(e.response?.data || { error: { message: e.message, type: 'proxy_error' } });
  }
});
```

> 放在最后注册，避免吞掉 /v1/* 路由（Express 按注册顺序匹配，/api/* 只在 /api 前缀命中）。

## 验证清单（全部通过再提交）

```bash
# 1. 模型列表（OpenAI 格式）
curl http://127.0.0.1:3000/v1/models
# 2. 模型列表（Anthropic 格式）
curl http://127.0.0.1:3000/v1/models -H "anthropic-version: 2023-06-01"
# 3. 会话设置读写
curl -X PUT http://127.0.0.1:8001/conv/tg_test/settings -H 'Content-Type: application/json' \
  -d '{"window_mode":"n","window_n":20,"effort":"medium"}'
curl http://127.0.0.1:8001/conv/tg_test/settings
# 4. 模拟 TG 请求（组装模式）
curl -X POST http://127.0.0.1:3000/v1/chat/completions -H 'Content-Type: application/json' \
  -H 'x-client-name: tg-bot' -d '{
    "model": "claude-sonnet-4-5",
    "conversation_id": "tg_test",
    "messages": [{"role": "user", "content": "你好，还记得我们聊过什么吗？"}],
    "stream": false
  }'
# 5. 增量拉取
curl "http://127.0.0.1:3000/api/conversations/tg_test/messages?after_id=0"
# 6. 检查 L0 落库与 client 标记
sqlite3 /home/qingzhi/memory-system/data/sqlitememory.db \
  "SELECT conv_id, client, role, substr(content,1,40) FROM l0_messages WHERE conv_id='tg_test' ORDER BY id"
# 7. 并发串行：同一 conv_id 连发两个请求，网关日志应显示按顺序处理
```

> 注意：验证清单里的 sqlite 路径 `/home/qingzhi/memory-system/data/sqlitememory.db` 来自现有代码
> （server.js 中 dataDir 也是 /home/qingzhi/memory-system/...），如实际路径不同请按真实环境替换。

## 验收标准

- [ ] /v1/models 返回模型列表，Anthropic header 下格式不同
- [ ] conv_settings 可读写，__default__ 存在
- [ ] TG 组装请求能拿到带记忆注入的回复（日志出现 [CACHE] 等现有输出）
- [ ] 该请求落 L0，client='tg-bot'
- [ ] /api/conversations 能看到 tg_test，messages 增量接口按 id 递增返回
- [ ] 同一 conv_id 并发请求不交错（日志顺序处理）
- [ ] Kelivo 原有流程（非 tg 请求）行为不变

## 已知限制（后续任务，不在本文件范围）

- Kelivo 前端改动（绑定 tg 会话、RemoteSync 拉历史+轮询、来源角标）另行
- TG bot 本体（aiogram 等，流式 + 思维链折叠块呈现）另行
- 工具路由（Kelivo MCP 工具透传）另行；当前网关对 Kelivo 的 isToolLoopback 行为保持不变
- Dashboard 编辑 conv_settings 的 UI 另行（接口已就绪）

## 注意事项及版本管理
我给你配置了免密重启权限，你可以直接用 
!sudo systemctl restart memory-gateway 重启等
每次改完一个功能，帮我 git commit 并 push 到 public 仓库。并用一键脚本同步到两个仓库
commit message 写清楚改了什么。
执行中有任何问题可以随时和我沟通和确认哦








