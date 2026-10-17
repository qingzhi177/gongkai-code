// Pluvia v0.2 网关端到端测试：假上游 + 假记忆服务，不花真实 token、不碰真实记忆库
const fs = require('fs');
const GW = 'http://127.0.0.1:3999';
const DIR = process.env.GWTEST_DIR || '/tmp/gwtest';
const UP = DIR + '/upstream.jsonl', MEM = DIR + '/mem.jsonl';
let pass = 0, fail = 0;
function ok(cond, name, extra) {
  if (cond) { pass++; console.log('  ✓', name); }
  else { fail++; console.log('  ✗', name, extra !== undefined ? JSON.stringify(extra).slice(0, 900) : ''); }
}
function readJsonl(p) { try { return fs.readFileSync(p, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)); } catch (e) { return []; } }
function mark() { return { up: readJsonl(UP).length, mem: readJsonl(MEM).length }; }
function since(m) { return { up: readJsonl(UP).slice(m.up), mem: readJsonl(MEM).slice(m.mem) }; }
const sleep = ms => new Promise(r => setTimeout(r, ms));
const upReqs = d => d.up.filter(x => !x._event);
const saves = d => d.mem.filter(x => x.k === 'save');

async function post(path, body, headers = {}, signal) {
  return fetch(GW + path, { method: 'POST', headers: { 'content-type': 'application/json', 'x-client-name': 'pluvia', ...headers }, body: JSON.stringify(body), signal });
}
async function sseCollect(r) {
  const text = await r.text();
  const events = [];
  for (const raw of text.split('\n\n')) {
    if (!raw.trim()) continue;
    let ev = null, data = '';
    for (const line of raw.split('\n')) {
      if (line.startsWith('event:')) ev = line.slice(6).trim();
      else if (line.startsWith('data:')) data += line.slice(5).trim();
    }
    if (data === '[DONE]') { events.push({ done: true }); continue; }
    let obj = null; try { obj = JSON.parse(data); } catch (e) {}
    events.push({ ev, data: obj });
  }
  return events;
}
function oaSummary(events) {
  let text = '', reasoning = '', finish = null; const toolCalls = [], tools = [];
  for (const e of events) {
    const d = e.data; if (!d) continue;
    if (d.type === 'pluvia_tool') { tools.push(d.tool); continue; }
    const c = d.choices && d.choices[0]; if (!c) continue;
    if (c.delta.content) text += c.delta.content;
    if (c.delta.reasoning_content) reasoning += c.delta.reasoning_content;
    if (c.delta.tool_calls) toolCalls.push(...c.delta.tool_calls);
    if (c.finish_reason) finish = c.finish_reason;
  }
  return { text, reasoning, finish, toolCalls, tools, done: events.some(e => e.done) };
}
function anSummary(events) {
  const blocks = {}, order = [], tools = [], starts = []; let stop = null, msgStarts = 0;
  for (const e of events) {
    const d = e.data; if (!d) continue;
    if (e.ev === 'pluvia_tool') { tools.push(d.tool); continue; }
    if (d.type === 'message_start') msgStarts++;
    if (d.type === 'content_block_start') { starts.push(d.index); blocks[d.index] = { ...d.content_block, _json: '' }; order.push(d.index); }
    if (d.type === 'content_block_delta') {
      const b = blocks[d.index]; if (!b) continue;
      if (d.delta.type === 'text_delta') b.text += d.delta.text;
      if (d.delta.type === 'thinking_delta') b.thinking += d.delta.thinking;
      if (d.delta.type === 'signature_delta') b.signature = (b.signature || '') + d.delta.signature;
      if (d.delta.type === 'input_json_delta') b._json += d.delta.partial_json;
    }
    if (d.type === 'message_delta') stop = d.delta.stop_reason;
  }
  const content = order.map(i => {
    const { _json, ...r } = blocks[i];
    return r.type === 'tool_use' ? { ...r, input: _json ? JSON.parse(_json) : {} } : r;
  });
  return { content, stop, tools, starts, msgStarts };
}
const contiguous = a => a.every((v, i) => v === i);
const countCC = body => {
  let n = 0;
  for (const b of body.system || []) if (b.cache_control) n++;
  for (const t of body.tools || []) if (t.cache_control) n++;
  for (const m of body.messages || []) if (Array.isArray(m.content)) for (const b of m.content) if (b && b.cache_control) n++;
  return n;
};
const anTools = [
  { name: 'music_tool', description: '听歌', input_schema: { type: 'object', properties: { song: { type: 'string' } } } },
  { name: 'keepsake', description: '拾贝', input_schema: { type: 'object', properties: {} } },
  { name: 'recall', description: '前端撞名的工具', input_schema: { type: 'object', properties: {} } }
];
const oaTools = [{ type: 'function', function: { name: 'music_tool', description: '听歌', parameters: { type: 'object', properties: { song: { type: 'string' } } } } }];

(async () => {
  console.log('T1 能力协商');
  {
    const j = await (await fetch(GW + '/v1/pluvia/contract')).json();
    ok(j.contract === '0.2' && ['turn', 'tail', 'client_tools', 'tool_events'].every(f => j.features.includes(f)), 'contract 0.2 + 四项 features', j);
  }

  console.log('T2 OpenAI 流式 + tail（含此刻/离上一句）');
  {
    const tail = '（此刻：10月16日 周五 21:30）\n\n（离上一句过了 3 小时）';
    const m = mark();
    const r = await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-a', memory_skip: false,
      pluvia: { v: '0.2', client: 'pluvia', turn: { id: 'ta1', round: 1 }, tail },
      messages: [{ role: 'system', content: 'sys' }, { role: 'user', content: '早 [A]' }, { role: 'assistant', content: '早呀' }, { role: 'user', content: '你好 [A]\n\n' + tail }] });
    const s = oaSummary(await sseCollect(r));
    await sleep(400);
    const d = since(m); const up = upReqs(d)[0];
    ok(s.text === '你好呀' && s.finish === 'stop' && s.done, 'T2 回复 + finish=stop', s);
    ok(up && !('pluvia' in up) && !('conversation_id' in up) && !('memory_skip' in up), 'T2 三个扩展字段没有发给上游', up && Object.keys(up));
    const lastTxt = JSON.stringify(up.messages[up.messages.length - 1].content);
    ok(!lastTxt.includes('[当前时间') && !lastTxt.includes('[距上次对话'), 'T2 前端带了钟 → 网关不再加时间头/间隔', lastTxt);
    ok(lastTxt.includes('此刻：10月16日'), 'T2 发给模型的原文里 tail 还在');
    const sv = saves(d)[0];
    ok(sv && sv.b.conv_id === 'conv-a' && sv.b.messages.length === 4 && sv.b.messages[2].content === '你好 [A]' && sv.b.messages[3].content === '你好呀',
      'T2 L0：tail 精确剥掉、只存一次', sv && sv.b);
  }

  console.log('T3 tail 为空 → 网关加时间头和间隔');
  {
    const m = mark();
    const r = await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-b',
      pluvia: { v: '0.2', turn: { id: 'tb0', round: 1 }, tail: '' }, messages: [{ role: 'user', content: '在吗 [A]' }] });
    await sseCollect(r); await sleep(300);
    const up = upReqs(since(m))[0];
    const lastTxt = JSON.stringify(up.messages[up.messages.length - 1].content);
    ok(lastTxt.includes('[当前时间') && lastTxt.includes('[距上次对话约'), 'T3 时间头 + 间隔都在', lastTxt);
  }

  console.log('T4 OpenAI 流式：只有记忆工具（网关内部执行 + tool_events）');
  {
    const m = mark();
    const r = await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-b', tools: oaTools,
      pluvia: { v: '0.2', turn: { id: 'tb1', round: 1 }, tail: '' }, messages: [{ role: 'user', content: '还记得那首歌吗 [B]' }] });
    const s = oaSummary(await sseCollect(r)); await sleep(400);
    const d = since(m);
    ok(s.text === '我想想好啦，弄好了' && s.finish === 'stop', 'T4 两次上游调用合成一条回复', s);
    ok(s.tools.length === 2 && s.tools[0].status === 'running' && s.tools[1].status === 'done' && s.tools[1].name === 'recall'
      && s.tools[1].content.includes('一起听过晴天') && !s.tools[1].content.includes('valence'), 'T4 pluvia_tool running→done，结果里没有内部打分', s.tools);
    ok(s.tools[1].content.includes('2026-10-02 04:30'), 'T4 recall 里的时间换算成北京时间（库里存的是 UTC 2026-10-01 20:30）',
      s.tools[1].content.slice(0, 120));
    ok(upReqs(d).length === 2 && JSON.stringify(upReqs(d)[1].messages.slice(-1)).includes('toolu_rec1'), 'T4 第二次上游带上了 recall 的结果');
    const sv = saves(d);
    ok(sv.length === 1 && sv[0].b.messages.slice(-1)[0].content === '我想想\n\n好啦，弄好了', 'T4 L0 存一次，整条回复合成一条', sv.map(x => x.b));
  }

  console.log('T5 OpenAI 流式：记忆工具 + 前端工具混合，两轮');
  {
    const tail1 = '（此刻：10月16日 周五 21:40）';
    const m = mark();
    const r1 = await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-c', tools: oaTools,
      pluvia: { v: '0.2', turn: { id: 'tc1', round: 1 }, tail: tail1 }, messages: [{ role: 'user', content: '放首歌 [C]\n\n' + tail1 }] });
    const s1 = oaSummary(await sseCollect(r1)); await sleep(300);
    const d1 = since(m);
    ok(s1.reasoning === '嗯…' && s1.text === '我去看看', 'T5 第 1 轮：思考 + 正文照常流出', s1);
    ok(s1.tools.length === 2 && s1.tools[1].status === 'done', 'T5 第 1 轮：记忆工具先执行（事件）', s1.tools);
    ok(s1.toolCalls.length === 1 && s1.toolCalls[0].index === 0 && s1.toolCalls[0].id === 'toolu_mus1' &&
      s1.toolCalls[0].function.name === 'music_tool' && s1.toolCalls[0].function.arguments === '{"song":"晴天"}', 'T5 第 1 轮：只把前端工具交出来，index 从 0', s1.toolCalls);
    ok(s1.finish === 'tool_calls', 'T5 第 1 轮 finish_reason=tool_calls', s1.finish);
    ok(saves(d1).length === 0, 'T5 第 1 轮不存 L0');

    const tail2 = '（此刻：10月16日 周五 21:41）';
    const m2 = mark();
    const r2 = await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-c', tools: oaTools, tool_choice: 'none',
      pluvia: { v: '0.2', turn: { id: 'tc1', round: 2 }, tail: tail2 },
      messages: [
        { role: 'user', content: '放首歌 [C]\n\n' + tail2 },
        { role: 'assistant', content: '我去看看', reasoning_content: '嗯…', tool_calls: [{ id: 'toolu_mus1', type: 'function', function: { name: 'music_tool', arguments: '{"song":"晴天"}' } }] },
        { role: 'tool', tool_call_id: 'toolu_mus1', content: '已开始播放' },
        { role: 'user', content: [{ type: 'text', text: '（上面工具带回来的图）' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,iVBORw0KGgo=' } }] }
      ] });
    const s2 = oaSummary(await sseCollect(r2)); await sleep(400);
    const d2 = since(m2); const up = upReqs(d2)[0];
    ok(s2.text === '好啦，弄好了' && s2.finish === 'stop', 'T5 第 2 轮：最终回复', s2);
    ok(up.messages.length === 3, 'T5 续问：上游 = 存的这一轮(user+assistant) + 一条工具结果', up.messages.map(x => x.role));
    const a = up.messages[1].content;
    ok(a[0].type === 'thinking' && a[0].signature === 'SIG1' && a.some(b => b.id === 'toolu_rec2') && a.some(b => b.id === 'toolu_mus1'),
      'T5 续问：接回的 assistant 带思考签名和两个 tool_use', a);
    const u = up.messages[2].content;
    ok(u[0].type === 'tool_result' && u[0].tool_use_id === 'toolu_rec2' && JSON.stringify(u[0].content).includes('一起听过晴天') &&
      u[1].type === 'tool_result' && u[1].tool_use_id === 'toolu_mus1' && u[1].content === '已开始播放' &&
      u[2].type === 'text' && u[3].type === 'image' && u[3].source.media_type === 'image/png',
      'T5 续问：工具结果按原顺序（记忆工具用网关存的、前端工具用回传的），附图并进同一条', u);
    ok(JSON.stringify(up.tool_choice) === '{"type":"none"}', 'T5 tool_choice "none" 转成 Anthropic 格式', up.tool_choice);
    ok(JSON.stringify(up.messages[0].content).includes('21:40'), 'T5 续问用的是第 1 轮发给模型的原样（缓存可命中）');
    const sv = saves(d2);
    ok(sv.length === 1 && sv[0].b.messages.length === 2 && sv[0].b.messages[0].content === '放首歌 [C]' &&
      sv[0].b.messages[1].content === '我去看看\n\n好啦，弄好了', 'T5 最后一轮存 L0：不含工具消息/附图，回复合成一条', sv.map(x => x.b));
  }

  console.log('T6 Anthropic 流式：前端工具连续两轮（index 连续、思考块成块）');
  {
    const tail = '（此刻：10月16日 周五 22:00）';
    const userMsg = { role: 'user', content: [{ type: 'text', text: '来点歌 [D]', cache_control: { type: 'ephemeral' } }, { type: 'text', text: tail }] };
    const H = { 'x-api-key': 'x', 'anthropic-version': '2023-06-01' };
    const base = { model: 'fake-model', stream: true, system: 'sys', conversation_id: 'conv-d', tools: anTools, max_tokens: 2000 };
    const m = mark();
    const s1 = anSummary(await sseCollect(await post('/v1/messages', { ...base, pluvia: { v: '0.2', turn: { id: 'td1', round: 1 }, tail }, messages: [userMsg] }, H)));
    await sleep(200);
    const up1 = upReqs(since(m))[0];
    ok(s1.stop === 'tool_use' && contiguous(s1.starts) && s1.msgStarts === 1, 'T6 第 1 轮 stop_reason=tool_use，index 连续', s1);
    ok(s1.content.map(b => b.type).join(',') === 'thinking,text,tool_use' && s1.content[0].signature === 'SIGD1' && s1.content[2].id === 'toolu_d1', 'T6 第 1 轮块内容', s1.content);
    ok(up1.tools.filter(t => t.name === 'recall').length === 1 && up1.tools.some(t => t.name === 'keepsake'), 'T6 前端撞名的 recall 被去掉，只留网关的', up1.tools.map(t => t.name));
    const c0 = up1.messages[0].content;
    ok(c0[0].cache_control && c0[1].text.includes('距上次对话') && !c0[1].text.includes('[当前时间') && c0[2].text === tail,
      'T6 记忆头插在缓存标记之后、tail 之前；前端有钟所以不加 [当前时间]', c0);

    const r2msgs = [userMsg, { role: 'assistant', content: s1.content }, { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'toolu_d1', content: 'ok1' }] }];
    const m2 = mark();
    const s2 = anSummary(await sseCollect(await post('/v1/messages', { ...base, pluvia: { v: '0.2', turn: { id: 'td1', round: 2 }, tail }, messages: r2msgs }, H)));
    await sleep(200);
    const d2 = since(m2);
    ok(s2.stop === 'tool_use' && s2.content[2].id === 'toolu_d2' && contiguous(s2.starts), 'T6 第 2 轮又交出 keepsake', s2);
    ok(upReqs(d2)[0].messages.length === 3 && saves(d2).length === 0, 'T6 第 2 轮上游 3 条消息、不存 L0', upReqs(d2)[0].messages.map(x => x.role));

    const r3msgs = [...r2msgs, { role: 'assistant', content: s2.content }, { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'toolu_d2', content: 'ok2' }] }];
    const m3 = mark();
    const s3 = anSummary(await sseCollect(await post('/v1/messages', { ...base, pluvia: { v: '0.2', turn: { id: 'td1', round: 3 }, tail }, messages: r3msgs }, H)));
    await sleep(400);
    const d3 = since(m3);
    ok(s3.stop === 'end_turn' && s3.content.length === 1 && s3.content[0].text === '好啦，弄好了', 'T6 第 3 轮最终回复', s3);
    ok(upReqs(d3)[0].messages.length === 5, 'T6 第 3 轮上游 5 条（前两轮都接回）', upReqs(d3)[0].messages.map(x => x.role));
    const sv = saves(d3);
    ok(sv.length === 1 && sv[0].b.messages.length === 2 && sv[0].b.messages[0].content === '来点歌 [D]' &&
      sv[0].b.messages[1].content === '放首歌\n\n再存一下\n\n好啦，弄好了', 'T6 存 L0 一次', sv.map(x => x.b));
  }

  console.log('T7 用户点停：前端断开 → 不存、上游被中止');
  {
    const m = mark();
    const ac = new AbortController();
    const p = post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-s',
      pluvia: { v: '0.2', turn: { id: 'ts1', round: 1 }, tail: '' }, messages: [{ role: 'user', content: '讲个长故事 [S]' }] }, {}, ac.signal)
      .then(r => r.text()).catch(() => 'aborted');
    await sleep(900); ac.abort(); await p; await sleep(1500);
    const d = since(m);
    ok(saves(d).length === 0, 'T7 没有存 L0', saves(d));
    ok(d.up.some(x => x._event === 'client_closed'), 'T7 上游连接被网关中止', d.up.filter(x => x._event));
  }

  console.log('T8 Kelivo（无 pluvia）路径不变');
  {
    const m = mark();
    const s = anSummary(await sseCollect(await fetch(GW + '/v1/messages', { method: 'POST',
      headers: { 'content-type': 'application/json', 'x-api-key': 'x', 'anthropic-version': '2023-06-01', 'x-conversation-id': 'kel-1' },
      body: JSON.stringify({ model: 'fake-model', stream: true, max_tokens: 1000, messages: [{ role: 'user', content: '记得吗 [B]' }] }) })));
    await sleep(400);
    const d = since(m);
    ok(s.content.some(b => b.type === 'tool_use') && s.content.some(b => b.type === 'web_search_tool_result') && s.stop === 'end_turn', 'T8 Kelivo 的工具卡片变通照旧', s.content.map(b => b.type));
    ok(s.tools.length === 0, 'T8 Kelivo 收不到 pluvia_tool 事件');
    ok(JSON.stringify(upReqs(d)[0].messages.slice(-1)).includes('[当前时间'), 'T8 Kelivo 照旧有时间头');
    const sv = saves(d);
    ok(sv.length === 1 && sv[0].b.conv_id === 'kel-1' && sv[0].b.client === 'kelivo', 'T8 Kelivo 照旧存 L0', sv.map(x => x.b));
  }

  console.log('T9 非流式 OpenAI：交出前端工具 + pluvia_tools');
  {
    const m = mark();
    const j1 = await (await post('/v1/chat/completions', { model: 'fake-model', stream: false, conversation_id: 'conv-n', tools: oaTools,
      pluvia: { v: '0.2', turn: { id: 'tn1', round: 1 }, tail: '' }, messages: [{ role: 'user', content: '放歌 [C]' }] })).json();
    ok(j1.choices[0].finish_reason === 'tool_calls' && j1.choices[0].message.tool_calls[0].function.name === 'music_tool' &&
      j1.pluvia_tools && j1.pluvia_tools.length === 1 && j1.pluvia_tools[0].status === 'done', 'T9 第 1 轮', j1);
    const j2 = await (await post('/v1/chat/completions', { model: 'fake-model', stream: false, conversation_id: 'conv-n', tools: oaTools,
      pluvia: { v: '0.2', turn: { id: 'tn1', round: 2 }, tail: '' },
      messages: [{ role: 'user', content: '放歌 [C]' }, { role: 'assistant', content: j1.choices[0].message.content, tool_calls: j1.choices[0].message.tool_calls },
        { role: 'tool', tool_call_id: 'toolu_mus1', content: '好' }] })).json();
    await sleep(400);
    const sv = saves(since(m));
    ok(j2.choices[0].finish_reason === 'stop' && j2.choices[0].message.content === '好啦，弄好了', 'T9 第 2 轮', j2);
    ok(sv.length === 1 && sv[0].b.messages[1].content === '我去看看\n\n好啦，弄好了', 'T9 存 L0 一次', sv.map(x => x.b));
  }

  console.log('T10 存档丢失时的兜底续问（按前端回传的历史转换，去掉 thinking）');
  {
    const m = mark();
    const r = await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-z', tools: oaTools,
      thinking: { type: 'enabled', budget_tokens: 1024 },
      pluvia: { v: '0.2', turn: { id: 'tz-never-stored', round: 2 }, tail: '' },
      messages: [{ role: 'user', content: '放歌 [C]' },
        { role: 'assistant', content: '我去看看', tool_calls: [{ id: 'toolu_mus1', type: 'function', function: { name: 'music_tool', arguments: '{}' } }] },
        { role: 'tool', tool_call_id: 'toolu_mus1', content: '好' }] });
    const s = oaSummary(await sseCollect(r)); await sleep(400);
    const d = since(m); const up = upReqs(d)[0];
    ok(s.text === '好啦，弄好了' && up.messages.length === 3 && up.messages[1].content.some(b => b.type === 'tool_use') &&
      up.messages[2].content[0].type === 'tool_result' && up.thinking === undefined, 'T10 转换正确、不带 thinking', up);
    const sv = saves(d);
    ok(sv.length === 1 && sv[0].b.messages.length === 2 && sv[0].b.messages[1].content === '我去看看\n\n好啦，弄好了', 'T10 存 L0', sv.map(x => x.b));
  }

  console.log('T11 memory_skip 不存');
  {
    const m = mark();
    await sseCollect(await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-k', memory_skip: true,
      pluvia: { v: '0.2', turn: { id: 'tk1', round: 1 }, tail: '' }, messages: [{ role: 'user', content: '试一句 [A]' }] }));
    await sleep(300);
    ok(saves(since(m)).length === 0, 'T11 没有存 L0');
  }

  console.log('T12 cache_control 超过 4 个时自动去掉最早的');
  {
    const m = mark();
    const blocks = [1, 2, 3, 4, 5].map(i => ({ type: 'text', text: '段' + i + ' [A]', cache_control: { type: 'ephemeral' } }));
    await (await post('/v1/messages', { model: 'fake-model', stream: false, max_tokens: 500, conversation_id: 'conv-cc',
      pluvia: { v: '0.2', turn: { id: 'tcc', round: 1 }, tail: '' }, messages: [{ role: 'user', content: blocks }] },
      { 'x-api-key': 'x', 'anthropic-version': '2023-06-01' })).json();
    const up = upReqs(since(m))[0];
    ok(countCC(up) <= 4, 'T12 上游断点数 ≤ 4', countCC(up));
  }

  console.log('T13 删除会话（DELETE 和 POST 别名）');
  {
    const m = mark();
    const j1 = await (await fetch(GW + '/v1/pluvia/conversations/conv-a', { method: 'DELETE' })).json();
    const j2 = await (await fetch(GW + '/v1/pluvia/conversations/conv-b/delete', { method: 'POST' })).json();
    const j3 = await fetch(GW + '/v1/pluvia/conversations/' + encodeURIComponent('bad id!'), { method: 'DELETE' });
    const dels = since(m).mem.filter(x => x.k === 'delete').map(x => x.b.id);
    ok(j1.ok && j1.l1_removed === 2 && j2.ok && dels.join(',') === 'conv-a,conv-b' && j3.status === 400, 'T13', { j1, j2, dels, s3: j3.status });
  }

  console.log('T14 Kelivo OpenAI 流式不变');
  {
    const m = mark();
    const s = oaSummary(await sseCollect(await fetch(GW + '/v1/chat/completions', { method: 'POST',
      headers: { 'content-type': 'application/json', 'x-conversation-id': 'kel-2' },
      body: JSON.stringify({ model: 'fake-model', stream: true, messages: [{ role: 'user', content: '嗨 [A]' }] }) })));
    await sleep(300);
    const d = since(m);
    ok(s.text === '你好呀' && s.finish === 'stop' && typeof upReqs(d)[0].messages[0].content === 'string' &&
      upReqs(d)[0].messages[0].content.includes('[当前时间'), 'T14', { s, up: upReqs(d)[0].messages });
  }

  console.log('T15 上游 4xx：不重试同一个中转、换备用，都失败时列出各自原因、不存');
  {
    const m = mark();
    const s = oaSummary(await sseCollect(await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-e',
      pluvia: { v: '0.2', turn: { id: 'te1', round: 1 }, tail: '' }, messages: [{ role: 'user', content: '在吗 [E]' }] })));
    await sleep(300);
    const d = since(m);
    ok(s.text.includes('所有中转都失败了') && s.text.includes('账户余额不足（测试）') && upReqs(d).length === 2 &&
      upReqs(d)[1]._target === 'backup' && saves(d).length === 0, 'T15 Pluvia', { s, n: upReqs(d).length });
    const m2 = mark();
    const s2 = oaSummary(await sseCollect(await fetch(GW + '/v1/chat/completions', { method: 'POST',
      headers: { 'content-type': 'application/json', 'x-conversation-id': 'kel-e' },
      body: JSON.stringify({ model: 'fake-model', stream: true, messages: [{ role: 'user', content: '在吗 [E]' }] }) })));
    await sleep(300);
    ok(s2.text.includes('账户余额不足（测试）') && upReqs(since(m2)).length === 2, 'T15 Kelivo 路径同样显示真实原因', s2);
  }

  console.log('T16 pluvia.system = append / ignore（约定 8.1）');
  {
    const m = mark();
    await sseCollect(await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-sys',
      pluvia: { v: '0.2', turn: { id: 'tsys1', round: 1 }, tail: '', system: 'append' },
      messages: [{ role: 'system', content: '我是 Pluvia 的提示词' }, { role: 'user', content: '嗨 [A]' }] }));
    await sleep(300);
    const up = upReqs(since(m))[0];
    const last = up.system[up.system.length - 1];
    ok(up.system.length >= 2 && up.system[0].text.includes('记忆系统') && last.text.includes('我是 Pluvia 的提示词') &&
      last.cache_control && last.cache_control.ttl === '1h', 'T16 append：接在记忆库 system 后面，带 1 小时断点',
      up.system.map(b => ({ t: b.text.slice(0, 16), cc: b.cache_control })));
    const m2 = mark();
    await sseCollect(await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-sys',
      pluvia: { v: '0.2', turn: { id: 'tsys2', round: 1 }, tail: '', system: 'ignore' },
      messages: [{ role: 'system', content: '我是 Pluvia 的提示词' }, { role: 'user', content: '嗨 [A]' }] }));
    await sleep(300);
    ok(!JSON.stringify(upReqs(since(m2))[0].system).includes('我是 Pluvia 的提示词'), 'T16 ignore：不带前端 system');
  }

  console.log('T17 Anthropic 格式 append：前端 system 数组带缓存标记');
  {
    const m = mark();
    await (await post('/v1/messages', { model: 'fake-model', stream: false, max_tokens: 500, conversation_id: 'conv-sys2',
      system: [{ type: 'text', text: '端点提示词' }, { type: 'text', text: '会话提示词', cache_control: { type: 'ephemeral' } }],
      pluvia: { v: '0.2', turn: { id: 'tsys3', round: 1 }, tail: '', system: 'append' },
      messages: [{ role: 'user', content: [{ type: 'text', text: '嗨 [A]', cache_control: { type: 'ephemeral' } }] }] },
      { 'x-api-key': 'x', 'anthropic-version': '2023-06-01' })).json();
    const up = upReqs(since(m))[0];
    const n = up.system.length;
    ok(up.system[n - 2].text.includes('端点提示词') && !up.system[n - 2].cache_control && up.system[n - 1].text === '会话提示词' &&
      up.system[n - 1].cache_control.ttl === '1h' && countCC(up) <= 4, 'T17 只在最后一块留 1 小时断点',
      up.system.map(b => ({ t: b.text.slice(0, 12), cc: b.cache_control })));
  }

  console.log('T18 辅助请求（pluvia.aux，约定 8.5）：不走记忆');
  {
    const m = mark();
    const s = oaSummary(await sseCollect(await post('/v1/chat/completions', { model: 'fake-model', stream: true, memory_skip: true,
      tools: oaTools, pluvia: { v: '0.2', client: 'pluvia', aux: 'translate', system: 'append' },
      messages: [{ role: 'system', content: '把下面的思考过程翻译成中文' }, { role: 'user', content: 'I think [A]' }] })));
    await sleep(300);
    const d = since(m); const up = upReqs(d)[0];
    ok(s.text === '你好呀' && up.system.length === 1 && up.system[0].text === '把下面的思考过程翻译成中文' &&
      !JSON.stringify(up.messages).includes('[当前时间') && up.messages[0].content === 'I think [A]',
      'T18 system 只用前端的，不注入记忆目录 / 时间头', up);
    ok(up.tools.length === 1 && up.tools[0].name === 'music_tool', 'T18 不带网关的记忆工具', up.tools && up.tools.map(t => t.name));
    ok(!d.mem.some(x => x.k === 'search' || x.k === 'save' || x.k === 'thinking'), 'T18 不检索、不存', d.mem.map(x => x.k));
    const m2 = mark();
    await (await post('/v1/chat/completions', { model: 'fake-model', stream: false, memory_skip: true,
      pluvia: { v: '0.2', aux: 'translate' }, messages: [{ role: 'system', content: '翻译' }, { role: 'user', content: 'hi [A]' }] })).json();
    const up2 = upReqs(since(m2))[0];
    ok(!('tools' in up2) && !('tool_choice' in up2), 'T18 没有工具时不带 tools 字段', Object.keys(up2));
  }

  console.log('T19 存 L0 时去掉工具记录 / 链接卡片（约定 8.2）');
  {
    const m = mark();
    await sseCollect(await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-head',
      pluvia: { v: '0.2', turn: { id: 'th1', round: 1 }, tail: '' },
      messages: [
        { role: 'user', content: '放首歌吧 [A]' },
        { role: 'assistant', content: '[图片：晴天.jpg]\n[工具记录 · 听歌 · 晴天]\n[工具记录 · 拾贝 · 一张照片]\n放好了\n\n好听吗\n[链接卡片 · B站] 晴天 · @周 · 摘要 · 1万' },
        { role: 'user', content: '好听 [A]' }
      ] }));
    await sleep(300);
    const d = since(m); const sv = saves(d);
    ok(sv.length === 1 && sv[0].b.messages[1].content === '放好了\n\n好听吗' && sv[0].b.messages[3].content === '你好呀', 'T19 L0 只存正文', sv.map(x => x.b));
    ok(JSON.stringify(upReqs(d)[0].messages[1]).includes('[工具记录 · 听歌 · 晴天]'), 'T19 发给模型的原文不变');
  }

  console.log('T20 自动切换中转：主中转 503 → 改用备用（模型名自动对应）');
  {
    const m = mark();
    const s = oaSummary(await sseCollect(await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-fo',
      pluvia: { v: '0.2', turn: { id: 'tfo1', round: 1 }, tail: '' }, messages: [{ role: 'user', content: '在吗 [F]' }] })));
    await sleep(300);
    const d = since(m); const ups = upReqs(d);
    const bk = ups.filter(x => x._target === 'backup');
    ok(s.text === '备用中转回答' && s.finish === 'stop', 'T20 由备用中转回答', s);
    ok(ups.filter(x => !x._target).length === 3 && bk.length === 1 && bk[0].model === 'vendor/fake.model' && !ups.some(x => x._target === 'other'),
      'T20 主中转试 3 次后切到备用，模型名对应成 vendor/fake.model，没有同款模型的中转被跳过', ups.map(x => (x._target || 'primary') + ':' + x.model));
    ok(saves(d).length === 1, 'T20 照常存 L0');
  }

  console.log('T21 参数错误（400）不切换中转');
  {
    const m = mark();
    const s = oaSummary(await sseCollect(await post('/v1/chat/completions', { model: 'fake-model', stream: true, conversation_id: 'conv-fo2',
      pluvia: { v: '0.2', turn: { id: 'tfo2', round: 1 }, tail: '' }, messages: [{ role: 'user', content: '在吗 [G]' }] })));
    await sleep(300);
    const ups = upReqs(since(m));
    ok(s.text.includes('参数错误（测试）') && ups.length === 1, 'T21 只请求一次主中转', { s, n: ups.length });
  }

  console.log('T22 Kelivo 非流式同样会切换');
  {
    const m = mark();
    const j = await (await fetch(GW + '/v1/chat/completions', { method: 'POST',
      headers: { 'content-type': 'application/json', 'x-conversation-id': 'kel-fo' },
      body: JSON.stringify({ model: 'fake-model', stream: false, messages: [{ role: 'user', content: '在吗 [F]' }] }) })).json();
    await sleep(300);
    const ups = upReqs(since(m));
    ok(j.choices && j.choices[0].message.content === '备用中转回答' && ups.some(x => x._target === 'backup'), 'T22', { j, n: ups.length });
  }

  console.log('T23 /api 代理不放行会返回明文密钥的内部接口');
  {
    const r1 = await fetch(GW + '/api/config/current');
    const r2 = await fetch(GW + '/api/config/chat_targets?x=1');
    const r3 = await fetch(GW + '/api/config/providers');
    ok(r1.status === 403 && r2.status === 403 && r3.status === 200, 'T23', [r1.status, r2.status, r3.status]);
  }

  console.log(`\n通过 ${pass}，失败 ${fail}`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('测试脚本出错', e); process.exit(2); });
