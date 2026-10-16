// 假的 Anthropic Messages 上游：按消息里的场景标记 [A]/[B]/[C]/[D]/[S] 返回固定内容，并把收到的请求体记到 upstream.jsonl
const http = require('http');
const fs = require('fs');
const LOG = (process.env.GWTEST_DIR || '/tmp/gwtest') + '/upstream.jsonl';

function sse(res, ev, data) { res.write(`event: ${ev}\ndata: ${JSON.stringify(data)}\n\n`); }
function scenario(body) {
  const m = JSON.stringify(body.messages || []).match(/\[(A|B|C|D|S|E)\]/g);
  return m ? m[m.length - 1][1] : 'A';
}
function resultIds(msgs) {
  const ids = new Set();
  for (const m of msgs) if (Array.isArray(m.content)) for (const b of m.content) if (b.type === 'tool_result') ids.add(b.tool_use_id);
  return ids;
}

http.createServer((req, res) => {
  let buf = '';
  req.on('data', c => { buf += c; });
  req.on('end', async () => {
    if (req.method === 'GET') { res.setHeader('content-type', 'application/json'); return res.end(JSON.stringify({ data: [{ id: 'fake-model' }] })); }
    const body = JSON.parse(buf || '{}');
    fs.appendFileSync(LOG, JSON.stringify(body) + '\n');
    const scen = scenario(body);
    if (scen === 'E') {   // 模拟中转站余额不足
      res.statusCode = 403;
      res.setHeader('content-type', 'application/json');
      return res.end(JSON.stringify({ error: { type: 'new_api_error', message: '账户余额不足（测试）' }, type: 'error' }));
    }
    const msgs = body.messages || [];
    const ids = resultIds(msgs);
    let blocks, stop = 'end_turn';
    if (scen === 'B' && !ids.has('toolu_rec1')) {
      blocks = [{ type: 'text', text: '我想想' }, { type: 'tool_use', id: 'toolu_rec1', name: 'recall', input: { query: '晴天' } }]; stop = 'tool_use';
    } else if (scen === 'C' && !ids.has('toolu_mus1')) {
      blocks = [{ type: 'thinking', thinking: '嗯…', signature: 'SIG1' }, { type: 'text', text: '我去看看' },
        { type: 'tool_use', id: 'toolu_rec2', name: 'recall', input: { query: '歌' } },
        { type: 'tool_use', id: 'toolu_mus1', name: 'music_tool', input: { song: '晴天' } }]; stop = 'tool_use';
    } else if (scen === 'D' && !ids.has('toolu_d1')) {
      blocks = [{ type: 'thinking', thinking: '先放歌', signature: 'SIGD1' }, { type: 'text', text: '放首歌' },
        { type: 'tool_use', id: 'toolu_d1', name: 'music_tool', input: { song: 'A' } }]; stop = 'tool_use';
    } else if (scen === 'D' && !ids.has('toolu_d2')) {
      blocks = [{ type: 'thinking', thinking: '再拾个贝', signature: 'SIGD2' }, { type: 'text', text: '再存一下' },
        { type: 'tool_use', id: 'toolu_d2', name: 'keepsake', input: { note: 'x' } }]; stop = 'tool_use';
    } else if (scen === 'S') {
      blocks = [{ type: 'text', text: '慢慢说', slow: true }];
    } else {
      blocks = [{ type: 'text', text: ids.size ? '好啦，弄好了' : '你好呀' }];
    }
    const usage = { input_tokens: 100, output_tokens: 0, cache_read_input_tokens: 10, cache_creation_input_tokens: 5 };
    if (!body.stream) {
      res.setHeader('content-type', 'application/json');
      return res.end(JSON.stringify({ id: 'msg_up', type: 'message', role: 'assistant', model: body.model,
        content: blocks.map(({ slow, ...b }) => b), stop_reason: stop, usage: { ...usage, output_tokens: 20 } }));
    }
    res.setHeader('content-type', 'text/event-stream');
    let closed = false;
    res.on('close', () => { if (!res.writableFinished) { closed = true; fs.appendFileSync(LOG, JSON.stringify({ _event: 'client_closed', scen }) + '\n'); } });
    sse(res, 'message_start', { type: 'message_start', message: { id: 'msg_up', type: 'message', role: 'assistant', content: [], usage } });
    for (let i = 0; i < blocks.length; i++) {
      const b = blocks[i];
      if (b.type === 'thinking') {
        sse(res, 'content_block_start', { type: 'content_block_start', index: i, content_block: { type: 'thinking', thinking: '' } });
        sse(res, 'content_block_delta', { type: 'content_block_delta', index: i, delta: { type: 'thinking_delta', thinking: b.thinking } });
        sse(res, 'content_block_delta', { type: 'content_block_delta', index: i, delta: { type: 'signature_delta', signature: b.signature } });
      } else if (b.type === 'text') {
        sse(res, 'content_block_start', { type: 'content_block_start', index: i, content_block: { type: 'text', text: '' } });
        if (b.slow) {
          for (let k = 0; k < 10 && !closed; k++) {
            sse(res, 'content_block_delta', { type: 'content_block_delta', index: i, delta: { type: 'text_delta', text: b.text + k } });
            await new Promise(r => setTimeout(r, 300));
          }
          if (closed) return;
        } else {
          sse(res, 'content_block_delta', { type: 'content_block_delta', index: i, delta: { type: 'text_delta', text: b.text } });
        }
      } else if (b.type === 'tool_use') {
        sse(res, 'content_block_start', { type: 'content_block_start', index: i, content_block: { type: 'tool_use', id: b.id, name: b.name, input: {} } });
        sse(res, 'content_block_delta', { type: 'content_block_delta', index: i, delta: { type: 'input_json_delta', partial_json: JSON.stringify(b.input) } });
      }
      sse(res, 'content_block_stop', { type: 'content_block_stop', index: i });
    }
    sse(res, 'message_delta', { type: 'message_delta', delta: { stop_reason: stop }, usage: { output_tokens: 20 } });
    sse(res, 'message_stop', { type: 'message_stop' });
    res.end();
  });
}).listen(3997, '127.0.0.1', () => console.log('fake upstream on 3997'));
