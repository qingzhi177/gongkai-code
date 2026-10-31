#!/usr/bin/env node
// 假的 claude（cc-bridge 测试用）：读 stream-json 的输入，按官方的事件格式回几行。把收到的参数和环境记到 CC_FAKE_LOG。
'use strict';
const fs = require('fs');
const LOG = process.env.CC_FAKE_LOG;
const note = o => LOG && fs.appendFileSync(LOG, JSON.stringify(o) + '\n');
note({ argv: process.argv.slice(2), has_api_key: 'ANTHROPIC_API_KEY' in process.env, has_base_url: 'ANTHROPIC_BASE_URL' in process.env, cwd: process.cwd() });
const out = o => process.stdout.write(JSON.stringify(o) + '\n');
const resume = process.argv.indexOf('--resume');
const sid = resume >= 0 ? process.argv[resume + 1] : 'sess-' + process.pid;
let buf = '', n = 0, pending = null, st = 0;
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => {
  buf += c;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i); buf = buf.slice(i + 1);
    if (!line.trim()) continue;
    const m = JSON.parse(line);
    note({ stdin: m });
    if (m.type === 'control_request' && m.request && m.request.subtype === 'mcp_status') {   // 前两次还在连，第三次连上
      st = (typeof st === 'number' ? st : 0) + 1;
      out({ type: 'control_response', response: { subtype: 'success', request_id: m.request_id,
        response: { mcpServers: [{ name: 'memory', status: st >= 3 ? 'connected' : 'pending' }] } } });
      continue;
    }
    if (m.type === 'control_request' && m.request && m.request.subtype === 'interrupt') {
      if (pending) { clearTimeout(pending); pending = null; out({ type: 'result', subtype: 'error_during_execution', is_error: true, session_id: sid, usage: {} }); }
      continue;
    }
    if (m.type !== 'user') continue;
    n++;
    const text = typeof m.message.content === 'string' ? m.message.content : JSON.stringify(m.message.content);
    out({ type: 'system', subtype: 'init', session_id: sid, model: 'fake-opus', tools: ['mcp__memory__recall'], mcp_servers: [{ name: 'memory', status: 'connected' }] });
    out({ type: 'stream_event', event: { type: 'message_start', message: { usage: { input_tokens: 5, cache_read_input_tokens: 100 } } }, session_id: sid });
    out({ type: 'stream_event', event: { type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: '想一想' } }, session_id: sid });
    if (/\[T\]/.test(text)) {
      out({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'toolu_x1', name: 'mcp__memory__recall', input: { query: '猫' } }] }, session_id: sid });
      out({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_x1', content: [{ type: 'text', text: '记忆 #1：猫' }] }] }, session_id: sid });
    }
    out({ type: 'stream_event', event: { type: 'content_block_delta', index: 1, delta: { type: 'text_delta', text: '你好' } }, session_id: sid });
    out({ type: 'stream_event', parent_tool_use_id: 'toolu_sub', event: { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: '子代理的' } }, session_id: sid });
    if (/\[SLOW\]/.test(text)) { pending = setTimeout(() => { pending = null; out({ type: 'result', subtype: 'success', result: '慢', session_id: sid, usage: {} }); }, 3000); continue; }
    out({ type: 'stream_event', event: { type: 'content_block_delta', index: 1, delta: { type: 'text_delta', text: '呀（第' + n + '轮）' } }, session_id: sid });
    out({ type: 'assistant', message: { content: [{ type: 'text', text: '你好呀（第' + n + '轮）' }] }, session_id: sid });
    out({ type: 'result', subtype: 'success', is_error: false, result: '你好呀（第' + n + '轮）', session_id: sid, num_turns: 1, duration_ms: 12,
      total_cost_usd: 0.01, usage: { input_tokens: 5, output_tokens: 9, cache_read_input_tokens: 100, cache_creation_input_tokens: 3 } });
  }
});
process.stdin.on('end', () => process.exit(0));
