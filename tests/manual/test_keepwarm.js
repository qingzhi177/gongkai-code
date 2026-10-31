// 缓存心跳（services/gateway/keepwarm.js）的单元测试：假时钟、假上游，不发任何真实请求。
// 运行：NODE_PATH=services/gateway/node_modules node tests/manual/test_keepwarm.js
'use strict';
const path = require('path');
const K = require(path.join(__dirname, '../../services/gateway/keepwarm.js'));

let pass = 0, fail = 0;
function ok(cond, name, extra) {
  if (cond) { pass++; console.log('  ✓', name); }
  else { fail++; console.log('  ✗', name, extra !== undefined ? JSON.stringify(extra).slice(0, 600) : ''); }
}

(async () => {
  console.log('\n[1] 看出是哪家、换缓存时长');
  ok(K.providerOf('msg_bdrk_01abc') === 'amazon-bedrock' && K.providerOf('msg_vrtx_011x') === 'google-vertex'
    && K.providerOf('msg_011CgaZt') === 'anthropic' && K.providerOf('chatcmpl-1') === null && K.providerOf(null) === null, '回复 id 的前缀');
  ok(K.isOpenRouter('https://openrouter.ai/api/v1/messages') && !K.isOpenRouter('http://127.0.0.1:3997/v1/messages'), '认得 OpenRouter');
  const ms = [{ role: 'user', content: '早' }, { role: 'user', content: [{ type: 'text', text: 'a', cache_control: { type: 'ephemeral' } }, { type: 'text', text: 'tail' }] }];
  K.applyTtl(ms, '1h');
  ok(ms[1].content[0].cache_control.ttl === '1h' && !ms[1].content[1].cache_control, '1 小时：断点带 ttl 1h，没有断点的块不加');
  K.applyTtl(ms, '5m');
  ok(!('ttl' in ms[1].content[0].cache_control) && ms[1].content[0].cache_control.type === 'ephemeral', '5 分钟：不带 ttl');

  console.log('\n[2] 心跳发的消息');
  const hist = [
    { role: 'user', content: '早呀' }, { role: 'assistant', content: '早' },
    { role: 'user', content: [{ type: 'text', text: '记忆目录（断点前）' }, { type: 'text', text: '今天去看海', cache_control: { type: 'ephemeral', ttl: '1h' } },
      { type: 'text', text: '［记忆附注：这一轮的］' }, { type: 'text', text: '（此刻：10 月 31 日）' }] }
  ];
  const bm = K.beatMessages(hist);
  const last = bm.messages[bm.messages.length - 1];
  ok(bm.cut && bm.messages.length === 3 && bm.messages[0] === hist[0] && last.content.length === 3
    && last.content[1].text === '今天去看海' && last.content[2].text === K.BEAT_TEXT, '到断点那一块为止，后面换成保温说明', last.content);
  ok(hist[2].content.length === 4, '不改原来的消息');
  const nb = K.beatMessages([{ role: 'user', content: '没有断点' }]);
  ok(!nb.cut && nb.messages.length === 1 && nb.messages[0].content[0].text === K.BEAT_TEXT, '消息里没有断点：只发一句保温说明（只有 system 有缓存）');

  console.log('\n[3] 什么时候心跳');
  let t = Date.parse('2026-10-31T12:00:00Z');
  const sent = [];
  let reply = { id: 'msg_bdrk_1', usage: { input_tokens: 40, cache_read_input_tokens: 25000, cache_creation_input_tokens: 0 } };
  const kw = K.createKeepwarm({ memoryUrl: 'http://127.0.0.1:9', report: false, now: () => t, log: () => {},
    postStream: async (target, body) => { sent.push({ target, body }); if (reply instanceof Error) throw reply; return reply; } });
  kw._setSettings({});
  const target = { name: 'openrouter', apiUrl: 'https://openrouter.ai/api/v1/messages', apiKey: 'k', model: 'anthropic/claude-opus-4.6' };
  const base = { system: [{ type: 'text', text: 'S', cache_control: { type: 'ephemeral', ttl: '1h' } }], messages: hist,
    tools: [{ name: 'recall' }], thinking: { type: 'enabled', budget_tokens: 32000 }, outputConfig: { effort: 'high' }, toolChoice: null };
  const slug = kw.noteTurn({ convId: 'c1', target, result: { id: 'msg_bdrk_9' }, usage: { input_tokens: 99 }, base, ttlUsed: '1h' });
  ok(slug === 'amazon-bedrock' && kw.preferFor('c1', target.apiUrl) === 'amazon-bedrock' && kw.preferFor('c1', 'http://x/v1/messages') === null
    && kw.preferFor('c2', target.apiUrl) === null, '记住这个对话上一轮是哪家回答的（只对 OpenRouter 生效）');
  t += 49 * 60000; await kw.tick();
  ok(sent.length === 0, '49 分钟：还不到');
  t += 2 * 60000; await kw.tick();
  const b = sent[0] && sent[0].body;
  ok(sent.length === 1 && b.system === base.system && b.tools === base.tools && b.thinking === base.thinking
    && b.output_config === base.outputConfig && b.max_tokens === 32000 + 256, '51 分钟：心跳一次，system / 工具 / thinking 一模一样', b && { ...b, system: 0, messages: 0 });
  ok(b && b.provider && b.provider.order[0] === 'amazon-bedrock' && b.provider.allow_fallbacks === false, '只发给上一轮那家（不许换）', b && b.provider);
  ok(b && b.messages[b.messages.length - 1].content.slice(-1)[0].text === K.BEAT_TEXT && Array.isArray(b.stop_sequences), '带保温说明和 stop_sequences');
  t += 10 * 60000; await kw.tick();
  ok(sent.length === 1, '心跳以后重新计时（10 分钟后不再发）');
  kw.begin(); t += 45 * 60000; await kw.tick(); kw.end();
  ok(sent.length === 1, '正在聊天（有一轮在进行）：不心跳');
  await kw.tick();
  ok(sent.length === 2, '聊完了、到点了：心跳');

  console.log('\n[4] 什么时候停');
  t += 50 * 60000; await kw.tick(); t += 50 * 60000; await kw.tick();
  ok(sent.length === 4 && kw._armed(), '停下 3 个多小时：还在心跳');
  t += 50 * 60000; await kw.tick();
  ok(sent.length === 4 && !kw._armed() && /太久/.test(kw.state().reason), '超过 4 小时：不再心跳', kw.state());
  kw.noteTurn({ convId: 'c1', target, result: { id: 'msg_bdrk_9' }, usage: {}, base, ttlUsed: '1h' });
  reply = { id: 'msg_bdrk_2', usage: { cache_read_input_tokens: 0, cache_creation_input_tokens: 24000 } };
  t += 51 * 60000; await kw.tick();
  ok(sent.length === 5 && !kw._armed() && /没了/.test(kw.state().reason), '心跳时缓存已经没了（写了一大段）：不再心跳', kw.state());
  kw.noteTurn({ convId: 'c1', target, result: { id: 'msg_bdrk_9' }, usage: {}, base, ttlUsed: '1h' });
  reply = Object.assign(new Error('boom'), { response: { status: 503 } });
  t += 51 * 60000; await kw.tick();
  ok(sent.length === 6 && !kw._armed() && /失败/.test(kw.state().reason), '心跳出错：不再心跳', kw.state());
  reply = { id: 'msg_bdrk_3', usage: { cache_read_input_tokens: 25000 } };
  kw._setSettings({ heartbeat: false });
  kw.noteTurn({ convId: 'c1', target, result: { id: 'msg_bdrk_9' }, usage: {}, base, ttlUsed: '1h' });
  t += 51 * 60000; await kw.tick();
  ok(sent.length === 6 && kw.state().armed === false, '心跳关着：不发');
  kw._setSettings({ daily_max: 1 });
  t = Date.parse('2026-11-05T01:00:00Z');   // 北京时间 11 月 5 日 9 点：新的一天
  kw.noteTurn({ convId: 'c1', target, result: { id: 'msg_bdrk_9' }, usage: {}, base, ttlUsed: '1h' });
  t += 51 * 60000; await kw.tick();
  t += 51 * 60000; await kw.tick();
  ok(sent.length === 7 && !kw._armed() && /上限/.test(kw.state().reason), '每天的上限：到了就停', kw.state());

  console.log('\n[5] 5 分钟缓存');
  kw._setSettings({ ttl_1h: false });
  ok(kw.ttl() === '5m', '设置成 5 分钟');
  t = Date.parse('2026-11-06T01:00:00Z');
  kw.noteTurn({ convId: 'c1', target, result: { id: 'msg_0111' }, usage: {}, base, ttlUsed: '5m' });
  t += 4 * 60000; await kw.tick();
  ok(sent.length === 7, '4 分钟：还不到');
  t += 40000; await kw.tick();
  ok(sent.length === 8 && sent[7].body.provider.order[0] === 'anthropic', '4 分 40 秒：心跳（发给 Anthropic）');
  for (let i = 0; i < 7; i++) { t += 4.6 * 60000; await kw.tick(); }
  ok(!kw._armed() && /太久/.test(kw.state().reason), '停下 30 分钟以后不再心跳', kw.state());

  console.log('\n[6] 不保温的情况');
  kw._setSettings({});
  kw.noteTurn({ convId: 'c9', target: { ...target, format: 'openai' }, result: { id: 'x' }, usage: {}, base, ttlUsed: '1h' });
  ok(!kw._armed(), 'OpenAI 格式的上游：不保温（缓存规则不一样）');
  kw.noteTurn({ convId: 'c1', target, result: { id: 'msg_0111' }, usage: {}, base, ttlUsed: '1h' });
  const before = kw._armed().lastTouch;
  t += 20 * 60000;
  kw.noteTurn({ convId: 'c1', target, result: { id: 'msg_0111' }, usage: {}, base: null, ttlUsed: '1h' });
  ok(kw._armed() && kw._armed().lastTouch === before + 20 * 60000, '同一轮的续问（前端工具回来）：从现在重新计时，保温的还是第一轮的前缀');
  const tgt2 = { ...target, apiUrl: 'https://relay.example/v1/messages' };
  kw.noteTurn({ convId: 'c3', target: tgt2, result: { id: 'msg_0111' }, usage: {}, base, ttlUsed: '1h' });
  t += 51 * 60000; await kw.tick();
  ok(!('provider' in sent[sent.length - 1].body), '别的中转：心跳不带 provider');

  console.log(`\n通过 ${pass}，失败 ${fail}`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('测试出错', e); process.exit(2); });
