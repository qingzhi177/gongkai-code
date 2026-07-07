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
  
  // Kelivo 常用工具兼容
  if (name === 'get_current_time' || name === 'get_time') {
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
    
    if (model && model.includes('deepseek')) {
      apiUrl = 'https://api.deepseek.com/v1/chat/completions';
      apiKey = process.env.DEEPSEEK_API_KEY;
      requestModel = model;
    } else if (model && model.includes('claude')) {
      apiUrl = 'https://api.anthropic.com/v1/messages';
      apiKey = process.env.CLAUDE_API_KEY;
      requestModel = model;
    } else {
      // 默认走 DepSeek
      apiUrl = 'https://api.deepseek.com/v1/chat/completions';
      apiKey = process.env.DEEPSEEK_API_KEY;
      requestModel = 'deepseek-chat';
    }
    
    // 发送请求（带工具定义）
    const apiResponse = await axios.post(apiUrl, {
      model: requestModel,
      messages: augmentedMessages,
      tools: [...(req.body.tools || []), ...TOOLS],
      stream: false
    }, {
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json'
      },
      timeout: 120000
    });
    
    let result = apiResponse.data;
    
    // 处理工具调用循环
    let maxLoops = 5;
    while (maxLoops > 0) {
      const choice = result.choices && result.choices[0];
      if (!choice) break;
      const msg = choice.message;
      if (choice.finish_reason !== 'tool_calls' || !msg.tool_calls) break;
      
      // 执行工具
      const toolMessages = [];
      for (const tc of msg.tool_calls) {
        const toolResult = await executeTool(
          tc.function.name,
          JSON.parse(tc.function.arguments)
        );
        toolMessages.push({
          role: 'tool',
          tool_call_id: tc.id,
          content: toolResult
        });
      }
      // 把工具结果喂回 LM
      augmentedMessages.push(msg);
      augmentedMessages.push(...toolMessages);
      
      const followUp = await axios.post(apiUrl, {
        model: requestModel,
        messages: augmentedMessages,
      tools: [...(req.body.tools || []), ...TOOLS],
        stream: false
      }, {
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json'
        },
        timeout: 120000
      });
      result = followUp.data;
      maxLoops--;
    }
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
     
    // 返回最终结果
    res.json(result);
    
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
