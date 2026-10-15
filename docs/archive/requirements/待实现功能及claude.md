# 待实现功能 - Claude Code 执行指南

> 生成日期：2026-08-14  
> 基于对 memory-system-code-main 代码的逐行检查，整理出所有尚未实现或存在缺陷的功能。  
> **请先阅读全文再开始编码。** 按优先级顺序实现，每完成一项 commit + push。（有一键脚本同步到两个仓库）

---

## 阅读本文前的准备

先确认以下文件你已经了解：
- `services/gateway/server.js`（网关核心，本次改动最多）
- `services/memory-service/main.py`（记忆服务）
- `services/dashboard/index.html`（单文件前端）
- `Narrative生成提示词.md`（项目根目录）

---

## 优先级一：影响实际聊天体验（必须先做）

---

### 任务 A：Gateway 注入 Shared Narrative 和 Recent Summary 作为 cache② ③

**问题描述：**  
`server.js` 的 `systemBlocks` 当前只有一个 block（cache①），Shared Narrative 和 Recent Summary 虽然有后端 API 和 Dashboard，但从未被注入到实际聊天的 system prompt 里。Claude 每轮聊天根本收不到这两层内容，功能对聊天体验完全无效。

**目标结构（Shared_Narrative.md 第7、8节）：**
```
system:
  cache① Memory Continuity Context + L2画像 + tools说明   ← 稳定，已实现
  cache② Shared Narrative                                  ← 缺失
  cache③ Recent Summary（近期摘要）                        ← 缺失
messages:
  Kelivo 原始消息（不动）
```

**修改文件：** `services/gateway/server.js`

**具体改法：**

在 `getProfile()` 函数下方（大约第 284 行附近），新增两个异步函数：

```js
// 获取 Shared Narrative（cache②）
async function getSharedNarrative() {
  try {
    const res = await axios.get(`${MEMORY_SERVICE_URL}/narrative/current`, { timeout: 5000 });
    if (res.data && res.data.exists && res.data.content) {
      return res.data.content;
    }
    return null;
  } catch (e) {
    console.error('[NARRATIVE] 获取失败:', e.message);
    return null;
  }
}

// 获取 Recent Summary（cache③）
async function getRecentSummary() {
  try {
    const res = await axios.get(`${MEMORY_SERVICE_URL}/summary/current`, { timeout: 5000 });
    if (res.data && res.data.exists && res.data.content) {
      return res.data.content;
    }
    return null;
  } catch (e) {
    console.error('[SUMMARY] 获取失败:', e.message);
    return null;
  }
}
```

然后找到请求处理主函数里（约第 938 行）的 `Promise.all`，改为同时获取三份数据：

```js
// 改前：
const [profile, memoryMenu] = await Promise.all([
  getProfile(),
  getMemoryMenu(userContent, convIdForMenu)
]);

// 改后：
const [profile, memoryMenu, sharedNarrative, recentSummary] = await Promise.all([
  getProfile(),
  getMemoryMenu(userContent, convIdForMenu),
  getSharedNarrative(),
  getRecentSummary()
]);
```

然后修改 `systemBlocks` 的构造（约第 960 行），添加 cache② 和 cache③：

```js
// 改前：
const systemBlocks = [
  { type: 'text', text: systemPrefix, cache_control: { type: 'ephemeral' } }
];

// 改后：
const systemBlocks = [
  { type: 'text', text: systemPrefix, cache_control: { type: 'ephemeral', ttl: 3600 } }
];

// 按需添加 cache② Shared Narrative
if (sharedNarrative) {
  systemBlocks.push({
    type: 'text',
    text: `[共同经历叙事]\n\n${sharedNarrative}`,
    cache_control: { type: 'ephemeral', ttl: 3600 }
  });
}

// 按需添加 cache③ Recent Summary
if (recentSummary) {
  systemBlocks.push({
    type: 'text',
    text: `[近期阶段摘要]\n\n${recentSummary}`,
    cache_control: { type: 'ephemeral', ttl: 3600 }
  });
}
```

**注意事项：**
- Anthropic 最多支持 4 个 cache breakpoint，目前最多用到 3 个，留 1 个备用，安全
- `sharedNarrative` / `recentSummary` 为 null 时不添加 block，保证没内容时不出错
- `cache_control` 同时加上 `ttl: 3600`（见任务 B）

**验证方式：**  
重启网关后，在 Kelivo 里发一条消息，看日志里 `[CACHE] 读缓存` 计数是否 > 0，说明 cache 结构生效。如果 Narrative 已经有内容，可以直接问 Claude "我们之前经历过什么"，看回复是否引用了叙事内容。

---

### 任务 B：所有 cache_control 加显式 ttl: 3600

**问题描述：**  
当前所有 `cache_control` 只有 `{ type: 'ephemeral' }`，没有指定 TTL。Anthropic 现支持两种 TTL（5分钟和1小时），应显式指定 1 小时提高缓存命中率。

**修改文件：** `services/gateway/server.js`

全局搜索 `cache_control: { type: 'ephemeral' }` 并替换为 `cache_control: { type: 'ephemeral', ttl: 3600 }`。  
（做完任务 A 时一并处理即可，不需要单独一次 commit）

---

### 任务 C：Identity Context 改为 Memory Continuity Context

**问题描述：**  
`server.js` 第 945 行 `systemPrefix` 开头是 `"你是一位有记忆的AI伙伴"`。需求明确要求不用这句话，改为 Memory Continuity Context 风格的描述。

**修改文件：** `services/gateway/server.js`

将 `systemPrefix` 的内容改为以下结构（保留 profile 和 tools 注入，只改开头的角色描述和画像解释层）：

```js
const systemPrefix = `你拥有一个持续维护的记忆系统。

这个记忆系统记录了过去交流中逐渐形成的理解，包括对用户的理解、对自身交流方式的理解，以及双方共同经历的重要内容。

这些记忆用于帮助你理解当前交流中的连续性。记忆不是固定设定，也不是必须遵守的规则。它们来自过去经验，并可能随着新的交流不断变化。

记忆是沉淀物，不是日记。记忆不能替代思考。记忆来自过去，情绪和感受来自当下。

请结合当前对话理解当下。不要机械复述过去，也不要因为过去记录限制新的理解。

---

以下是过去长期交流中逐渐形成的理解记录：

about_user（关于用户的理解）、about_ai（关于自身交流方式和自我理解的记录）、about_us（关于双方交流历程和互动方式的理解）。

这些内容不是固定人格设定，也不是必须遵守的规则。它们代表过去形成的认识，可以帮助理解当前交流。随着新的交流，这些理解也可以变化。

[人格画像]
${profile}

你可以使用以下工具：
- recall: 需要更多细节时搜索记忆库
- feel: 记录你当下的感受
- update_profile: 更新人格画像
- bocha_search: 用博查联网搜索可靠的中文网络信息（供应商不支持内置搜索、或需要可信来源时使用）

当需要查询当前时间时，必须调用 get_current_time 工具获取准确时间，不要自己编造。`;
```

**注意：**  
`systemPrefix` 修改后，cache① 的内容会变，Anthropic 会重新写入 cache（第一轮不命中是正常的），之后再次对话就会命中新缓存。

---

### 任务 D：修复 Narrative 生成提示词内容未注入的 Bug

**问题描述：**  
`main.py` 里的 `build_narrative_prompt()` 函数（约第 1761 行）读取 `Narrative生成提示词.md` 后，试图用 `.replace("{existing}", ...)` 和 `.replace("{memories}", ...)` 注入现有叙事和记忆。

**但是**，实际的 `Narrative生成提示词.md` 文件里没有 `{existing}` 和 `{memories}` 这两个占位符——文件是角色指令式的叙事风格，没有模板变量。

结果：两次 `replace()` 都是空操作，LLM 只收到了指令模板，**既看不到现有叙事，也看不到新记忆**，相当于无料生成。

**修改文件：** `services/memory-service/main.py`

将 `build_narrative_prompt()` 和 `call_llm_for_narrative()` 的配合方式改为：  
- 把 `Narrative生成提示词.md` 的内容作为 **system message**（角色指令）  
- 把现有叙事 + 新记忆组成 **user message**（待处理内容）

```python
async def build_narrative_prompt(existing_narrative: str, core_memories: list, recent_memories: list) -> dict:
    """构建 Narrative 生成提示词，返回 {system, user} 两段"""
    # 读取系统角色指令（Narrative生成提示词.md）
    prompt_file = Path(__file__).parent.parent.parent / "Narrative生成提示词.md"
    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            system_prompt = f.read()
    except:
        system_prompt = "你是一段亲密关系的记忆守护者。维护一份连续的共同经历叙事。保留时间锚点、情感转折点、共同经历、关系变化。"

    # 构建记忆列表（user message 的内容）
    memory_text = "### 核心记忆\n"
    for m in core_memories:
        memory_text += f"- [{m[2][:10] if m[2] else ''}] {m[0]}\n"
        if m[1]:
            memory_text += f"  > 原话：{m[1]}\n"

    memory_text += "\n### 近期高唤醒记忆\n"
    for m in recent_memories[:20]:
        memory_text += f"- [{m[2][:10] if m[2] else ''}] {m[0]}\n"

    user_message = f"""请基于以下材料，更新我们的共同经历叙事。

## 现有叙事
{existing_narrative or "（这是第一次生成，请基于记忆从头写起）"}

## 新增记忆材料
{memory_text}

请生成更新后的完整叙事（按时间顺序，markdown 章节格式）："""

    return {"system": system_prompt, "user": user_message}
```

同步修改调用方 `generate_narrative()` 和 `call_llm_for_narrative()`：

在 `generate_narrative()` 里（约第 1348 行）：
```python
# 改前：
prompt = await build_narrative_prompt(existing_narrative, core_memories, recent_memories)
new_narrative = await call_llm_for_narrative(config, prompt)

# 改后：
prompt_parts = await build_narrative_prompt(existing_narrative, core_memories, recent_memories)
new_narrative = await call_llm_for_narrative(config, prompt_parts)
```

修改 `call_llm_for_narrative()`（约第 1798 行）：
```python
async def call_llm_for_narrative(config: dict, prompt_parts: dict) -> str:
    """调用 LLM 生成 Narrative（system+user 两段式）"""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{config['base_url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": config['model'],
                    "messages": [
                        {"role": "system", "content": prompt_parts["system"]},
                        {"role": "user", "content": prompt_parts["user"]}
                    ],
                    "max_tokens": 4000
                }
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"LLM call error: {e}")
        return None
```

---

## 优先级二：重要功能补全

---

### 任务 E：自动触发 Narrative 更新（让配置真正生效）

**问题描述：**  
`narrative_config` 表里有 `auto_update_enabled`、`check_threshold_turns`、`check_threshold_l1` 字段，Dashboard 也能配置，但代码里从未有任何地方读取这些值并实际触发 Narrative 更新。配置是死的。

**修改文件：** `services/memory-service/extract_l1.py`

在 `extract_l1.py` 文件末尾，L1 提取完成后，加入 Narrative 自动更新检查逻辑：

```python
def check_and_auto_update_narrative():
    """L1提取完成后检查是否需要自动更新 Narrative"""
    try:
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()

        # 读取配置
        cfg = c.execute(
            "SELECT auto_update_enabled, check_threshold_turns, check_threshold_l1 FROM narrative_config WHERE id=1"
        ).fetchone()

        if not cfg or not cfg[0]:  # auto_update_enabled = 0
            conn.close()
            return

        threshold_turns = cfg[1] or 50
        threshold_l1 = cfg[2] or 10

        # 获取上次 Narrative 更新时间
        last_narrative = c.execute(
            "SELECT ts FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        last_ts = last_narrative[0] if last_narrative else None

        # 统计上次更新后新增的 L1 数量
        if last_ts:
            new_l1_count = c.execute(
                "SELECT COUNT(*) FROM l1_memories WHERE status='active' AND ts > ?", (last_ts,)
            ).fetchone()[0]
        else:
            new_l1_count = c.execute(
                "SELECT COUNT(*) FROM l1_memories WHERE status='active'"
            ).fetchone()[0]

        # 统计上次更新后新增的 L0 对话轮数
        if last_ts:
            new_turns = c.execute(
                "SELECT COUNT(*) FROM l0_messages WHERE status='active' AND ts > ? AND role='assistant'", (last_ts,)
            ).fetchone()[0]
        else:
            new_turns = c.execute(
                "SELECT COUNT(*) FROM l0_messages WHERE status='active' AND role='assistant'"
            ).fetchone()[0]

        conn.close()

        should_update = (new_l1_count >= threshold_l1) or (new_turns >= threshold_turns)
        print(f"[NARRATIVE AUTO] 新增L1={new_l1_count}(阈值{threshold_l1}), 新增轮次={new_turns}(阈值{threshold_turns}), 触发={should_update}")

        if should_update:
            print("[NARRATIVE AUTO] 触发自动更新...")
            import requests
            requests.post(
                "http://localhost:8001/narrative/generate",
                json={"trigger_type": "auto_threshold", "force": False},
                timeout=120
            )
            print("[NARRATIVE AUTO] 自动更新请求已发送")

    except Exception as e:
        print(f"[NARRATIVE AUTO] 检查失败: {e}")


if __name__ == "__main__":
    # 原有 L1 提取逻辑...（保持不变）
    extract_new_l1()
    # 提取完成后检查是否需要更新 Narrative
    check_and_auto_update_narrative()
```

**注意：** 找到 `extract_l1.py` 的 `if __name__ == "__main__":` 入口，在原有提取函数调用之后追加 `check_and_auto_update_narrative()` 即可，不要动原有逻辑。

---

### 任务 F：narrative_state 增量状态表

**问题描述：**  
需求要求增加 `narrative_state` 表记录"上次已处理到哪条消息"，避免每次生成 Narrative 都重读全部历史。当前每次生成都全量读 L1。

**修改文件：** `services/memory-service/main.py`

在 `init_db()` 函数里（约第 65 行，其他 CREATE TABLE 附近）新增：

```python
c.execute('''CREATE TABLE IF NOT EXISTS narrative_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_processed_l1_id INTEGER DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)''')
```

然后修改 `generate_narrative()` 函数，利用 `narrative_state` 实现增量：

```python
@app.post("/narrative/generate")
async def generate_narrative(req: NarrativeGenerateRequest):
    try:
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()

        # 1. 获取现有 narrative
        existing = c.execute(
            "SELECT content FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        existing_narrative = existing[0] if existing else ""

        # 2. 获取增量状态（上次处理到哪条 L1）
        state = c.execute("SELECT last_processed_l1_id FROM narrative_state WHERE id=1").fetchone()
        last_id = state[0] if state else 0

        # 3. 如果是强制重生成，忽略增量，读取全部
        if req.force or not existing_narrative:
            core_memories = c.execute(
                "SELECT content, quote, ts FROM l1_memories WHERE status='active' AND is_core=1 ORDER BY ts DESC LIMIT 20"
            ).fetchall()
            recent_memories = c.execute(
                "SELECT content, quote, ts FROM l1_memories WHERE status='active' ORDER BY ts DESC LIMIT 50"
            ).fetchall()
        else:
            # 增量：只读上次之后新增的记忆
            new_memories = c.execute(
                "SELECT content, quote, ts FROM l1_memories WHERE status='active' AND id > ? ORDER BY ts ASC",
                (last_id,)
            ).fetchall()
            # 如果新增不足 5 条，跳过（内容太少不值得更新）
            if len(new_memories) < 5 and not req.force:
                conn.close()
                return {"status": "skip", "message": f"新增记忆不足（{len(new_memories)}条），跳过更新"}
            core_memories = c.execute(
                "SELECT content, quote, ts FROM l1_memories WHERE status='active' AND is_core=1 ORDER BY ts DESC LIMIT 10"
            ).fetchall()
            recent_memories = new_memories

        # 4. 获取最新 L1 id，用于更新 state
        latest_l1 = c.execute(
            "SELECT MAX(id) FROM l1_memories WHERE status='active'"
        ).fetchone()
        latest_l1_id = latest_l1[0] if latest_l1 and latest_l1[0] else 0

        conn.close()

        # 5. 获取模型配置
        config = await get_model_config("narrative")
        if not config["configured"]:
            return {"status": "error", "message": "Narrative 模型未配置"}

        # 6. 构建提示词并调用 LLM
        prompt_parts = await build_narrative_prompt(existing_narrative, core_memories, recent_memories)
        new_narrative = await call_llm_for_narrative(config, prompt_parts)

        if not new_narrative:
            return {"status": "error", "message": "生成失败"}

        # 7. 保存新版本 + 更新增量状态
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()
        c.execute("UPDATE shared_narrative SET status='superseded' WHERE status='active'")
        last_version = c.execute("SELECT MAX(version) FROM shared_narrative").fetchone()[0] or 0
        new_version = last_version + 1
        c.execute(
            "INSERT INTO shared_narrative (content, version, trigger_type, trigger_details, status) VALUES (?, ?, ?, ?, 'active')",
            (new_narrative, new_version, req.trigger_type, json.dumps({"timestamp": datetime.now().isoformat()}))
        )
        # 更新增量状态
        c.execute(
            "INSERT OR REPLACE INTO narrative_state (id, last_processed_l1_id, updated_at) VALUES (1, ?, ?)",
            (latest_l1_id, datetime.now().isoformat())
        )
        conn.commit()
        new_id = c.lastrowid
        conn.close()

        return {"status": "ok", "id": new_id, "version": new_version, "content": new_narrative}

    except Exception as e:
        print(f"Narrative generation error: {e}")
        import traceback; traceback.print_exc()
        return {"status": "error", "message": str(e)}
```

---

### 任务 G：Custom Prompt 功能（自定义交互偏好）

**问题描述：**  
需求第9节：用户可以在 Dashboard 里设置一段自定义的交互偏好 prompt（回答风格、分析方式、表达偏好），这段内容被注入进 cache① 里（作为 systemPrefix 的一部分）。

当前：`custom_prompts` 表不存在，无 API，无 UI，未注入。

**修改文件：**

**1. `services/memory-service/main.py` — 数据库和 API**

在 `init_db()` 新增表：
```python
c.execute('''CREATE TABLE IF NOT EXISTS custom_prompts (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    content TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)''')
```

新增 API（加在 model config API 附近）：
```python
@app.get("/custom_prompt")
async def get_custom_prompt():
    conn = sqlite3.connect(str(SQLITE_PATH))
    row = conn.execute("SELECT content, enabled FROM custom_prompts WHERE id=1").fetchone()
    conn.close()
    if not row:
        return {"content": "", "enabled": 1}
    return {"content": row[0] or "", "enabled": row[1]}

class CustomPromptUpdate(BaseModel):
    content: str
    enabled: int = 1

@app.put("/custom_prompt")
async def update_custom_prompt(req: CustomPromptUpdate):
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO custom_prompts (id, content, enabled, updated_at) VALUES (1, ?, ?, ?)",
        (req.content, req.enabled, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}
```

**2. `services/gateway/server.js` — 注入 custom prompt**

在 `getProfile()` 附近新增：
```js
async function getCustomPrompt() {
  try {
    const res = await axios.get(`${MEMORY_SERVICE_URL}/custom_prompt`, { timeout: 3000 });
    if (res.data && res.data.enabled && res.data.content && res.data.content.trim()) {
      return res.data.content.trim();
    }
    return null;
  } catch (e) {
    return null;
  }
}
```

在主处理函数的 `Promise.all` 里加入：
```js
const [profile, memoryMenu, sharedNarrative, recentSummary, customPrompt] = await Promise.all([
  getProfile(),
  getMemoryMenu(userContent, convIdForMenu),
  getSharedNarrative(),
  getRecentSummary(),
  getCustomPrompt()
]);
```

在 `systemPrefix` 末尾追加 customPrompt（如果有的话）：
```js
const systemParts = [ /* ...基础 systemPrefix 文本... */ ];
if (customPrompt) {
  systemParts.push(`\n---\n[用户交互偏好]\n${customPrompt}`);
}
const systemPrefix = systemParts.join('');
```

或者直接在现有 systemPrefix 字符串末尾加：
```js
const systemPrefix = `...（原有内容）...${customPrompt ? '\n\n---\n[用户交互偏好]\n' + customPrompt : ''}`;
```

**3. `services/dashboard/index.html` — UI**

在配置页面（`id="config"` 的 panel）里，现有 provider 配置下方新增一个区域：

```html
<div class="config-section" style="margin-top:30px;">
  <h3>交互偏好（Custom Prompt）</h3>
  <p style="font-size:12px;color:#888;margin-bottom:10px;">
    设置你希望 AI 遵循的回答风格、分析方式或表达偏好。这里不是身份定义，而是交互习惯配置。<br>
    例如：回答时先给结论再展开；偏好用比喻和类比来解释概念；保持口语化。
  </p>
  <div class="config-field">
    <label>启用：</label>
    <select id="custom-prompt-enabled">
      <option value="1">启用</option>
      <option value="0">禁用</option>
    </select>
  </div>
  <div class="config-field">
    <textarea id="custom-prompt-content" rows="5"
      style="width:100%;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:13px;"
      placeholder="输入你的交互偏好..."></textarea>
  </div>
  <button class="btn" onclick="saveCustomPrompt()">保存</button>
  <span id="custom-prompt-status" style="margin-left:10px;font-size:12px;"></span>
</div>
```

对应 JS 函数（加在 `<script>` 里）：
```js
function loadCustomPrompt() {
  fetch(API + '/custom_prompt').then(function(r) { return r.json(); }).then(function(d) {
    document.getElementById('custom-prompt-content').value = d.content || '';
    document.getElementById('custom-prompt-enabled').value = d.enabled ? '1' : '0';
  });
}

function saveCustomPrompt() {
  var status = document.getElementById('custom-prompt-status');
  status.textContent = '保存中...';
  fetch(API + '/custom_prompt', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      content: document.getElementById('custom-prompt-content').value,
      enabled: parseInt(document.getElementById('custom-prompt-enabled').value)
    })
  }).then(function(r) { return r.json(); }).then(function(d) {
    status.textContent = d.status === 'ok' ? '✓ 已保存（下次对话生效）' : '✗ 保存失败';
    setTimeout(function() { status.textContent = ''; }, 3000);
  });
}
```

在 `showPanel('config')` 时调用 `loadCustomPrompt()`：
```js
if (name === 'config') { loadConfig(); loadCustomPrompt(); }
```

---

## 优先级三：Dashboard 体验完善

---

### 任务 H：Dashboard 标签名改为"自动更新检查策略"

**问题描述：**  
需求（更新检查周期.md）明确说不要叫"轮换周期"，应叫"**自动更新检查策略**"。当前 Dashboard 摘要配置区的标签是"自动更新："。

**修改文件：** `services/dashboard/index.html`

在 Summary 配置区（约第 279 行），将：
```html
<label>自动更新：</label>
```
改为：
```html
<label>自动更新检查策略：</label>
```

同时将该配置区的标题：
```html
<h3 style="margin-top:30px;">摘要配置</h3>
```
改为：
```html
<h3 style="margin-top:30px;">更新检查配置</h3>
```

---

### 任务 I：Narrative 时间线视觉化模式

**问题描述：**  
需求要求 Narrative 页面提供三种模式：阅读、编辑、**时间线视觉化**。当前只有阅读和编辑，时间线模式缺失。

时间线模式：从叙事文本中提取时间节点 / 阶段标题 / 重要变化，纵向排列展示，不修改底层存储。

**修改文件：** `services/dashboard/index.html`

在 Narrative 页面的按钮区（约第 255 行）新增按钮：
```html
<button class="btn" onclick="showNarrativeTimeline()" style="background:#6f42c1;">时间线</button>
```

新增 JS 函数（解析 Narrative markdown，提取 `### YYYY年...` 格式的章节标题作为时间节点）：
```js
var narrativeViewMode = 'read'; // 'read' | 'timeline'

function showNarrativeTimeline() {
  narrativeViewMode = 'timeline';
  var content = document.getElementById('narrative-content').textContent;
  if (!content || content === '暂无内容') {
    alert('请先生成叙事内容');
    return;
  }

  // 提取 ### 开头的章节作为时间节点
  var lines = content.split('\n');
  var nodes = [];
  var currentNode = null;

  lines.forEach(function(line) {
    if (line.startsWith('### ')) {
      if (currentNode) nodes.push(currentNode);
      currentNode = { title: line.replace('### ', ''), summary: [] };
    } else if (currentNode && line.trim()) {
      if (currentNode.summary.length < 2) { // 只取每节前两行摘要
        currentNode.summary.push(line.trim().substring(0, 80));
      }
    }
  });
  if (currentNode) nodes.push(currentNode);

  if (nodes.length === 0) {
    alert('未找到时间节点（叙事格式需包含 ### 开头的章节标题）');
    return;
  }

  // 渲染时间线
  var html = '<div style="position:relative;padding-left:40px;">';
  html += '<div style="position:absolute;left:16px;top:0;bottom:0;width:2px;background:#e0e0e0;"></div>';
  nodes.forEach(function(node, i) {
    html += '<div style="position:relative;margin-bottom:24px;">';
    html += '<div style="position:absolute;left:-32px;top:4px;width:12px;height:12px;border-radius:50%;background:#6f42c1;border:2px solid #fff;box-shadow:0 0 0 2px #6f42c1;"></div>';
    html += '<div style="font-weight:bold;color:#333;margin-bottom:4px;">' + node.title + '</div>';
    node.summary.forEach(function(s) {
      html += '<div style="font-size:12px;color:#666;">' + s + (s.length >= 80 ? '...' : '') + '</div>';
    });
    html += '</div>';
  });
  html += '</div>';
  html += '<div style="margin-top:16px;"><button class="btn" onclick="showNarrativeRead()" style="background:#999;">返回阅读</button></div>';

  document.getElementById('narrative-content').innerHTML = html;
}

function showNarrativeRead() {
  narrativeViewMode = 'read';
  loadNarrative(); // 重新加载原始文本
}
```

---

## 实现顺序建议

```
第一批（核心功能，影响聊天体验）：
  任务 D → 任务 C → 任务 A + B → 重启 memory-service 和 memory-gateway → 测试

第二批（功能补全）：
  任务 E → 任务 F → 重启 memory-service → 测试自动触发

第三批（Dashboard 完善）：
  任务 G → 任务 H → 任务 I → 强刷 Dashboard 测试
```

每完成一个任务，执行：
```bash
git add <改动文件>
git commit -m "实现：<任务名>"
./push-all.sh
```

---

## 验收检查清单

完成所有任务后，逐一确认：

- [ ] **A** — 发一条消息后，网关日志显示获取 Shared Narrative 成功；问 Claude "我们之前经历过什么" 能引用叙事
- [ ] **B** — 日志里 `[CACHE] 读缓存 > 0`（第二轮起）
- [ ] **C** — Claude 不再自称"有记忆的AI伙伴"，改用记忆系统语境描述
- [ ] **D** — 手动触发 Narrative 生成，确认生成内容有实际的叙事而非空洞模板
- [ ] **E** — 修改阈值为较小值（如5条）手动跑 extract_l1.py，看日志触发了 `/narrative/generate`
- [ ] **F** — 生成 Narrative 后，`narrative_state` 表里 `last_processed_l1_id` 有值
- [ ] **G** — Dashboard 能保存 Custom Prompt；下一轮聊天 Claude 遵循了该偏好
- [ ] **H** — Dashboard 摘要配置区显示"自动更新检查策略"
- [ ] **I** — Narrative 页面出现"时间线"按钮，点击后展示竖向时间轴

---

## 关键约定提醒

- **不改 `.env` 里的现有 key**（可以新增）
- **不修改 `req.body.messages`**（禁止 fake user/assistant message）
- **重启命令**：`sudo systemctl restart memory-gateway` / `sudo systemctl restart memory-service` 已配置免密重启权限
- **ChromaDB 向量 ID 格式**：严格用 `l1_{数字}`
- **Dashboard 改完后**浏览器 `Ctrl+Shift+R` 强制刷新
- SQLite 存 UTC，显示时注意 +8 转换（但dashboard显示好像会加上8小时，根据具体的数据存储逻辑判断是否转换）
- 每个功能做完可以让我测试（你也可以执行你自己的测试，可以按需在我测试时开启对应的日志方便你确认测试中的效果等）
