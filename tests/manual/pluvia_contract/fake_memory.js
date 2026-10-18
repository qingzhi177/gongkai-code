// 假的记忆服务：只回网关需要的字段，并把 save / thinking / delete 等调用记到 mem.jsonl
const express = require('express');
const fs = require('fs');
const LOG = (process.env.GWTEST_DIR || '/tmp/gwtest') + '/mem.jsonl';
const log = (k, b) => fs.appendFileSync(LOG, JSON.stringify({ k, b }) + '\n');
const app = express();
app.use(express.json({ limit: '50mb' }));
app.get('/config/current', (q, r) => r.json({ configured: true, name: 'fake', base_url: 'http://127.0.0.1:3997/v1', api_key: 'k', model: 'fake-model' }));
app.get('/config/providers', (q, r) => r.json([]));
app.get('/config/chat_targets', (q, r) => r.json({ targets: [
  { id: 1, name: 'fake', base_url: 'http://127.0.0.1:3997/v1', api_key: 'k', models: ['fake-model'], active: true },
  { id: 2, name: 'backup', base_url: 'http://127.0.0.1:3997/backup/v1', api_key: 'k2', models: ['vendor/fake.model:batch', 'vendor/fake.model'], active: false },
  { id: 3, name: 'other', base_url: 'http://127.0.0.1:3997/other/v1', api_key: 'k3', models: ['some-other-model'], active: false }
] }));
app.get('/last_session', (q, r) => r.json({ last_ts: '2026-10-14 01:00:00' }));
app.post('/search', (q, r) => {
  log('search', q.body);
  r.json({ memories: [{ id: 7, l1_summary: '一起听过晴天', vec_sim: 0.9, score: 1,
    metadata: { ts: '2026-10-01 20:30:00', event_type: 'event', client: 'pluvia', valence: 0.5, arousal: 0.6 },
    l0_context: '用户：晴天\nAI：好' }] });
});
app.post('/search_serendipity', (q, r) => r.json({ memories: [] }));
app.get('/narrative/current', (q, r) => r.json({ exists: false }));
app.get('/summary/current', (q, r) => r.json({ exists: false }));
app.get('/custom_prompt', (q, r) => r.json({ enabled: false }));
app.get('/conv/:id/settings', (q, r) => r.json({}));
app.post('/save_conversation', (q, r) => { log('save', q.body); r.json({ status: 'ok' }); });
app.post('/thinking', (q, r) => { log('thinking', q.body); r.json({ ok: true }); });
app.delete('/l0/conversation/:id', (q, r) => { log('delete', { id: q.params.id }); r.json({ status: 'ok', l1_removed: 2 }); });
app.post('/compose/turn', (q, r) => { log('compose', q.body); r.json({ mode: 'shadow', card: '', picks: [] }); });
app.use((q, r) => { log('other', { path: q.path, method: q.method }); r.json({ status: 'ok' }); });
app.listen(3998, '127.0.0.1', () => console.log('fake memory on 3998'));
