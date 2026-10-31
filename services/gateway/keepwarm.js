// 缓存时长、同一家供应商、缓存心跳（保温）。Dashboard「缓存与心跳」页能调（记忆服务 /cache/settings）。
//
// 背景（2026-10-31 看真实日志）：
//  - Anthropic 的缓存按前缀算：system（画像、记忆）网关一直打 1 小时的断点；聊天记录那部分的断点是前端打在最后一条
//    user 消息上的，默认 5 分钟，隔 5 分钟以上再回就要把整段聊天记录重新写一遍（写缓存比读贵 10–20 倍）。
//    → 网关把消息里的缓存标记统一换成设置里的时长（默认 1 小时）。
//  - OpenRouter 会把同一个模型分给 Anthropic / Google Vertex / Amazon Bedrock，每家的缓存是分开的（实测同一段前缀
//    换一家就要重新写）。回复 id 的前缀能看出是哪家：msg_bdrk_… / msg_vrtx_… / msg_01…。
//    → 下一轮优先发给上一轮回答的那家（provider.order，allow_fallbacks 仍然开着，那家不可用时照样换）。
//  - 停下来没聊时缓存会过期。心跳：隔一段时间把上一轮的前缀原样再发一次，读一下缓存就会续期。
//    · 只保留到缓存断点（最后一条 user 消息里带 cache_control 的那一块）为止，后面加一句「这是保温，只回一个字」；
//      system、工具、thinking 参数、tool_choice 都和上一轮一模一样（这些变了缓存就对不上）。
//    · 用流式发，收到 message_start（里面已经有缓存用量）就断开，不等模型说完（Anthropic 自家支持中途停止计费；
//      不支持的供应商也有 stop_sequences 和「只回一个字」兜着，输出很短）。
//    · 只发给上一轮回答的那家（allow_fallbacks: false）：换一家等于重新写一遍，不如不发。
//    · 读到的缓存很少 / 写了一大段：说明缓存已经没了（过期或者换了家），不再心跳，等下一轮真聊天。
//    · 停下太久（默认 1 小时缓存 4 小时、5 分钟缓存 30 分钟）、或者当天次数到了上限，就不再心跳。
//  心跳不存记忆、不发给前端；每一轮和每次心跳的用量报给记忆服务（只有数字），Dashboard 上能看到命中率和大概花了多少。
'use strict';

const axios = require('axios');
const { StringDecoder } = require('string_decoder');

const BEAT_TEXT = '［记忆系统｜这一轮是缓存保温，不是对方发来的消息，对方看不到，回复也不会给对方。请不要回应上面那句话，也不用多想，只输出一个字：嗯］';
const BEAT_STOPS = ['。', '！', '～', '，', '\n'];
const DEFAULTS = {
  ttl_1h: true, heartbeat: true, sticky_provider: true,
  beat_minutes_1h: 50, beat_minutes_5m: 4.5, idle_hours_1h: 4, idle_minutes_5m: 30, daily_max: 40
};
const LOST_READ = 1000;     // 心跳读到的缓存少于这么多 token：缓存已经没了
const LOST_WRITE = 2000;    // 心跳写了这么多：等于重新写了一遍
const PROVIDER_NAMES = { anthropic: 'Anthropic', 'google-vertex': 'Google Vertex', 'amazon-bedrock': 'Amazon Bedrock' };

function providerOf(id) {
  const s = String(id || '');
  if (s.startsWith('msg_bdrk_')) return 'amazon-bedrock';
  if (s.startsWith('msg_vrtx_')) return 'google-vertex';
  if (/^msg_0/.test(s)) return 'anthropic';
  return null;
}
function providerName(slug) { return slug ? (PROVIDER_NAMES[slug] || slug) : ''; }
function isOpenRouter(apiUrl) { return /openrouter\.ai/i.test(String(apiUrl || '')); }

// 消息里的缓存标记统一换成 ttl（'1h' | '5m'）。system 由网关自己打 1 小时，不在这里改。
// 1 小时的断点必须排在 5 分钟的前面：system（1h）→ 消息（1h 或 5m），顺序总是对的。
function applyTtl(messages, ttl) {
  let n = 0;
  for (const m of messages || []) {
    if (!m || !Array.isArray(m.content)) continue;
    for (const b of m.content) {
      if (b && typeof b === 'object' && b.cache_control) {
        b.cache_control = ttl === '5m' ? { type: 'ephemeral' } : { type: 'ephemeral', ttl: '1h' };
        n++;
      }
    }
  }
  return n;
}

// 心跳发的消息：到最后一个缓存断点为止，后面接一句保温说明。没有断点（只有 system 有缓存）时只发一句说明。
function beatMessages(messages) {
  const msgs = messages || [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (!m || !Array.isArray(m.content)) continue;
    let k = -1;
    m.content.forEach((b, j) => { if (b && typeof b === 'object' && b.cache_control) k = j; });
    if (k < 0) continue;
    const out = msgs.slice(0, i);
    const head = m.content.slice(0, k + 1);
    if (m.role === 'user') out.push({ ...m, content: [...head, { type: 'text', text: BEAT_TEXT }] });
    else out.push({ ...m, content: head }, { role: 'user', content: [{ type: 'text', text: BEAT_TEXT }] });
    return { messages: out, cut: true };
  }
  return { messages: [{ role: 'user', content: [{ type: 'text', text: BEAT_TEXT }] }], cut: false };
}

function dayKey(tz, t = Date.now()) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(t));
}

function createKeepwarm({ memoryUrl, userTz = 'Asia/Shanghai', log = console.log, now = () => Date.now(),
  postStream = null, report = true, tickMs = 20000 } = {}) {
  let cached = { at: 0, values: { ...DEFAULTS } };
  let fetching = null;
  const lastProvider = new Map();   // convId -> slug（上一轮回答的那家）
  let armed = null;                 // 现在在保温的那一轮（只保温最近一轮聊天）
  let realInflight = 0;
  let beatsDay = dayKey(userTz, now());
  let beatsToday = 0;
  let lastReason = '';
  let timer = null;

  async function refresh() {
    if (fetching) return fetching;
    fetching = axios.get(`${memoryUrl}/cache/settings`, { timeout: 3000 }).then(r => {
      const vals = { ...DEFAULTS };
      for (const it of (r.data && r.data.items) || []) if (it && it.key in vals) vals[it.key] = it.value;
      cached = { at: now(), values: vals };
    }).catch(() => { cached = { ...cached, at: now() }; }).finally(() => { fetching = null; });
    return fetching;
  }
  function settings() {
    if (now() - cached.at > 30000) refresh();
    return cached.values;
  }
  function ttl() { return settings().ttl_1h === false ? '5m' : '1h'; }

  function preferFor(convId, apiUrl) {
    if (!settings().sticky_provider || !isOpenRouter(apiUrl)) return null;
    return lastProvider.get(convId) || null;
  }

  function post(path, body) {
    if (!report) return;
    axios.post(`${memoryUrl}${path}`, body, { timeout: 3000 }).catch(() => {});
  }
  function state() {
    const s = settings();
    const interval = armed ? (armed.ttl === '1h' ? s.beat_minutes_1h : s.beat_minutes_5m) * 60000 : 0;
    const cap = armed ? (armed.ttl === '1h' ? s.idle_hours_1h * 3600000 : s.idle_minutes_5m * 60000) : 0;
    return {
      armed: !!(armed && s.heartbeat), conv_id: armed ? armed.convId : null, provider: armed ? armed.provider : null,
      ttl: armed ? armed.ttl : ttl(), next_at: armed ? new Date(armed.lastTouch + interval).toISOString() : null,
      idle_until: armed ? new Date(armed.lastReal + cap).toISOString() : null, beats_today: beatsToday,
      reason: s.heartbeat ? lastReason : '缓存心跳关着'
    };
  }
  function reportState() { post('/cache/keepwarm', state()); }
  function disarm(reason) {
    if (armed) log(`[KEEPWARM] 不再心跳：${reason}`);
    armed = null;
    lastReason = reason;
    reportState();
  }

  // 一轮真聊天开始 / 结束
  function begin() { realInflight++; }
  function end() { realInflight = Math.max(0, realInflight - 1); }

  // 一轮聊天成功结束：记用量、记住是哪家回答的、把这一轮设成保温对象
  // base：这一轮第一次发给上游的请求（工具循环之前），{ system, messages, tools, thinking, outputConfig, toolChoice, maxTokens, model }
  function noteTurn({ convId, target, result, usage, base, arm = true, ttlUsed }) {
    const slug = providerOf(result && result.id);
    if (convId && slug) lastProvider.set(convId, slug);
    if (lastProvider.size > 200) lastProvider.delete(lastProvider.keys().next().value);
    const t = ttlUsed || ttl();
    post('/cache/events', {
      kind: 'turn', conv_id: convId, provider: providerName(slug), relay: target && target.name, model: target && target.model,
      ttl: t, input: usage.input_tokens || 0, cache_write: usage.cache_creation_input_tokens || 0,
      cache_read: usage.cache_read_input_tokens || 0, output: usage.output_tokens || 0
    });
    if (!base && armed && armed.convId === convId) {   // 同一轮的续问（前端工具回来了）：缓存刚读过，从现在重新算
      armed.lastTouch = armed.lastReal = now();
      return slug;
    }
    if (!arm || !base || !target || target.format === 'openai') return slug;
    armed = {
      convId, base, target, ttl: t, provider: providerName(slug), slug,
      lastTouch: now(), lastReal: now(), inflight: false
    };
    lastReason = '';
    reportState();
    return slug;
  }

  // 流式发一次，读到 message_start 就断开：返回 { id, usage }
  async function defaultPostStream(target, body) {
    const ac = new AbortController();
    const resp = await axios.post(target.apiUrl, { ...body, model: target.model, stream: true }, {
      headers: { 'Authorization': `Bearer ${target.apiKey}`, 'Content-Type': 'application/json', 'anthropic-version': '2023-06-01' },
      timeout: 60000, responseType: 'stream', signal: ac.signal
    });
    return await new Promise((resolve, reject) => {
      const dec = new StringDecoder('utf8');
      let buf = '';
      let done = false;
      const finish = (v, err) => {
        if (done) return;
        done = true;
        try { ac.abort(); } catch (_) {}
        if (err) reject(err); else resolve(v);
      };
      resp.data.on('data', chunk => {
        buf += dec.write(chunk);
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (!line.startsWith('data:')) continue;
          let evt;
          try { evt = JSON.parse(line.slice(5)); } catch (_) { continue; }
          if (evt.type === 'message_start') return finish({ id: evt.message && evt.message.id, usage: (evt.message && evt.message.usage) || {} });
          if (evt.type === 'error') return finish(null, new Error((evt.error && evt.error.message) || 'upstream error'));
        }
      });
      resp.data.on('end', () => finish(null, new Error('流结束了也没收到 message_start')));
      resp.data.on('error', e => { if (!done) finish(null, e); });
    });
  }

  async function beat() {
    const a = armed;
    const b = a.base;
    const { messages } = beatMessages(b.messages);
    const thinking = b.thinking;
    const budget = thinking && thinking.budget_tokens;
    const body = { system: b.system, messages, max_tokens: budget ? budget + 256 : 256, stop_sequences: BEAT_STOPS };
    if (b.tools && b.tools.length) body.tools = b.tools;
    if (b.toolChoice && body.tools) body.tool_choice = b.toolChoice;
    if (thinking) body.thinking = thinking;
    if (b.outputConfig) body.output_config = b.outputConfig;
    if (isOpenRouter(a.target.apiUrl) && a.slug) body.provider = { order: [a.slug], allow_fallbacks: false };
    a.inflight = true;
    try {
      const r = await (postStream || defaultPostStream)(a.target, body);
      const u = r.usage || {};
      const read = u.cache_read_input_tokens || 0, write = u.cache_creation_input_tokens || 0;
      const lost = read < LOST_READ || write > LOST_WRITE;
      post('/cache/events', {
        kind: 'beat', conv_id: a.convId, provider: providerName(providerOf(r.id)) || a.provider, relay: a.target.name,
        model: a.target.model, ttl: a.ttl, input: u.input_tokens || 0, cache_write: write, cache_read: read, output: 0,
        note: lost ? '缓存已经没了' : null
      });
      log(`[KEEPWARM] 心跳：读缓存 ${read} | 写缓存 ${write} | ${providerName(providerOf(r.id)) || '?'} · ${a.ttl}`);
      if (armed !== a) return;          // 心跳途中来了新的一轮
      beatsToday++;
      if (lost) return disarm('心跳时缓存已经没了（过期了，或者换了一家），等下一轮聊天再写');
      a.lastTouch = now();
      reportState();
    } catch (e) {
      const st = e.response && e.response.status;
      log(`[KEEPWARM] 心跳失败：${st || e.message}`);
      if (armed === a) disarm(`心跳失败（${st || e.message}），等下一轮聊天`);
    } finally {
      a.inflight = false;
    }
  }

  async function tick() {
    const s = settings();
    const d = dayKey(userTz, now());
    if (d !== beatsDay) { beatsDay = d; beatsToday = 0; }
    if (!armed || armed.inflight || realInflight > 0) return;
    if (!s.heartbeat) return;
    const t = now();
    const interval = (armed.ttl === '1h' ? s.beat_minutes_1h : s.beat_minutes_5m) * 60000;
    const cap = armed.ttl === '1h' ? s.idle_hours_1h * 3600000 : s.idle_minutes_5m * 60000;
    if (t - armed.lastTouch < interval) return;
    if (t - armed.lastReal > cap) return disarm('停下来太久了（超过设置的时长），下次聊天时重新写缓存');
    if (beatsToday >= s.daily_max) return disarm(`今天的心跳次数到上限了（${s.daily_max} 次）`);
    await beat();
  }

  function start() {
    refresh();
    if (!timer) {
      timer = setInterval(() => { tick().catch(e => log('[KEEPWARM] 出错：' + e.message)); }, tickMs);
      if (timer.unref) timer.unref();
    }
  }
  function stop() { if (timer) clearInterval(timer); timer = null; }

  return {
    applyTtl: (messages) => applyTtl(messages, ttl()), ttl, preferFor, noteTurn, begin, end, start, stop, tick, refresh,
    state, _armed: () => armed, _setSettings: (v) => { cached = { at: now(), values: { ...DEFAULTS, ...v } }; }
  };
}

module.exports = { createKeepwarm, providerOf, providerName, applyTtl, beatMessages, isOpenRouter, BEAT_TEXT, DEFAULTS };
