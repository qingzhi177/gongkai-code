// 真实模型冒烟测试：走正在运行的网关（127.0.0.1:3000）和真实上游，会花少量 token。
// 所有请求都带 memory_skip，不存记忆。用法：node tests/manual/pluvia_contract/real_smoke.js [R1,R2,...]
const fs = require('fs');
const path = require('path');
const GW = process.env.GW || 'http://127.0.0.1:3000';
const LOG = path.join(__dirname, '../../../services/gateway/gateway.log');
const ONLY = (process.argv[2] || '').split(',').filter(Boolean);
const H = { 'content-type': 'application/json', 'x-client-name': 'pluvia' };
const HA = { ...H, 'x-api-key': 'local', 'anthropic-version': '2023-06-01' };
const sleep = ms => new Promise(r => setTimeout(r, ms));
let logMark = 0;
function markLog() { logMark = fs.statSync(LOG).size; }
function newLogLines(re) {
  const fd = fs.openSync(LOG, 'r');
  const size = fs.statSync(LOG).size;
  const buf = Buffer.alloc(Math.max(0, size - logMark));
  fs.readSync(fd, buf, 0, buf.length, logMark);
  fs.closeSync(fd);
  return buf.toString('utf8').split('\n').filter(l => re.test(l));
}
async function post(p, body, headers = H) {
  return fetch(GW + p, { method: 'POST', headers, body: JSON.stringify(body) });
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
    if (data === '[DONE]') continue;
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
  return { text, reasoning, finish, toolCalls, tools };
}
function anSummary(events) {
  const blocks = {}, order = [], tools = []; let stop = null;
  for (const e of events) {
    const d = e.data; if (!d) continue;
    if (e.ev === 'pluvia_tool') { tools.push(d.tool); continue; }
    if (d.type === 'content_block_start') { blocks[d.index] = { ...d.content_block, _json: '' }; order.push(d.index); }
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
  return { content, stop, tools };
}
const short = s => String(s || '').replace(/\s+/g, ' ').slice(0, 120);
function report(name, ok, detail) {
  console.log((ok ? '  ✓ ' : '  ✗ ') + name + (detail ? '  ' + detail : ''));
}
function usageLines() {
  return newLogLines(/\[UPSTREAM\]|\[CACHE\] 输入|网关错误|上游出错|上游错误说明/).map(l => '      ' + l.slice(0, 230));
}
const weatherOA = [{ type: 'function', function: { name: 'get_weather', description: '查询某个城市现在的天气', parameters: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] } } }];
const weatherAN = [{ name: 'get_weather', description: '查询某个城市现在的天气', input_schema: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] } }];
const music = [{ type: 'function', function: { name: 'music_tool', description: '放歌', parameters: { type: 'object', properties: { song: { type: 'string' } } } } }];
const tiny = (turnId, extra) => ({ stream: true, max_tokens: 60, conversation_id: 'smoke-pluvia', memory_skip: true,
  pluvia: { v: '0.2', client: 'pluvia', turn: { id: turnId, round: 1 }, tail: '（此刻：10月18日 周日 00:10）' },
  messages: [{ role: 'user', content: '连接测试，只回一个字：好\n\n（此刻：10月18日 周日 00:10）' }], ...extra });

const CASES = {
  async R1() {   // 复现当时报 77,753 token 的请求
    const s = oaSummary(await sseCollect(await post('/v1/chat/completions', tiny('smk-r1', { tool_choice: 'none', tools: music }))));
    report('R1 带 tool_choice:none + 1 个前端工具', !!s.text && !s.text.includes('网关错误'), short(s.text));
  },
  async R2() {
    const s = oaSummary(await sseCollect(await post('/v1/chat/completions', tiny('smk-r2', { tools: music }))));
    report('R2 不带 tool_choice', !!s.text && !s.text.includes('网关错误'), short(s.text));
  },
  async R3() {
    const s = oaSummary(await sseCollect(await post('/v1/chat/completions', tiny('smk-r3', {}))));
    report('R3 不带前端工具', !!s.text && !s.text.includes('网关错误'), short(s.text));
  },
  async R4() {   // 约定 8.1：前端 system 里的事实模型能不能看到（不靠模型愿不愿意照做）
    const s = oaSummary(await sseCollect(await post('/v1/chat/completions', {
      stream: true, max_tokens: 100, conversation_id: 'smoke-sys', memory_skip: true,
      pluvia: { v: '0.2', turn: { id: 'smk-r4b', round: 1 }, tail: '', system: 'append' },
      messages: [{ role: 'system', content: '（测试资料）用户养的猫叫「团子」。' }, { role: 'user', content: '我养的猫叫什么名字？只回答名字。' }] })));
    report('R4 pluvia.system=append：模型能看到前端 system 里的内容', s.text.includes('团子'), short(s.text));
  },
  async R5() {   // Anthropic 流式 + thinking + 前端工具两轮
    const base = { stream: true, max_tokens: 3000, thinking: { type: 'enabled', budget_tokens: 1024 }, conversation_id: 'smoke-tools', memory_skip: true,
      system: '你可以用 get_weather 查天气。用户问天气时，必须先调用 get_weather，不要凭空回答。', tools: weatherAN };
    const user = { role: 'user', content: '上海现在天气怎么样？' };
    const s1 = anSummary(await sseCollect(await post('/v1/messages', { ...base, pluvia: { v: '0.2', turn: { id: 'smk-r5', round: 1 }, tail: '', system: 'append' }, messages: [user] }, HA)));
    const tu = s1.content.find(b => b.type === 'tool_use');
    report('R5 第 1 轮交出 get_weather（stop_reason=tool_use）', s1.stop === 'tool_use' && tu && tu.name === 'get_weather',
      'blocks=' + s1.content.map(b => b.type + (b.signature ? '(签名)' : '')).join(',') + ' input=' + JSON.stringify(tu && tu.input));
    if (!tu) { console.log('      ', JSON.stringify(s1.content).slice(0, 400)); return; }
    const s2 = anSummary(await sseCollect(await post('/v1/messages', { ...base, pluvia: { v: '0.2', turn: { id: 'smk-r5', round: 2 }, tail: '', system: 'append' },
      messages: [user, { role: 'assistant', content: s1.content }, { role: 'user', content: [{ type: 'tool_result', tool_use_id: tu.id, content: '上海：晴，22°C，微风' }] }] }, HA)));
    const txt = s2.content.filter(b => b.type === 'text').map(b => b.text).join('');
    report('R5 第 2 轮带着结果续问，正常回答', s2.stop === 'end_turn' && txt.includes('22') && !txt.includes('网关错误'), short(txt));
  },
  async R6() {   // OpenAI 非流式 + 前端工具两轮
    const base = { stream: false, max_tokens: 800, conversation_id: 'smoke-tools2', memory_skip: true, tools: weatherOA };
    const msgs = [{ role: 'system', content: '用户问天气时，必须先调用 get_weather，不要凭空回答。' }, { role: 'user', content: '北京现在天气怎么样？' }];
    const j1 = await (await post('/v1/chat/completions', { ...base, pluvia: { v: '0.2', turn: { id: 'smk-r6', round: 1 }, tail: '', system: 'append' }, messages: msgs })).json();
    const m1 = j1.choices && j1.choices[0];
    const tc = m1 && m1.message.tool_calls && m1.message.tool_calls[0];
    report('R6 第 1 轮 finish_reason=tool_calls', m1 && m1.finish_reason === 'tool_calls' && tc && tc.function.name === 'get_weather', tc ? tc.function.arguments : JSON.stringify(j1).slice(0, 300));
    if (!tc) return;
    const j2 = await (await post('/v1/chat/completions', { ...base, pluvia: { v: '0.2', turn: { id: 'smk-r6', round: 2 }, tail: '', system: 'append' },
      messages: [...msgs, { role: 'assistant', content: m1.message.content, tool_calls: m1.message.tool_calls }, { role: 'tool', tool_call_id: tc.id, content: '北京：多云，18°C' }] })).json();
    const m2 = j2.choices && j2.choices[0];
    report('R6 第 2 轮正常回答', m2 && m2.finish_reason === 'stop' && /18/.test(m2.message.content), short(m2 && m2.message.content));
  },
  async R7() {   // 约定 8.5：辅助请求
    const s = oaSummary(await sseCollect(await post('/v1/chat/completions', {
      stream: true, max_tokens: 300, memory_skip: true, pluvia: { v: '0.2', client: 'pluvia', aux: 'translate', system: 'append' },
      messages: [{ role: 'system', content: '把用户给的英文思考过程翻译成简体中文，只输出译文。' },
        { role: 'user', content: 'The user seems tired tonight. I should keep my reply short and warm.' }] })));
    report('R7 辅助请求（翻译）', /[一-龥]/.test(s.text) && !s.text.includes('网关错误'), short(s.text));
  }
};

(async () => {
  for (const [name, fn] of Object.entries(CASES)) {
    if (ONLY.length && !ONLY.includes(name)) continue;
    markLog();
    try { await fn(); } catch (e) { report(name + ' 出错', false, e.message); }
    await sleep(500);
    usageLines().forEach(l => console.log(l));
  }
})();
