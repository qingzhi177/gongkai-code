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
  let textStarted = false;
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
    // 转发一段正文文本
    textDelta(text) {
      if (!text) return;
      if (isAnthropic) {
        if (!textStarted) {
          sseSend(res, 'content_block_start', {
            type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' }
          });
          textStarted = true;
        }
        sseSend(res, 'content_block_delta', {
          type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text }
        });
      } else {
        sseSendRaw(res, {
          id: msgId, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000),
          model: requestModel, choices: [{ index: 0, delta: { content: text }, finish_reason: null }]
        });
      }
    },
    // 收尾
    finish(stopReason) {
      if (isAnthropic) {
        if (textStarted) sseSend(res, 'content_block_stop', { type: 'content_block_stop', index: 0 });
        sseSend(res, 'message_delta', {
          type: 'message_delta',
          delta: { stop_reason: stopReason || 'end_turn', stop_sequence: null },
          usage: { output_tokens: 0 }
        });
        sseSend(res, 'message_stop', { type: 'message_stop' });
      } else {
        sseSendRaw(res, {
          id: msgId, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000),
          model: requestModel, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }]
        });
        res.write('data: [DONE]\n\n');
      }
      res.end();
    }
  };
}

// 跑一轮上游流式请求：边收边把正文文本转发给前端，同时重建完整 content（含 tool_use）用于工具循环。
// 返回 { id, content, stop_reason }，与非流式的上游响应结构一致。
async function streamRound({ apiUrl, apiKey, requestModel, systemContent, messages, tools, emitter }) {
  const upstream = await axios.post(apiUrl, {
    model: requestModel,
    system: systemContent,
    messages,
    tools,
    max_tokens: 4096,
    stream: true
  }, {
    headers: {
      'Authorization': `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
      'anthropic-version': '2023-06-01'
    },
    timeout: 120000,
    responseType: 'stream'
  });

  return await new Promise((resolve, reject) => {
    const blocks = {};       // 上游 index -> block
    let stopReason = null;
    let msgId = null;
    let buffer = '';

    function handleEvent(evt) {
      switch (evt.type) {
        case 'message_start':
          msgId = (evt.message && evt.message.id) || msgId;
          break;
        case 'content_block_start': {
          const cb = evt.content_block || {};
          blocks[evt.index] = {
            type: cb.type, text: cb.text || '', name: cb.name, id: cb.id, _partialJson: ''
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
          }
          // thinking_delta 暂不处理（留给功能2）
          break;
        }
        case 'message_delta':
          if (evt.delta && evt.delta.stop_reason) stopReason = evt.delta.stop_reason;
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
        if (b.type === 'text') return { type: 'text', text: b.text };
        if (b.type === 'tool_use') {
          let input = {};
          try { input = b._partialJson ? JSON.parse(b._partialJson) : {}; } catch (e) { }
          return { type: 'tool_use', id: b.id, name: b.name, input };
        }
        return null;
      }).filter(Boolean);
      resolve({ id: msgId, content, stop_reason: stopReason });
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
    // 获取用户最新消息
    const lastUserMsg = [...messages].reverse().find(m => m.role === 'user');
    const userContent = lastUserMsg ? lastUserMsg.content : '';
    
    // 获取画像和记忆目录
    const [profile, memoryMenu] = await Promise.all([
      getProfile(),
      getMemoryMenu(userContent)
    ]);
    // 构建 system prompt
    const systemPrompt = `你是一位有记忆的AI伙伴。以下是你的人格画像和相关记忆。

[人格画像]
${profile}
${memoryMenu}

你可以使用以下工具：
- recall: 需要更多细节时搜索记忆库
- feel: 记录你当下的感受
- update_profile: 更新人格画像

当需要查询当前时间时，必须调用 get_current_time 工具获取准确时间，不要自己编造。
根据对话内容自然地回应，记忆是你的底色而非剧本。`;

    // 注入 system prompt
    const augmentedMessages = [
      { role: 'system', content: systemPrompt },
      ...messages.filter(m => m.role !== 'system')
    ];
    // 确定转发目标
    let apiUrl, apiKey, requestModel;
    const claudeBaseUrl = process.env.CLAUDE_BASE_URL || 'https://api.anthropic.com/v1';
    const defaultModel = process.env.DEFAULT_MODEL || 'deepseek-chat';

    if (model && model.includes('deepseek')) {
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

    // 发送请求（Anthropic 原生格式）
    const systemMessage = augmentedMessages.find(m => m.role === 'system');
    const nonSystemMessages = augmentedMessages.filter(m => m.role !== 'system');
    const allTools = [...(req.body.tools || []), ...TOOLS];

    // ============ 功能1：流式分支（stream:true 时走这里，非流式逻辑完全不变） ============
    if (stream) {
      const isAnthropic = !!req.body._anthropicFormat;
      res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
      res.setHeader('Cache-Control', 'no-cache');
      res.setHeader('Connection', 'keep-alive');
      res.flushHeaders && res.flushHeaders();

      const emitter = makeEmitter(res, isAnthropic, requestModel);
      emitter.start();

      const systemContent = systemMessage ? systemMessage.content : '';
      let result = null;
      let maxLoops = 5;

      // 工具循环：内部可跑多轮，但前端只看到一条消息
      while (maxLoops > 0) {
        result = await streamRound({
          apiUrl, apiKey, requestModel, systemContent,
          messages: nonSystemMessages, tools: allTools, emitter
        });

        if (result.stop_reason !== 'tool_use') break;
        const toolUseBlocks = result.content.filter(c => c.type === 'tool_use');
        if (toolUseBlocks.length === 0) break;

        // 本地静默执行工具（功能1不向前端展示，留给功能3）
        const toolResults = [];
        for (const toolUse of toolUseBlocks) {
          const toolResult = await executeTool(toolUse.name, toolUse.input);
          toolResults.push({ type: 'tool_result', tool_use_id: toolUse.id, content: toolResult });
        }
        nonSystemMessages.push({ role: 'assistant', content: result.content });
        nonSystemMessages.push({ role: 'user', content: toolResults });
        maxLoops--;
      }

      emitter.finish(result ? result.stop_reason : 'end_turn');

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

    const apiResponse = await axios.post(apiUrl, {
      model: requestModel,
      system: systemMessage ? systemMessage.content : '',
      messages: nonSystemMessages,
      tools: [...(req.body.tools || []), ...TOOLS],
      max_tokens: 4096
    }, {
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01'
      },
      timeout: 120000
    });

    let result = apiResponse.data;
    console.log('[DEBUG] API返回:', JSON.stringify(result).substring(0, 500));

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

      const followUp = await axios.post(apiUrl, {
        model: requestModel,
        system: systemMessage ? systemMessage.content : '',
        messages: nonSystemMessages,
        tools: [...(req.body.tools || []), ...TOOLS],
        max_tokens: 4096
      }, {
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
          'anthropic-version': '2023-06-01'
        },
        timeout: 120000
      });

      result = followUp.data;
      maxLoops--;
    }

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
        }]
      };
      res.json(openaiResult);
    }
    
  } catch (error) {
    console.error('网关错误:', error.response?.data || error.message);
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

// 启动
app.listen(PORT, () => {
  console.log(`Memory Gateway running on port ${PORT}`);
});
