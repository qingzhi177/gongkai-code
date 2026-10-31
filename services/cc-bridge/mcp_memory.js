#!/usr/bin/env node
// Claude Code 里的记忆工具：一个 stdio MCP server（不依赖任何 npm 包，手写 JSON-RPC）。
//
// cc-bridge 启动 Claude Code 时用 --mcp-config 挂上它（服务名 memory，工具名在 Claude Code 里是 mcp__memory__recall 这样）。
// 工具清单和执行都转给网关的内部接口（只听本机的端口，默认 127.0.0.1:3011）：
//   GET  /internal/tools  → 和 API 模式同一份工具说明（recall / feel / update_profile / jot / get_current_time）
//   POST /internal/tool   → 和 API 模式同一套执行逻辑（executeTool），带上这个会话对应的对话 id（MEMORY_CONV_ID）
// 所以两条路的记忆工具不会走样：改工具说明只改网关一处。
//
// 环境变量：GATEWAY_INTERNAL_URL（默认 http://127.0.0.1:3011）、MEMORY_CONV_ID（cc-bridge 按会话填）。
'use strict';

const http = require('http');
const BASE = process.env.GATEWAY_INTERNAL_URL || 'http://127.0.0.1:3011';
const CONV = process.env.MEMORY_CONV_ID || '';

function request(method, path, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(path, BASE);
    const data = body ? Buffer.from(JSON.stringify(body)) : null;
    const req = http.request({ method, hostname: u.hostname, port: u.port, path: u.pathname,
      headers: data ? { 'content-type': 'application/json', 'content-length': data.length } : {}, timeout: 30000 }, res => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') }); }
        catch (e) { reject(new Error('网关返回的不是 JSON')); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('网关超时')));
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

function send(msg) { process.stdout.write(JSON.stringify(msg) + '\n'); }
function reply(id, result) { send({ jsonrpc: '2.0', id, result }); }
function fail(id, code, message) { send({ jsonrpc: '2.0', id, error: { code, message } }); }

async function handle(msg) {
  const { id, method, params } = msg || {};
  if (method === 'initialize') {
    return reply(id, {
      protocolVersion: (params && params.protocolVersion) || '2025-06-18',
      capabilities: { tools: {} },
      serverInfo: { name: 'memory', version: '0.1.0' },
      instructions: '记忆库的工具：想起以前的事（recall）、记下此刻的感受（feel）、更新对彼此的认识（update_profile）、在小本子上记一笔（jot）、看现在几点（get_current_time）。'
    });
  }
  if (method === 'notifications/initialized' || (method && method.startsWith('notifications/'))) return;   // 通知不用回
  if (method === 'ping') return reply(id, {});
  if (method === 'tools/list') {
    try {
      const r = await request('GET', '/internal/tools');
      return reply(id, { tools: (r.body && r.body.tools) || [] });
    } catch (e) {
      return reply(id, { tools: [] });   // 网关没起来：先给空清单，不让 Claude Code 卡住
    }
  }
  if (method === 'tools/call') {
    const name = params && params.name;
    try {
      const r = await request('POST', '/internal/tool', { name, args: (params && params.arguments) || {}, conv_id: CONV || null });
      const b = r.body || {};
      return reply(id, { content: [{ type: 'text', text: String(b.content || '（无返回内容）') }], isError: !!b.is_error || r.status >= 400 });
    } catch (e) {
      return reply(id, { content: [{ type: 'text', text: `记忆工具暂时用不了：${e.message}` }], isError: true });
    }
  }
  if (id !== undefined) fail(id, -32601, `不支持的方法：${method}`);
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  buf += chunk;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch (e) { fail(null, -32700, '不是合法的 JSON'); continue; }
    handle(msg).catch(e => { if (msg && msg.id !== undefined) fail(msg.id, -32603, e.message); });
  }
});
process.stdin.on('end', () => process.exit(0));
