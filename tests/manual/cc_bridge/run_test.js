// cc-bridge 的测试：生命线、MCP 记忆工具（假网关）、桥（假 claude）。不花订阅额度、不碰真实数据。
// 运行：node tests/manual/cc_bridge/run_test.js
'use strict';
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const ROOT = path.resolve(__dirname, '../../..');
const B = path.join(ROOT, 'services/cc-bridge');
let pass = 0, fail = 0;
const ok = (c, name, extra) => { if (c) { pass++; console.log('  ✓', name); } else { fail++; console.log('  ✗', name, extra !== undefined ? JSON.stringify(extra).slice(0, 600) : ''); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));

function lifeline(cmd) {
  const r = spawnSync(process.execPath, [path.join(B, 'lifeline.js')], { input: JSON.stringify({ tool_name: 'Bash', tool_input: { command: cmd } }) });
  const out = r.stdout.toString();
  return out ? JSON.parse(out).hookSpecificOutput.permissionDecision : 'allow';
}

function mcpSession(env) {
  const p = spawn(process.execPath, [path.join(B, 'mcp_memory.js')], { env: { ...process.env, ...env } });
  let buf = ''; const waiters = {};
  p.stdout.on('data', c => { buf += c; let i; while ((i = buf.indexOf('\n')) >= 0) { const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); if (waiters[m.id]) waiters[m.id](m); } });
  let id = 0;
  return { call: (method, params) => new Promise(r => { const k = ++id; waiters[k] = r; p.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: k, method, params }) + '\n'); }),
    notify: (method) => p.stdin.write(JSON.stringify({ jsonrpc: '2.0', method }) + '\n'), close: () => p.stdin.end() };
}

async function sse(port, body) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body));
    const req = http.request({ method: 'POST', host: '127.0.0.1', port, path: '/v1/turn', headers: { 'content-type': 'application/json', 'content-length': data.length } }, res => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', c => { text += c; });
      res.on('end', () => {
        if (res.statusCode !== 200) return resolve({ status: res.statusCode, events: [], raw: text });
        resolve({ status: 200, events: text.split('\n\n').filter(Boolean).map(x => JSON.parse(x.replace(/^data: /, ''))) });
      });
    });
    req.on('error', reject);
    req.end(data);
  });
}
const getJ = (port, p) => new Promise(r => http.get({ host: '127.0.0.1', port, path: p }, res => { let t = ''; res.on('data', c => t += c); res.on('end', () => r(JSON.parse(t))); }));
const postJ = (port, p, body) => new Promise(r => { const d = Buffer.from(JSON.stringify(body)); const q = http.request({ method: 'POST', host: '127.0.0.1', port, path: p, headers: { 'content-type': 'application/json', 'content-length': d.length } }, res => { let t = ''; res.on('data', c => t += c); res.on('end', () => r(JSON.parse(t))); }); q.end(d); });

(async () => {
  console.log('\n[1] 生命线');
  ok(lifeline('tmux kill-session -t pluvia') === 'deny', '不许关 tmux 会话');
  ok(lifeline('pkill -f node') === 'deny' && lifeline('killall claude') === 'deny', '不许结束桥 / 网关 / claude 的进程');
  ok(lifeline('rm -rf ~/memory-system/data/sqlite') === 'deny' && lifeline('rm ~/.claude/projects/x.jsonl') === 'deny', '不许删记忆库数据和会话记录');
  ok(lifeline('sudo systemctl stop memory-gateway') === 'deny', '不许停服务');
  ok(lifeline('cat ~/.claude/.credentials.json') === 'deny', '不许碰登录凭证');
  ok(lifeline('ls -la && git status') === 'allow' && lifeline('rm /tmp/x.txt') === 'allow', '普通命令照常');

  console.log('\n[2] MCP 记忆工具（假网关）');
  const calls = [];
  const gw = http.createServer((req, res) => {
    let t = ''; req.on('data', c => t += c); req.on('end', () => {
      if (req.url === '/internal/tools') return res.end(JSON.stringify({ tools: [{ name: 'recall', description: '回忆', inputSchema: { type: 'object', properties: { query: { type: 'string' } } } }] }));
      calls.push(JSON.parse(t));
      res.end(JSON.stringify({ content: '记忆 #1：猫', is_error: false }));
    });
  }).listen(39011, '127.0.0.1');
  const m = mcpSession({ GATEWAY_INTERNAL_URL: 'http://127.0.0.1:39011', MEMORY_CONV_ID: 'conv-x' });
  const init = await m.call('initialize', { protocolVersion: '2025-06-18', capabilities: {} });
  ok(init.result && init.result.protocolVersion === '2025-06-18' && init.result.capabilities.tools, 'initialize', init);
  m.notify('notifications/initialized');
  const tl = await m.call('tools/list', {});
  ok(tl.result.tools.length === 1 && tl.result.tools[0].name === 'recall' && tl.result.tools[0].inputSchema, 'tools/list 转给网关', tl);
  const tc = await m.call('tools/call', { name: 'recall', arguments: { query: '猫' } });
  ok(tc.result.content[0].text === '记忆 #1：猫' && !tc.result.isError && calls[0].conv_id === 'conv-x' && calls[0].args.query === '猫', 'tools/call 带上对话 id', { tc, calls });
  const bad = await m.call('no/such', {});
  ok(bad.error && bad.error.code === -32601, '不认识的方法报错');
  m.close(); gw.close();
  const m2 = mcpSession({ GATEWAY_INTERNAL_URL: 'http://127.0.0.1:39999' });
  const tl2 = await m2.call('tools/list', {});
  const tc2 = await m2.call('tools/call', { name: 'recall', arguments: {} });
  ok(tl2.result.tools.length === 0 && tc2.result.isError, '网关没起来：清单是空的、调用报错，不卡住');
  m2.close();

  console.log('\n[3] 桥（假 claude）');
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ccb_'));
  const LOG = path.join(home, 'fake.log');
  const PORT = 39010;
  const br = spawn(process.execPath, [path.join(B, 'bridge.js')], { env: { ...process.env, CC_BRIDGE_PORT: String(PORT), CC_HOME: home,
    CLAUDE_BIN: path.join(__dirname, 'fake_claude.js'), CC_FAKE_LOG: LOG, ANTHROPIC_API_KEY: 'should-be-dropped', ANTHROPIC_BASE_URL: 'http://x' } });
  let brLog = ''; br.stdout.on('data', c => brLog += c); br.stderr.on('data', c => brLog += c);
  await sleep(400);
  const r1 = await sse(PORT, { conv_id: 'conv-a', system: '你是测试用的。', content: '早 [T]', model: 'opus' });
  const types = r1.events.map(e => e.type);
  ok(r1.status === 200 && types[0] === 'init' && types.includes('thinking') && types[types.length - 1] === 'done', '一轮：init → 思考 → … → done', types);
  const text = r1.events.filter(e => e.type === 'text').map(e => e.text).join('');
  ok(text === '你好呀（第1轮）', '逐字拼起来是完整的一句（子代理的不转）', text);
  const tools = r1.events.filter(e => e.type === 'tool');
  ok(tools.length === 2 && tools[0].name === 'recall' && tools[0].status === 'running' && tools[1].status === 'done' && tools[1].name === 'recall' && tools[1].content === '记忆 #1：猫', '工具调用：名字去掉 mcp__memory__ 前缀，运行中 → 完成', tools);
  const done = r1.events[r1.events.length - 1];
  ok(done.usage.cache_read_input_tokens === 100 && done.session_id && done.cost_usd === 0.01, 'done 带用量和 session_id', done);
  const lines = fs.readFileSync(LOG, 'utf8').trim().split('\n').map(l => JSON.parse(l));
  const a1 = lines[0];
  ok(!a1.has_api_key && !a1.has_base_url, '子进程里去掉了 ANTHROPIC_API_KEY / BASE_URL（只用订阅登录）', a1);
  ok(a1.argv.includes('--input-format') && a1.argv[a1.argv.indexOf('--tools') + 1] === '' && a1.argv.includes('--strict-mcp-config')
    && a1.argv[a1.argv.indexOf('--permission-mode') + 1] === 'dontAsk' && a1.argv.includes('--system-prompt-file') && a1.cwd === home,
    '常驻 stream-json、不给内置工具、只挂记忆 MCP、在陪伴目录里跑', a1.argv);
  const order = lines.filter(x => x.stdin).map(x => x.stdin.type === 'user' ? 'user' : x.stdin.request.subtype);
  ok(order.slice(0, 4).join(',') === 'mcp_status,mcp_status,mcp_status,user', '新进程先等记忆工具（MCP）连上，再发第一句话', order);
  const sp = fs.readFileSync(a1.argv[a1.argv.indexOf('--system-prompt-file') + 1], 'utf8');
  ok(sp === '你是测试用的。', 'system 写进文件传给 --system-prompt-file');
  const r2 = await sse(PORT, { conv_id: 'conv-a', system: '你是测试用的。', content: '再说一句', model: 'opus' });
  const t2 = r2.events.filter(e => e.type === 'text').map(e => e.text).join('');
  const spawns1 = fs.readFileSync(LOG, 'utf8').trim().split('\n').map(l => JSON.parse(l)).filter(x => x.argv).length;
  ok(t2 === '你好呀（第2轮）' && spawns1 === 1, '第二轮还是同一个进程（常驻）', { t2, spawns1 });
  const sid = done.session_id;
  const r3 = await sse(PORT, { conv_id: 'conv-a', system: '你是测试用的。（第二天的 system）', content: '第二天', model: 'opus' });
  const all = fs.readFileSync(LOG, 'utf8').trim().split('\n').map(l => JSON.parse(l)).filter(x => x.argv);
  ok(all.length === 2 && all[1].argv[all[1].argv.indexOf('--resume') + 1] === sid && r3.events.some(e => e.type === 'done'),
    'system 变了：两轮之间换进程，用新的 system --resume 接着同一段会话', all.map(x => x.argv.includes('--resume')));
  const sess = await getJ(PORT, '/v1/sessions');
  ok(sess.sessions.length === 1 && sess.sessions[0].conv_id === 'conv-a' && sess.sessions[0].session_id === sid
    && sess.sessions[0].mcp[0].status === 'connected', '/v1/sessions（带 MCP 状态）', sess);
  const saved = JSON.parse(fs.readFileSync(path.join(home, 'run/sessions.json'), 'utf8'));
  ok(saved['conv-a'].session_id === sid, '对话和会话的对应存了盘（桥重启后能接上）');
  const pSlow = sse(PORT, { conv_id: 'conv-a', system: '你是测试用的。（第二天的 system）', content: '慢慢说 [SLOW]' });
  await sleep(300);
  const busy = await sse(PORT, { conv_id: 'conv-a', system: '你是测试用的。（第二天的 system）', content: '插话' });
  ok(busy.status === 409, '上一轮没说完时再来一轮：409');
  const intr = await postJ(PORT, '/v1/interrupt', { conv_id: 'conv-a' });
  const slow = await pSlow;
  const last = slow.events[slow.events.length - 1];
  ok(intr.interrupted && last.type === 'done' && last.is_error, '点了停：发 interrupt，这一轮结束', { intr, last });
  const ctl = fs.readFileSync(LOG, 'utf8').trim().split('\n').map(l => JSON.parse(l)).filter(x => x.stdin && x.stdin.type === 'control_request');
  ok(ctl.some(x => x.stdin.request.subtype === 'interrupt'), 'interrupt 用的是 control_request');
  const eph = await sse(PORT, { conv_id: 'conv-test', system: 's', content: '测试', ephemeral: true });
  const allp = fs.readFileSync(LOG, 'utf8').trim().split('\n').map(l => JSON.parse(l)).filter(x => x.argv);
  ok(eph.events.some(e => e.type === 'done') && allp[allp.length - 1].argv.includes('--no-session-persistence') && !allp[allp.length - 1].argv.includes('--resume'),
    '测试对话：--no-session-persistence，不落盘');
  const bad1 = await sse(PORT, { system: 's', content: 'x' });
  ok(bad1.status === 400, '没带 conv_id：400');
  br.kill('SIGTERM');
  await sleep(300);
  fs.rmSync(home, { recursive: true, force: true });
  console.log(`\n通过 ${pass}，失败 ${fail}`);
  if (fail) console.log(brLog.slice(-2000));
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('测试出错', e); process.exit(2); });
