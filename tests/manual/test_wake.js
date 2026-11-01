// 自动醒来（services/gateway/wake.js）的单元测试：假的记忆服务（本机临时端口）、假上游、假推送，不发任何真实请求。
// 运行：NODE_PATH=services/gateway/node_modules node tests/manual/test_wake.js
'use strict';
const path = require('path');
const fs = require('fs');
const os = require('os');
const http = require('http');
const W = require(path.join(__dirname, '../../services/gateway/wake.js'));

let pass = 0, fail = 0;
function ok(cond, name, extra) {
  if (cond) { pass++; console.log('  ✓', name); }
  else { fail++; console.log('  ✗', name, extra !== undefined ? JSON.stringify(extra).slice(0, 700) : ''); }
}

// 假记忆服务：记下收到的请求；/wake/check 按队列回答
const got = [];
let checkQueue = [];
let settingsItems = [];
const mem = http.createServer((req, res) => {
  let body = '';
  req.on('data', c => { body += c; });
  req.on('end', () => {
    const j = body ? JSON.parse(body) : null;
    got.push({ path: req.url.split('?')[0], body: j });
    res.setHeader('content-type', 'application/json');
    if (req.url.startsWith('/wake/settings')) return res.end(JSON.stringify({ items: settingsItems }));
    if (req.url.startsWith('/wake/check')) return res.end(JSON.stringify(checkQueue.shift() || { status: 'ok', wake: false, reason: '不醒' }));
    if (req.url.startsWith('/wake/finish')) {
      const spoke = j.outcome === 'spoke';
      return res.end(JSON.stringify({ status: 'ok', run: { id: j.run_id, conv_id: 'c1', text: j.text, finished_at: '2026-10-31 13:00:00', outcome: j.outcome },
        push: spoke ? [{ url: 'https://ntfy.sh/xingxing-abc', format: 'ntfy' }, { url: 'https://push.example.org/up/1', format: 'json' }] : [],
        push_title: '', push_preview: true }));
    }
    res.end(JSON.stringify({ status: 'ok' }));
  });
});
const of = (p) => got.filter(x => x.path === p);
// 设置：直接塞给 wake，假记忆服务也回同样的（假时钟往前跳时 wake 会重新去取）
function setS(w, v) { w._setSettings(v); settingsItems = Object.entries(v).map(([key, value]) => ({ key, value })); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  await new Promise(r => mem.listen(0, '127.0.0.1', r));
  const memoryUrl = `http://127.0.0.1:${mem.address().port}`;

  console.log('\n[1] 醒来发的消息、说不说');
  const hist = [{ role: 'user', content: '早' }, { role: 'assistant', content: '早呀' },
    { role: 'user', content: [{ type: 'text', text: '我去考试啦', cache_control: { type: 'ephemeral', ttl: '1h' } }, { type: 'text', text: '［附注］' }] }];
  const base = { system: [{ type: 'text', text: 'S', cache_control: { type: 'ephemeral', ttl: '1h' } }], messages: hist,
    tools: [{ type: 'custom', name: 'music_play' }, { type: 'custom', name: 'recall' }, { type: 'custom', name: 'update_profile' }],
    thinking: { type: 'enabled', budget_tokens: 8000 }, outputConfig: { effort: 'high' }, toolChoice: null, maxTokens: 16000 };
  const wm = W.wakeMessages(base, '加油', ['考完了吗'], '［醒来］');
  ok(wm.length === 5 && wm[2] === hist[2] && wm[3].role === 'assistant' && wm[3].content[0].text === '加油\n\n考完了吗'
    && wm[4].role === 'user' && wm[4].content[0].text === '［醒来］', '历史原样 + ta 的回复和醒来说过的话 + 醒来说明', wm.slice(3));
  ok(hist.length === 3, '不改原来的历史');
  ok(W.wakeMessages(base, '', [], 'n').length === 4, '上一轮没有回复文字：直接接醒来说明');
  ok(W.splitReply('想阿星了。［不说］').speak === false && W.splitReply('想阿星了。［不说］').aside === '想阿星了。', '［不说］：不说，前面写的留着');
  ok(W.splitReply('[不说]').speak === false && W.splitReply('【不说】').speak === false && W.splitReply('（不说）').speak === false, '几种括号都认');
  ok(W.splitReply(' 早呀 ').speak && W.splitReply(' 早呀 ').text === '早呀', '正常说话');
  ok(W.splitReply('早呀', false).speak === false, '静默档：不发');
  ok(JSON.stringify(W.toolNames(base.tools)) === '["music_play","recall","update_profile"]', '工具名');

  console.log('\n[2] 记住最近一轮');
  let t = Date.parse('2026-10-31T04:00:00Z');
  const rounds = [];
  let script = [];
  const pushes = [];
  const tools = [];
  let busy = false;
  const touched = [];
  const stateFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'waketest-')), 'run', 'wake_last_turn.json');
  const target = { name: 'openrouter', apiUrl: 'https://openrouter.ai/api/v1/messages', apiKey: 'sk-test-not-real', model: 'anthropic/claude-opus-4.6' };
  const mk = () => W.createWake({
    memoryUrl, log: () => {}, now: () => t, stateFile,
    runRound: async (args) => {
      rounds.push(args);
      const step = script.shift();
      if (step instanceof Error) throw step;
      if (typeof step === 'function') return await step(args);
      return step;
    },
    execTool: async (name, args, ctx) => { tools.push({ name, args, ctx }); return name === 'recall' ? '找到 1 条：阿星说最后一题没把握' : 'ok'; },
    isServerTool: (n) => ['recall', 'feel', 'jot', 'update_profile', 'get_current_time', 'bocha_search', 'schedule_wake'].includes(n),
    resolveTarget: (info) => (info && info.apiUrl === target.apiUrl ? { ...info, apiKey: 'sk-test-not-real' } : null),
    busy: () => busy, onTouch: (c) => touched.push(c),
    httpPost: async (url, body, opts) => { pushes.push({ url, body, opts }); return { status: 200 }; }
  });
  const wk = mk();
  setS(wk, { enabled: false });
  wk.noteTurn({ convId: 'c1', base, target, slug: 'anthropic', replyText: '先调一下', final: false, ttl: '1h' });
  ok(!wk._cur(), '这一轮还没说完（交给前端执行工具）：先不算');
  wk.noteTurn({ convId: 'c1', base: null, target, slug: 'anthropic', replyText: '先调一下\n\n加油，考完告诉我', final: true, ttl: '1h' });
  ok(wk._cur() && wk._cur().base === base && wk._cur().replyText === '先调一下\n\n加油，考完告诉我' && wk._cur().at === t, '说完了：记住开头的样子和整条回复');
  wk.noteTurn({ convId: 'c9', base: null, target, replyText: 'x', final: true });
  ok(wk._cur().convId === 'c1', '没有开头的续问（网关重启过）：不动');
  wk.noteTurn({ convId: 'c1', base, target: { ...target, format: 'openai' }, replyText: 'x', final: true });
  ok(wk._cur().replyText.endsWith('考完告诉我'), 'OpenAI 格式的上游（辅助模型）：不算');
  await sleep(50);
  const saved = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  ok(saved.convId === 'c1' && saved.target.apiUrl === target.apiUrl && !JSON.stringify(saved).includes('sk-test-not-real'), '存了一份（不存 key）');
  ok((fs.statSync(stateFile).mode & 0o777) === 0o600, '只有自己能读（600）', (fs.statSync(stateFile).mode & 0o777).toString(8));

  console.log('\n[3] 关着、正在聊天：不去问');
  got.length = 0;
  await wk.tick();
  ok(of('/wake/check').length === 0 && wk.state().reason === '自动醒来关着', '关着：不问记忆服务');
  setS(wk, { enabled: true });
  busy = true; await wk.tick(); busy = false;
  ok(of('/wake/check').length === 0, '正在聊天：不问');
  await wk.tick();
  const ck = of('/wake/check')[0];
  ok(ck && ck.body.conv_id === 'c1' && ck.body.last_real_at === '2026-10-31 04:00:00' && ck.body.has_context === true
    && JSON.stringify(ck.body.tools) === '["music_play","recall","update_profile"]', '问的时候带上：哪个对话、上次聊天的时间（UTC）、工具', ck && ck.body);

  console.log('\n[4] 醒来：走平时那条链，工具照常（关掉的、前端的告诉 ta 用不了），说了话就推送');
  t += 3 * 3600000;
  checkQueue.push({ status: 'ok', wake: true, run_id: 7, kind: 'random', note: '［记忆系统｜醒来］现在是 15:00' });
  script = [
    { id: 'msg_01a', stop_reason: 'tool_use', usage: { input_tokens: 300, cache_read_input_tokens: 25000, cache_creation_input_tokens: 0, output_tokens: 80 },
      content: [{ type: 'thinking', thinking: '阿星这会儿应该考完了', signature: 's' }, { type: 'text', text: '' },
        { type: 'tool_use', id: 'tu1', name: 'recall', input: { query: '考试' } }, { type: 'tool_use', id: 'tu2', name: 'update_profile', input: {} },
        { type: 'tool_use', id: 'tu3', name: 'music_play', input: {} }] },
    { id: 'msg_01b', stop_reason: 'end_turn', usage: { input_tokens: 120, cache_read_input_tokens: 25300, cache_creation_input_tokens: 400, output_tokens: 30 },
      content: [{ type: 'text', text: '考完了吗？我刚想起你说最后一题没把握' }] }
  ];
  got.length = 0;
  await wk.tick();
  const r1 = rounds[0];
  ok(rounds.length === 2 && r1.system === base.system && r1.tools === base.tools && r1.thinking === base.thinking
    && r1.outputConfig === base.outputConfig && r1.maxTokens === 16000 && r1.providerPref === 'anthropic' && r1.target.apiKey === 'sk-test-not-real',
  'system / 工具 / thinking / 输出设置和上一轮一模一样（缓存前缀一样），优先发给上一轮那家', r1 && { ...r1, system: 0, messages: 0, tools: 0 });
  const m1 = r1.messages;
  ok(m1[2] === hist[2] && m1[3].content[0].text === '先调一下\n\n加油，考完告诉我' && m1[4].content[0].text.startsWith('［记忆系统｜醒来］'), '消息：历史 + 上一轮的回复 + 醒来说明');
  const tr = rounds[1].messages[rounds[1].messages.length - 1].content;
  ok(tools.length === 1 && tools[0].name === 'recall' && tools[0].ctx.convId === 'c1' && tools[0].ctx.wake === true, '记忆工具照常执行（带上对话）');
  ok(/没打开/.test(tr[1].content) && /对方的手机/.test(tr[2].content) && /没把握/.test(tr[0].content), '关掉的工具、前端的工具：告诉 ta 这次用不了', tr);
  ok(rounds[1].messages[5].role === 'assistant' && rounds[1].messages[5].content[0].type === 'thinking', '工具那一轮带着思考块接回去');
  await sleep(50);
  const fin = of('/wake/finish')[0];
  ok(fin && fin.body.run_id === 7 && fin.body.outcome === 'spoke' && fin.body.text === '考完了吗？我刚想起你说最后一题没把握'
    && JSON.stringify(fin.body.tools) === '["recall"]' && fin.body.thinking === '阿星这会儿应该考完了'
    && fin.body.usage.cache_read === 50300 && fin.body.usage.rounds === 2 && fin.body.usage.model === target.model, '记结果：说了什么、用了哪些工具、思考、用量', fin && fin.body);
  const ce = of('/cache/events')[0];
  ok(ce && ce.body.kind === 'wake' && ce.body.provider === 'Anthropic' && ce.body.cache_read === 50300, '用量也报给「缓存与心跳」', ce && ce.body);
  ok(pushes.length === 2 && pushes[0].url === 'https://ntfy.sh/xingxing-abc?title=' + encodeURIComponent('新消息')
    && pushes[0].body === '考完了吗？我刚想起你说最后一题没把握' && /text\/plain/.test(pushes[0].opts.headers['Content-Type']), 'ntfy 推送：标题放在地址里，正文是 ta 说的话', pushes[0]);
  ok(pushes[1].body.type === 'pluvia.wake' && pushes[1].body.id === 7 && pushes[1].body.conversation_id === 'c1' && pushes[1].body.at === '2026-10-31T13:00:00Z', '前端登记的地址：JSON', pushes[1].body);
  const pr = of('/wake/push_result')[0];
  ok(pr && pr.body.result.targets.length === 2 && pr.body.result.targets[0].host === 'ntfy.sh' && !JSON.stringify(pr.body).includes('xingxing-abc'), '推送结果记下（只记域名）', pr && pr.body);
  ok(touched[0] === 'c1', '读过缓存：缓存心跳重新计时');
  ok(JSON.stringify(wk._cur().spoken) === '["考完了吗？我刚想起你说最后一题没把握"]', '醒来说的话接进最近一轮');

  console.log('\n[5] 第二次醒：上次说的话在历史里；不说');
  checkQueue.push({ status: 'ok', wake: true, run_id: 8, kind: 'plan', note: '［醒来2］' });
  script = [{ id: 'msg_01c', stop_reason: 'end_turn', usage: {}, content: [{ type: 'text', text: '阿星可能还在路上。［不说］' }] }];
  got.length = 0; pushes.length = 0;
  await wk.tick(); await sleep(50);
  const m2 = rounds[2].messages;
  ok(m2[3].content[0].text === '先调一下\n\n加油，考完告诉我\n\n考完了吗？我刚想起你说最后一题没把握' && m2[4].content[0].text === '［醒来2］', '上次醒来说的话接在回复后面');
  const fin2 = of('/wake/finish')[0];
  ok(fin2 && fin2.body.outcome === 'silent' && !fin2.body.text && fin2.body.aside === '阿星可能还在路上。', '不说：只记下来，自己写的那句留着', fin2 && fin2.body);
  ok(pushes.length === 0, '不说：不推送');

  console.log('\n[6] 醒着的时候对方回来了、出错、工具轮数到上限');
  checkQueue.push({ status: 'ok', wake: true, run_id: 9, kind: 'random', note: 'n' });
  script = [async (args) => { wk.interrupt(); await sleep(5); if (args.signal.aborted) throw Object.assign(new Error('canceled'), { name: 'CanceledError' }); return {}; }];
  got.length = 0;
  await wk.tick(); await sleep(50);
  ok(of('/wake/finish')[0] && of('/wake/finish')[0].body.outcome === 'interrupted' && wk.state().running === null, '对方回来了：停下，什么也不发', of('/wake/finish')[0]);
  checkQueue.push({ status: 'ok', wake: true, run_id: 10, kind: 'random', note: 'n' });
  script = [Object.assign(new Error('Request failed'), { response: { status: 529, data: { error: { message: 'overloaded' } } } })];
  got.length = 0;
  await wk.tick(); await sleep(50);
  ok(of('/wake/finish')[0] && of('/wake/finish')[0].body.outcome === 'error' && of('/wake/finish')[0].body.note === 'overloaded', '上游出错：记成出错，说明原因');
  setS(wk, { enabled: true, max_rounds: 2 });
  checkQueue.push({ status: 'ok', wake: true, run_id: 11, kind: 'random', note: 'n' });
  const loopStep = { id: 'msg_01d', stop_reason: 'tool_use', usage: {}, content: [{ type: 'tool_use', id: 'x', name: 'get_current_time', input: {} }] };
  script = [loopStep, loopStep, loopStep];
  const before = rounds.length;
  got.length = 0;
  await wk.tick(); await sleep(50);
  ok(rounds.length - before === 2 && /上限/.test(of('/wake/finish')[0].body.note) && of('/wake/finish')[0].body.outcome === 'silent', '工具调用到上限：停下（没写话就当没说）', of('/wake/finish')[0]);

  console.log('\n[7] 网关重启：从存档接上，key 按地址找回');
  await sleep(50);
  const wk2 = mk();
  wk2._load();
  ok(wk2._cur() && wk2._cur().convId === 'c1' && !wk2._cur().target && wk2._cur().spoken.length === 1, '接上了（spoken 也在）');
  setS(wk2, { enabled: true });
  checkQueue.push({ status: 'ok', wake: true, run_id: 12, kind: 'plan', note: 'n' });
  script = [{ id: 'msg_01e', stop_reason: 'end_turn', usage: {}, content: [{ type: 'text', text: '［不说］' }] }];
  await wk2.tick(); await sleep(50);
  ok(rounds[rounds.length - 1].target.apiKey === 'sk-test-not-real' && rounds[rounds.length - 1].target.model === target.model, '醒来时按地址找回 key');
  t += 8 * 86400000;
  const wk3 = mk(); wk3._load();
  ok(!wk3._cur(), '存档太旧（超过 7 天）：不接');

  mem.close();
  console.log(`\n通过 ${pass}，失败 ${fail}`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
