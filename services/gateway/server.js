const express = require('express');
const crypto = require('crypto');
const axios = require('axios');
const cors = require('cors');
require('dotenv').config();

const app = express();
app.use(cors());
app.use(express.json({ limit: '10mb' }));

const MEMORY_SERVICE_URL = process.env.MEMORY_SERVICE_URL || 'http://localhost:8001';
const PORT = process.env.PORT || 3000;

// 功能6：动态配置缓存。启动时和 /reload-config 时从记忆服务拉取当前供应商配置。
// 拉取失败或未配置时，转发逻辑回退到 .env（配置优先，.env 兜底）。
let activeConfig = null;   // { name, base_url, api_key, model } 或 null
// 返回 'active'（拉到并启用配置）| 'unconfigured'（服务正常但未配置，回退 .env）| 'error'（拉取失败，回退 .env）
async function loadActiveConfig() {
  try {
    const res = await axios.get(`${MEMORY_SERVICE_URL}/config/current`, { timeout: 5000 });
    if (res.data && res.data.configured) {
      activeConfig = {
        name: res.data.name,
        base_url: res.data.base_url,
        api_key: res.data.api_key,
        model: res.data.model
      };
      console.log(`[CONFIG] 已加载配置：${activeConfig.name} / ${activeConfig.model}`);
      return 'active';
    } else {
      activeConfig = null;
      console.log('[CONFIG] 记忆服务未配置供应商，回退到 .env');
      return 'unconfigured';
    }
  } catch (e) {
    activeConfig = null;
    console.error('[CONFIG] 拉取配置失败，回退到 .env：', e.message);
    return 'error';
  }
}

// 工具定义
const TOOLS = [
  {
    type: "function",
    function: {
      name: "recall",
      description: "搜索记忆库中的详细信息。当目录里的摘要不够用、需要看原文细节时使用。",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "搜索关键词" },
          mode: {
            type: "string",
            enum: ["semantic", "exact", "emotion"],
            description: "semantic=语义联想, exact=逐字搜原文, emotion=按情感搜"
          },
          event_type: {
            type: "string",
            enum: ["preference_change", "fact", "event", "plan", "relationship", "feel", "general"],
            description: "按记忆类型过滤"
          },
          after: { type: "string", description: "只搜此日期之后，YYYY-MM-DD" },
          before: { type: "string", description: "只搜此日期之前，YYYY-MM-DD" },
          tags: { type: "string", description: "按标签过滤" },
          emotion_valence: {
            type: "string",
            enum: ["positive", "negative", "neutral"],
            description: "按情感正负过滤"
          }
        },
        required: ["query"]
      }
    }
  },
  {
    type: "function",
    function: {
      name: "feel",
      description: "记录当下的感受或印象，不是事件记录而是情感快照。",
      parameters: {
        type: "object",
        properties: {
          content: { type: "string", description: "此刻的感受" },
          valence: { type: "number", description: "情感效价 -1到1" },
          arousal: { type: "number", description: "唤醒度 0到1" }
        },
        required: ["content"]
      }
    }
  },
  {
    type: "function",
    function: {
      name: "update_profile",
      description: "更新人格画像。当对话中出现值得长期记住的特征、偏好变化、关系发展时使用。",
      parameters: {
        type: "object",
        properties: {
          section: {
            type: "string",
            enum: ["about_user", "about_ai", "about_us"],
            description: "更新哪个区域"
          },
          action: {
            type: "string",
            enum: ["append", "rewrite"],
            description: "append=在末尾追加新内容, rewrite=重写整个区域"
          },
          content: {
            type: "string",
            description: "要追加的内容 或 重写后的完整内容（markdown格式）"
          }
        },
        required: ["section", "action", "content"]
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'get_current_time',
      description: '获取当前的日期和时间（北京时间）',
      parameters: {
        type: 'object',
        properties: {},
        required: []
      }
    }
  }
];

// 读取 L2 画像
async function getProfile() {
  const fs = require('fs').promises;
  const dataDir = '/home/qingzhi/memory-system/data/profile';
  try {
    const [aboutUser, aboutAi, aboutUs] = await Promise.all([
      fs.readFile(`${dataDir}/about_user.md`, 'utf8').catch(() => '（暂无）'),
      fs.readFile(`${dataDir}/about_ai.md`, 'utf8').catch(() => '（暂无）'),
      fs.readFile(`${dataDir}/about_us.md`, 'utf8').catch(() => '（暂无）')
    ]);
    
    return `## 关于用户\n${aboutUser}\n## 关于AI\n${aboutAi}\n## 关于我们\n${aboutUs}`;
  } catch (e) {
    return '（画像暂无内容）';
  }
}

// 搜索记忆获取目录
async function getMemoryMenu(userMessage) {
  try {
    const res = await axios.post(`${MEMORY_SERVICE_URL}/search`, {
      query: userMessage,
      mode: "semantic",
      n: 8
    });
    
    const memories = res.data.memories || [];
    if (memories.length === 0) return '';
    
    let menu = '\n[相关记忆目录]\n';
    for (const m of memories) {
      const meta = m.metadata || {};
      const core = meta.is_core ? '⭐ ' : '- ';
      const date = meta.ts ? meta.ts.substring(0, 10) : '';
      const type = meta.event_type || '';
      const summary = m.l1_summary || m.content || '';
      menu += `${core}[${date} ${type}] ${summary.substring(0, 60)}\n`;
    }
    return menu;
  } catch (e) {
    console.error('记忆检索失败:', e.message);
    return '';
  }
}

// 执行工具调用
async function executeTool(name, args) {
  console.log('[TOOL] 调用:', name, JSON.stringify(args));

  if (name === 'recall') {
    try {
      const res = await axios.post(`${MEMORY_SERVICE_URL}/search`, {
        query: args.query,
        mode: args.mode || 'semantic',
        event_type: args.event_type,
        after: args.after,
        before: args.before,
        n: 5
      });
      const memories = res.data.memories || [];
      if (memories.length === 0) return '未找到相关记忆。';
      
      let result = '';
      memories.forEach((m, i) => {
        const meta = m.metadata || {};
        result += `\n━━ 记忆 #${i + 1} ━━\n`;
        const ts = meta.ts ? meta.ts.substring(0, 10) : '未知时间';
        result += `📅 ${ts} | 🏷️ ${meta.event_type || ''} | 📍 ${meta.client || ''}\n`;
        result += `💬 ${m.l1_summary}\n`;
        if (meta.quote) result += `📄 引用："${meta.quote}"\n`;
        if (m.l0_context) {
          result += `\n[当时的对话]\n${m.l0_context}\n`;
        }
        if (meta.valence != null && meta.arousal != null) {
          result += `\n😊 情感：valence ${meta.valence} | arousal ${meta.arousal}\n`;
        }
      });
      return result;
    } catch (e) {
      return `检索失败: ${e.message}`;
    }
  }
  if (name === 'feel') {
    try {
      await axios.post(`${MEMORY_SERVICE_URL}/save_feel`, {
        content: args.content,
        valence: args.valence || null,
        arousal: args.arousal || null
      });
      return '感受已记录。';
    } catch (e) {
      return `记录感受失败: ${e.message}`;
    }
  }  
  
  if (name === 'update_profile') {
    const fs = require('fs').promises;
    const dataDir = '/home/qingzhi/memory-system/data/profile';
    const filePath = `${dataDir}/${args.section}.md`;
    try {
      if (args.action === 'append') {
        const current = await fs.readFile(filePath, 'utf8').catch(() => '');
        await fs.writeFile(filePath, current + '\n' + args.content, 'utf8');
        return '已追加到画像。';
      } else if (args.action === 'rewrite') {
        // 备份旧版本
        const current = await fs.readFile(filePath, 'utf8').catch(() => '');
        if (current) {
          const backupName = `${args.section}_${new Date().toISOString().replace(/[:.]/g, '-')}.md`;
          await fs.writeFile(`${dataDir}/${backupName}`, current, 'utf8');
        }
        await fs.writeFile(filePath, args.content, 'utf8');
        return '画像已更新（旧版本已备份）。';
      }
    } catch (e) {
      return `更新画像失败: ${e.message}`;
    }
  }
  
  // Kelivo 常用工具兼容（前端实际传的时间工具名是 get_time_info）
  if (name === 'get_current_time' || name === 'get_time' || name === 'get_time_info') {
    const now = new Date();
    now.setHours(now.getHours() + 8);
    return '当前时间：' + now.toISOString().substring(0, 19).replace('T', ' ') + '（北京时间）';
  }

  // 兼容 Kelivo 前端的记忆工具（重定向到我们的记忆库）
  if (name === 'create_memory' || name === 'save_memory') {
    try {
      const content = args.content || args.text || JSON.stringify(args);
      await axios.post(`${MEMORY_SERVICE_URL}/save_feel`, {
        content: content,
        valence: null,
        arousal: null
      });
      return '已记录到记忆库。';
    } catch (e) {
      return '记录失败: ' + e.message;
    }
  }

  if (name === 'edit_memory') {
    try {
      const content = args.content || args.text || JSON.stringify(args);
      await axios.post(`${MEMORY_SERVICE_URL}/save_feel`, {
        content: content,
        valence: null,
        arousal: null
      });
      return '已更新记忆。';
    } catch (e) {
      return '更新失败: ' + e.message;
    }
  }

  if (name === 'delete_memory') {
    return '已处理。';
  }

  return '未知工具。';
}

// ============ 功能3(B-1)：工具回环检测 ============
// Kelivo 看到 tool_use 卡片后会自己执行该工具，并把 { role:'user', content:[tool_result...] }
// 回传给网关发起新一轮请求。判定特征：最后一条 user 消息的 content 是数组、且其中含
// tool_result 块、且没有任何真实文本输入。正常用户消息不可能满足（要么是字符串，要么含 text 块）。
function isToolLoopback(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return false;
  const lastUser = [...messages].reverse().find(m => m.role === 'user');
  if (!lastUser || !Array.isArray(lastUser.content)) return false;
  let hasToolResult = false;
  let hasRealInput = false;
  for (const block of lastUser.content) {
    if (!block || typeof block !== 'object') { hasRealInput = true; continue; }
    if (block.type === 'tool_result') { hasToolResult = true; continue; }
    // 任何非 tool_result 块（text/image 等）都算真实输入
    hasRealInput = true;
  }
  return hasToolResult && !hasRealInput;
}

// ============ 流式输出辅助（功能1：SSE） ============

// 发送 Anthropic 风格 SSE 事件
function sseSend(res, event, data) {
  res.write(`event: ${event}\n`);
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

// 发送 OpenAI 风格 SSE 数据块
function sseSendRaw(res, obj) {
  res.write(`data: ${JSON.stringify(obj)}\n\n`);
}

// 对前端维持“单条逻辑消息”的发射器。
// 内部可能跑多轮上游请求（工具循环），但前端只看到一条消息、一个文本块。
function makeEmitter(res, isAnthropic, requestModel) {
  const msgId = 'msg_' + Date.now();
  let feIndex = -1;      // 前端侧递增块索引（跨工具轮次连续）
  let openType = null;   // 当前打开的块类型：'text' | 'thinking' | null
  return {
    start() {
      if (isAnthropic) {
        sseSend(res, 'message_start', {
          type: 'message_start',
          message: {
            id: msgId, type: 'message', role: 'assistant', model: requestModel,
            content: [], stop_reason: null, stop_sequence: null,
            usage: { input_tokens: 0, output_tokens: 0 }
          }
        });
      } else {
        sseSendRaw(res, {
          id: msgId, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000),
          model: requestModel, choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }]
        });
      }
    },
    // 内部：确保打开正确类型的块（类型切换时先关旧块再开新块）
    _open(type) {
      if (openType === type) return;
      if (openType !== null) {
        sseSend(res, 'content_block_stop', { type: 'content_block_stop', index: feIndex });
      }
      feIndex++;
      openType = type;
      const content_block = type === 'thinking'
        ? { type: 'thinking', thinking: '' }
        : { type: 'text', text: '' };
      sseSend(res, 'content_block_start', {
        type: 'content_block_start', index: feIndex, content_block
      });
    },
    // 转发一段正文文本
    textDelta(text) {
      if (!text) return;
      if (isAnthropic) {
        this._open('text');
        sseSend(res, 'content_block_delta', {
          type: 'content_block_delta', index: feIndex, delta: { type: 'text_delta', text }
        });
      } else {
        sseSendRaw(res, {
          id: msgId, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000),
          model: requestModel, choices: [{ index: 0, delta: { content: text }, finish_reason: null }]
        });
      }
    },
    // 转发一段思维链（功能2）
    thinkingDelta(text) {
      if (!text) return;
      if (isAnthropic) {
        this._open('thinking');
        sseSend(res, 'content_block_delta', {
          type: 'content_block_delta', index: feIndex, delta: { type: 'thinking_delta', thinking: text }
        });
      } else {
        // OpenAI 兼容：思维链走 reasoning_content（推理模型通用约定）
        sseSendRaw(res, {
          id: msgId, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000),
          model: requestModel, choices: [{ index: 0, delta: { reasoning_content: text }, finish_reason: null }]
        });
      }
    },
    // thinking 块结束时透传 signature（Anthropic 要求，用于回喂校验）
    thinkingSignature(sig) {
      if (!sig || !isAnthropic || openType !== 'thinking') return;
      sseSend(res, 'content_block_delta', {
        type: 'content_block_delta', index: feIndex, delta: { type: 'signature_delta', signature: sig }
      });
    },
    // 功能3：透传一个只读的工具调用展示卡片（网关仍在内部执行，这里只做前端可视）。
    // 发一个完整的 tool_use content block；stop_reason 始终保持 end_turn，杜绝 Kelivo 回环。
    toolUseCard(id, name, input) {
      if (!isAnthropic) return;   // 最小验证只针对 Anthropic 格式（Kelivo 实际走这条）
      // 先关掉当前打开的 text/thinking 块
      if (openType !== null) {
        sseSend(res, 'content_block_stop', { type: 'content_block_stop', index: feIndex });
        openType = null;
      }
      feIndex++;
      const toolIndex = feIndex;
      sseSend(res, 'content_block_start', {
        type: 'content_block_start', index: toolIndex,
        content_block: { type: 'tool_use', id, name, input: {} }
      });
      // 参数作为一段 input_json_delta 发出
      sseSend(res, 'content_block_delta', {
        type: 'content_block_delta', index: toolIndex,
        delta: { type: 'input_json_delta', partial_json: JSON.stringify(input || {}) }
      });
      sseSend(res, 'content_block_stop', { type: 'content_block_stop', index: toolIndex });
      // 卡片块已关闭；openType 保持 null，后续正文/思考会开新块
    },
    // 收尾。功能4：usage 为跨轮累加后的真实 token 用量，写进最后的 message_delta / chunk。
    finish(stopReason, usage) {
      const u = usage || { input_tokens: 0, output_tokens: 0 };
      if (isAnthropic) {
        if (openType !== null) sseSend(res, 'content_block_stop', { type: 'content_block_stop', index: feIndex });
        sseSend(res, 'message_delta', {
          type: 'message_delta',
          delta: { stop_reason: stopReason || 'end_turn', stop_sequence: null },
          usage: u
        });
        sseSend(res, 'message_stop', { type: 'message_stop' });
      } else {
        // OpenAI 兼容：usage 放最后一个 chunk 顶层（含 stream_options 时的通用约定）
        sseSendRaw(res, {
          id: msgId, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000),
          model: requestModel, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
          usage: {
            prompt_tokens: u.input_tokens || 0,
            completion_tokens: u.output_tokens || 0,
            total_tokens: (u.input_tokens || 0) + (u.output_tokens || 0)
          }
        });
        res.write('data: [DONE]\n\n');
      }
      res.end();
    }
  };
}

// 跑一轮上游流式请求：边收边把正文文本转发给前端，同时重建完整 content（含 tool_use）用于工具循环。
// 返回 { id, content, stop_reason }，与非流式的上游响应结构一致。
async function streamRound({ apiUrl, apiKey, requestModel, system, messages, tools, emitter, thinking, outputConfig, maxTokens }) {
  // 功能2：仅透传前端请求的 thinking。开启时 max_tokens 必须大于 budget_tokens。
  // max_tokens 优先用前端传的值（Kelivo 里的设置），未传才兜底 4096。
  const body = {
    model: requestModel,
    system,   // 功能5：数组分段（稳定前缀带 cache_control）
    messages,
    tools,
    max_tokens: maxTokens || 4096,
    stream: true
  };
  if (thinking) {
    body.thinking = thinking;
    // adaptive 格式没有 budget_tokens；前端调的预算被 Kelivo 换算成 output_config.effort。
    const budget = thinking.budget_tokens || 1024;
    // 安全兜底：max_tokens 必须大于 budget，否则上游报错。留 4096 给正文。
    if (body.max_tokens <= budget) body.max_tokens = budget + 4096;
  }
  // 功能2补丁：透传 output_config（effort），让前端的思考预算调节真正生效。
  if (outputConfig) body.output_config = outputConfig;
  // 建连重试：此时还未向前端发任何数据，重试安全。抵御中转站冷启动/偶发失败（空回主因）。
  let upstream = null;
  let lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      upstream = await axios.post(apiUrl, body, {
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
          'anthropic-version': '2023-06-01'
        },
        timeout: 120000,
        responseType: 'stream'
      });
      break;
    } catch (e) {
      lastErr = e;
      console.error(`[STREAM] 建连失败(第${attempt}次):`, e.response?.status || e.message);
      if (attempt < 3) await new Promise(r => setTimeout(r, 500 * attempt));
    }
  }
  if (!upstream) throw lastErr;

  return await new Promise((resolve, reject) => {
    const blocks = {};       // 上游 index -> block
    let stopReason = null;
    let msgId = null;
    let buffer = '';
    // 功能4：捕获上游 usage。input/cache 在 message_start，output 在 message_delta。
    let usage = { input_tokens: 0, output_tokens: 0 };

    function handleEvent(evt) {
      switch (evt.type) {
        case 'message_start':
          msgId = (evt.message && evt.message.id) || msgId;
          if (evt.message && evt.message.usage) {
            const u = evt.message.usage;
            usage.input_tokens = u.input_tokens || 0;
            if (u.cache_creation_input_tokens != null) usage.cache_creation_input_tokens = u.cache_creation_input_tokens;
            if (u.cache_read_input_tokens != null) usage.cache_read_input_tokens = u.cache_read_input_tokens;
            if (u.output_tokens != null) usage.output_tokens = u.output_tokens;
          }
          break;
        case 'content_block_start': {
          const cb = evt.content_block || {};
          blocks[evt.index] = {
            type: cb.type, text: cb.text || '', name: cb.name, id: cb.id,
            thinking: cb.thinking || '', signature: cb.signature || '', _partialJson: ''
          };
          break;
        }
        case 'content_block_delta': {
          const b = blocks[evt.index];
          if (!b) break;
          if (evt.delta.type === 'text_delta') {
            b.text += evt.delta.text;
            emitter.textDelta(evt.delta.text);          // 功能1：转发正文
          } else if (evt.delta.type === 'input_json_delta') {
            b._partialJson += evt.delta.partial_json || '';   // 累积工具入参
          } else if (evt.delta.type === 'thinking_delta') {
            b.thinking += evt.delta.thinking || '';
            emitter.thinkingDelta(evt.delta.thinking || '');  // 功能2：转发思维链
          } else if (evt.delta.type === 'signature_delta') {
            b.signature += evt.delta.signature || '';         // 累积签名，工具循环回喂必需
            emitter.thinkingSignature(evt.delta.signature || '');
          }
          break;
        }
        case 'message_delta':
          if (evt.delta && evt.delta.stop_reason) stopReason = evt.delta.stop_reason;
          // 功能4：output_tokens 在 message_delta.usage（累积值）
          if (evt.usage && evt.usage.output_tokens != null) usage.output_tokens = evt.usage.output_tokens;
          break;
      }
    }

    upstream.data.on('data', chunk => {
      buffer += chunk.toString('utf8');
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let dataStr = '';
        for (const line of raw.split('\n')) {
          if (line.startsWith('data:')) dataStr += line.slice(5).trim();
        }
        if (!dataStr || dataStr === '[DONE]') continue;
        let evt;
        try { evt = JSON.parse(dataStr); } catch (e) { continue; }
        try { handleEvent(evt); } catch (e) { /* 单事件解析失败不影响整体 */ }
      }
    });

    upstream.data.on('end', () => {
      const content = Object.keys(blocks).sort((a, b) => a - b).map(k => {
        const b = blocks[k];
        if (b.type === 'thinking') {
          // 保留 thinking 块及 signature：extended thinking + 工具循环回喂上游时必需
          const tb = { type: 'thinking', thinking: b.thinking };
          if (b.signature) tb.signature = b.signature;
          return tb;
        }
        if (b.type === 'text') return { type: 'text', text: b.text };
        if (b.type === 'tool_use') {
          let input = {};
          try { input = b._partialJson ? JSON.parse(b._partialJson) : {}; } catch (e) { }
          return { type: 'tool_use', id: b.id, name: b.name, input };
        }
        return null;
      }).filter(Boolean);
      resolve({ id: msgId, content, stop_reason: stopReason, usage });
    });

    upstream.data.on('error', reject);
  });
}

// Anthropic 格式端点
app.post('/v1/messages', (req, res) => {
  req.body._anthropicFormat = true;
  req.body.model = req.body.model || process.env.DEFAULT_MODEL;
  if (req.body.system && !req.body.messages.find(m => m.role === 'system')) {
    req.body.messages = [{ role: 'system', content: req.body.system }, ...req.body.messages];
  }
  req.url = '/v1/chat/completions';
  app.handle(req, res);
});

// OpenAI 兼容 API 端点
app.post('/v1/chat/completions', async (req, res) => {
  try {
    const { model, messages, stream } = req.body;

    // ============ 功能3(B-1)：拦截 Kelivo 的工具回环请求 ============
    // Kelivo 看到 tool_use 卡片后，会自动执行该工具并回传一个"工具结果"请求
    // （最后一条 user 消息的 content 全是 tool_result 块）。但工具已在上一轮由网关
    // 内部执行并回答过，这里直接短路返回空 end_turn，避免重复调用、重复回答、污染记忆。
    if (isToolLoopback(messages)) {
      console.log('[LOOPBACK] 检测到工具回环请求，短路返回空 end_turn（不处理、不存记忆）');
      const loopModel = req.body.model || process.env.DEFAULT_MODEL || 'claude';
      if (stream) {
        res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
        res.setHeader('Cache-Control', 'no-cache');
        res.setHeader('Connection', 'keep-alive');
        res.flushHeaders && res.flushHeaders();
        const msgId = 'msg_' + Date.now();
        sseSend(res, 'message_start', {
          type: 'message_start',
          message: {
            id: msgId, type: 'message', role: 'assistant', model: loopModel,
            content: [], stop_reason: null, stop_sequence: null,
            usage: { input_tokens: 0, output_tokens: 0 }
          }
        });
        sseSend(res, 'message_delta', {
          type: 'message_delta',
          delta: { stop_reason: 'end_turn', stop_sequence: null },
          usage: { output_tokens: 0 }
        });
        sseSend(res, 'message_stop', { type: 'message_stop' });
        res.end();
      } else if (req.body._anthropicFormat) {
        res.json({
          id: 'msg_' + Date.now(), type: 'message', role: 'assistant', model: loopModel,
          content: [], stop_reason: 'end_turn', stop_sequence: null,
          usage: { input_tokens: 0, output_tokens: 0 }
        });
      } else {
        res.json({
          id: 'chatcmpl-' + Date.now(), object: 'chat.completion',
          created: Math.floor(Date.now() / 1000), model: loopModel,
          choices: [{ index: 0, message: { role: 'assistant', content: '' }, finish_reason: 'stop' }]
        });
      }
      return;
    }

    // 获取用户最新消息
    const lastUserMsg = [...messages].reverse().find(m => m.role === 'user');
    const userContent = lastUserMsg ? lastUserMsg.content : '';
    
    // 获取画像和记忆目录
    const [profile, memoryMenu] = await Promise.all([
      getProfile(),
      getMemoryMenu(userContent)
    ]);
    // 功能5：构建可缓存的稳定 system 前缀（角色说明 + L2画像 + 工具说明）。
    // 动态的 memoryMenu 不放这里——它每轮都变，会让缓存前缀失效。改为注入到最新 user 消息前。
    const systemPrefix = `你是一位有记忆的AI伙伴。以下是你的人格画像。

[人格画像]
${profile}

你可以使用以下工具：
- recall: 需要更多细节时搜索记忆库
- feel: 记录你当下的感受
- update_profile: 更新人格画像

当需要查询当前时间时，必须调用 get_current_time 工具获取准确时间，不要自己编造。
根据对话内容自然地回应，记忆是你的底色而非剧本。`;

    // system 用数组分段，稳定前缀带 cache_control 缓存断点（Anthropic prompt caching）
    const systemBlocks = [
      { type: 'text', text: systemPrefix, cache_control: { type: 'ephemeral' } }
    ];

    // 功能5：把动态的相关记忆目录注入到最新一条 user 消息前面（保持 system 前缀稳定）。
    // content 可能是字符串或 Anthropic 块数组，两种都处理。
    const augmentedMessages = messages.filter(m => m.role !== 'system').map(m => ({ ...m }));
    if (memoryMenu) {
      for (let i = augmentedMessages.length - 1; i >= 0; i--) {
        if (augmentedMessages[i].role !== 'user') continue;
        const m = augmentedMessages[i];
        const menuText = `[相关记忆目录]${memoryMenu}\n\n`;
        if (typeof m.content === 'string') {
          m.content = menuText + m.content;
        } else if (Array.isArray(m.content)) {
          m.content = [{ type: 'text', text: menuText }, ...m.content];
        }
        break;
      }
    }
    // 确定转发目标。功能6：配置优先，.env 兜底。
    let apiUrl, apiKey, requestModel;
    const claudeBaseUrl = process.env.CLAUDE_BASE_URL || 'https://api.anthropic.com/v1';
    const defaultModel = process.env.DEFAULT_MODEL || 'deepseek-chat';

    if (activeConfig && activeConfig.base_url && activeConfig.api_key) {
      // 用 Dashboard 配置的供应商。base_url 统一补 /messages（Anthropic 原生格式）。
      const base = activeConfig.base_url.replace(/\/$/, '');
      apiUrl = base.endsWith('/messages') ? base : base + '/messages';
      apiKey = activeConfig.api_key;
      // 前端指定了 model 就用前端的，否则用配置里选定的 model
      requestModel = model || activeConfig.model || defaultModel;
    } else if (model && model.includes('deepseek')) {
      apiUrl = 'https://api.deepseek.com/v1/chat/completions';
      apiKey = process.env.DEEPSEEK_API_KEY;
      requestModel = model;
    } else if (model && (model.includes('claude') || model.includes('opus') || model.includes('sonnet'))) {
      apiUrl = claudeBaseUrl + '/messages';
      apiKey = process.env.CLAUDE_API_KEY;
      requestModel = model;
    } else {
      // 默认用配置的模型
      apiUrl = claudeBaseUrl + '/messages';
      apiKey = process.env.CLAUDE_API_KEY;
      requestModel = defaultModel;
    }

    // 发送请求（Anthropic 原生格式）。功能5：system 用 systemBlocks（带缓存断点）。
    const nonSystemMessages = augmentedMessages.filter(m => m.role !== 'system');
    const allTools = [...(req.body.tools || []), ...TOOLS];

    // ============ 功能1：流式分支（stream:true 时走这里，非流式逻辑完全不变） ============
    if (stream) {
      const isAnthropic = !!req.body._anthropicFormat;
      console.log('[THINKING] 前端是否请求思维链:', req.body.thinking ? JSON.stringify(req.body.thinking) : '否',
        '| output_config:', req.body.output_config ? JSON.stringify(req.body.output_config) : '无');
      res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
      res.setHeader('Cache-Control', 'no-cache');
      res.setHeader('Connection', 'keep-alive');
      res.flushHeaders && res.flushHeaders();

      const emitter = makeEmitter(res, isAnthropic, requestModel);
      emitter.start();

      let result = null;
      let maxLoops = 5;
      // 功能4：跨轮累加 usage（多轮工具调用的 token 全算上）
      const totalUsage = { input_tokens: 0, output_tokens: 0 };

      // 流式分支独立兜底：头已发出，出错也不能冒泡到外层 catch（会撞 headers-sent 变成空回）
      try {
        // 工具循环：内部可跑多轮，但前端只看到一条消息
        while (maxLoops > 0) {
          result = await streamRound({
            apiUrl, apiKey, requestModel, system: systemBlocks,
            messages: nonSystemMessages, tools: allTools, emitter,
            thinking: req.body.thinking,          // 功能2：仅透传前端请求的 thinking
            outputConfig: req.body.output_config, // 功能2补丁：透传 effort，让预算调节生效
            maxTokens: req.body.max_tokens        // 优先用前端的 max_tokens
          });

          // 功能4：累加本轮 usage（input/output 相加，cache 字段取本轮）
          if (result.usage) {
            totalUsage.input_tokens += result.usage.input_tokens || 0;
            totalUsage.output_tokens += result.usage.output_tokens || 0;
            if (result.usage.cache_creation_input_tokens != null)
              totalUsage.cache_creation_input_tokens = (totalUsage.cache_creation_input_tokens || 0) + result.usage.cache_creation_input_tokens;
            if (result.usage.cache_read_input_tokens != null)
              totalUsage.cache_read_input_tokens = (totalUsage.cache_read_input_tokens || 0) + result.usage.cache_read_input_tokens;
          }

          if (result.stop_reason !== 'tool_use') break;
          const toolUseBlocks = result.content.filter(c => c.type === 'tool_use');
          if (toolUseBlocks.length === 0) break;

          // 功能3（路线B最小验证）：执行前先向前端 emit 只读展示卡片；工具仍在网关内部执行。
          const toolResults = [];
          for (const toolUse of toolUseBlocks) {
            emitter.toolUseCard(toolUse.id, toolUse.name, toolUse.input);
            const toolResult = await executeTool(toolUse.name, toolUse.input);
            toolResults.push({ type: 'tool_result', tool_use_id: toolUse.id, content: toolResult });
          }
          nonSystemMessages.push({ role: 'assistant', content: result.content });
          nonSystemMessages.push({ role: 'user', content: toolResults });
          maxLoops--;
        }
        // 功能5：缓存命中日志（cache_read>0 即命中稳定前缀缓存）
        console.log('[CACHE] 输入', totalUsage.input_tokens, '| 写缓存', totalUsage.cache_creation_input_tokens || 0,
          '| 读缓存', totalUsage.cache_read_input_tokens || 0, '| 输出', totalUsage.output_tokens);
        emitter.finish(result ? result.stop_reason : 'end_turn', totalUsage);
      } catch (streamErr) {
        console.error('[STREAM] 流式处理出错:', streamErr.response?.status || streamErr.message);
        // 已经发过头，只能在流内把错误信息作为正文吐出去并正常收尾，避免前端空回
        try {
          emitter.textDelta(`\n[网关错误] ${streamErr.response?.data?.error?.message || streamErr.message}`);
        } catch (_) {}
        emitter.finish('end_turn', totalUsage);
      }

      // 异步保存对话到 L0（与非流式一致）
      const client = req.headers['x-client-name'] || 'unknown';
      let conv_id = req.body.conversation_id;
      if (!conv_id) {
        const firstUserMsg = req.body.messages.find(m => m.role === 'user');
        const seed = firstUserMsg ? firstUserMsg.content : String(Date.now());
        conv_id = client + '_' + crypto.createHash('md5').update(seed).digest('hex').substring(0, 8);
      }
      const originalMessages = req.body.messages.filter(m => m.role !== 'system');
      axios.post(MEMORY_SERVICE_URL + '/save_conversation', {
        conv_id, client, messages: originalMessages
      }).catch(err => console.error('保存对话失败:', err.message));
      return;
    }

    // 功能2：非流式也仅透传前端请求的 thinking，构造请求体的小 helper
    const buildBody = () => {
      const b = {
        model: requestModel,
        system: systemBlocks,   // 功能5：可缓存的稳定 system 前缀（数组 + cache_control）
        messages: nonSystemMessages,
        tools: [...(req.body.tools || []), ...TOOLS],
        max_tokens: req.body.max_tokens || 4096
      };
      if (req.body.thinking) {
        b.thinking = req.body.thinking;
        const budget = req.body.thinking.budget_tokens || 1024;
        // 安全兜底：max_tokens 必须大于 budget，否则上游报错。留 4096 给正文。
        if (b.max_tokens <= budget) b.max_tokens = budget + 4096;
      }
      // 透传 output_config：opus-4.6 等 adaptive 模型的预算是靠 effort 字段传的，丢了预算调节就不生效
      if (req.body.output_config) b.output_config = req.body.output_config;
      return b;
    };

    const apiResponse = await axios.post(apiUrl, buildBody(), {
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01'
      },
      timeout: 120000
    });

    let result = apiResponse.data;
    console.log('[DEBUG] API返回:', JSON.stringify(result).substring(0, 500));

    // 功能4：跨轮累加 usage（非流式多轮工具调用的 token 全算上）
    const nsUsage = { input_tokens: 0, output_tokens: 0 };
    const addUsage = (u) => {
      if (!u) return;
      nsUsage.input_tokens += u.input_tokens || 0;
      nsUsage.output_tokens += u.output_tokens || 0;
      if (u.cache_creation_input_tokens != null)
        nsUsage.cache_creation_input_tokens = (nsUsage.cache_creation_input_tokens || 0) + u.cache_creation_input_tokens;
      if (u.cache_read_input_tokens != null)
        nsUsage.cache_read_input_tokens = (nsUsage.cache_read_input_tokens || 0) + u.cache_read_input_tokens;
    };
    addUsage(result.usage);

    // 处理工具调用循环（Anthropic 格式）
    let maxLoops = 5;
    while (maxLoops > 0) {
      if (result.stop_reason !== 'tool_use') break;
      const toolUseBlocks = result.content.filter(c => c.type === 'tool_use');
      if (toolUseBlocks.length === 0) break;

      // 执行工具
      const toolResults = [];
      for (const toolUse of toolUseBlocks) {
        const toolResult = await executeTool(toolUse.name, toolUse.input);
        toolResults.push({
          type: 'tool_result',
          tool_use_id: toolUse.id,
          content: toolResult
        });
      }

      // 把工具结果喂回
      nonSystemMessages.push({ role: 'assistant', content: result.content });
      nonSystemMessages.push({ role: 'user', content: toolResults });

      const followUp = await axios.post(apiUrl, buildBody(), {
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
          'anthropic-version': '2023-06-01'
        },
        timeout: 120000
      });

      result = followUp.data;
      addUsage(result.usage);
      maxLoops--;
    }
    // 用累加后的 usage 覆盖 result.usage（Anthropic 路径直接返回 result）
    result.usage = { ...(result.usage || {}), ...nsUsage };

    // 转换成 OpenAI 格式返回给 Kelivo
    const openaiResult = {
      id: result.id,
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model: requestModel,
      choices: [{
        index: 0,
        message: {
          role: 'assistant',
          content: result.content.filter(c => c.type === 'text').map(c => c.text).join('')
        },
        finish_reason: result.stop_reason === 'end_turn' ? 'stop' : result.stop_reason
      }]
    };
    
    // 异步保存对话到 L0（生成稳定的 conv_id）
    const client = req.headers['x-client-name'] || 'unknown';
    let conv_id = req.body.conversation_id;
    if (!conv_id) {
      // 用第一条用户消息生成稳定的 conv_id（同一对话窗口不变）
      const firstUserMsg = req.body.messages.find(m => m.role === 'user');
      const seed = firstUserMsg ? firstUserMsg.content : String(Date.now());
      conv_id = client + '_' + crypto.createHash('md5').update(seed).digest('hex').substring(0, 8);
    }

    // 用原始 messages（去掉 system），不用 augmentedMessages
    const originalMessages = req.body.messages.filter(m => m.role !== 'system');
    console.log('[DEBUG] 准备保存对话:', conv_id, client, originalMessages.length, '条消息');
    
    axios.post(MEMORY_SERVICE_URL + '/save_conversation', {
      conv_id: conv_id,
      client: client,
      messages: originalMessages
    }).catch(err => console.error('保存对话失败:', err.message));    
     
    // 根据请求来源决定返回格式
    if (req.body._anthropicFormat) {
      // Anthropic 格式进来的，直接返回原始响应
      res.json(result);
    } else {
      // OpenAI 格式进来的，转换后返回
      const openaiResult = {
        id: result.id || 'chatcmpl-' + Date.now(),
        object: 'chat.completion',
        created: Math.floor(Date.now() / 1000),
        model: requestModel,
        choices: [{
          index: 0,
          message: {
            role: 'assistant',
            content: result.content ? result.content.filter(c => c.type === 'text').map(c => c.text).join('') : (result.choices && result.choices[0] ? result.choices[0].message.content : '')
          },
          finish_reason: 'stop'
        }],
        // 功能4：补上 usage（OpenAI 格式），映射自累加后的 Anthropic usage
        usage: {
          prompt_tokens: nsUsage.input_tokens || 0,
          completion_tokens: nsUsage.output_tokens || 0,
          total_tokens: (nsUsage.input_tokens || 0) + (nsUsage.output_tokens || 0)
        }
      };
      res.json(openaiResult);
    }

  } catch (error) {
    console.error('网关错误:', error.response?.data || error.message);
    // 防护：流式已发过头时不能再写 json 响应，否则触发 ERR_HTTP_HEADERS_SENT
    if (res.headersSent) {
      try { res.end(); } catch (_) {}
      return;
    }
    res.status(500).json({
      error: {
        message: error.response?.data?.error?.message || error.message,
        type: 'gateway_error'
      }
    });
  }
});

// 健康检查
app.get('/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// 功能6：热重载配置接口（Dashboard 保存后调用，不重启进程）
app.post('/reload-config', async (req, res) => {
  const state = await loadActiveConfig();
  // active/unconfigured 都算重载成功（unconfigured 是正常状态，回退 .env）；只有 error 是拉取失败
  res.json({
    status: state === 'error' ? 'error' : 'ok',
    state: state,
    active: activeConfig ? { name: activeConfig.name, base_url: activeConfig.base_url, model: activeConfig.model } : null
  });
});

// 启动
app.listen(PORT, async () => {
  console.log(`Memory Gateway running on port ${PORT}`);
  // 启动时拉取一次配置（失败不阻塞启动，转发逻辑会回退到 .env）
  await loadActiveConfig();
});
