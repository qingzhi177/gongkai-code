// 自动醒来（网关这边）：对方不在的时候，ta 偶尔醒来一下。要不要醒、醒来说明怎么写、记录和前端取消息都在记忆服务（app/wakes.py）；
// Dashboard「自动醒来」页能调（默认关着）。
//
// 醒来不另开一条「心跳请求」，走的是平时说话那条链：
//  - 记住最近一轮真聊天第一次发给上游的样子（system、历史、工具、thinking 参数，和缓存心跳用的是同一份），和 ta 那一轮的回复；
//  - 到点时原样发出去，后面接上 ta 上一轮的回复（醒来时说过的话也接上），再接一段「醒来」的说明（只给 ta 看，不存进聊天记录）。
//    工具、system、历史和上一轮一模一样，缓存前缀也一样：醒一次大约多花一轮的钱（读一遍缓存 + 几百字）。
//  - 记忆工具照常能用（设置里能关掉几样：工具清单不变——变了缓存就对不上——调到关掉的那几样时告诉 ta 这次用不了）；
//    前端的工具（要在对方手机上执行的）醒来时用不了。
//  - ta 写了话：记忆服务放进「待取」，前端来取（/v1/pluvia/inbox），取走以后存进聊天记录；设置了推送地址就推一条。
//    ta 只写「［不说］」：只记在醒来记录里。下次对方回来说话时，附注里告诉 ta 醒来过几次。
//  - 醒着的时候对方正好回来说话：这次醒来停下（不发任何东西），先回对方。
// 最近一轮存一份在 data/run/（只有自己能读，不存 API key），网关重启以后 ta 约好的时间照样能醒。
'use strict';

const axios = require('axios');
const fs = require('fs');
const path = require('path');
const { providerOf, providerName } = require('./keepwarm');

const DEFAULTS = {
  enabled: false, plans: true, speak: true, max_rounds: 5,
  allow_recall: true, allow_feel: true, allow_jot: true, allow_profile: false, allow_web: false,
  push_url: '', push_title: '', push_preview: true
};
// 网关的工具 → 醒来时能不能用（看哪个设置；null = 一直能用）。和 app/wakes.py 的 TOOL_ALLOW 一致
const TOOL_ALLOW = {
  recall: 'allow_recall', feel: 'allow_feel', jot: 'allow_jot', update_profile: 'allow_profile',
  bocha_search: 'allow_web', get_current_time: null, schedule_wake: 'plans'
};
const SILENT_RE = /[［\[【（(]\s*不说\s*[］\]】）)]/g;
const PUSH_FALLBACK = '有一条新消息';
const PUSH_TEXT_MAX = 1000;
const STATE_MAX_DAYS = 7;

function utcStr(ms) {
  return new Date(ms).toISOString().slice(0, 19).replace('T', ' ');
}

function toolNames(tools) {
  return (tools || []).map(t => t && (t.name || (t.function && t.function.name))).filter(Boolean);
}

// 醒来时发的消息：上一轮第一次发给上游的历史（到对方那句为止）+ ta 那一轮的回复和醒来时说过的话 + 醒来说明
function wakeMessages(base, replyText, spoken, note) {
  const msgs = (base.messages || []).slice();
  const said = [replyText, ...(spoken || [])].map(s => String(s || '').trim()).filter(Boolean);
  if (said.length) msgs.push({ role: 'assistant', content: [{ type: 'text', text: said.join('\n\n') }] });
  msgs.push({ role: 'user', content: [{ type: 'text', text: note }] });
  return msgs;
}

// ta 醒来写的东西 → { speak, text, aside }（和 app/wakes.py 的 split_reply 一致）
function splitReply(text, speak = true) {
  const t = String(text || '').trim();
  if (!t) return { speak: false, text: '', aside: '' };
  if (new RegExp(SILENT_RE.source).test(t)) return { speak: false, text: '', aside: t.replace(SILENT_RE, '').trim() };
  if (!speak) return { speak: false, text: '', aside: t };
  return { speak: true, text: t, aside: '' };
}

function hostOf(url) {
  try { return new URL(url).host; } catch (_) { return '?'; }
}

function createWake({ memoryUrl, log = console.log, now = () => Date.now(), runRound, execTool, isServerTool = () => true,
  resolveTarget = (t) => t, busy = () => false, onTouch = () => {}, httpPost = null, stateFile = null, tickMs = 60000 } = {}) {
  let cached = { at: 0, values: { ...DEFAULTS } };
  let fetching = null;
  let cur = null;        // 最近一轮真聊天（说完了的）：{ convId, base, target, targetInfo, slug, replyText, spoken, at }
  let pending = null;    // 这一轮还没说完（前端工具执行中），说完了才变成 cur
  let running = null;    // { runId, ac, convId }
  let timer = null;
  let ticking = false;
  let lastReason = '';
  const post = httpPost || ((url, body, opts) => axios.post(url, body, opts));

  async function refresh() {
    if (fetching) return fetching;
    fetching = axios.get(`${memoryUrl}/wake/settings`, { timeout: 3000 }).then(r => {
      const vals = { ...DEFAULTS };
      for (const it of (r.data && r.data.items) || []) if (it && it.key) vals[it.key] = it.value;
      cached = { at: now(), values: vals };
    }).catch(() => { cached = { ...cached, at: now() }; }).finally(() => { fetching = null; });
    return fetching;
  }
  function settings() {
    if (now() - cached.at > 30000) refresh();
    return cached.values;
  }
  const enabled = () => !!settings().enabled;
  const toolEnabled = () => !!(settings().enabled && settings().plans);

  // ---------- 记住最近一轮 ----------
  function persist() {
    if (!stateFile || !cur) return;
    const data = {
      v: 1, convId: cur.convId, base: cur.base, slug: cur.slug, ttl: cur.ttl, replyText: cur.replyText, spoken: cur.spoken, at: cur.at,
      target: cur.targetInfo
    };
    fs.promises.mkdir(path.dirname(stateFile), { recursive: true })
      .then(() => fs.promises.writeFile(stateFile, JSON.stringify(data), { mode: 0o600 }))
      .then(() => fs.promises.chmod(stateFile, 0o600))
      .catch(e => log('[WAKE] 存最近一轮失败（不影响聊天）：' + e.message));
  }
  function load() {
    if (!stateFile) return;
    try {
      const d = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
      if (!d || !d.base || !d.convId || !d.at || now() - d.at > STATE_MAX_DAYS * 86400000) return;
      cur = { convId: d.convId, base: d.base, target: null, targetInfo: d.target || null, slug: d.slug || null, ttl: d.ttl || '1h',
        replyText: d.replyText || '', spoken: Array.isArray(d.spoken) ? d.spoken : [], at: d.at };
      log(`[WAKE] 接上了重启前的最近一轮（${d.convId}，${utcStr(d.at)} UTC）`);
    } catch (_) { /* 没有或读不了：等下一轮聊天 */ }
  }

  // 一轮真聊天：base 是这一轮第一次发给上游的样子；final=这一轮说完了（不是交给前端执行工具的中途）
  function noteTurn({ convId, base, target, slug, replyText, final, ttl }) {
    if (!convId || !target || target.format === 'openai') return;
    const info = { name: target.name, apiUrl: target.apiUrl, model: target.model };
    if (base) {
      const t = { convId, base, target, targetInfo: info, slug: slug || null, ttl: ttl || '1h', replyText: '', spoken: [], at: null };
      if (!final) { pending = t; return; }
      cur = t;
    } else if (final && pending && pending.convId === convId) {
      cur = pending;   // 前端工具执行完、这一轮说完了
      if (slug) cur.slug = slug;
    } else {
      return;          // 续问但没有这一轮开头的样子（网关重启过）：沿用之前的最近一轮
    }
    pending = null;
    cur.replyText = String(replyText || '');
    cur.spoken = [];
    cur.at = now();
    persist();
  }

  // 对方回来说话了：正醒着的这次停下
  function interrupt() {
    if (running) {
      log(`[WAKE] 对方回来了，醒来 #${running.runId} 停下`);
      try { running.ac.abort(); } catch (_) {}
    }
  }

  function state() {
    return {
      has_context: !!(cur && cur.at), conv_id: cur ? cur.convId : null, last_real_at: cur && cur.at ? utcStr(cur.at) : null,
      running: running ? running.runId : null, reason: lastReason
    };
  }

  // ---------- 醒来 ----------
  async function runTool(u, s, convId, used) {
    const name = u.name;
    if (!isServerTool(name)) return '醒来的时候用不了这个工具（它要在对方的手机上执行）。';
    const key = TOOL_ALLOW[name];
    if (key && !s[key]) return '这个工具这次醒来用不了（对方在设置里没打开）。';
    used.push(name);
    try {
      const out = await execTool(name, u.input || {}, { convId, wake: true });
      return out == null || out === '' ? '（无返回内容）' : String(out);
    } catch (e) {
      return `工具执行失败: ${e.message}`;
    }
  }

  async function pushAll(targets, run, title, preview) {
    const results = [];
    for (const t of targets || []) {
      const text = preview ? String(run.text || '').slice(0, PUSH_TEXT_MAX) : PUSH_FALLBACK;
      try {
        let resp;
        if (t.format === 'ntfy') {
          const url = t.url + (t.url.includes('?') ? '&' : '?') + 'title=' + encodeURIComponent(title || '新消息');
          resp = await post(url, text, { timeout: 8000, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
        } else {
          resp = await post(t.url, {
            type: 'pluvia.wake', id: run.id, conversation_id: run.conv_id, text: preview ? text : null,
            at: run.finished_at ? run.finished_at.replace(' ', 'T') + 'Z' : null
          }, { timeout: 8000, headers: { 'Content-Type': 'application/json' } });
        }
        results.push({ host: hostOf(t.url), format: t.format, ok: true, status: resp && resp.status });
      } catch (e) {
        results.push({ host: hostOf(t.url), format: t.format, ok: false, status: e.response ? e.response.status : null,
          error: String(e.message || '').slice(0, 120) });
      }
    }
    return results;
  }

  async function runWake(ctx, d, s) {
    const target = (ctx.target && ctx.target.apiKey) ? ctx.target : resolveTarget(ctx.targetInfo);
    const finish = (body) => axios.post(`${memoryUrl}/wake/finish`, { run_id: d.run_id, ...body }, { timeout: 8000 })
      .then(r => r.data || {}).catch(e => { log('[WAKE] 记结果失败：' + e.message); return {}; });
    if (!target) {
      await finish({ outcome: 'error', note: '找不到上一轮用的中转（Dashboard 上的配置改过？），等下一轮聊天' });
      return;
    }
    if (!ctx.target) ctx.target = target;
    const b = ctx.base;
    const convo = wakeMessages(b, ctx.replyText, ctx.spoken, d.note);
    const usage = { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 };
    const texts = [], thinks = [], used = [];
    const ac = new AbortController();
    running = { runId: d.run_id, ac, convId: ctx.convId };
    let outcome = null, note = null, rounds = 0, answeredBy = null;
    log(`[WAKE] 醒来 #${d.run_id}（${d.kind}）| ${ctx.convId} | ${target.name} / ${target.model}`);
    try {
      const max = Math.max(1, parseInt(s.max_rounds, 10) || 5);
      for (let round = 0; round < max; round++) {
        const result = await runRound({
          target, system: b.system, messages: convo, tools: b.tools, thinking: b.thinking, outputConfig: b.outputConfig,
          toolChoice: b.toolChoice, maxTokens: b.maxTokens, signal: ac.signal, providerPref: ctx.slug
        });
        rounds++;
        if (!answeredBy) answeredBy = providerOf(result.id);
        const u = result.usage || {};
        for (const k of Object.keys(usage)) usage[k] += u[k] || 0;
        const content = Array.isArray(result.content) ? result.content : [];
        const t = content.filter(c => c.type === 'text').map(c => c.text || '').join('').trim();
        if (t) texts.push(t);
        const th = content.filter(c => c.type === 'thinking').map(c => c.thinking || '').join('\n').trim();
        if (th) thinks.push(th);
        if (result.stop_reason !== 'tool_use') break;
        const uses = content.filter(c => c.type === 'tool_use');
        if (!uses.length) break;
        if (round === max - 1) { note = `工具调用到了上限（${max} 轮），停在这里`; break; }
        convo.push({ role: 'assistant', content });
        const results = [];
        for (const x of uses) results.push({ type: 'tool_result', tool_use_id: x.id, content: await runTool(x, s, ctx.convId, used) });
        convo.push({ role: 'user', content: results });
        if (ac.signal.aborted) throw Object.assign(new Error('aborted'), { name: 'CanceledError' });
      }
    } catch (e) {
      if (ac.signal.aborted) { outcome = 'interrupted'; note = '对方正好回来了，这次醒来停下（什么也没发）'; }
      else { outcome = 'error'; note = String((e.response && e.response.data && e.response.data.error && e.response.data.error.message) || e.message || e).slice(0, 300); }
    } finally {
      running = null;
    }
    if (rounds > 0) onTouch(ctx.convId);   // 读过一遍缓存：缓存心跳从现在重新算
    let split = { speak: false, text: '', aside: '' };
    if (!outcome) {
      split = splitReply(texts.join('\n\n'), !!s.speak);
      outcome = split.speak ? 'spoke' : 'silent';
    }
    const r = await finish({
      outcome, text: split.text || null, aside: split.aside || (outcome === 'interrupted' || outcome === 'error' ? texts.join('\n\n') : null) || null,
      thinking: thinks.join('\n\n') || null, tools: used, note,
      usage: { input: usage.input_tokens, cache_read: usage.cache_read_input_tokens, cache_write: usage.cache_creation_input_tokens,
        output: usage.output_tokens, rounds, model: target.model, ttl: ctx.ttl }
    });
    if (rounds > 0) {
      axios.post(`${memoryUrl}/cache/events`, {
        kind: 'wake', conv_id: ctx.convId, provider: providerName(answeredBy), relay: target.name, model: target.model, ttl: ctx.ttl,
        input: usage.input_tokens, cache_write: usage.cache_creation_input_tokens, cache_read: usage.cache_read_input_tokens,
        output: usage.output_tokens, note: `自动醒来 #${d.run_id}`
      }, { timeout: 3000 }).catch(() => {});
    }
    log(`[WAKE] 醒来 #${d.run_id}：${{ spoke: '说了话', silent: '没说话', error: '出错', interrupted: '停下了' }[outcome] || outcome}`
      + (used.length ? ` | 工具 ${used.join('、')}` : '') + ` | 读缓存 ${usage.cache_read_input_tokens} 写缓存 ${usage.cache_creation_input_tokens}`
      + (note ? ` | ${note}` : ''));
    if (outcome === 'spoke') {
      ctx.spoken.push(split.text);
      persist();
      if (r.run && r.push && r.push.length) {
        const res = await pushAll(r.push, r.run, r.push_title, r.push_preview !== false);
        log('[WAKE] 推送：' + res.map(x => `${x.host} ${x.ok ? 'ok' : '失败 ' + (x.status || x.error)}`).join('；'));
        axios.post(`${memoryUrl}/wake/push_result`, { run_id: d.run_id, result: { at: utcStr(now()), targets: res } }, { timeout: 5000 }).catch(() => {});
      }
    }
  }

  async function tick() {
    if (ticking) return;
    ticking = true;
    try {
      const s = settings();
      if (!s.enabled) { lastReason = '自动醒来关着'; return; }
      if (running || busy()) return;
      const ctx = cur && cur.at ? cur : null;
      const r = await axios.post(`${memoryUrl}/wake/check`, {
        conv_id: ctx ? ctx.convId : null, last_real_at: ctx ? utcStr(ctx.at) : null,
        tools: ctx ? toolNames(ctx.base.tools) : [], has_context: !!ctx
      }, { timeout: 5000 });
      const d = r.data || {};
      lastReason = d.reason || '';
      if (!d.wake || !d.run_id) return;
      if (!ctx || busy()) {
        await axios.post(`${memoryUrl}/wake/finish`, { run_id: d.run_id, outcome: 'interrupted', note: '正要醒的时候对方回来了' }, { timeout: 5000 }).catch(() => {});
        return;
      }
      await runWake(ctx, d, s);
    } catch (e) {
      log('[WAKE] 出错：' + e.message);
    } finally {
      ticking = false;
    }
  }

  function start() {
    load();
    refresh();
    if (!timer) {
      timer = setInterval(() => { tick(); }, tickMs);
      if (timer.unref) timer.unref();
    }
  }
  function stop() { if (timer) clearInterval(timer); timer = null; }

  return {
    noteTurn, interrupt, tick, start, stop, refresh, settings, enabled, toolEnabled, state,
    _cur: () => cur, _setSettings: (v) => { cached = { at: now(), values: { ...DEFAULTS, ...v } }; }, _load: load
  };
}

module.exports = { createWake, wakeMessages, splitReply, toolNames, TOOL_ALLOW, DEFAULTS };
