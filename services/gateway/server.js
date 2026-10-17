const express = require('express');
const crypto = require('crypto');
const axios = require('axios');
const cors = require('cors');
require('dotenv').config();

const app = express();
app.use(cors());
app.use(express.json({ limit: '100mb' }));

const MEMORY_SERVICE_URL = process.env.MEMORY_SERVICE_URL || 'http://localhost:8001';
const PORT = process.env.PORT || 3000;

// ========== 记忆浮现参数配置 ==========
const SURFACE_PROBABILITY = 0.3;          // 30% 概率开放回忆
// B1 修复：以前用 RRF 分数做门槛（MIN_RELEVANCE=0.15），但 RRF 只表示名次，最大约 0.016，
// 主通道因此几乎从不浮现。现在改用记忆服务返回的 vec_sim（查询与记忆的余弦相似度，0~1）。
// 默认 0.55 来自少量样本（相关约 0.68~0.71，"嗯嗯"这类无关消息约 0.45~0.48），
// 有了真实数据后看 gateway.log 里的 [SURFACE] 行再校准；可用环境变量 SURFACE_MIN_SIM 覆盖。
const SURFACE_MIN_SIM = parseFloat(process.env.SURFACE_MIN_SIM || '0.55');
const COOLDOWN_HOURS = 24;                // 主通道冷却时长（小时）
const SERENDIPITY_PROBABILITY = 0.05;    // 5% 概率触发无由来浮现
const SERENDIPITY_COOLDOWN_DAYS = 7;     // 无由来浮现冷却时长（天）
const SHOW_COUNT_WEIGHTS = [
  { value: 0, weight: 0.2 },  // 20% 不显示
  { value: 1, weight: 0.6 },  // 60% 显示 1 条
  { value: 2, weight: 0.2 }   // 20% 显示 2 条
];
const SESSION_GAP_THRESHOLD_HOURS = 4;   // 会话间隔感知阈值

// bug修复：工具回环短路时不请求上游、没有真实 usage。缓存上一轮真实回答的 usage（按 conv_id），
// 回环时取出返回，避免 Kelivo 把已显示的 token 数用 0 覆盖（工具场景显示 0 的根因）。
const lastUsageByConv = new Map();
const LAST_USAGE_MAX = 500;   // 简单容量上限，超了清最早的
function rememberUsage(convId, usage) {
  if (!convId || !usage) return;
  if (lastUsageByConv.size >= LAST_USAGE_MAX) {
    const firstKey = lastUsageByConv.keys().next().value;
    lastUsageByConv.delete(firstKey);
  }
  lastUsageByConv.set(convId, { ...usage });
}
// 解析对话 ID：优先 body.conversation_id，其次 Kelivo 发的 x-conversation-id header
// （问题2 Bug B：Kelivo 不发 body.conversation_id，但发 x-conversation-id header，
//  且同一对话窗口内跨轮稳定。老代码只读 body，导致每轮用"首条user的MD5"生成 id，
//  Kelivo 每轮只发当前消息 → 每轮 id 都不同 → 对话被打散成一堆单条）。
// 都没有才 fallback 到首条 user 消息的 MD5（保底，理论上不会走到）。
// Pluvia 会在"当前这条" user 消息末尾追加时间戳，如 "  （此刻：10月16日 周五 02:28）"，历史消息里则不带。
// 不剥掉的话：① 首轮请求的首条消息带戳、后续轮不带戳 → MD5 兜底算出的会话 id 不同 → 前两句被存成单独会话；
// ② 同一条消息先带戳存一次、下一轮不带戳再存一次 → L0 版本抖动（superseded 重复）。
// 网关自己会注入时间头，所以存储、会话 id、检索查询都用剥掉戳的文本；发给模型的原文不动。
const CLIENT_STAMP_RE = /\s*（此刻：[^（）\n]{1,40}）\s*$/;
// 约定 v0.2 第 3 节的兜底规则（请求里没带 pluvia.tail 时用）：「此刻」可能由几段组成、段间空一行，
// 剥掉末尾连续的、整行被全角括号包住、并以这几个词开头的段落。只剥后缀，结果一定是原文的前缀。
const CLIENT_TAIL_LINE_RE = /^\s*（(?:此刻：|离上一句|正在听|ta 排的那首)[^\n]*）\s*$/;
function stripClientStamp(text) {
  if (typeof text !== 'string') return text;
  const lines = text.split('\n');
  let cut = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].trim()) continue;
    if (CLIENT_TAIL_LINE_RE.test(lines[i])) { cut = i; continue; }
    break;
  }
  const out = cut >= 0 ? lines.slice(0, cut).join('\n').replace(/\s+$/, '') : text;
  return out.replace(CLIENT_STAMP_RE, '');
}

// ============ 时间（Phase 1：统一 UTC） ============
// 记忆服务里的时间一律按 UTC 存（'YYYY-MM-DD HH:MM:SS'，不带时区标记）；给模型看的地方换算成用户时区
// （.env 的 USER_TZ，默认 Asia/Shanghai）。以前有的地方把 UTC 当北京时间用，日期会差 8 小时（凌晨的事被算成前一天）。
const USER_TZ = process.env.USER_TZ || 'Asia/Shanghai';
const USER_TZ_LABEL = USER_TZ === 'Asia/Shanghai' ? '北京时间' : USER_TZ;
const WEEKDAY_CN = { Mon: '周一', Tue: '周二', Wed: '周三', Thu: '周四', Fri: '周五', Sat: '周六', Sun: '周日' };
// 解析库里存的时间：不带时区的当 UTC，带 Z / 偏移的按本身，也支持毫秒时间戳。解析不了返回 null。
function parseStoredTs(v) {
  if (v == null || v === '') return null;
  if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
  if (typeof v === 'number') return new Date(v > 1e12 ? v : v * 1000);
  let s = String(v).trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    s += 'T00:00:00Z';
  } else {
    if (/^\d{4}-\d{2}-\d{2} \d/.test(s)) s = s.replace(' ', 'T');
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) s += 'Z';
  }
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}
const LOCAL_FMT = new Intl.DateTimeFormat('en-US', {
  timeZone: USER_TZ, year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', weekday: 'short'
});
function localParts(d) {
  const p = {};
  for (const x of LOCAL_FMT.formatToParts(d)) p[x.type] = x.value;
  return p;
}
// 'YYYY-MM-DD HH:MM'（用户时区）；seconds 加秒，weekday 加「周几」，time:false 只要日期
function fmtLocal(v, { time = true, seconds = false, weekday = false } = {}) {
  const d = parseStoredTs(v);
  if (!d) return '';
  const p = localParts(d);
  let s = `${p.year}-${p.month}-${p.day}`;
  if (time) s += ` ${p.hour}:${p.minute}` + (seconds ? `:${p.second}` : '');
  if (weekday) s += ' ' + (WEEKDAY_CN[p.weekday] || '');
  return s;
}
// 用户时区的日历天编号（按日历天算今天 / 昨天）
function localDayNumber(d) {
  const p = localParts(d);
  return Math.floor(Date.UTC(+p.year, +p.month - 1, +p.day) / 86400000);
}

function resolveConvId(req) {
  if (req.body.conversation_id) return req.body.conversation_id;
  const headerConvId = req.headers['x-conversation-id'];
  if (headerConvId) return headerConvId;
  const client = req.headers['x-client-name'] || 'kelivo';
  const firstUserMsg = memoryMessages(req).find(m => m && m.role === 'user');
  const seed = firstUserMsg ? memoryText(req, firstUserMsg.content) : String(Date.now());
  return client + '_' + crypto.createHash('md5').update(seed).digest('hex').substring(0, 8);
}

// 与保存对话时的 conv_id 计算保持一致，保证回环请求能命中 usage 缓存。
function computeConvId(req) {
  return resolveConvId(req);
}

// 问题2：识别 Kelivo 的"标题生成"请求。Kelivo 每开新对话会额外发一个
// LLM 请求，用固定英文模板让模型把对话概括成短标题。这类请求走同一 endpoint，
// 不该存进 L0（否则模板 prompt + 标题会污染记忆、被 L1 提取、被 recall 搜到）。
// 特征：user 消息里含 Kelivo 模板的固定英文句子。命中则跳过保存（标题照常返回）。
function isTitleGenerationRequest(req) {
  // 前端显式声明"这条请求不进记忆"（Pluvia 的标题生成、连接测试等）：
  // body.memory_skip === true，或请求头 x-memory-skip: 1。命中后同样跳过保存与展示计数。
  if (req.body && req.body.memory_skip === true) return true;
  if (String(req.headers['x-memory-skip'] || '') === '1') return true;
  const msgs = req.body.messages || [];
  for (const m of msgs) {
    if (m.role !== 'user') continue;
    const text = typeof m.content === 'string'
      ? m.content
      : Array.isArray(m.content)
        ? m.content.filter(b => b && b.type === 'text').map(b => b.text).join(' ')
        : '';
    if (/summarize the conversation/i.test(text) && /into a short title/i.test(text)) {
      return true;
    }
  }
  return false;
}

// 把消息 content 归一化成纯文本。必须与记忆服务 main.py 的 normalize_content
// 完全一致（块数组只取 text 块、用 '\n' 拼），否则同一句话两侧归一化结果不同 →
// dedup 误判成内容变化 → 触发无谓的版本切换。
function toPlainText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    const parts = [];
    for (const b of content) {
      if (typeof b === 'string') parts.push(b);
      else if (b && b.type === 'text' && b.text) parts.push(b.text);
    }
    return parts.join('\n');
  }
  if (content == null) return '';
  return String(content);
}

// 从上游结果中提取 AI 回复的纯文本（忽略 thinking/tool_use 块）。
// 兼容流式(result.content 块数组)和非流式(可能是 OpenAI choices 结构)。
function extractAssistantText(result) {
  if (!result) return '';
  if (Array.isArray(result.content)) {
    return result.content.filter(c => c.type === 'text').map(c => c.text).join('\n');
  }
  if (result.choices && result.choices[0] && result.choices[0].message) {
    return result.choices[0].message.content || '';
  }
  return '';
}

// L0 重复根治：保存前把 Kelivo 传回的消息序列清洗成干净对话流。
//  1. 去 system
//  2. 去归一化后为空的消息（工具轮次：tool_use-only assistant、tool_result user、
//     纯 thinking），它们本不该进 L0，且空消息占 msg_idx 下标会导致下标漂移
//  3. 去相邻的同 role 同内容重复（工具调用会把一轮 assistant 拆成多条、Kelivo
//     还会重发同句），保留最早那条
// 最后 append 本轮 aiText（若与末条 assistant 相同则不重复加）。
// 清洗后 msg_idx 基于这个稳定的干净序列，dedup(按 conv_id+msg_idx) 不再失配。
// 相邻同 role 去重的比较键：忽略所有空白字符。工具调用会把一轮 assistant
// 拆成多个 text 块，Kelivo 重发时块间拼接的空白（\n）可能不一致，导致"同一句
// 回复"两条只差空白 → 精确相等拦不住。相邻同 role 本就只在工具轮次出现，按
// 空白不敏感合并是安全的。
function dedupKey(s) {
  return (s || '').replace(/\s+/g, '');
}
// opts.stripStamp（默认 true）：对 user 消息套用「此刻」兜底剥离；已按 pluvia.tail 精确剥过时传 false。
// opts.mergeAssistant：相邻的几条 assistant 合成一条（Pluvia：一条回复中间夹着工具调用时，
//   前端历史里可能拆成几条；L0 里只存一条，和网关在最后一轮存的保持一致）。
// opts.pluviaDecor：assistant 消息去掉 Pluvia 加的工具记录 / 链接卡片（约定 8.2），只留正文。
// role 'tool'（OpenAI 格式的工具结果）一律不进 L0。
function cleanMessagesForSave(messages, aiText, opts = {}) {
  const stripStamp = opts.stripStamp !== false;
  const mergeAssistant = !!opts.mergeAssistant;
  const pluviaDecor = !!opts.pluviaDecor;
  // 工具轮后重发去重：assistant(文本A) → user(tool_result) → assistant(文本A 相同)
  // → 跳过第二条 A（保留先发的）。L0 不存工具轮，去掉这种冗余让上下文更干净。
  const pre = [];
  let lastAssistantText = null;
  let afterToolResult = false;
  for (const m of (messages || [])) {
    const isToolResult = m && m.role === 'user' && Array.isArray(m.content) &&
      m.content.some(b => b && typeof b === 'object' && b.type === 'tool_result');
    const text = m ? toPlainText(m.content).trim() : '';
    if (m && m.role === 'assistant' && text && afterToolResult && lastAssistantText &&
        dedupKey(text) === dedupKey(lastAssistantText)) {
      afterToolResult = false;
      continue;
    }
    if (isToolResult) {
      afterToolResult = true;
    } else if (m && m.role === 'assistant' && text) {
      lastAssistantText = text;
      afterToolResult = false;
    } else {
      afterToolResult = false;
    }
    pre.push(m);
  }
  const out = [];
  for (const m of (pre || [])) {
    if (!m || m.role === 'system' || m.role === 'tool') continue;
    let text = (m.role === 'user' && stripStamp) ? stripClientStamp(toPlainText(m.content)) : toPlainText(m.content);
    if (pluviaDecor && m.role === 'assistant') text = stripPluviaAssistantDecor(text).trim();
    if (!text) continue;
    const last = out[out.length - 1];
    // 相邻同 role 且空白无关内容相同 → 视为重复，保留先到的那条
    if (last && last.role === m.role && dedupKey(last.content) === dedupKey(text)) continue;
    if (mergeAssistant && last && last.role === 'assistant' && m.role === 'assistant') {
      last.content += '\n\n' + text;
      continue;
    }
    out.push({ role: m.role, content: text });
  }
  if (aiText) {
    const last = out[out.length - 1];
    if (!(last && last.role === 'assistant' && dedupKey(last.content) === dedupKey(aiText))) {
      out.push({ role: 'assistant', content: aiText });
    }
  }
  return out;
}

// 只保存本轮新增（最后一条 user 文本 + AI 回复）。
// 历史全量由 Kelivo 上行上报（带 group 语义）管理；网关不再全量重存历史，
// 避免“路径A无group重存”与“路径B带group上报”并存造成重复/乱序/旧版本残留。
function buildTurnMessages(messages, aiText) {
  const out = [];
  for (let i = (messages || []).length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role !== 'user') continue;
    const t = toPlainText(m.content).trim();
    if (t) { out.push({ role: 'user', content: t }); break; }
  }
  if (aiText) out.push({ role: 'assistant', content: aiText });
  return out;
}

// ============ Pluvia 接入约定 v0.2（docs/plan/pluvia-gateway-contract.md） ============
// 只有 body 里带了 pluvia 对象的请求才走 v0.2 逻辑；Kelivo 等其它客户端的路径不变。
const PLUVIA_CONTRACT = { contract: '0.2', features: ['turn', 'tail', 'client_tools', 'tool_events'] };

// 网关时间头：auto（默认）= 前端已经带了「此刻：」就不再加 [当前时间]、带了「离上一句」就不再加 [距上次对话]，
// 免得模型同时看到两个钟；on = 总是加；off = 都不加。在 .env 里设 GATEWAY_TIME_HEADER 修改。
const TIME_HEADER_MODE = String(process.env.GATEWAY_TIME_HEADER || 'auto').trim().toLowerCase();

function pluviaCtx(req) {
  if (req._pv !== undefined) return req._pv;
  const p = req.body && req.body.pluvia;
  let ctx = null;
  if (p && typeof p === 'object') {
    const t = p.turn;
    const turn = (t && typeof t === 'object' && t.id != null && String(t.id))
      ? { id: String(t.id), round: Math.max(1, parseInt(t.round, 10) || 1), regen_of: t.regen_of || null }
      : null;
    ctx = {
      v: String(p.v || ''), turn, tail: typeof p.tail === 'string' ? p.tail : null,
      system: p.system === 'append' ? 'append' : 'ignore',                      // 约定 8.1
      aux: (typeof p.aux === 'string' && p.aux.trim()) ? p.aux.trim() : null    // 约定 8.5
    };
  }
  req._pv = ctx;
  return ctx;
}
function isPluviaClient(req) {
  return String(req.headers['x-client-name'] || '').trim().toLowerCase() === 'pluvia' || !!pluviaCtx(req);
}
function isServerTool(name) {
  return TOOLS.some(t => t.function && t.function.name === name);
}
function toolNameOf(t) {
  return t ? (t.function ? t.function.name : t.name) : undefined;
}
// 客户端发来的 system（OpenAI 的 role:"system" 消息；Anthropic 的 system 字段已在 /v1/messages 入口转成同样的消息）→ 文本块
function clientSystemBlocks(messages) {
  const out = [];
  for (const m of messages || []) {
    if (!m || m.role !== 'system') continue;
    if (typeof m.content === 'string') { if (m.content.trim()) out.push({ type: 'text', text: m.content }); continue; }
    if (!Array.isArray(m.content)) continue;
    for (const b of m.content) {
      if (typeof b === 'string') { if (b.trim()) out.push({ type: 'text', text: b }); continue; }
      if (b && b.type === 'text' && typeof b.text === 'string' && b.text.trim()) {
        const blk = { type: 'text', text: b.text };
        if (b.cache_control) blk.cache_control = b.cache_control;
        out.push(blk);
      }
    }
  }
  return out;
}
// Pluvia 历史里 assistant 消息的固定格式（约定 8.2）：[图片：…] 行 + HEAD（[工具记录 · …] 行）+ 正文 + TAIL（链接卡片 / 整帖）。
// 存 L0 只要正文，和网关在最后一轮存的保持一致；发给模型的原文不动。
const PV_HEAD_RE = /^(?:\[(?:工具记录 · [^\n]*|图片：[^\n]*)\]\n)+/;
const PV_TAIL_RE = /(?:^|\n)\[(?:链接卡片|整帖) · /;
function stripPluviaAssistantDecor(text) {
  if (typeof text !== 'string') return text;
  let s = text.replace(PV_HEAD_RE, '');
  const cut = s.search(PV_TAIL_RE);
  if (cut >= 0) s = s.slice(0, cut);
  return s;
}
// 确定转发目标（与 chatCompletionsMain 里的逻辑一致：Dashboard 配置优先，.env 兜底）
function resolveUpstreamTarget(model) {
  const claudeBaseUrl = process.env.CLAUDE_BASE_URL || 'https://api.anthropic.com/v1';
  const defaultModel = process.env.DEFAULT_MODEL || 'deepseek-chat';
  if (activeConfig && activeConfig.base_url && activeConfig.api_key) {
    const base = activeConfig.base_url.replace(/\/$/, '');
    return { apiUrl: base.endsWith('/messages') ? base : base + '/messages', apiKey: activeConfig.api_key,
      requestModel: model || activeConfig.model || defaultModel };
  }
  if (model && model.includes('deepseek')) {
    return { apiUrl: 'https://api.deepseek.com/v1/chat/completions', apiKey: process.env.DEEPSEEK_API_KEY, requestModel: model };
  }
  if (model && (model.includes('claude') || model.includes('opus') || model.includes('sonnet'))) {
    return { apiUrl: claudeBaseUrl + '/messages', apiKey: process.env.CLAUDE_API_KEY, requestModel: model };
  }
  return { apiUrl: claudeBaseUrl + '/messages', apiKey: process.env.CLAUDE_API_KEY, requestModel: defaultModel };
}

// 按 pluvia.tail 原文精确剥掉「此刻」。返回 { content, ok }。
//  字符串：整条就是 tail，或以「换行 + tail」结尾（忽略首尾空白差异）；数组：去掉最后一个文本恰好等于 tail 的块。
function stripTailExact(content, tail) {
  if (!tail || !tail.trim()) return { content, ok: false };
  const t = tail.trim();
  if (typeof content === 'string') {
    const c = content.replace(/\s+$/, '');
    if (c.trim() === t) return { content: '', ok: true };
    if (c.endsWith(t)) {
      const head = c.slice(0, c.length - t.length);
      if (/\n\s*$/.test(head)) return { content: head.replace(/\s+$/, ''), ok: true };
    }
    return { content, ok: false };
  }
  if (Array.isArray(content)) {
    for (let i = content.length - 1; i >= 0; i--) {
      const b = content[i];
      if (b && b.type === 'text' && typeof b.text === 'string' && (b.text === tail || b.text.trim() === t)) {
        return { content: content.slice(0, i).concat(content.slice(i + 1)), ok: true };
      }
    }
  }
  return { content, ok: false };
}

// 「给记忆用」的消息视图：检索、算兜底会话 id、存 L0 都用它；发给模型的原文不动。
// 带 pluvia.tail 时，从后往前找第一条能精确剥掉 tail 的 user 消息剥掉（req._tailOk 记下是否成功）。
function memoryMessages(req) {
  if (req._memMsgs) return req._memMsgs;
  const msgs = Array.isArray(req.body && req.body.messages) ? req.body.messages : [];
  let out = msgs;
  req._tailOk = false;
  const pv = pluviaCtx(req);
  if (pv && pv.tail && pv.tail.trim()) {
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (!m || m.role !== 'user') continue;
      const r = stripTailExact(m.content, pv.tail);
      if (r.ok) {
        out = msgs.slice();
        out[i] = { ...m, content: r.content };
        req._tailOk = true;
        break;
      }
    }
  }
  req._memMsgs = out;
  return out;
}
// 一条消息「给记忆用」的纯文本：已按 tail 精确剥过就不再套兜底规则，否则用兜底规则剥「此刻」。
function memoryText(req, content) {
  memoryMessages(req);
  const text = toPlainText(content);
  return req._tailOk ? text : stripClientStamp(text);
}
// 前端这一轮有没有自带时钟 / 间隔提示（决定 auto 模式下网关还加不加时间头）
function clientClockFlags(req, rawLastUserText) {
  const pv = pluviaCtx(req);
  let src;
  if (pv && pv.tail != null) {
    src = pv.tail;
  } else {
    const raw = String(rawLastUserText || '');
    src = raw.slice(stripClientStamp(raw).length);   // 只看被兜底规则认出来的末尾那几段
  }
  return { hasClock: /（\s*此刻：/.test(src), hasGap: /（\s*离上一句/.test(src) };
}

// ---- client_tools：一条回复（turn）在网关这边的状态，按 (conversation_id, turn.id) 存 10 分钟 ----
// 存的是交出前端工具那一刻的完整上游消息序列（含记忆目录注入、思考块与签名、记忆工具的调用和结果），
// 以及还没回填的那组工具结果。续问时以它为准接回去，前端回传的 assistant 只用来按 tool_call_id 对上。
const TURN_TTL_MS = 10 * 60 * 1000;
const turnStore = new Map();
function turnKey(convId, turnId) { return String(convId) + '\u0000' + String(turnId); }
function turnStoreGet(key) {
  const s = turnStore.get(key);
  if (!s) return null;
  if (Date.now() - s.ts > TURN_TTL_MS) { turnStore.delete(key); return null; }
  return s;
}
function turnStoreSet(key, s) {
  const now = Date.now();
  for (const [k, v] of turnStore) if (now - v.ts > TURN_TTL_MS) turnStore.delete(k);
  while (turnStore.size >= 100) turnStore.delete(turnStore.keys().next().value);
  s.ts = now;
  turnStore.set(key, s);
}

// ---- OpenAI 格式 → Anthropic Messages 格式（只用于 Pluvia；上游始终是 Anthropic 格式） ----
const ANTHROPIC_BLOCK_TYPES = new Set(['image', 'document', 'tool_use', 'tool_result', 'thinking', 'redacted_thinking']);
function imageUrlToAnthropic(p) {
  const url = typeof p.image_url === 'string' ? p.image_url : (p.image_url && p.image_url.url);
  if (typeof url !== 'string' || !url) return null;
  const m = /^data:(image\/[a-zA-Z0-9.+\-]+);base64,(.+)$/s.exec(url);
  if (m) return { type: 'image', source: { type: 'base64', media_type: m[1], data: m[2] } };
  if (/^https?:\/\//i.test(url)) return { type: 'image', source: { type: 'url', url } };
  return null;
}
function oaContentToBlocks(content) {
  if (content == null) return [];
  if (typeof content === 'string') return content.trim() ? [{ type: 'text', text: content }] : [];
  if (!Array.isArray(content)) return [{ type: 'text', text: String(content) }];
  const out = [];
  for (const p of content) {
    if (typeof p === 'string') { if (p.trim()) out.push({ type: 'text', text: p }); continue; }
    if (!p || typeof p !== 'object') continue;
    if (p.type === 'text') {
      if (typeof p.text !== 'string' || !p.text.trim()) continue;
      const b = { type: 'text', text: p.text };
      if (p.cache_control) b.cache_control = p.cache_control;
      out.push(b);
      continue;
    }
    if (p.type === 'image_url') { const b = imageUrlToAnthropic(p); if (b) out.push(b); continue; }
    if (ANTHROPIC_BLOCK_TYPES.has(p.type)) { out.push(p); continue; }
    // 其它类型（音频等）上游不认识，丢掉
  }
  return out;
}
function oaToolResultContent(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    const blocks = oaContentToBlocks(content).filter(b => b.type === 'text' || b.type === 'image');
    return blocks.length ? blocks : '';
  }
  return content == null ? '' : JSON.stringify(content);
}
// assistant 的 tool_calls → tool_use 块；role:'tool' → user 消息里的 tool_result 块；
// 相邻同角色合并（工具结果后面另起的「附图」user 消息并进同一条，tool_result 排最前）。
// reasoning_content 没有签名，不能回传给 Anthropic，丢掉（续问时以网关存的那一轮为准）。
function oaMessagesToAnthropic(messages) {
  const out = [];
  const isEmpty = c => (typeof c === 'string' ? !c.trim() : !c.length);
  const asBlocks = c => (typeof c === 'string' ? [{ type: 'text', text: c }] : c);
  const push = (role, content) => {
    if (isEmpty(content)) return;
    const last = out[out.length - 1];
    if (last && last.role === role) {
      const merged = [...asBlocks(last.content), ...asBlocks(content)];
      if (role === 'user') merged.sort((x, y) => (y.type === 'tool_result') - (x.type === 'tool_result'));
      last.content = merged;
    } else {
      out.push({ role, content });
    }
  };
  for (const m of messages || []) {
    if (!m || m.role === 'system') continue;
    if (m.role === 'tool') {
      push('user', [{ type: 'tool_result', tool_use_id: m.tool_call_id, content: oaToolResultContent(m.content) }]);
      continue;
    }
    if (m.role === 'assistant') {
      const calls = Array.isArray(m.tool_calls) ? m.tool_calls : [];
      if (!calls.length && typeof m.content === 'string') { push('assistant', m.content); continue; }
      const blocks = oaContentToBlocks(m.content);
      for (const tc of calls) {
        const fn = (tc && tc.function) || {};
        let input = {};
        try { input = fn.arguments ? JSON.parse(fn.arguments) : {}; } catch (e) { input = {}; }
        blocks.push({ type: 'tool_use', id: tc.id, name: fn.name, input });
      }
      push('assistant', blocks);
      continue;
    }
    if (m.role === 'user') push('user', typeof m.content === 'string' ? m.content : oaContentToBlocks(m.content));
  }
  return out;
}
// OpenAI 的 tool_choice（"none"/"auto"/"required"/{type:"function"}）→ Anthropic；已是 Anthropic 格式的原样返回
function toAnthropicToolChoice(tc) {
  if (tc == null) return undefined;
  if (typeof tc === 'string') {
    if (tc === 'none') return { type: 'none' };
    if (tc === 'auto') return { type: 'auto' };
    if (tc === 'required' || tc === 'any') return { type: 'any' };
    return undefined;
  }
  if (typeof tc === 'object') {
    if (tc.type === 'function' && tc.function && tc.function.name) return { type: 'tool', name: tc.function.name };
    return tc;
  }
  return undefined;
}
// Anthropic 一次请求最多 4 个 cache_control 断点。网关 system 自己用 1–3 个，Pluvia 的 system 接在后面再占 1 个，
// 前端在消息里还会打 1 个，可能超。超了按这个顺序去掉（越靠后的断点覆盖的前缀越长、越值得留）：
// 较早的消息断点 → 工具上的 → 较早的 system 断点 → 最后一个消息断点 → 最后一个 system 断点。不超时什么都不改。
function capCacheControl(system, messages, tools, max = 4) {
  const sysRefs = [], msgRefs = [], toolRefs = [];
  (system || []).forEach((b, i) => { if (b && b.cache_control) sysRefs.push(i); });
  (messages || []).forEach((m, i) => {
    if (m && Array.isArray(m.content)) m.content.forEach((b, j) => { if (b && typeof b === 'object' && b.cache_control) msgRefs.push([i, j]); });
  });
  (tools || []).forEach((t, i) => { if (t && t.cache_control) toolRefs.push(i); });
  let excess = sysRefs.length + msgRefs.length + toolRefs.length - max;
  if (excess <= 0) return 0;
  const order = [
    ...msgRefs.slice(0, -1).map(r => ['m', r]),
    ...toolRefs.map(i => ['t', i]),
    ...sysRefs.slice(0, -1).map(i => ['s', i]),
    ...msgRefs.slice(-1).map(r => ['m', r]),
    ...sysRefs.slice(-1).map(i => ['s', i])
  ];
  let removed = 0;
  for (const [kind, ref] of order) {
    if (excess <= 0) break;
    if (kind === 'm') {
      const [i, j] = ref;
      const m = messages[i];
      const content = m.content.slice();
      const { cache_control, ...rest } = content[j];
      content[j] = rest;
      messages[i] = { ...m, content };
    } else if (kind === 't') {
      const { cache_control, ...rest } = tools[ref];
      tools[ref] = rest;
    } else {
      const { cache_control, ...rest } = system[ref];
      system[ref] = rest;
    }
    excess--; removed++;
  }
  console.log('[CACHE] cache_control 断点超过', max, '个，去掉了', removed, '个');
  return removed;
}
// 把动态的记忆目录 / 时间头注入一条 user 消息。afterCacheMarker：消息里有 cache_control 块时
// 插在它后面（Pluvia 把「此刻」放在缓存标记之后），这样标记之前的前缀每轮不变、下一轮还能命中缓存。
function injectMenu(m, menuText, afterCacheMarker) {
  if (typeof m.content === 'string') { m.content = menuText + m.content; return; }
  if (!Array.isArray(m.content)) return;
  if (afterCacheMarker) {
    let k = -1;
    m.content.forEach((b, j) => { if (b && typeof b === 'object' && b.cache_control) k = j; });
    if (k >= 0) {
      m.content = [...m.content.slice(0, k + 1), { type: 'text', text: menuText.replace(/\s+$/, '') }, ...m.content.slice(k + 1)];
      return;
    }
  }
  m.content = [{ type: 'text', text: menuText }, ...m.content];
}

// 一条 assistant 消息里的工具调用 id（OpenAI tool_calls 或 Anthropic tool_use 块）
function assistantToolIds(m) {
  if (!m || m.role !== 'assistant') return [];
  const ids = [];
  if (Array.isArray(m.tool_calls)) for (const tc of m.tool_calls) if (tc && tc.id) ids.push(tc.id);
  if (Array.isArray(m.content)) for (const b of m.content) if (b && b.type === 'tool_use' && b.id) ids.push(b.id);
  return ids;
}
function isToolResultMsg(m) {
  return !!(m && m.role === 'user' && Array.isArray(m.content) && m.content.some(b => b && b.type === 'tool_result'));
}
// 第 2 轮起，本条回复前几轮的 assistant(工具调用) / 工具结果 / 附图消息都在末尾。
// 从后往前找到本条回复开始的那条 user 消息，返回它后面第一条带工具调用的 assistant 的位置（存 L0 时从这里截掉）。
function turnCut(messages, round) {
  if ((round || 1) <= 1) return messages.length;
  let cut = messages.length;
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m) continue;
    if (assistantToolIds(m).length) { cut = i; continue; }
    if (m.role === 'user' && !isToolResultMsg(m)) {
      const p = messages[i - 1];
      if (p && (p.role === 'tool' || isToolResultMsg(p))) continue;   // 工具结果后面另起的「附图」消息
      break;   // 本条回复开始的那条 user 消息
    }
  }
  return cut;
}
// 续问：网关存的这一轮 + 按原顺序回填的工具结果（记忆工具的用网关存的，前端工具的用前端回传的）。
// 前端最后一条 assistant 里的工具 id 对不上就返回 null（改按前端回传的历史转换）。
function buildContinuation(req, S) {
  const msgs = (req.body.messages || []).filter(m => m && m.role !== 'system');
  let lastA = -1;
  for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === 'assistant') { lastA = i; break; }
  if (lastA < 0) return null;
  const ids = assistantToolIds(msgs[lastA]);
  if (!S.pending.clientIds.every(id => ids.includes(id))) return null;
  const clientResults = {};
  const extra = [];
  for (const m of msgs.slice(lastA + 1)) {
    if (m.role === 'tool') { clientResults[m.tool_call_id] = { content: oaToolResultContent(m.content) }; continue; }
    if (m.role !== 'user') continue;
    if (typeof m.content === 'string') { if (m.content.trim()) extra.push({ type: 'text', text: m.content }); continue; }
    for (const b of (Array.isArray(m.content) ? m.content : [])) {
      if (b && b.type === 'tool_result') { clientResults[b.tool_use_id] = { content: b.content, is_error: b.is_error }; continue; }
      extra.push(...oaContentToBlocks([b]));
    }
  }
  const results = S.pending.order.map(id => {
    if (Object.prototype.hasOwnProperty.call(S.pending.server, id)) {
      return { type: 'tool_result', tool_use_id: id, content: S.pending.server[id] };
    }
    const r = clientResults[id];
    if (!r) return { type: 'tool_result', tool_use_id: id, content: '（前端没有返回这个工具的结果）', is_error: true };
    const out = { type: 'tool_result', tool_use_id: id, content: r.content == null ? '' : r.content };
    if (r.is_error) out.is_error = true;
    return out;
  });
  return { messages: [...S.upstream, { role: 'user', content: [...results, ...extra] }] };
}
// 开着思考时，工具结果前面那条 assistant 必须以 thinking 块开头。按前端回传的历史转换（没有签名）时
// 拿不回思考块，这一轮只好不带 thinking 参数，否则上游会报错。
function toolLoopLacksThinking(messages) {
  const last = messages[messages.length - 1];
  if (!last || last.role !== 'user' || !Array.isArray(last.content) ||
      !last.content.some(b => b && b.type === 'tool_result')) return false;
  for (let i = messages.length - 2; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role !== 'assistant') continue;
    const first = Array.isArray(m.content) ? m.content[0] : null;
    return !(first && (first.type === 'thinking' || first.type === 'redacted_thinking'));
  }
  return false;
}

// ---- tool_events：记忆工具执行过程，推给前端只做显示 ----
const TOOL_ERR_RE = /^(检索失败|记忆检索出错|记录感受失败|更新画像失败|网络搜索失败|网络搜索未配置|工具执行失败)/;
function toolEventContent(name, out) {
  let s = String(out == null ? '' : out);
  if (name === 'recall') s = s.replace(/\n*😊 情感：valence [^\n]*/g, '');   // 内部打分不给前端看
  s = s.replace(/^\s+/, '');
  return s.length > 2000 ? s.slice(0, 2000) + '…' : s;
}
async function runServerTool(u, emitter, events) {
  const base = { id: u.id, name: u.name, arguments: u.input || {} };
  if (emitter) emitter.toolEvent({ ...base, status: 'running' });
  let out;
  let status = 'done';
  try {
    out = await executeTool(u.name, u.input || {});
    if (out == null || out === '') out = '（无返回内容）';
    if (TOOL_ERR_RE.test(String(out))) status = 'error';
  } catch (e) {
    out = `工具执行失败: ${e.message}`;
    status = 'error';
  }
  const ev = { ...base, status, content: toolEventContent(u.name, out) };
  if (emitter) emitter.toolEvent(ev);
  events.push(ev);
  return out;
}

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
      await refreshModelsCache(true);
      await loadChatTargets();
      return 'active';
    } else {
      activeConfig = null;
      console.log('[CONFIG] 记忆服务未配置供应商，回退到 .env');
      await refreshModelsCache(true);
      await loadChatTargets();
      return 'unconfigured';
    }
  } catch (e) {
    activeConfig = null;
    console.error('[CONFIG] 拉取配置失败，回退到 .env：', e.message);
    return 'error';
  }
}

// ============ per-conv 串行队列：同一 conv_id 的请求排队执行，防止上下文错乱 ============
const convQueues = new Map();
// OpenAI image_url 块 -> Anthropic image 块（TG 桥图片消息用）
function toAnthropicContent(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return content;
  return content.map(b => {
    if (!b || typeof b !== 'object') return b;
    if (b.type === 'image_url' && b.image_url && typeof b.image_url.url === 'string' && b.image_url.url.startsWith('data:')) {
      const m = /^data:(image\/[a-zA-Z0-9.+\-]+);base64,(.+)$/.exec(b.image_url.url);
      if (m) return { type: 'image', source: { type: 'base64', media_type: m[1], data: m[2] } };
    }
    return b;
  });
}

function enqueueConv(convId, task) {
  const prev = convQueues.get(convId) || Promise.resolve();
  const next = prev.then(task, task);
  convQueues.set(convId, next.catch(() => {}));
  return next;
}

// 工具定义
const TOOLS = [
  {
    type: "function",
    function: {
      name: "recall",
      description: "当你隐约觉得某个话题以前聊过、或想确认某个细节时，用这个工具主动回忆。不需要确定记忆一定存在才搜——凭感觉去找也可以。目录里没显示的，不代表记忆库里没有。\n\n模式说明：\n- semantic（默认）：语义联想，适合模糊回忆\n- exact：逐字搜原文，能找到未提炼进记忆的对话片段（标记为 unprocessed: true）\n- emotion：按情感搜索高唤醒度的记忆",
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
      description: "记录你当下的感受或印象，不是事件记录而是情感快照。可选使用和弦锚格式帮助情绪稳定复现：一行具体情境 + 一行和弦（如 \"> 你早上六点叫我，我手里还有皮筋\n> Fmaj9 → C/E → Am add9 → G6sus4 · 60bpm\"）。规则：情境必有；每行最多4个和弦用→隔开；bpm/力度可选；紧张系和弦（Em(maj7)/B7♭9/Dm7♭5等）旁加动作词（盯/压/憋/狂）；只在记录感受/回顾时用锚，日常对话不用。",
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
  },
  {
    type: 'function',
    function: {
      name: 'bocha_search',
      description: '用博查 API 联网搜索，返回可信来源的实时网页摘要。在需要可靠的中文网络信息、供应商不支持内置搜索时使用。',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string', description: '搜索关键词' }
        },
        required: ['query']
      }
    }
  }
];

// 功能7补丁：Anthropic 原生格式转换。
// 我们的 TOOLS 用 OpenAI 格式 {type:"function", function:{name,description,parameters}}，
// 新版 Anthropic API 要求原生格式 {type:"custom", name, description, input_schema}。
// 前端工具（Kelivo 发来的）已经是原生格式，原样保留。
function toAnthropicTool(t) {
  if (t.function) {
    // OpenAI 格式 → Anthropic 原生
    const fn = t.function;
    return {
      type: 'custom',
      name: fn.name,
      description: fn.description || '',
      input_schema: fn.parameters || { type: 'object', properties: {} }
    };
  }
  // 已经是原生格式，原样返回
  return t;
}

// 读取 L2 画像
async function getProfile() {
  const fs = require('fs').promises;
  const dataDir = (process.env.MEMORY_DATA_DIR || (require('os').homedir() + '/memory-system/data')) + '/profile';
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

// 获取 Custom Prompt（用户自定义交互偏好）
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

// 搜索记忆获取目录 + 时间感知头部 + 随机浮现逻辑
// 头部两条动态信息（都注入 memoryMenu，不进 system 前缀，不影响 prompt caching）：
//   1. 当前北京时间 —— 给 LLM 时间锚点，避免把历史记忆里的相对时间（"过了十一天"）
//      误当成当前状态（问题1）。
//   2. 会话间隔感知 —— 距上次对话多久，像朋友那样感知"好久不见"（问题2，阈值触发）。
// clock：前端自带的时钟提示（clientClockFlags），配合 GATEWAY_TIME_HEADER 决定加不加时间头。
async function getMemoryMenu(userMessage, convId, menuCtx, clock = {}) {
  const showClock = TIME_HEADER_MODE === 'on' || (TIME_HEADER_MODE !== 'off' && !clock.hasClock);
  const showGap = TIME_HEADER_MODE === 'on' || (TIME_HEADER_MODE !== 'off' && !clock.hasGap);
  // 1. 当前时间（用户时区，带周几）。realNowMs 是真实的 UTC 毫秒数，用来算间隔。
  const realNowMs = Date.now();
  const currentDateStr = fmtLocal(new Date(realNowMs), { weekday: true });

  // 2. 距上次对话的间隔感知（阈值触发）
  let sessionNote = '';
  if (showGap) try {
    const sessionRes = await axios.get(
      `${MEMORY_SERVICE_URL}/last_session?conv_id=${encodeURIComponent(convId || '')}`,
      { timeout: 3000 }
    );
    const lastTs = sessionRes.data.last_ts;
    if (lastTs) {
      // 库里存的是 UTC（"YYYY-MM-DD HH:MM:SS"），parseStoredTs 按 UTC 解析；解析不了会在下一行抛错、被外层跳过
      const lastDate = parseStoredTs(lastTs);
      const gapHours = (realNowMs - lastDate.getTime()) / (1000 * 60 * 60);
      if (gapHours >= SESSION_GAP_THRESHOLD_HOURS && gapHours < 24) {
        sessionNote = `距上次对话约${Math.round(gapHours)}小时`;
      } else if (gapHours >= 24 && gapHours < 24 * 7) {
        sessionNote = `距上次对话约${Math.round(gapHours / 24)}天`;
      } else if (gapHours >= 24 * 7) {
        sessionNote = `距上次对话约${Math.round(gapHours / 24)}天（有段时间没见）`;
      }
    }
  } catch (e) {
    // 查不到上次时间时静默跳过，不影响主流程
  }

  // 3. 构建头部（时间锚点 + 可选的间隔感知）；两项都不加时头部为空，没有浮现记忆就整段不注入
  const headerLines = [];
  if (showClock) headerLines.push(`[当前时间：${currentDateStr}（${USER_TZ_LABEL}）]`);
  if (sessionNote) headerLines.push(`[${sessionNote}]`);
  const header = headerLines.join('\n');

  // 4. 随机门控：是否开放回忆？
  if (Math.random() > SURFACE_PROBABILITY) {
    return header + '\n';
  }

  // 5. 主通道：相关度驱动
  let selectedMemories = await relevanceDrivenSurfacing(userMessage);

  // 6. 并行通道：无由来浮现
  if (Math.random() < SERENDIPITY_PROBABILITY) {
    const serendipityMemory = await serendipitousSurfacing(convId);
    if (serendipityMemory) {
      // 无由来浮现优先，但总数不超过 2 条
      selectedMemories = [serendipityMemory, ...selectedMemories].slice(0, 2);
    }
  }

  if (selectedMemories.length === 0) return header + '\n';

  // 7. 格式化为自然语言念头
  const formattedMemories = formatMemoriesAsThoughts(selectedMemories);

  // 8. 展示指标：不在这里立即更新（以前组装菜单时就计数，请求失败/重新生成也会算"展示过"并进入冷却）。
  //    把 id 交给调用方，等回复正常结束后再更新（见 chatCompletionsMain 里的 updateDisplayMetrics 调用）。
  const shownIds = selectedMemories.map(m => m.id);
  if (menuCtx) menuCtx.shownIds = shownIds;
  else await updateDisplayMetrics(shownIds);

  return header + formattedMemories;
}

// 相关度驱动的记忆浮现（主通道）
async function relevanceDrivenSurfacing(userMessage) {
  // 寒暄/极短回复（"嗯嗯""好呀""哈哈"）不走相关度召回：这类消息几乎和任何记忆都"有点像"
  // （实测"嗯嗯"的 vec_sim 也能到 0.5 左右），召回出来只会显得突兀。无由来浮现通道不受影响。
  const core = String(userMessage || '').replace(/[\s\p{P}\p{S}~～]/gu, '');
  if (core.length <= 3) return [];
  try {
    const res = await axios.post(`${MEMORY_SERVICE_URL}/search`, {
      query: userMessage,
      mode: "semantic",
      n: 20  // 扩大候选池
    });
    let memories = res.data.memories || [];

    // 相关度过滤（B1：用余弦相似度 vec_sim 做绝对门槛）
    // 校准用日志：只记 id 和分数，不记内容
    console.log('[SURFACE] 候选 vec_sim:', memories.slice(0, 8).map(m => `${m.id}=${m.vec_sim}`).join(' '),
      '| 门槛', SURFACE_MIN_SIM);
    memories = memories.filter(m => typeof m.vec_sim === 'number' && m.vec_sim >= SURFACE_MIN_SIM);

    // 冷却过滤
    const cooldownMs = COOLDOWN_HOURS * 3600 * 1000;
    const now = Date.now();
    memories = memories.filter(m => {
      const lastShown = m.metadata.last_shown_at;
      if (!lastShown) return true;
      return (now - lastShown) >= cooldownMs;
    });

    if (memories.length === 0) return [];

    // 加权随机抽取
    const numToShow = weightedRandomChoice(SHOW_COUNT_WEIGHTS);
    if (numToShow === 0) return [];

    return weightedSample(memories, numToShow);
  } catch (e) {
    console.error('记忆检索失败:', e.message);
    return [];
  }
}

// 无由来浮现（并行通道）
async function serendipitousSurfacing(convId) {
  try {
    const res = await axios.post(`${MEMORY_SERVICE_URL}/search_serendipity`, {
      min_arousal: 0.6,
      cooldown_days: SERENDIPITY_COOLDOWN_DAYS,
      n: 10
    });

    const candidates = res.data.memories || [];
    if (candidates.length === 0) return null;

    // 加权随机选一条（情感强度 + 核心记忆权重 + 时间衰减）
    const weights = candidates.map(m => {
      const arousal = m.metadata.arousal || 0;
      const coreBonus = m.metadata.is_core ? 0.5 : 0;
      const memoryDate = parseStoredTs(m.metadata.ts) || new Date();
      const daysAgo = (Date.now() - memoryDate.getTime()) / (24 * 3600 * 1000);
      const timeDecay = Math.max(0.3, 1 - daysAgo / 365);
      return (arousal + coreBonus) * timeDecay;
    });

    const totalWeight = weights.reduce((sum, w) => sum + w, 0);
    let rand = Math.random() * totalWeight;

    for (let i = 0; i < candidates.length; i++) {
      rand -= weights[i];
      if (rand <= 0) return candidates[i];
    }

    return candidates[0];  // fallback
  } catch (e) {
    console.error('[SERENDIPITY] 查询失败:', e.message);
    return null;
  }
}

// 加权随机选择（从权重数组中选一个值）
function weightedRandomChoice(options) {
  const total = options.reduce((sum, opt) => sum + opt.weight, 0);
  let rand = Math.random() * total;
  for (const opt of options) {
    rand -= opt.weight;
    if (rand <= 0) return opt.value;
  }
  return options[options.length - 1].value;
}

// 加权采样（按 score 加权，分数越高越容易被选中）
function weightedSample(items, n) {
  const sampled = [];
  const pool = [...items];

  for (let i = 0; i < n && pool.length > 0; i++) {
    const totalScore = pool.reduce((sum, m) => sum + m.score, 0);
    if (totalScore === 0) break;

    let rand = Math.random() * totalScore;

    for (let j = 0; j < pool.length; j++) {
      rand -= pool[j].score;
      if (rand <= 0) {
        sampled.push(pool[j]);
        pool.splice(j, 1);
        break;
      }
    }
  }

  return sampled;
}

// 格式化为自然语言念头
function formatMemoriesAsThoughts(memories) {
  if (memories.length === 0) return '';

  const thoughts = memories.map(m => {
    const meta = m.metadata || {};
    const timePhrase = toFuzzyTime(meta.ts);
    const coreMarker = meta.is_core ? '⭐ ' : '';
    const summary = m.l1_summary || '';

    return `（${coreMarker}${timePhrase}，${summary}）`;
  });

  return '\n' + thoughts.join('\n\n') + '\n';
}

// 模糊化时间（精确日期 → 自然语言时间描述）
function toFuzzyTime(isoTimestamp) {
  if (!isoTimestamp) return '不知何时';

  try {
    // 按用户时区的日历天算（以前按「过了几个 24 小时」，昨天傍晚的事会被说成「今天早些时候」，
    // 而且把 UTC 当本地时间解析，又差了 8 小时）
    const memoryDate = parseStoredTs(isoTimestamp);
    if (!memoryDate) return '很久之前';
    const now = new Date();
    const dayDiff = localDayNumber(now) - localDayNumber(memoryDate);

    if (dayDiff <= 0) return '今天早些时候';
    if (dayDiff === 1) return '昨天';
    if (dayDiff === 2) return '前天';
    if (dayDiff < 7) return '最近';
    if (dayDiff < 14) return '前阵子';
    if (dayDiff < 30) return '不久之前';

    // 季节映射（用户时区的月份）
    const month = +localParts(memoryDate).month - 1;  // 0-11
    const seasons = [
      '冬天', '冬末', '早春', '春天', '晚春', '初夏',
      '夏天', '盛夏', '初秋', '秋天', '深秋', '初冬'
    ];
    const season = seasons[month];

    if (dayDiff < 365) {
      return season;
    } else {
      const yearDiff = +localParts(now).year - +localParts(memoryDate).year;
      if (yearDiff <= 1) return `去年${season}`;
      return `${yearDiff}年前的${season}`;
    }
  } catch (e) {
    return '很久之前';
  }
}

// 更新展示指标（display_count + last_shown_at）
async function updateDisplayMetrics(memoryIds) {
  if (memoryIds.length === 0) return;

  try {
    await axios.post(`${MEMORY_SERVICE_URL}/memories/increment_display_count`, {
      memory_ids: memoryIds
    });

    await axios.post(`${MEMORY_SERVICE_URL}/memories/update_last_shown`, {
      memory_ids: memoryIds,
      timestamp: Date.now()
    });
  } catch (e) {
    console.error('[DISPLAY_METRICS] 更新失败:', e.message);
  }
}


// 执行工具调用
// 把工具执行结果文本转成 Kelivo 原生"来源摘要卡"的 items（仅前端展示，不进 L0/上下文）。
// recall 文本含 "━━记忆 #N ━━" 分隔块 → 每条记忆一个条目；其他工具 → 前两行摘要。
function toolResultToItems(name, resultText) {
  const text = String(resultText || '');
  const today = new Date().toISOString().slice(0, 10);
  const SH = String.fromCharCode(35);
  const NL = String.fromCharCode(10);
  function mk(title) {
    return { type: 'web_search_result', title: String(title).slice(0, 120), url: '', page_age: today };
  }
  if (name === 'recall') {
    const items = [];
    const blocks = text.split('━━');
    for (let i = 0; i < blocks.length; i++) {
      const b = blocks[i];
      if (b.indexOf('记忆 ' + SH) < 0) continue;
      const body = blocks[i + 1] || '';
      let title = '';
      for (const ln of body.split(NL)) {
        const t = ln.trim();
        if (t.indexOf('💬') === 0) { title = t.slice(2).trim(); break; }
        if (t.indexOf('📅') === 0 && title === '') { title = t.slice(2).trim() || '记忆'; }
      }
      const num = b.split('记忆 ' + SH)[1].split(' ')[0].trim();
      items.push(mk('记忆 ' + SH + num + (title ? ' · ' + title : '')));
      i++;
    }
    if (items.length === 0) {
      const head = text.split(NL).filter(x => x.trim()).slice(0, 2).join(' · ');
      items.push(mk(head || '未找到相关记忆。'));
    }
    return items.slice(0, 8);
  }
  const head = text.split(NL).filter(x => x.trim()).slice(0, 2).join(' · ');
  return [mk(head || '已完成')];
}

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
        emotion_valence: args.emotion_valence,   // B3：以前工具声明了这个参数却没有转发
        n: 5
      });
      if (res.data.error) return `记忆检索出错：${res.data.error}`;
      const memories = res.data.memories || [];
      if (memories.length === 0) return '未找到相关记忆。';

      // 更新 access_count（显式 recall）
      const memoryIds = memories.map(m => m.id);
      await axios.post(`${MEMORY_SERVICE_URL}/memories/increment_access_count`, {
        memory_ids: memoryIds
      }).catch(e => console.error('[ACCESS_COUNT] 更新失败:', e.message));

      let result = '';
      memories.forEach((m, i) => {
        const meta = m.metadata || {};
        result += `\n━━ 记忆 #${i + 1} ━━\n`;
        const ts = fmtLocal(meta.ts) || '未知时间';   // 用户时区的日期和时刻（以前直接截 UTC 日期）
        const feelSrc = (meta.event_type === 'feel')
          ? ((meta.client === 'ai_self') ? 'AI自述' : '对话提取') : '';
        result += `📅 ${ts} | 🏷️ ${meta.event_type || ''} | 📍 ${meta.client || ''}${feelSrc ? '（' + feelSrc + '）' : ''}\n`;
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
      return '感受已记录 ✓ 内容：「' + String(args.content || '').slice(0, 80) + '」';
    } catch (e) {
      return `记录感受失败: ${e.message}`;
    }
  }  
  
  if (name === 'update_profile') {
    const fs = require('fs').promises;
    const dataDir = (process.env.MEMORY_DATA_DIR || (require('os').homedir() + '/memory-system/data')) + '/profile';
    const filePath = `${dataDir}/${args.section}.md`;
    try {
      if (args.action === 'append') {
        const current = await fs.readFile(filePath, 'utf8').catch(() => '');
        await fs.writeFile(filePath, current + '\n' + args.content, 'utf8');
        return '已追加到画像（' + (args.section || '') + '）：「' + String(args.content || '').slice(0, 80) + '」';
      } else if (args.action === 'rewrite') {
        // 备份旧版本
        const current = await fs.readFile(filePath, 'utf8').catch(() => '');
        if (current) {
          const backupName = `${args.section}_${new Date().toISOString().replace(/[:.]/g, '-')}.md`;
          await fs.writeFile(`${dataDir}/${backupName}`, current, 'utf8');
        }
        await fs.writeFile(filePath, args.content, 'utf8');
        return '画像已重写（' + (args.section || '') + '）：「' + String(args.content || '').slice(0, 80) + '」（旧版本已备份）';
      }
    } catch (e) {
      return `更新画像失败: ${e.message}`;
    }
  }
  
  // Kelivo 常用工具兼容（前端实际传的时间工具名是 get_time_info）
  if (name === 'get_current_time' || name === 'get_time' || name === 'get_time_info') {
    return '当前时间：' + fmtLocal(new Date(), { seconds: true, weekday: true }) + '（' + USER_TZ_LABEL + '）';
  }

  // 功能7：博查 API 联网搜索（显式可控的搜索工具）
  if (name === 'bocha_search') {
    console.log('🔍 [BOCHA_SEARCH] 触发！query:', args.query);
    const bochaKey = process.env.BOCHA_API_KEY;
    if (!bochaKey) return '网络搜索未配置（缺少 BOCHA_API_KEY）。';
    try {
      const res = await axios.post('https://api.bochaai.com/v1/web-search', {
        query: args.query,
        summary: true,
        count: 5
      }, {
        headers: {
          'Authorization': `Bearer ${bochaKey}`,
          'Content-Type': 'application/json'
        },
        timeout: 20000
      });
      const pages = res.data?.data?.webPages?.value || [];
      if (pages.length === 0) return '未搜索到相关结果。';
      let out = `[网络搜索结果：${args.query}]\n`;
      pages.forEach((p, i) => {
        const text = p.summary || p.snippet || '';
        out += `\n${i + 1}. ${p.name || ''}\n${p.url || ''}\n${text}\n`;
      });
      return out;
    } catch (e) {
      return `网络搜索失败: ${e.response?.status || ''} ${e.message}`;
    }
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
  let started = false;   // bug修复：message_start 是否已发（Anthropic 只能发一次）
  return {
    start() {
      // bug修复：Anthropic 的 message_start 延迟到拿到上游真实 input usage 再发（见 _ensureStarted）。
      // Kelivo 只认第一个 message_start，提前发占位 0 会让输入 token 永远显示 0。
      if (!isAnthropic) {
        sseSendRaw(res, {
          id: msgId, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000),
          model: requestModel, choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }]
        });
      }
    },
    // bug修复：惰性发送 Anthropic message_start，只发一次。
    // 优先带上游真实 input usage；若在拿到 usage 前就有内容要发（异常兜底），用占位 0。
    _ensureStarted(u) {
      if (!isAnthropic || started) return;
      started = true;
      const usageObj = { input_tokens: (u && u.input_tokens) || 0, output_tokens: 0 };
      if (u && u.cache_creation_input_tokens != null) usageObj.cache_creation_input_tokens = u.cache_creation_input_tokens;
      if (u && u.cache_read_input_tokens != null) usageObj.cache_read_input_tokens = u.cache_read_input_tokens;
      sseSend(res, 'message_start', {
        type: 'message_start',
        message: {
          id: msgId, type: 'message', role: 'assistant', model: requestModel,
          content: [], stop_reason: null, stop_sequence: null,
          usage: usageObj
        }
      });
    },
    // 内部：确保打开正确类型的块（类型切换时先关旧块再开新块）
    _open(type) {
      this._ensureStarted();   // bug修复：任何内容块前必须已发 message_start
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
      this._ensureStarted();   // bug修复：工具卡片块前必须已发 message_start
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
    // bug修复：解除工具卡片的 loading 状态。发一个 web_search_tool_result 块（Kelivo 的 Claude 解析器
    // 唯一会转成 toolResults chunk 的流块），tool_use_id 匹配卡片 id → Kelivo 按 id 命中并把该工具块
    // loading 置为 false。不解除的话 hasLoadingTool 恒真，token 永远显示 0。
    // v2: content 填 items（web_search_result 类型），Kelivo 渲染成"N 条记忆/引用"摘要卡片，展示工具结果。
    toolResolve(id, items) {
      if (!isAnthropic) return;
      feIndex++;
      const idx = feIndex;
      sseSend(res, 'content_block_start', {
        type: 'content_block_start', index: idx,
        content_block: {
          type: 'web_search_tool_result',
          tool_use_id: id,
          // content 为工具结果摘要 items（Kelivo 原生渲染来源摘要卡）
          content: items || []
        }
      });
      sseSend(res, 'content_block_stop', { type: 'content_block_stop', index: idx });
    },
    // Pluvia：每次上游调用结束后关掉当前块，让每轮的 thinking/text 各自成块（不和下一轮的拼在一起）
    closeBlock() {
      if (!isAnthropic || openType === null) return;
      sseSend(res, 'content_block_stop', { type: 'content_block_stop', index: feIndex });
      openType = null;
    },
    // Pluvia tool_events：记忆工具执行过程（只用于显示）。不认识它的客户端会忽略：
    // OpenAI 这一块没有 choices；Anthropic 是自定义事件类型。
    toolEvent(tool) {
      const data = { type: 'pluvia_tool', tool };
      if (isAnthropic) { this._ensureStarted(); sseSend(res, 'pluvia_tool', data); }
      else sseSendRaw(res, data);
    },
    // Pluvia client_tools：把前端工具的调用原样交给前端（随后用 finish(..., 'tool_calls') 收尾）
    clientToolCalls(uses) {
      if (isAnthropic) {
        this._ensureStarted();
        if (openType !== null) {
          sseSend(res, 'content_block_stop', { type: 'content_block_stop', index: feIndex });
          openType = null;
        }
        for (const u of uses) {
          feIndex++;
          sseSend(res, 'content_block_start', {
            type: 'content_block_start', index: feIndex,
            content_block: { type: 'tool_use', id: u.id, name: u.name, input: {} }
          });
          sseSend(res, 'content_block_delta', {
            type: 'content_block_delta', index: feIndex,
            delta: { type: 'input_json_delta', partial_json: JSON.stringify(u.input || {}) }
          });
          sseSend(res, 'content_block_stop', { type: 'content_block_stop', index: feIndex });
        }
      } else {
        uses.forEach((u, i) => {
          sseSendRaw(res, {
            id: msgId, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000),
            model: requestModel,
            choices: [{ index: 0, delta: { tool_calls: [{ index: i, id: u.id, type: 'function',
              function: { name: u.name, arguments: JSON.stringify(u.input || {}) } }] }, finish_reason: null }]
          });
        });
      }
    },
    // bug修复：上游 message_start 拿到真实 input usage 时调用，触发惰性发送带真实 token 的 message_start。
    // Kelivo 从 message_start.message.usage.input_tokens 读输入 token。output_tokens 走 message_delta。
    inputUsage(u) {
      this._ensureStarted(u);
    },
    // 收尾。功能4：usage 为跨轮累加后的真实 token 用量，写进最后的 message_delta / chunk。
    // openaiReason：OpenAI 格式的 finish_reason，默认 'stop'（Kelivo 路径不传，保持原样）
    finish(stopReason, usage, openaiReason) {
      const u = usage || { input_tokens: 0, output_tokens: 0 };
      if (isAnthropic) {
        this._ensureStarted(u);
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
          model: requestModel, choices: [{ index: 0, delta: {}, finish_reason: openaiReason || 'stop' }],
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
// signal（可选）：前端断开时中止上游请求（Pluvia 路径用；Kelivo 路径不传，行为不变）。
async function streamRound({ apiUrl, apiKey, requestModel, system, messages, tools, emitter, thinking, outputConfig, maxTokens, toolChoice, signal }) {
  // 功能2：仅透传前端请求的 thinking。开启时 max_tokens 必须大于 budget_tokens。
  // max_tokens 优先用前端传的值（Kelivo 里的设置），未传才兜底 4096。
  const body = {
    model: requestModel,
    system,   // 功能5：数组分段（稳定前缀带 cache_control）
    messages,
    max_tokens: maxTokens || 4096,
    stream: true
  };
  // 没有工具时不带 tools / tool_choice（Pluvia 的辅助请求）；Kelivo 路径总有记忆工具，不受影响
  if (tools && tools.length) body.tools = tools;
  // 功能7补丁：透传 tool_choice，让强制工具调用生效
  if (toolChoice && body.tools) body.tool_choice = toolChoice;
  if (thinking) {
    body.thinking = thinking;
    // adaptive 格式没有 budget_tokens；前端调的预算被 Kelivo 换算成 output_config.effort。
    const budget = thinking.budget_tokens || 1024;
    // 安全兜底：max_tokens 必须大于 budget，否则上游报错。留 4096 给正文。
    if (body.max_tokens <= budget) body.max_tokens = budget + 4096;
  }
  // 功能2补丁：透传 output_config（effort），让前端的思考预算调节真正生效。
  if (outputConfig) body.output_config = outputConfig;
  logUpstreamBody(body);
  // 建连：此时还没往前端发任何数据，重试、换中转都安全（见 postWithFailover）。
  // 抵御中转站冷启动 / 偶发失败（空回主因），以及余额不足、密钥失效这类整个中转不可用的情况。
  const upstream = (await postWithFailover({ apiUrl, apiKey, requestModel, body, stream: true, signal })).resp;

  return await new Promise((resolve, reject) => {
    const blocks = {};       // 上游 index -> block
    let stopReason = null;
    let msgId = null;
    let buffer = '';
    let ended = false;
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
            // bug修复：拿到真实 input usage 后补发 message_start 覆盖占位的 0（只补发一次）
            emitter.inputUsage(usage);
          }
          break;
        case 'content_block_start': {
          const cb = evt.content_block || {};
          blocks[evt.index] = {
            type: cb.type, text: cb.text || '', name: cb.name, id: cb.id,
            thinking: cb.thinking || '', signature: cb.signature || '', data: cb.data, _partialJson: ''
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
        // 被加密的思考块：工具循环回喂上游时必须原样带上（以前直接丢了）
        if (b.type === 'redacted_thinking') return b.data ? { type: 'redacted_thinking', data: b.data } : null;
        if (b.type === 'text') return { type: 'text', text: b.text };
        if (b.type === 'tool_use') {
          let input = {};
          try { input = b._partialJson ? JSON.parse(b._partialJson) : {}; } catch (e) { }
          return { type: 'tool_use', id: b.id, name: b.name, input };
        }
        return null;
      }).filter(Boolean);
      console.log('[THINK-DBG] 聚合块:', JSON.stringify(content.map(function(c){ return c.type; })));
      console.log('[THINK-DBG] 聚合块:', JSON.stringify(content.map(function(c){ return c.type; })));
      ended = true;
      resolve({ id: msgId, content, stop_reason: stopReason, usage });
    });

    upstream.data.on('error', reject);
    // 中止（前端断开）时流可能只 close 不 end，这里兜底让 Promise 结束
    upstream.data.on('close', () => { if (!ended) reject(new Error('上游流在结束前被关闭')); });
  });
}

// 记一行上游请求体的规模（只记大小，不记内容），用来核对中转站报的 token 数
function logUpstreamBody(body) {
  try {
    const sysChars = JSON.stringify(body.system || '').length;
    const msgChars = JSON.stringify(body.messages || []).length;
    const toolChars = JSON.stringify(body.tools || []).length;
    console.log('[UPSTREAM] 请求体：system', sysChars, '字符 | 消息', (body.messages || []).length, '条', msgChars, '字符 | 工具',
      (body.tools || []).length, '个', toolChars, '字符 | tool_choice', body.tool_choice ? JSON.stringify(body.tool_choice) : '无',
      '| thinking', body.thinking ? JSON.stringify(body.thinking) : '无');
  } catch (_) {}
}

// 读出 axios 错误里的响应体（流式请求时它是一个流），最多 4KB、最多等 3 秒
async function readErrorBody(e) {
  const d = e && e.response && e.response.data;
  if (d == null) return null;
  if (typeof d.on !== 'function') return typeof d === 'string' ? d : JSON.stringify(d);
  return await new Promise(resolve => {
    let buf = '';
    const t = setTimeout(() => { resolve(buf); try { d.destroy(); } catch (_) {} }, 3000);
    d.on('data', c => { if (buf.length < 4096) buf += c.toString('utf8'); });
    d.on('end', () => { clearTimeout(t); resolve(buf); });
    d.on('error', () => { clearTimeout(t); resolve(buf); });
  });
}
function upstreamErrorMessage(raw) {
  try {
    const j = JSON.parse(raw);
    return (j.error && (j.error.message || j.error.type)) || j.message || String(raw);
  } catch (_) {
    return String(raw);
  }
}

// ============ 多中转自动切换（failover） ============
// 当前启用的中转出错时（网络错误、超时、5xx、429、401/402/403/404 —— 比如余额不足、密钥失效），
// 自动改用 Dashboard 里配置的其它「聊天」中转（Anthropic 格式）。
//  - 400 之类的请求错误换中转也没用，不切换；
//  - 只在还没往前端输出任何内容时切换（建连阶段），输出到一半出错不切换；
//  - 模型名按「去掉厂商前缀、点换成横线」对应（claude-opus-4-6 ↔ anthropic/claude-opus-4.6），
//    没有同款模型的中转直接跳过，不会悄悄换成别的模型。
// .env：GATEWAY_FAILOVER=on|off（默认 on）；GATEWAY_FAILOVER_ORDER=fuse,openrouter（只用这几个、按这个顺序；不设则按 Dashboard 里的顺序）。
const FAILOVER_MODE = String(process.env.GATEWAY_FAILOVER || 'on').trim().toLowerCase();
const FAILOVER_ORDER = String(process.env.GATEWAY_FAILOVER_ORDER || '').split(',').map(s => s.trim()).filter(Boolean);
let chatTargets = [];   // [{ name, apiUrl, apiKey, models, active }]，来自记忆服务 /config/chat_targets

async function loadChatTargets() {
  try {
    const r = await axios.get(`${MEMORY_SERVICE_URL}/config/chat_targets`, { timeout: 5000 });
    const list = (r.data && r.data.targets) || [];
    chatTargets = list.map(t => {
      const base = String(t.base_url || '').replace(/\/$/, '');
      return { name: t.name, apiUrl: base.endsWith('/messages') ? base : base + '/messages', apiKey: t.api_key,
        models: Array.isArray(t.models) ? t.models : [], active: !!t.active };
    }).filter(t => t.apiKey && t.apiUrl);
    console.log('[FAILOVER] 可用中转：' + (chatTargets.map(t => t.name + (t.active ? '（当前）' : '')).join('、') || '无') +
      ' | 自动切换：' + FAILOVER_MODE + (FAILOVER_ORDER.length ? ' | 备用顺序：' + FAILOVER_ORDER.join('、') : ''));
  } catch (e) {
    console.error('[FAILOVER] 读取中转列表失败（不影响主中转）：', e.message);
  }
}

function normalizeModelName(s) {
  return String(s || '').toLowerCase().replace(/^[^/]+\//, '').replace(/:.*$/, '').replace(/\./g, '-');
}
// 这次请求依次尝试的上游：先主中转，再其它中转（各自换成对应的模型名）
function upstreamCandidates(apiUrl, apiKey, requestModel) {
  const primaryName = (activeConfig && apiKey === activeConfig.api_key) ? activeConfig.name : '主上游';
  const list = [{ name: primaryName, apiUrl, apiKey, model: requestModel }];
  if (FAILOVER_MODE === 'off') return list;
  let others = chatTargets.filter(t => !(t.apiUrl === apiUrl && t.apiKey === apiKey));
  if (FAILOVER_ORDER.length) {
    others = others.filter(t => FAILOVER_ORDER.includes(t.name))
      .sort((a, b) => FAILOVER_ORDER.indexOf(a.name) - FAILOVER_ORDER.indexOf(b.name));
  }
  const want = normalizeModelName(requestModel);
  for (const t of others) {
    let model = t.models.includes(requestModel) ? requestModel : null;
    if (!model) {
      const same = t.models.filter(m => normalizeModelName(m) === want);
      model = same.find(m => !m.includes(':')) || same[0] || null;
    }
    if (model) list.push({ name: t.name, apiUrl: t.apiUrl, apiKey: t.apiKey, model });
  }
  return list;
}
function isFailoverError(e) {
  if (!e || e.code === 'ERR_CANCELED' || e.name === 'CanceledError') return false;
  const st = e.response && e.response.status;
  if (!st) return true;   // 网络错误、超时
  return st >= 500 || [401, 402, 403, 404, 408, 429].includes(st);
}
// 发一次上游请求：主中转对偶发错误最多试 3 次；不行就按顺序换中转（每个试 1 次）。
// 成功返回 { resp, target }；全部失败时抛出最后一个错误，message 里列出每个中转的失败原因。
async function postWithFailover({ apiUrl, apiKey, requestModel, body, stream, signal }) {
  const cands = upstreamCandidates(apiUrl, apiKey, requestModel);
  const failures = [];
  let lastErr = null;
  for (let ci = 0; ci < cands.length; ci++) {
    const c = cands[ci];
    const attempts = ci === 0 ? 3 : 1;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      try {
        const resp = await axios.post(c.apiUrl, { ...body, model: c.model }, {
          headers: { 'Authorization': `Bearer ${c.apiKey}`, 'Content-Type': 'application/json', 'anthropic-version': '2023-06-01' },
          timeout: 120000,
          signal,
          ...(stream ? { responseType: 'stream' } : {})
        });
        if (ci > 0) console.log(`[FAILOVER] 这次由 ${c.name} 回答（${c.model}）`);
        return { resp, target: c };
      } catch (e) {
        lastErr = e;
        if (signal && signal.aborted) throw e;   // 前端已断开
        const st = e.response && e.response.status;
        console.error(`[UPSTREAM] ${c.name} 请求失败（第${attempt}次）：`, st || e.message);
        if (st && st < 500 && st !== 429 && st !== 408) break;   // 4xx 不重试同一个中转
        if (attempt < attempts) await new Promise(r => setTimeout(r, 500 * attempt));
      }
    }
    // 流式请求的错误说明在响应流里，读出来换成可读的文字
    const raw = await readErrorBody(lastErr);
    const msg = (raw ? upstreamErrorMessage(raw) : (lastErr.response ? `HTTP ${lastErr.response.status}` : lastErr.message)).slice(0, 200);
    console.error(`[UPSTREAM] ${c.name} 错误说明：`, msg);
    failures.push(`${c.name}：${msg}`);
    lastErr.message = msg;
    if (lastErr.response) lastErr.response.data = { error: { message: msg } };
    if (!isFailoverError(lastErr)) break;
    if (ci + 1 < cands.length) console.warn(`[FAILOVER] ${c.name} 不可用，改用 ${cands[ci + 1].name}`);
  }
  if (failures.length > 1) {
    const msg = '所有中转都失败了 —— ' + failures.join('；');
    lastErr.message = msg;
    if (lastErr.response) lastErr.response.data = { error: { message: msg } };
  }
  throw lastErr;
}

// 非流式跑一次上游（Pluvia 非流式用），返回结构与 streamRound 一致
async function upstreamOnce({ apiUrl, apiKey, requestModel, system, messages, tools, thinking, outputConfig, maxTokens, toolChoice, signal }) {
  const body = { model: requestModel, system, messages, max_tokens: maxTokens || 4096 };
  if (tools && tools.length) body.tools = tools;
  if (toolChoice && body.tools) body.tool_choice = toolChoice;
  if (thinking) {
    body.thinking = thinking;
    const budget = thinking.budget_tokens || 1024;
    if (body.max_tokens <= budget) body.max_tokens = budget + 4096;
  }
  if (outputConfig) body.output_config = outputConfig;
  logUpstreamBody(body);
  const r = (await postWithFailover({ apiUrl, apiKey, requestModel, body, stream: false, signal })).resp;
  const d = r.data || {};
  return { id: d.id, content: Array.isArray(d.content) ? d.content : [], stop_reason: d.stop_reason, usage: d.usage };
}

function addUsageTo(total, u) {
  if (!u) return;
  total.input_tokens += u.input_tokens || 0;
  total.output_tokens += u.output_tokens || 0;
  if (u.cache_creation_input_tokens != null)
    total.cache_creation_input_tokens = (total.cache_creation_input_tokens || 0) + u.cache_creation_input_tokens;
  if (u.cache_read_input_tokens != null)
    total.cache_read_input_tokens = (total.cache_read_input_tokens || 0) + u.cache_read_input_tokens;
}

// ============ Pluvia 辅助请求（约定 8.5：翻译思考链等，body 带 pluvia.aux） ============
// 不检索记忆；不注入记忆库的 system、记忆目录和时间头；system 只用前端给的；不带网关的记忆工具；不存 L0。
async function runPluviaAux(req, res, pv) {
  const messages = req.body.messages || [];
  const { apiUrl, apiKey, requestModel } = resolveUpstreamTarget(req.body.model);
  const systemBlocks = clientSystemBlocks(messages);
  const upstreamMessages = req.body._anthropicFormat
    ? messages.filter(m => m && m.role !== 'system').map(m => ({ ...m }))
    : oaMessagesToAnthropic(messages);
  const tools = (req.body.tools || []).map(toAnthropicTool);
  capCacheControl(systemBlocks, upstreamMessages, tools);
  console.log('[PLUVIA] 辅助请求 aux=' + pv.aux + '：不检索、不注入、不存');
  return await runPluvia({
    req, res, pv, stream: !!req.body.stream, isAnthropic: !!req.body._anthropicFormat,
    apiUrl, apiKey, requestModel, systemBlocks: systemBlocks.length ? systemBlocks : undefined,
    messages: upstreamMessages, allTools: tools, toolChoice: toAnthropicToolChoice(req.body.tool_choice),
    menuCtx: { shownIds: [] }, convId: resolveConvId(req), continued: false
  });
}

// ============ Pluvia v0.2 主流程（turn / tail / client_tools / tool_events） ============
// 和 Kelivo 路径的区别：
//  - 记忆工具在网关执行，过程用 pluvia_tool 事件告诉前端；不发 Kelivo 那两张变通卡片；
//  - 模型调了前端工具 → 把这些调用交回前端（OpenAI finish_reason=tool_calls / Anthropic stop_reason=tool_use），
//    这一轮的上游状态按 (conversation_id, turn.id) 存 10 分钟，前端带着工具结果续问时接回去；
//  - 只在一条回复的最后一轮存 L0（整条回复的文字合成一条 assistant）；前端中途断开（用户点停）就不存，并中止上游。
async function runPluvia({ req, res, pv, stream, isAnthropic, apiUrl, apiKey, requestModel, systemBlocks, messages, allTools, toolChoice, menuCtx, convId, continued }) {
  const key = pv.turn ? turnKey(convId, pv.turn.id) : null;
  const prev = (key && continued) ? turnStoreGet(key) : null;   // 本条回复前几轮的累计（文字、思考、浮现的记忆）
  const skipMemory = isTitleGenerationRequest(req) || !!pv.aux;   // 辅助请求（翻译思考链等）不进记忆
  const ac = new AbortController();
  let clientGone = false;
  res.on('close', () => {
    if (!res.writableFinished) { clientGone = true; ac.abort(); }
  });

  let thinking = req.body.thinking;
  if (thinking && toolLoopLacksThinking(messages)) {
    console.log('[PLUVIA] 续问时拿不回带签名的思考块，这一轮不带 thinking 参数');
    thinking = undefined;
  }

  let emitter = null;
  if (stream) {
    res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.flushHeaders && res.flushHeaders();
    emitter = makeEmitter(res, isAnthropic, requestModel);
    emitter.start();
  }

  const totalUsage = { input_tokens: 0, output_tokens: 0 };
  const toolEvents = [];       // 非流式时放进 pluvia_tools
  const displayBlocks = [];    // 非流式 Anthropic 响应的 content（不含记忆工具的 tool_use）
  const roundTexts = [];
  const thinkTexts = [];
  const convo = messages;      // 本次请求内的上游消息序列（工具循环中追加）
  let result = null;
  let handoff = null;
  let failed = null;

  try {
    let loops = 5;
    while (loops-- > 0) {
      const args = {
        apiUrl, apiKey, requestModel, system: systemBlocks, messages: convo, tools: allTools,
        thinking, outputConfig: req.body.output_config, maxTokens: req.body.max_tokens, toolChoice, signal: ac.signal
      };
      result = stream ? await streamRound({ ...args, emitter }) : await upstreamOnce(args);
      addUsageTo(totalUsage, result.usage);
      if (emitter) emitter.closeBlock();
      const t = extractAssistantText(result).trim();
      if (t) roundTexts.push(t);
      const th = result.content.filter(c => c.type === 'thinking').map(c => c.thinking || '').join('\n');
      if (th) thinkTexts.push(th);
      displayBlocks.push(...result.content.filter(c => c.type === 'thinking' || c.type === 'redacted_thinking' || c.type === 'text'));

      if (result.stop_reason !== 'tool_use') break;
      const uses = result.content.filter(c => c.type === 'tool_use');
      if (!uses.length) break;
      // 同一次回复里既有记忆工具又有前端工具：先执行记忆工具、结果留着，只把前端工具交出去
      const serverResults = {};
      for (const u of uses) {
        if (isServerTool(u.name)) serverResults[u.id] = await runServerTool(u, emitter, toolEvents);
      }
      convo.push({ role: 'assistant', content: result.content });
      const clientUses = uses.filter(u => !isServerTool(u.name));
      if (clientUses.length) { handoff = { uses, clientUses, serverResults }; break; }
      convo.push({ role: 'user', content: uses.map(u => ({ type: 'tool_result', tool_use_id: u.id, content: serverResults[u.id] })) });
      if (clientGone) break;
    }
  } catch (e) {
    failed = e;
  }

  if (clientGone) {
    console.log('[PLUVIA] 前端中途断开（用户点了停），这一轮不存 L0，已中止上游');
    return;
  }
  if (failed) {
    const msg = failed.response?.data?.error?.message || failed.message;
    console.error('[PLUVIA] 上游出错:', failed.response?.status || '', msg);
    if (stream) {
      try { emitter.textDelta(`\n[网关错误] ${msg}`); } catch (_) {}
      emitter.finish('end_turn', totalUsage);
    } else if (!res.headersSent) {
      res.status(502).json({ error: { message: msg, type: 'gateway_error' } });
    }
    return;   // 出错的这一轮不存；网关存的上一轮状态保留，前端可以重试
  }

  const allTexts = [...(prev ? prev.texts : []), ...roundTexts];
  const allThinks = [...(prev ? prev.thinks : []), ...thinkTexts];
  const shownIds = prev ? (prev.shownIds || []) : (menuCtx.shownIds || []);
  rememberUsage(convId, totalUsage);

  if (handoff) {
    if (key) {
      turnStoreSet(key, {
        upstream: convo.slice(),
        pending: {
          order: handoff.uses.map(u => u.id),
          server: handoff.serverResults,
          clientIds: handoff.clientUses.map(u => u.id)
        },
        round: pv.turn.round,
        texts: allTexts,
        thinks: allThinks,
        shownIds
      });
    }
    console.log('[PLUVIA] 交给前端执行:', handoff.clientUses.map(u => u.name).join(', '),
      '| turn=' + (pv.turn ? pv.turn.id + ' round=' + pv.turn.round : '（没带 turn，续问时接不回）'));
    if (stream) {
      emitter.clientToolCalls(handoff.clientUses);
      emitter.finish('tool_use', totalUsage, 'tool_calls');
    } else {
      sendPluviaNonStream(res, { isAnthropic, requestModel, result, usage: totalUsage, toolEvents,
        displayBlocks, texts: roundTexts, clientUses: handoff.clientUses });
    }
    return;   // 不是最后一轮，不存 L0
  }

  // 最后一轮
  const stopReason = (result && result.stop_reason && result.stop_reason !== 'tool_use') ? result.stop_reason : 'end_turn';
  if (stream) {
    emitter.finish(stopReason, totalUsage, stopReason === 'max_tokens' ? 'length' : 'stop');
  } else {
    sendPluviaNonStream(res, { isAnthropic, requestModel, result, usage: totalUsage, toolEvents,
      displayBlocks, texts: roundTexts, stopReason });
  }
  if (key) turnStore.delete(key);
  console.log('[CACHE] 输入', totalUsage.input_tokens, '| 写缓存', totalUsage.cache_creation_input_tokens || 0,
    '| 读缓存', totalUsage.cache_read_input_tokens || 0, '| 输出', totalUsage.output_tokens);
  if (skipMemory) return;

  // 存 L0：截掉本条回复前几轮的 assistant(工具调用)/工具结果，整条回复的文字合成一条 assistant
  const memMsgs = memoryMessages(req);
  let priorTexts = allTexts;
  if (!prev && pv.turn && pv.turn.round > 1) {
    // 没有存档（过期/重启）：前几轮的文字从前端回传的 assistant 里取
    const tail = memMsgs.slice(turnCut(memMsgs, pv.turn.round)).filter(m => m && m.role === 'assistant')
      .map(m => stripPluviaAssistantDecor(toPlainText(m.content)).trim()).filter(Boolean);
    priorTexts = [...tail, ...roundTexts];
  }
  // 各轮正文用空行连接，和前端历史里的写法一致（约定 8.2）
  const aiText = priorTexts.join('\n\n');
  const cut = pv.turn ? turnCut(memMsgs, pv.turn.round) : memMsgs.length;
  const cleaned = cleanMessagesForSave(memMsgs.slice(0, cut), aiText,
    { stripStamp: !req._tailOk, mergeAssistant: true, pluviaDecor: true });
  axios.post(MEMORY_SERVICE_URL + '/save_conversation', {
    conv_id: convId, client: req.headers['x-client-name'] || 'pluvia', messages: cleaned
  }).catch(err => console.error('保存对话失败:', err.message));
  updateDisplayMetrics(shownIds);
  const thinkText = allThinks.join('\n\n');
  if (thinkText.trim()) {
    axios.post(MEMORY_SERVICE_URL + '/thinking', {
      conv_id: convId, thinking: thinkText, answer_ref: aiText.slice(0, 200), full_reply: aiText
    }).catch(e => console.log('[THINK-SAVE] FAIL ' + e.message));
  }
}

function sendPluviaNonStream(res, { isAnthropic, requestModel, result, usage, toolEvents, displayBlocks, texts, clientUses, stopReason }) {
  const handoff = !!(clientUses && clientUses.length);
  if (isAnthropic) {
    const content = [...displayBlocks, ...(handoff ? clientUses : [])];
    const out = {
      id: (result && result.id) || 'msg_' + Date.now(), type: 'message', role: 'assistant', model: requestModel,
      content, stop_reason: handoff ? 'tool_use' : (stopReason || 'end_turn'), stop_sequence: null, usage
    };
    if (toolEvents.length) out.pluvia_tools = toolEvents;
    return res.json(out);
  }
  const message = { role: 'assistant', content: texts.join('\n\n') };
  if (handoff) {
    message.tool_calls = clientUses.map(u => ({
      id: u.id, type: 'function', function: { name: u.name, arguments: JSON.stringify(u.input || {}) }
    }));
  }
  const out = {
    id: (result && result.id) || 'chatcmpl-' + Date.now(), object: 'chat.completion',
    created: Math.floor(Date.now() / 1000), model: requestModel,
    choices: [{ index: 0, message, finish_reason: handoff ? 'tool_calls' : (stopReason === 'max_tokens' ? 'length' : 'stop') }],
    usage: {
      prompt_tokens: usage.input_tokens || 0,
      completion_tokens: usage.output_tokens || 0,
      total_tokens: (usage.input_tokens || 0) + (usage.output_tokens || 0)
    }
  };
  if (toolEvents.length) out.pluvia_tools = toolEvents;
  return res.json(out);
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
// /v1/chat/completions 入口：per-conv 串行队列包裹，防止同一会话并发导致上下文错乱。
// tg 客户端走组装模式（handleTgBotAssembled），其余走 chatCompletionsMain。
app.post('/v1/chat/completions', (req, res) => {
  const cid = resolveConvId(req);
  enqueueConv(cid, async () => {
    try {
      const clientName = (req.headers['x-client-name'] || '').toLowerCase();
      if (clientName.includes('tg')) {
        await handleTgBotAssembled(req, res);
      } else {
        await chatCompletionsMain(req, res);
      }
    } catch (e) {
      console.error('网关错误:', e.response?.data || e.message);
      if (res.headersSent) { try { res.end(); } catch (_) {} return; }
      res.status(500).json({ error: { message: e.response?.data?.error?.message || e.message, type: 'gateway_error' } });
    }
  });
});

// 原 handler 主体：Kelivo/Claude Code 等直接透传 messages 的客户端走这里。
async function chatCompletionsMain(req, res) {
  try {
    const { model, messages, stream } = req.body;
    const pv = pluviaCtx(req);              // Pluvia v0.2（body 带 pluvia）才有；否则 null
    const pvClient = isPluviaClient(req);   // Pluvia（含 v0.1）

    // ============ 功能3(B-1)：拦截 Kelivo 的工具回环请求 ============
    // Kelivo 看到 tool_use 卡片后，会自动执行该工具并回传一个"工具结果"请求
    // （最后一条 user 消息的 content 全是 tool_result 块）。但工具已在上一轮由网关
    // 内部执行并回答过，这里直接短路返回空 end_turn，避免重复调用、重复回答、污染记忆。
    // Pluvia v0.2 的工具结果是真正的续问（前端工具交回给前端执行），不能短路。
    if (!pv && isToolLoopback(messages)) {
      console.log('[LOOPBACK] 检测到工具回环请求，短路返回空 end_turn（不处理、不存记忆）');
      const loopModel = req.body.model || process.env.DEFAULT_MODEL || 'claude';
      // bug修复：回环没有上游请求、拿不到 usage。取出上一轮真实回答缓存的 usage 返回，
      // 否则回环是本轮最后收到的响应，Kelivo 会用它的 0 覆盖已显示的真实 token。
      const cachedUsage = lastUsageByConv.get(computeConvId(req)) || { input_tokens: 0, output_tokens: 0 };
      if (stream) {
        res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
        res.setHeader('Cache-Control', 'no-cache');
        res.setHeader('Connection', 'keep-alive');
        res.flushHeaders && res.flushHeaders();
        const msgId = 'msg_' + Date.now();
        // message_start 带回真实 input（含 cache 字段）
        const startUsage = { input_tokens: cachedUsage.input_tokens || 0, output_tokens: 0 };
        if (cachedUsage.cache_creation_input_tokens != null) startUsage.cache_creation_input_tokens = cachedUsage.cache_creation_input_tokens;
        if (cachedUsage.cache_read_input_tokens != null) startUsage.cache_read_input_tokens = cachedUsage.cache_read_input_tokens;
        sseSend(res, 'message_start', {
          type: 'message_start',
          message: {
            id: msgId, type: 'message', role: 'assistant', model: loopModel,
            content: [], stop_reason: null, stop_sequence: null,
            usage: startUsage
          }
        });
        sseSend(res, 'message_delta', {
          type: 'message_delta',
          delta: { stop_reason: 'end_turn', stop_sequence: null },
          usage: cachedUsage
        });
        sseSend(res, 'message_stop', { type: 'message_stop' });
        res.end();
      } else if (req.body._anthropicFormat) {
        res.json({
          id: 'msg_' + Date.now(), type: 'message', role: 'assistant', model: loopModel,
          content: [], stop_reason: 'end_turn', stop_sequence: null,
          usage: cachedUsage
        });
      } else {
        res.json({
          id: 'chatcmpl-' + Date.now(), object: 'chat.completion',
          created: Math.floor(Date.now() / 1000), model: loopModel,
          choices: [{ index: 0, message: { role: 'assistant', content: '' }, finish_reason: 'stop' }],
          usage: {
            prompt_tokens: cachedUsage.input_tokens || 0,
            completion_tokens: cachedUsage.output_tokens || 0,
            total_tokens: (cachedUsage.input_tokens || 0) + (cachedUsage.output_tokens || 0)
          }
        });
      }
      return;
    }

    // Pluvia 辅助请求（约定 8.5）：完全不走记忆，单独处理
    if (pv && pv.aux) return await runPluviaAux(req, res, pv);

    // 获取用户最新消息（检索用「给记忆用」的版本：剥掉 Pluvia 的「此刻」）
    let lastUserIdx = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i] && messages[i].role === 'user') { lastUserIdx = i; break; }
    }
    const lastUserMsg = lastUserIdx >= 0 ? messages[lastUserIdx] : null;
    const userContent = lastUserMsg ? lastUserMsg.content : '';
    const memUserText = lastUserIdx >= 0 ? memoryText(req, memoryMessages(req)[lastUserIdx].content) : '';

    // 时间感知：memoryMenu 头部要按 conv 算会话间隔，传入稳定的 conv_id
    const convIdForMenu = resolveConvId(req);

    // Pluvia client_tools 续问（同一条回复的第 2 轮起）：接回网关存的这一轮；记忆目录沿用第 1 轮注入的，不重新检索
    const pvContinuing = !!(pv && pv.turn && pv.turn.round > 1);
    let cont = null;
    if (pvContinuing) {
      const S = turnStoreGet(turnKey(convIdForMenu, pv.turn.id));
      if (S && S.pending) cont = buildContinuation(req, S);
      console.log('[PLUVIA] 续问 turn=' + pv.turn.id + ' round=' + pv.turn.round + (cont ? '，已接回网关存的这一轮' :
        '，没找到可用的存档（过期 / 重启 / 工具 id 对不上），改按前端回传的历史转换'));
    }

    // 任务E：effort 注入。Kelivo 显式传了 output_config/thinking 就不动；
    // 没传时按会话配置注入 effort（默认 medium），让每个会话可独立控制思考预算。
    if (!req.body.output_config && !req.body.thinking) {
      try {
        const s = await axios.get(`${MEMORY_SERVICE_URL}/conv/${encodeURIComponent(convIdForMenu)}/settings`, { timeout: 3000 });
        if (s.data && s.data.effort) req.body.output_config = { effort: s.data.effort };
      } catch (e) { /* 忽略，用上游默认 */ }
    }

    // 获取画像、记忆目录、Shared Narrative、Recent Summary、Custom Prompt（含时间锚点 + 会话间隔感知）
    const menuCtx = { shownIds: [] };   // 本轮浮现的记忆 id，回复成功后再计入展示指标
    const [profile, memoryMenu, sharedNarrative, recentSummary, customPrompt] = await Promise.all([
      getProfile(),
      pvContinuing ? Promise.resolve('')
        : getMemoryMenu(memUserText, convIdForMenu, menuCtx, clientClockFlags(req, toPlainText(userContent))),
      getSharedNarrative(),
      getRecentSummary(),
      getCustomPrompt()
    ]);
    // 功能5：构建可缓存的稳定 system 前缀（角色说明 + L2画像 + 工具说明）。
    // 动态的 memoryMenu 不放这里——它每轮都变，会让缓存前缀失效。改为注入到最新 user 消息前。
    let systemPrefix = `你拥有一个持续维护的记忆系统。

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
- recall: 需要更多细节时搜索记忆库（semantic=语义联想 / exact=逐字搜索完整原始对话记录，不受是否已被提炼进记忆摘要影响）
- feel: 记录你当下的感受
- update_profile: 更新人格画像
- bocha_search: 用博查联网搜索可靠的中文网络信息（供应商不支持内置搜索、或需要可信来源时使用）

当需要查询当前时间时，必须调用 get_current_time 工具获取准确时间，不要自己编造。

---

记忆系统偶尔会浮现一些过去的片段——这些不是你必须处理的信息，而是偶然想起的痕迹。

这是我们曾经共同经历过的一点痕迹。它们可能与当前话题有关，也可能无关。

不要刻意复述它们，也不要让它们限制你对当下的理解。只有在它们真的相关、真的有助于理解当前交流时，才自然地用上。

记忆片段只是部分提醒，不代表所有相关记忆都已列出。如果你觉得某个话题以前聊过但没看到相关片段，可以用 recall 去找（exact 模式能搜到原始对话）。`;

    // 如果有 Custom Prompt，追加到 systemPrefix
    if (customPrompt) {
      systemPrefix += `\n\n---\n\n[用户交互偏好]\n${customPrompt}`;
    }

    // system 用数组分段，稳定前缀带 cache_control 缓存断点（Anthropic prompt caching）
    const systemBlocks = [
      { type: 'text', text: systemPrefix, cache_control: { type: 'ephemeral', ttl: '1h' } }
    ];

    // 按需添加 cache② Shared Narrative
    if (sharedNarrative) {
      systemBlocks.push({
        type: 'text',
        text: `[共同经历叙事]\n\n${sharedNarrative}`,
        cache_control: { type: 'ephemeral', ttl: '1h' }
      });
    }

    // 按需添加 cache③ Recent Summary
    if (recentSummary) {
      systemBlocks.push({
        type: 'text',
        text: `[近期阶段摘要]\n\n${recentSummary}`,
        cache_control: { type: 'ephemeral', ttl: '1h' }
      });
    }

    // 功能5：把动态的相关记忆目录注入到最新一条 user 消息前面（保持 system 前缀稳定）。
    // content 可能是字符串或 Anthropic 块数组，两种都处理。
    // Pluvia 约定 8.1：pluvia.system = "append" 时，把前端的 system 接在记忆库 system 后面。
    // 前端自己打的缓存标记去掉，统一在最后一块放一个 1 小时断点（和网关 system 的其它断点一致）。
    if (pv && pv.system === 'append') {
      const cs = clientSystemBlocks(messages).map(({ cache_control, ...b }) => b);
      if (cs.length) {
        cs[0] = { ...cs[0], text: '\n\n---\n\n' + cs[0].text };
        cs[cs.length - 1].cache_control = { type: 'ephemeral', ttl: '1h' };
        systemBlocks.push(...cs);
      }
    }

    // Pluvia：OpenAI 格式先转成 Anthropic 格式（工具调用/工具结果/图片），续问时直接用接回的那一轮。
    const augmentedMessages = cont ? cont.messages
      : (pvClient && !req.body._anthropicFormat) ? oaMessagesToAnthropic(messages)
      : messages.filter(m => m.role !== 'system').map(m => ({ ...m }));
    if (!cont && memoryMenu && memoryMenu.trim()) {
      for (let i = augmentedMessages.length - 1; i >= 0; i--) {
        if (augmentedMessages[i].role !== 'user') continue;
        const m = augmentedMessages[i] = { ...augmentedMessages[i] };
        // memoryMenu 已自带头部（当前时间 / 会话间隔 / 相关记忆目录），直接注入，不再包一层
        injectMenu(m, `${memoryMenu}\n\n`, pvClient);
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
    // 功能7补丁：合并工具并统一转换为 Anthropic 原生格式（新版 API 要求）。
    // 前端工具已是原生格式，TOOLS 是 OpenAI 格式，toAnthropicTool 做统一归一。
    // Pluvia v0.2：前端工具和记忆工具撞名时以网关为准（去掉前端那份）。
    const clientTools = (req.body.tools || []).filter(t => !(pv && isServerTool(toolNameOf(t))));
    const allTools = [...clientTools, ...TOOLS].map(toAnthropicTool);
    console.log('[TOOLS] 合并后:', JSON.stringify(allTools.map(t => t.name)));
    // OpenAI 格式的 tool_choice（如 Pluvia 第 5 轮的 "none"）转成 Anthropic 格式；Anthropic 格式原样
    const toolChoice = toAnthropicToolChoice(req.body.tool_choice);
    capCacheControl(systemBlocks, nonSystemMessages, allTools);

    if (pv) {
      return await runPluvia({
        req, res, pv, stream: !!stream, isAnthropic: !!req.body._anthropicFormat,
        apiUrl, apiKey, requestModel, systemBlocks, messages: nonSystemMessages, allTools, toolChoice,
        menuCtx, convId: convIdForMenu, continued: !!cont
      });
    }

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
        const thinkAcc = [];
        while (maxLoops > 0) {
          result = await streamRound({
            apiUrl, apiKey, requestModel, system: systemBlocks,
            messages: nonSystemMessages, tools: allTools, emitter,
            thinking: req.body.thinking,
            outputConfig: req.body.output_config,
            maxTokens: req.body.max_tokens,
            toolChoice   // 功能7补丁
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

          const thinkBlocks = result.content.filter(c => c.type === 'thinking');
          if (thinkBlocks.length) thinkAcc.push(thinkBlocks.map(c => c.thinking || '').join('\n'));
          if (result.stop_reason !== 'tool_use') break;
          const toolUseBlocks = result.content.filter(c => c.type === 'tool_use');
          if (toolUseBlocks.length === 0) break;

          // 功能3：执行前先向前端 emit 只读展示卡片；工具仍在网关内部执行。
          const toolResults = [];
          for (const toolUse of toolUseBlocks) {
            // 下面两张卡片（toolUseCard / toolResolve）是专为 Kelivo 显示做的变通，Pluvia 不需要
            if (!pvClient) emitter.toolUseCard(toolUse.id, toolUse.name, toolUse.input);
            const toolResult = await executeTool(toolUse.name, toolUse.input);
            // 卡片是 tool_use 块 → Kelivo 会创建 loading 工具块，且因为它没有该工具的 onToolCall
            // 而永不解除，hasLoadingTool 恒真 → 阻断 token 归属（显示 0 的根因）。
            // 补发一个 tool_use_id 匹配的结果块解除该 loading（Kelivo 唯一会解析成 toolResults 的流块）。
            // Bug3：曾试过删掉这行、靠 Kelivo 本地路径解除 loading，但实测本地路径不解除（卡 loading/token 恒 0），
            // 故保留。原污染问题（web_search_tool_result 被硬编码成 search_web、按 tool_use_id 覆盖真实工具名，
            // 导致重进窗口工具卡片变"联网搜索"）已在 Kelivo 侧修复：解析 web_search_tool_result 时按
            // tool_use_id 取回真实工具名（如 recall），不再污染持久化事件。
            // toolResolve v2：工具结果转引用摘要卡（recall=记忆条目 / 其他=文本摘要），Kelivo 原生渲染
            if (!pvClient) emitter.toolResolve(toolUse.id, toolResultToItems(toolUse.name, toolResult));
            toolResults.push({ type: 'tool_result', tool_use_id: toolUse.id, content: toolResult });
          }
          nonSystemMessages.push({ role: 'assistant', content: result.content });
          nonSystemMessages.push({ role: 'user', content: toolResults });
          maxLoops--;
        }
        // 功能5：缓存命中日志（cache_read>0 即命中稳定前缀缓存）
        console.log('[CACHE] 输入', totalUsage.input_tokens, '| 写缓存', totalUsage.cache_creation_input_tokens || 0,
          '| 读缓存', totalUsage.cache_read_input_tokens || 0, '| 输出', totalUsage.output_tokens);
        // bug修复：记住本轮真实 usage，供随后可能到来的工具回环短路时返回（否则回环 0 会覆盖显示）
        rememberUsage(computeConvId(req), totalUsage);
        console.log('[THINK-DBG] thinkAcc=' + thinkAcc.length);
console.log('[THINK-DBG] thinkAcc=' + thinkAcc.length);
if (thinkAcc.length && result) {
          const replyText = extractAssistantText(result) || '';
          const thinkText = thinkAcc.join('\n\n');
          if (thinkText.trim()) {
            try {
              console.log('[THINK-SAVE] 保存思考链 len=' + thinkText.length + ' reply=' + replyText.length);
              axios.post(MEMORY_SERVICE_URL + '/thinking', {
                conv_id: computeConvId(req), thinking: thinkText,
                answer_ref: replyText.slice(0, 200), full_reply: replyText
              }).then(function() { console.log('[THINK-SAVE] OK'); }).catch(function(e) { console.log('[THINK-SAVE] FAIL ' + e.message); });
            } catch (e) {}
          }
        }
        emitter.finish(result ? result.stop_reason : 'end_turn', totalUsage);
        // 回复正常结束后才计入展示指标（display_count / last_shown_at → 冷却）
        if (!isTitleGenerationRequest(req)) updateDisplayMetrics(menuCtx.shownIds);
      } catch (streamErr) {
        console.error('[STREAM] 流式处理出错:', streamErr.response?.status || streamErr.message);
        // 已经发过头，只能在流内把错误信息作为正文吐出去并正常收尾，避免前端空回
        try {
          emitter.textDelta(`\n[网关错误] ${streamErr.response?.data?.error?.message || streamErr.message}`);
        } catch (_) {}
        emitter.finish('end_turn', totalUsage);
      }

      // 异步保存对话到 L0（与非流式一致）。问题2：标题生成请求不存记忆。
      if (!isTitleGenerationRequest(req)) {
        const client = req.headers['x-client-name'] || 'kelivo';
        const conv_id = resolveConvId(req);
        // 问题2 Bug A：把本轮生成的 AI 回复 append 进去再存。
        // Kelivo 每轮只发到当前 user 为止，不含刚生成的 AI 回复；不补的话
        // 单轮对话的回复永远存不进 L0，多轮也会丢最后一条 AI 回复。
        // L0 重复根治：cleanMessagesForSave 去空消息+相邻重复，稳定 msg_idx。
        const aiText = extractAssistantText(result);
        const cleanedMessages = cleanMessagesForSave(req.body.messages, aiText);
        axios.post(MEMORY_SERVICE_URL + '/save_conversation', {
          conv_id, client, messages: cleanedMessages
        }).catch(err => console.error('保存对话失败:', err.message));
      }
      return;
    }

    // 功能2：非流式也仅透传前端请求的 thinking，构造请求体的小 helper
    const buildBody = () => {
      const b = {
        model: requestModel,
        system: systemBlocks,   // 功能5：可缓存的稳定 system 前缀（数组 + cache_control）
        messages: nonSystemMessages,
        tools: allTools,   // 功能7补丁：用已格式转换的 allTools（Anthropic 原生格式）
        max_tokens: req.body.max_tokens || 4096
      };
      // 功能7补丁：透传 tool_choice（如果前端指定了），让强制工具调用生效
      if (toolChoice) b.tool_choice = toolChoice;
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

    const apiResponse = (await postWithFailover({ apiUrl, apiKey, requestModel, body: buildBody() })).resp;

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

      const followUp = (await postWithFailover({ apiUrl, apiKey, requestModel, body: buildBody() })).resp;

      result = followUp.data;
      addUsage(result.usage);
      maxLoops--;
    }
    // 用累加后的 usage 覆盖 result.usage（Anthropic 路径直接返回 result）
    result.usage = { ...(result.usage || {}), ...nsUsage };
    // bug修复：记住本轮真实 usage，供随后可能到来的工具回环短路时返回
    rememberUsage(computeConvId(req), nsUsage);

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
    
    // 异步保存对话到 L0（生成稳定的 conv_id）。问题2：标题生成请求不存记忆。
    if (!isTitleGenerationRequest(req)) {
      const client = req.headers['x-client-name'] || 'kelivo';
      const conv_id = resolveConvId(req);

      // 问题2 Bug A：append 本轮 AI 回复（原因见流式分支同处注释）
      // L0 重复根治：cleanMessagesForSave 去空消息+相邻重复，稳定 msg_idx。
      const aiText = extractAssistantText(result);
      const cleanedMessages = cleanMessagesForSave(req.body.messages, aiText);

      axios.post(MEMORY_SERVICE_URL + '/save_conversation', {
        conv_id: conv_id,
        client: client,
        messages: cleanedMessages
      }).catch(err => console.error('保存对话失败:', err.message));
      // 回复成功后才计入展示指标
      updateDisplayMetrics(menuCtx.shownIds);
    }

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
}

// ============ TG 桥专用：组装模式 ============
// TG 桥只发最新一条 user 消息 + conversation_id，网关从 L0 组装历史（按 conv_settings 窗口）。
// 独立分支，不影响 Kelivo 的 messages 透传逻辑。
async function handleTgBotAssembled(req, res) {
  try {
    const convId = req.body.conversation_id || resolveConvId(req);
    const msgs = req.body.messages || [];
    // 取最后一条 user 消息作为本轮输入（支持图片：保留完整 content）
    let currentText = '';
    let currentContent = null;
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role !== 'user') continue;
      const t = typeof m.content === 'string' ? m.content
        : Array.isArray(m.content) ? m.content.filter(b => b && b.type === 'text').map(b => b.text).join(' ') : '';
      if (t.trim() || (Array.isArray(m.content) && m.content.some(b => b && b.type === 'image_url'))) {
        currentText = t;
        currentContent = m.content;
        break;
      }
    }
    if (!currentText && !currentContent) {
      return res.status(400).json({ error: { message: 'no user text', type: 'invalid_request_error' } });
    }

    // 1) 会话设置（窗口 + effort + model）
    let settings = { window_mode: 'all', window_n: 50, window_tokens: 8000, effort: 'medium', model: '' };
    try {
      const s = await axios.get(`${MEMORY_SERVICE_URL}/conv/${encodeURIComponent(convId)}/settings`, { timeout: 3000 });
      settings = { ...settings, ...(s.data || {}) };
    } catch (e) { /* 用默认 */ }

    // 2) 从 L0 拉历史（用 last 取最新 N 条，避免 after_id=0 拉到最早历史）
    let lastN = 500;
    if (settings.window_mode === 'n' && settings.window_n > 0) {
      lastN = settings.window_n;
    } else if (settings.window_mode === 'tokens') {
      lastN = 2000; // tokens 模式拉大窗口，本地再按预算截断
    }
    let history = [];
    try {
      const h = await axios.get(`${MEMORY_SERVICE_URL}/conversations/${encodeURIComponent(convId)}/messages?last=${lastN}&order=anchor`, { timeout: 5000 });
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
    assembled.push({ role: 'user', content: toAnthropicContent(currentContent ?? currentText) });
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
  if (req.headers['anthropic-version'] || req.headers['x-api-key']) {
    // Anthropic Models API 格式（补齐 has_more / first_id / last_id，部分客户端会校验这几个字段）
    return res.json({
      data: modelsCache.map(id => ({ type: 'model', id, display_name: id, created_at: new Date(0).toISOString() })),
      has_more: false,
      first_id: modelsCache[0] || null,
      last_id: modelsCache[modelsCache.length - 1] || null
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

// ============ Pluvia 接入约定 ============
// 能力协商：前端按 features 决定发哪些 v0.2 字段（鉴权同 /v1/models，由 nginx 校验）
app.get('/v1/pluvia/contract', (req, res) => {
  res.json({ ...PLUVIA_CONTRACT, time_header: TIME_HEADER_MODE });
});

// 删除会话：L0 软删 + 这个会话提取出的 L1 记忆硬删 + 向量清理（复用记忆服务 DELETE /l0/conversation）。
// 不可恢复，前端应先让用户确认。nginx 的预检目前只放行 GET/POST，所以另给一个 POST 别名。
async function pluviaDeleteConversation(req, res) {
  const id = String(req.params.id || '');
  if (!/^[A-Za-z0-9_.:\-]{1,128}$/.test(id)) {
    return res.status(400).json({ error: { message: 'conversation id 不合法', type: 'invalid_request_error' } });
  }
  try {
    const r = await axios.delete(`${MEMORY_SERVICE_URL}/l0/conversation/${encodeURIComponent(id)}`, { timeout: 15000 });
    for (const k of [...turnStore.keys()]) if (k.startsWith(id + '\u0000')) turnStore.delete(k);
    console.log('[PLUVIA] 删除会话', id, JSON.stringify(r.data));
    res.json({ ok: r.data && r.data.status === 'ok', l1_removed: (r.data && r.data.l1_removed) || 0, warning: r.data && r.data.warning });
  } catch (e) {
    res.status(502).json({ error: { message: e.message, type: 'gateway_error' } });
  }
}
app.delete('/v1/pluvia/conversations/:id', pluviaDeleteConversation);
app.post('/v1/pluvia/conversations/:id/delete', pluviaDeleteConversation);

// 健康检查
app.get('/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// 功能6：热重载配置接口（Dashboard 保存后调用，不重启进程）
app.post('/reload-config', async (req, res) => {
  const state = await loadActiveConfig();
  await refreshModelsCache(true);
  // active/unconfigured 都算重载成功（unconfigured 是正常状态，回退 .env）；只有 error 是拉取失败
  res.json({
    status: state === 'error' ? 'error' : 'ok',
    state: state,
    active: activeConfig ? { name: activeConfig.name, base_url: activeConfig.base_url, model: activeConfig.model } : null
  });
});

// ============ /api 薄代理：转发到记忆服务的同步/配置接口 ============
// 放在最后注册，避免吞掉 /v1/* 路由（Express 按注册顺序匹配，/api/* 只在 /api 前缀命中）
app.all('/api/*splat', async (req, res) => {
  // originalUrl 已含 query，用 pathname 拼接避免与 params:req.query 双重附加 query
  const path = req.originalUrl.replace(/^\/api/, '');
  // 会返回明文 API Key 的内部接口只给网关在本机直接调用，不经过 /api 对外（Dashboard 也用不到它们）
  if (/^\/config\/(current|chat_targets)(\/|\?|$)/.test(path)) {
    return res.status(403).json({ error: { message: '内部接口，不对外开放', type: 'forbidden' } });
  }
  const target = MEMORY_SERVICE_URL + path;
  try {
    const upstream = await axios({
      method: req.method,
      url: target,   // originalUrl 已含 query string，不再传 params 避免重复
      data: ['GET', 'HEAD'].includes(req.method) ? undefined : req.body,
      timeout: 15000,
      responseType: 'json',
    });
    res.status(upstream.status).json(upstream.data);
  } catch (e) {
    const status = e.response?.status || 502;
    res.status(status).json(e.response?.data || { error: { message: e.message, type: 'proxy_error' } });
  }
});

// 启动
// S1 安全加固：只监听本机回环地址（IPv4 + IPv6），不再对公网开放 *:3000。
// 外部客户端统一走 nginx（https://域名/v1/，nginx 校验 Bearer token 后转发到 localhost:3000）。
// 如确需临时对外，可设置环境变量 GATEWAY_BIND_HOSTS=0.0.0.0（不推荐）。
const BIND_HOSTS = (process.env.GATEWAY_BIND_HOSTS || '127.0.0.1,::1').split(',').map(h => h.trim()).filter(Boolean);
let gatewayStarted = false;
for (const host of BIND_HOSTS) {
  const srv = app.listen(PORT, host, async () => {
    console.log(`Memory Gateway running on ${host}:${PORT}`);
    if (gatewayStarted) return;
    gatewayStarted = true;
    // 启动时拉取一次配置（失败不阻塞启动，转发逻辑会回退到 .env）
    // 记忆服务和网关同时重启时，记忆服务可能还没起来 → 失败后每 5 秒重试，最多 6 次
    let cfgState = await loadActiveConfig();
    for (let i = 0; cfgState === 'error' && i < 6; i++) {
      await new Promise(r => setTimeout(r, 5000));
      cfgState = await loadActiveConfig();
    }
    // 启动时刷新一次模型列表缓存
    await refreshModelsCache();
  });
  srv.on('error', e => console.error(`[BIND] ${host}:${PORT} 监听失败:`, e.message));
}
