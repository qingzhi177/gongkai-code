// 假的 Anthropic Messages 上游：按消息里的场景标记 [A]/[B]/[C]/[D]/[S] 返回固定内容，并把收到的请求体记到 upstream.jsonl
// /oa 开头的地址模拟 OpenAI 格式（/chat/completions）的供应商，/aux 开头的模拟辅助请求专用的 Anthropic 供应商
const http = require('http');
const fs = require('fs');
const LOG = (process.env.GWTEST_DIR || '/tmp/gwtest') + '/upstream.jsonl';

function sse(res, ev, data) { res.write(`event: ${ev}\ndata: ${JSON.stringify(data)}\n\n`); }
// [U] 场景：中文、少见的颜文字符号（4 字节）、组合附加符号、表情
const KAO = '\u{169B9}\u200E\u0847\u{169B9}\u200E ^_ \u00A0 \u032B _ \u0325\u4E36 暗色里，晚安\u{1F319}';
const sleepMs = ms => new Promise(r => setTimeout(r, ms));
// 把一段 SSE 分成好几块发，每一块都在某个字的中间断开（模拟网络把一个字切在两块数据里）
async function splitWrite(res, str) {
  const b = Buffer.from(str, 'utf8');
  const cuts = [];
  for (let i = 1; i < b.length && cuts.length < 8; i++) if ((b[i] & 0xC0) === 0x80 && (b[i - 1] & 0xC0) !== 0x80) cuts.push(i);
  let prev = 0;
  for (const c of cuts) { res.write(b.subarray(prev, c)); prev = c; await sleepMs(25); }
  res.write(b.subarray(prev));
}
function scenario(body) {
  const m = JSON.stringify(body.messages || []).match(/\[(A|B|C|D|S|E|F|G|H|N|U)\]/g);
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
    // /backup、/other 开头的地址模拟备用中转（自动切换测试用）
    const target = req.url.startsWith('/backup') ? 'backup' : req.url.startsWith('/other') ? 'other'
      : req.url.startsWith('/oa') ? 'oa' : req.url.startsWith('/aux') ? 'aux' : 'primary';
    fs.appendFileSync(LOG, JSON.stringify(target === 'primary' ? body : { ...body, _target: target, _auth: req.headers.authorization }) + '\n');
    const scen = scenario(body);
    if (scen === 'H' && (target === 'oa' || target === 'aux')) {   // 模拟辅助请求指定的供应商出错（模型名填错了之类）
      res.statusCode = 400;
      res.setHeader('content-type', 'application/json');
      return res.end(JSON.stringify({ error: { message: '模型不存在（测试）' } }));
    }
    if (scen === 'N' && target === 'primary') {   // 模拟中转「没有可用账号」
      res.statusCode = 503;
      res.setHeader('content-type', 'application/json');
      return res.end(JSON.stringify({ error: { message: 'No available accounts: no available accounts' } }));
    }
    if (scen === 'U' && body.stream) {   // 回复里的字被切在两块数据中间：网关要能拼回去
      res.setHeader('content-type', 'text/event-stream');
      if (target === 'oa') {
        await splitWrite(res, 'data: ' + JSON.stringify({ id: 'chatcmpl-u', object: 'chat.completion.chunk', model: body.model,
          choices: [{ index: 0, delta: { content: KAO }, finish_reason: null }] }) + '\n\n');
        res.write('data: ' + JSON.stringify({ id: 'chatcmpl-u', object: 'chat.completion.chunk', model: body.model,
          choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) + '\n\n');
        res.write('data: [DONE]\n\n');
        return res.end();
      }
      sse(res, 'message_start', { type: 'message_start', message: { id: 'msg_u', type: 'message', role: 'assistant', content: [],
        usage: { input_tokens: 10, output_tokens: 0 } } });
      sse(res, 'content_block_start', { type: 'content_block_start', index: 0, content_block: { type: 'thinking', thinking: '' } });
      await splitWrite(res, `event: content_block_delta\ndata: ${JSON.stringify({ type: 'content_block_delta', index: 0,
        delta: { type: 'thinking_delta', thinking: KAO } })}\n\n`);
      sse(res, 'content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'signature_delta', signature: 'SIGU' } });
      sse(res, 'content_block_stop', { type: 'content_block_stop', index: 0 });
      sse(res, 'content_block_start', { type: 'content_block_start', index: 1, content_block: { type: 'text', text: '' } });
      await splitWrite(res, `event: content_block_delta\ndata: ${JSON.stringify({ type: 'content_block_delta', index: 1,
        delta: { type: 'text_delta', text: KAO } })}\n\n`);
      sse(res, 'content_block_stop', { type: 'content_block_stop', index: 1 });
      sse(res, 'message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 20 } });
      sse(res, 'message_stop', { type: 'message_stop' });
      return res.end();
    }
    if (target === 'oa') {   // OpenAI 格式：先来一段推理（不该转发），再分两段给正文
      const usage = { prompt_tokens: 30, completion_tokens: 7, total_tokens: 37 };
      if (!body.stream) {
        res.setHeader('content-type', 'application/json');
        return res.end(JSON.stringify({ id: 'chatcmpl-oa', object: 'chat.completion', model: body.model,
          choices: [{ index: 0, message: { role: 'assistant', content: '译文：你好', reasoning_content: '想一想' }, finish_reason: 'stop' }], usage }));
      }
      res.setHeader('content-type', 'text/event-stream');
      const chunk = (delta, finish) => res.write('data: ' + JSON.stringify({ id: 'chatcmpl-oa', object: 'chat.completion.chunk', model: body.model,
        choices: [{ index: 0, delta, finish_reason: finish || null }] }) + '\n\n');
      chunk({ role: 'assistant', content: '' });
      chunk({ reasoning_content: '想一想' });
      chunk({ content: '译文：' });
      chunk({ content: '你好' });
      chunk({}, 'stop');
      res.write('data: ' + JSON.stringify({ id: 'chatcmpl-oa', object: 'chat.completion.chunk', model: body.model, choices: [], usage }) + '\n\n');
      res.write('data: [DONE]\n\n');
      return res.end();
    }
    if (scen === 'F' && target === 'primary') {   // 模拟主中转暂时不可用
      res.statusCode = 503;
      res.setHeader('content-type', 'application/json');
      return res.end(JSON.stringify({ error: { message: '上游暂时不可用（测试）' } }));
    }
    if (scen === 'G') {   // 模拟参数错误（换中转也没用）
      res.statusCode = 400;
      res.setHeader('content-type', 'application/json');
      return res.end(JSON.stringify({ error: { message: '参数错误（测试）' } }));
    }
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
      blocks = [{ type: 'text', text: (scen === 'F' || scen === 'N') ? '备用中转回答' : target === 'aux' ? '辅助模型回答' : (ids.size ? '好啦，弄好了' : '你好呀') }];
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
