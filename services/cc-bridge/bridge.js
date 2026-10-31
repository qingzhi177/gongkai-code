#!/usr/bin/env node
// cc-bridge：网关和 Claude Code 之间的桥（《优化与架构完善方案》9.10 节；设计见 docs/plan/claude-code-bridge.md）。
//
// 网关仍然是中间层（存 L0、找记忆、换窗附注都照旧），只是「问模型」这一步换成问本机的 Claude Code：
//   Pluvia → 网关 →（本机 HTTP）cc-bridge → 官方 claude 程序（订阅登录）→ Anthropic
// 订阅额度只经过官方的 claude 程序用（方案 9.11 节）：这里不读、不转发登录凭证，只把它留在子进程的环境里；
// 启动子进程前去掉 ANTHROPIC_API_KEY 这类变量（有它时 claude 会改走 API 计费）。
//
// 跑法（CC_MODE）：
//   print（默认，已实现）：每个对话一个常驻的 `claude -p --input-format stream-json --output-format stream-json`，
//       stdin 不关进程就不退，一轮一轮往里写；输出是结构化事件（逐字、思考、工具调用、用量），转成统一的事件流给网关。
//   sdk（之后）：Claude Agent SDK，能把 system 分成静态 / 动态两段（SYSTEM_PROMPT_DYNAMIC_BOUNDARY），缓存更省。
//   tmux（之后）：交互式 claude 常驻在 tmux 里，网页终端镜像、钩子出工具卡片；给想「看着它干活」的时候用。
// 三种跑法对网关是同一个接口（下面的 /v1/turn），换跑法不用改网关。
//
// 接口（只听 127.0.0.1，默认端口 3010）：
//   POST /v1/turn       { conv_id, system, content, model?, effort?, ephemeral?, tools? } → SSE 事件流（见 mapEvent）
//   POST /v1/interrupt  { conv_id }   用户点了「停」
//   POST /v1/close      { conv_id }   关掉这个对话的进程（会话记录还在，下次接着 --resume）
//   GET  /v1/sessions   现在开着哪些
//   GET  /health
'use strict';

const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const PORT = parseInt(process.env.CC_BRIDGE_PORT || '3010', 10);
const MODE = process.env.CC_MODE || 'print';
const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const HOME_DIR = process.env.CC_HOME || path.join(os.homedir(), 'companion-cc');   // claude 的工作目录：干净的、只放陪伴用的东西
const RUN_DIR = path.join(HOME_DIR, 'run');
const GATEWAY_INTERNAL = process.env.GATEWAY_INTERNAL_URL || 'http://127.0.0.1:3011';
const DEFAULT_MODEL = process.env.CC_MODEL || 'opus';
const THINKING_DISPLAY = (process.env.CC_THINKING_DISPLAY || 'summarized').trim();   // 隐藏参数，版本升级后不能用就设成 off
const IDLE_CLOSE_MIN = parseFloat(process.env.CC_IDLE_CLOSE_MIN || '180');          // 闲这么久关掉进程（下次 --resume 接上）
const SESSIONS_FILE = path.join(RUN_DIR, 'sessions.json');
const MCP_WAIT_MS = parseInt(process.env.CC_MCP_WAIT_MS || '12000', 10);   // 新进程等记忆工具（MCP）连上的最长时间
const DROP_ENV = ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL'];

fs.mkdirSync(RUN_DIR, { recursive: true });

// 对话 id → claude 的 session_id，存盘：bridge 重启以后还能 --resume 接上
function loadSaved() { try { return JSON.parse(fs.readFileSync(SESSIONS_FILE, 'utf8')); } catch (e) { return {}; } }
const saved = loadSaved();
function persist() { try { fs.writeFileSync(SESSIONS_FILE, JSON.stringify(saved, null, 1)); } catch (e) { /* 存不上不影响聊天 */ } }

const sessions = new Map();   // conv_id → session
const log = (...a) => console.log(new Date().toISOString(), ...a);
const sha = s => crypto.createHash('sha256').update(String(s)).digest('hex').slice(0, 12);

// 生命线（方案 9.6 节、tmux 那份讨论）：就算以后给 Claude Code 开了 Bash，也不许它关掉自己、桥、网关，或者动记忆库的数据。
// print 模式默认不给任何内置工具（--tools ""），这条是给以后开工具时兜底的。
const LIFELINE = {
  hooks: {
    PreToolUse: [{
      matcher: 'Bash',
      hooks: [{ type: 'command', command: `node ${JSON.stringify(path.join(__dirname, 'lifeline.js'))}` }]
    }]
  }
};

function buildArgs(s, opts) {
  const mcp = { mcpServers: { memory: { command: process.execPath, args: [path.join(__dirname, 'mcp_memory.js')],
    env: { GATEWAY_INTERNAL_URL: GATEWAY_INTERNAL, MEMORY_CONV_ID: s.conv } } } };
  const args = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
    '--model', s.model, '--system-prompt-file', s.spFile,
    '--tools', opts.tools || '',                       // 默认不给内置工具：陪着聊天用不上 Bash / 读写文件
    '--mcp-config', JSON.stringify(mcp), '--strict-mcp-config',
    '--settings', JSON.stringify(LIFELINE),
    '--permission-mode', 'dontAsk', '--allowedTools', 'mcp__memory'];
  if (THINKING_DISPLAY && THINKING_DISPLAY !== 'off') args.push('--thinking-display', THINKING_DISPLAY);
  if (opts.effort) args.push('--effort', opts.effort);
  if (opts.ephemeral) args.push('--no-session-persistence');
  else if (s.resumeId) args.push('--resume', s.resumeId);
  return args;
}

function childEnv() {
  const env = { ...process.env };
  for (const k of DROP_ENV) delete env[k];
  return env;
}

function startSession(conv, opts) {
  const sysHash = sha(opts.system || '');
  const spFile = path.join(RUN_DIR, `sp-${sysHash}.md`);
  if (!fs.existsSync(spFile)) fs.writeFileSync(spFile, opts.system || '', { mode: 0o600 });
  const s = {
    conv, model: opts.model || DEFAULT_MODEL, sysHash, spFile, ephemeral: !!opts.ephemeral,
    resumeId: opts.ephemeral ? null : (saved[conv] && saved[conv].session_id) || null,
    sessionId: null, proc: null, buf: '', turn: null, startedAt: Date.now(), lastUsed: Date.now(), turns: 0, lastUsage: null, tools: [],
    ctl: new Map(), ctlSeq: 0, mcp: null
  };
  const args = buildArgs(s, opts);
  const proc = spawn(CLAUDE_BIN, args, { cwd: HOME_DIR, env: childEnv(), stdio: ['pipe', 'pipe', 'pipe'] });
  s.proc = proc;
  log(`[SPAWN] ${conv.slice(0, 12)} model=${s.model} ${s.resumeId ? 'resume' : 'new'} pid=${proc.pid}`);
  proc.stdout.setEncoding('utf8');
  proc.stdout.on('data', chunk => {
    s.buf += chunk;
    let i;
    while ((i = s.buf.indexOf('\n')) >= 0) {
      const line = s.buf.slice(0, i).trim();
      s.buf = s.buf.slice(i + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch (e) { continue; }
      onEvent(s, ev);
    }
  });
  proc.stderr.setEncoding('utf8');
  proc.stderr.on('data', d => log(`[CC-ERR] ${conv.slice(0, 12)} ${String(d).slice(0, 300)}`));
  proc.on('close', code => {
    log(`[EXIT] ${conv.slice(0, 12)} code=${code}`);
    if (s.turn) finishTurn(s, { type: 'error', message: `Claude Code 退出了（${code}）` });
    if (sessions.get(conv) === s) sessions.delete(conv);
  });
  sessions.set(conv, s);
  s.ready = waitMcp(s);
  return s;
}

// 控制请求（stream-json 的 control_request / control_response）：问 MCP 状态、让这一轮停下
function control(s, subtype, timeoutMs = 3000) {
  const id = `ctl-${++s.ctlSeq}`;
  return new Promise(resolve => {
    const timer = setTimeout(() => { s.ctl.delete(id); resolve(null); }, timeoutMs);
    s.ctl.set(id, r => { clearTimeout(timer); resolve(r); });
    try { s.proc.stdin.write(JSON.stringify({ type: 'control_request', request_id: id, request: { subtype } }) + '\n'); }
    catch (e) { clearTimeout(timer); s.ctl.delete(id); resolve(null); }
  });
}

// 新进程里的记忆工具（MCP）是异步连上的（实测要 3 秒左右）：第一句话要等它连上再发，不然这一轮模型手里没有记忆工具
async function waitMcp(s) {
  const t0 = Date.now();
  while (Date.now() - t0 < MCP_WAIT_MS && s.proc.exitCode === null) {
    const r = await control(s, 'mcp_status');
    const list = (r && r.mcpServers) || [];
    s.mcp = list.map(m => ({ name: m.name, status: m.status }));
    if (r && list.every(m => m.status !== 'pending')) break;
    await new Promise(x => setTimeout(x, 400));
  }
  log(`[MCP] ${s.conv.slice(0, 12)} ${JSON.stringify(s.mcp)}（${Date.now() - t0}ms）`);
}

// claude 的 stream-json 事件 → 给网关的统一事件
function onEvent(s, ev) {
  const t = s.turn;
  if (ev.type === 'control_response' && ev.response) {
    const cb = s.ctl.get(ev.response.request_id);
    if (cb) { s.ctl.delete(ev.response.request_id); cb(ev.response.response || null); }
    return;
  }
  if (ev.type === 'system' && ev.subtype === 'init') {
    s.sessionId = ev.session_id;
    s.tools = ev.tools || [];
    if (!s.ephemeral && ev.session_id) { saved[s.conv] = { session_id: ev.session_id, updated_at: new Date().toISOString() }; persist(); }
    return t && emit(t, { type: 'init', session_id: ev.session_id, model: ev.model, tools: ev.tools, mcp_servers: ev.mcp_servers });
  }
  if (!t) return;
  if (ev.type === 'stream_event' && ev.event) {
    const e = ev.event;
    if (ev.parent_tool_use_id) return;            // 子代理的不转
    if (e.type === 'content_block_delta' && e.delta) {
      if (e.delta.type === 'text_delta') return emit(t, { type: 'text', text: e.delta.text });
      if (e.delta.type === 'thinking_delta') return emit(t, { type: 'thinking', text: e.delta.thinking });
    }
    if (e.type === 'message_start' && e.message && e.message.usage) t.lastStart = e.message.usage;
    return;
  }
  if (ev.type === 'assistant' && ev.message && Array.isArray(ev.message.content)) {
    for (const b of ev.message.content) {
      if (b.type === 'tool_use') {
        const name = String(b.name || '').replace(/^mcp__memory__/, '');
        t.toolNames[b.id] = name;
        emit(t, { type: 'tool', id: b.id, name, input: b.input, status: 'running' });
      }
      if (b.type === 'text' && b.text) t.texts.push(b.text);
    }
    return;
  }
  if (ev.type === 'user' && ev.message && Array.isArray(ev.message.content)) {
    for (const b of ev.message.content) {
      if (b.type !== 'tool_result') continue;
      const text = Array.isArray(b.content) ? b.content.map(c => c.text || '').join('') : String(b.content || '');
      emit(t, { type: 'tool', id: b.tool_use_id, name: t.toolNames[b.tool_use_id], status: b.is_error ? 'error' : 'done', content: text.slice(0, 2000) });
    }
    return;
  }
  if (ev.type === 'system' && ev.subtype === 'api_retry') return emit(t, { type: 'retry', attempt: ev.attempt, error: ev.error, retry_delay_ms: ev.retry_delay_ms });
  if (ev.type === 'system' && ev.subtype === 'compact_boundary') return emit(t, { type: 'compact' });
  if (ev.type === 'result') {
    s.lastUsage = ev.usage || null;
    return finishTurn(s, { type: 'done', subtype: ev.subtype, is_error: !!ev.is_error, result: ev.result, usage: ev.usage,
      cost_usd: ev.total_cost_usd, session_id: ev.session_id, num_turns: ev.num_turns, duration_ms: ev.duration_ms });
  }
}

function emit(t, obj) {
  for (const res of t.listeners) { try { res.write(`data: ${JSON.stringify(obj)}\n\n`); } catch (e) { /* 前端断了 */ } }
}
function finishTurn(s, obj) {
  const t = s.turn;
  if (!t) return;
  emit(t, obj);
  for (const res of t.listeners) { try { res.end(); } catch (e) { /* */ } }
  s.turn = null;
  s.lastUsed = Date.now();
  s.turns++;
}

function getSession(conv, opts) {
  let s = sessions.get(conv);
  const sysHash = sha(opts.system || '');
  const model = opts.model || DEFAULT_MODEL;
  // system 或模型变了（比如每天一次的日更块）：在两轮之间关掉、用新的 system --resume 接着同一段会话（历史还在）
  if (s && !s.turn && (s.sysHash !== sysHash || s.model !== model || !!opts.ephemeral !== s.ephemeral)) {
    log(`[RESTART] ${conv.slice(0, 12)} ${s.sysHash !== sysHash ? 'system 变了' : '模型 / 模式变了'}，接着 --resume`);
    try { s.proc.stdin.end(); s.proc.kill(); } catch (e) { /* */ }
    sessions.delete(conv);
    s = null;
  }
  return s || startSession(conv, opts);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')); } catch (e) { reject(e); } });
    req.on('error', reject);
  });
}
function json(res, code, obj) { res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); }

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  try {
    if (req.method === 'GET' && url.pathname === '/health') return json(res, 200, { ok: true, mode: MODE, sessions: sessions.size });
    if (req.method === 'GET' && url.pathname === '/v1/sessions') {
      return json(res, 200, { mode: MODE, sessions: [...sessions.values()].map(s => ({
        conv_id: s.conv, session_id: s.sessionId, pid: s.proc && s.proc.pid, model: s.model, busy: !!s.turn, turns: s.turns, mcp: s.mcp,
        started_at: new Date(s.startedAt).toISOString(), last_used: new Date(s.lastUsed).toISOString(), usage: s.lastUsage, tools: s.tools })) });
    }
    if (req.method !== 'POST') return json(res, 404, { error: 'not found' });
    const body = await readBody(req);
    const conv = String(body.conv_id || '').trim();
    if (!conv) return json(res, 400, { error: '要带 conv_id' });
    if (url.pathname === '/v1/interrupt') {
      const s = sessions.get(conv);
      if (s && s.turn) s.proc.stdin.write(JSON.stringify({ type: 'control_request', request_id: 'int-' + Date.now(), request: { subtype: 'interrupt' } }) + '\n');
      return json(res, 200, { ok: true, interrupted: !!(s && s.turn) });
    }
    if (url.pathname === '/v1/close') {
      const s = sessions.get(conv);
      if (s) { try { s.proc.stdin.end(); } catch (e) { /* */ } sessions.delete(conv); }
      return json(res, 200, { ok: true });
    }
    if (url.pathname === '/v1/turn') {
      if (MODE !== 'print') return json(res, 501, { error: `跑法 ${MODE} 还没做（现在只有 print）` });
      if (!body.content) return json(res, 400, { error: '要带 content' });
      const s = getSession(conv, body);
      if (s.turn) return json(res, 409, { error: '这个对话上一轮还没说完' });
      res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache', connection: 'keep-alive' });
      s.turn = { listeners: [res], texts: [], toolNames: {}, startedAt: Date.now() };
      // 网关断开（用户点了停）：不关进程，这一轮让它停下（interrupt），会话还在
      res.on('close', () => {
        const t = s.turn;
        if (t && t.listeners.includes(res) && !res.writableFinished) {
          t.listeners = t.listeners.filter(x => x !== res);
          try { s.proc.stdin.write(JSON.stringify({ type: 'control_request', request_id: 'int-' + Date.now(), request: { subtype: 'interrupt' } }) + '\n'); } catch (e) { /* */ }
        }
      });
      s.lastUsed = Date.now();
      await s.ready;                                   // 新进程：等记忆工具连上
      if (s.turn && s.proc.exitCode === null) s.proc.stdin.write(JSON.stringify({ type: 'user', message: { role: 'user', content: body.content } }) + '\n');
      return;
    }
    return json(res, 404, { error: 'not found' });
  } catch (e) {
    log('[ERR]', e.message);
    if (!res.headersSent) json(res, 500, { error: e.message });
  }
});

// 闲太久的进程关掉（会话记录在 ~/.claude/projects 里，下次 --resume 接上）
setInterval(() => {
  const now = Date.now();
  for (const s of sessions.values()) {
    if (!s.turn && now - s.lastUsed > IDLE_CLOSE_MIN * 60000) {
      log(`[IDLE] ${s.conv.slice(0, 12)} 闲了 ${IDLE_CLOSE_MIN} 分钟，先关掉`);
      try { s.proc.stdin.end(); } catch (e) { /* */ }
      sessions.delete(s.conv);
    }
  }
}, 60000).unref();

server.listen(PORT, '127.0.0.1', () => log(`[CC-BRIDGE] mode=${MODE} 127.0.0.1:${PORT} home=${HOME_DIR}`));
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    for (const s of sessions.values()) { try { s.proc.stdin.end(); } catch (e) { /* */ } }
    setTimeout(() => process.exit(0), 500);
  });
}
