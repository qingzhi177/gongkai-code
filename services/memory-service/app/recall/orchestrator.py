"""召回编排：每一轮聊天调一次 compose_turn（方案 8.1、8.3 节）。

正式模式（live，2026-10-26 起的默认；Dashboard「聊天里放什么」→「用新的召回」）：算出这一轮想起什么，
返回卡片给网关放进这一轮的附注，投递账本记成 planned；网关在回复正常结束后调 confirm()，才记成 delivered、进入冷却。
影子模式（shadow）：照常算、写轨迹和账本（状态 shadow），但不返回卡片，冷却也只看影子自己的记录。
请求里带了 shadow 就按请求的来，没带就看设置。

每一轮还会：
- 认出日常报备（早安 + 称呼、下课啦、到教室了、亲亲……）当寒暄处理，不走相关度召回（intent.is_routine）；
- 检索用的文本去掉开头、结尾的称呼（intent.strip_address）；
- 话里提到某段时间（昨天、上周三……）时，那段时间的每日片段也一起精排，够相关的放进卡片的〈那天〉；
- 网关带上这一轮发给模型的消息条数（window_size）时，从这个对话里、还在窗口里的那些消息提取出来的记忆不再浮现：
  模型本来就看得到原话，再想起一遍只是重复。
"""
import asyncio
import functools
import json
import logging
import random
import re
import sqlite3
import time
import uuid
from datetime import timedelta

import timeutil
from app import settings as app_settings
from app.config import get_conn
from app.recall import cards, channels, decide as decide_mod, intent as intent_mod, timeparse
from app.recall import rerank as rerank_mod

RRF_K = 60
TOP_N = 30
NAMES_TTL = 60.0         # 称呼表（共同语汇里的昵称 + 事实里的称呼）缓存多久
EPISODES_N = 6
SHORT_QUERY = 6          # 少于 6 个字的消息，拼上上文再检索（「然后呢」的意思全在上下文里）
LEGACY_MAX_CHARS = 4000


_names_cache = {"t": -1e9, "v": []}


def address_names(fresh=False):
    """称呼：共同语汇里收下的昵称（含别名）+ 事实里「你叫我什么 / 我叫你什么」的值。至少两个字，长的在前"""
    now = time.monotonic()
    if not fresh and now - _names_cache["t"] < NAMES_TTL:
        return _names_cache["v"]
    names = set()
    conn = get_conn()
    try:
        try:
            for term, aliases in conn.execute("SELECT term, aliases FROM lexicon WHERE status='active' AND kind='nickname'"):
                names.add(str(term or ""))
                try:
                    names.update(str(a) for a in json.loads(aliases or "[]"))
                except ValueError:
                    pass
        except sqlite3.OperationalError:
            pass
        try:
            for (v,) in conn.execute("SELECT value FROM facts WHERE status='current' AND fact_key LIKE 'us.nickname.%'"):
                names.update(p.strip() for p in re.split(r"[、，,;；/|｜]+", v or ""))
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    v = sorted({n.strip() for n in names if len(n.strip()) >= 2}, key=len, reverse=True)
    _names_cache.update(t=now, v=v)
    return v


def live_enabled():
    try:
        return bool(app_settings.get("compose").get("recall_live", True))
    except Exception as e:
        print(f"[RECALL] 读取「用新的召回」设置失败，按开着算：{e}")
        return True


def episodes_enabled():
    try:
        return bool(app_settings.get("compose").get("episodes", True))
    except Exception:
        return True


def window_start(session_id, window_size):
    """这一轮发给模型的窗口从什么时候开始：这个对话在库里最近 window_size 条消息里最早的时间（UTC）。没有就返回 None"""
    try:
        n = int(window_size or 0)
    except (TypeError, ValueError):
        return None
    if not session_id or n <= 0:
        return None
    conn = get_conn()
    try:
        r = conn.execute("SELECT MIN(ts) FROM (SELECT ts FROM l0_messages WHERE conv_id=? AND COALESCE(status, 'active')='active' "
                         "ORDER BY msg_idx DESC LIMIT ?)", (session_id, n)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return timeutil.to_utc_str(r[0]) if r and r[0] else None


def build_query(user_text, prev_user="", prev_assistant=""):
    core = intent_mod.core_text(user_text)
    if len(core) >= SHORT_QUERY or not (prev_user or prev_assistant):
        return user_text
    parts = [p for p in (str(prev_user or "")[:200], str(prev_assistant or "")[:100], user_text) if p]
    return " ".join(parts)


def fuse(vec, fts, intent_type, timed=()):
    """RRF 融合（只看名次）。回忆 / 事实意图时关键词、时间通道权重更高。返回 [(id, rrf)]"""
    strong = intent_type in ("recall", "fact")
    w_vec, w_fts, w_time = 1.0, (1.0 if strong else 0.6), (1.0 if strong else 0.8)
    scores = {}
    for rank, (mid, _) in enumerate(vec):
        scores[mid] = scores.get(mid, 0.0) + w_vec / (RRF_K + rank + 1)
    for rank, (mid, sc) in enumerate(fts):
        if sc > 0:
            scores[mid] = scores.get(mid, 0.0) + w_fts / (RRF_K + rank + 1)
    for rank, (mid, _) in enumerate(timed):
        scores[mid] = scores.get(mid, 0.0) + w_time / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])[:TOP_N]


def doc_text(m):
    text = (m.get("content") or "").strip()
    if m.get("quote"):
        text += f"（原话：{m['quote']}）"
    return text[:600]


def ledger_statuses(mode):
    return ("shadow",) if mode == "shadow" else ("delivered",)


def load_cooldown(conn, backend, session_id, mode, now=None, params=None):
    now = now or timeutil.utc_now()
    p = decide_mod.params_from(params)
    st = ledger_statuses(mode)
    qs = ",".join("?" * len(st))
    trace_ids = [r[0] for r in conn.execute(
        "SELECT id FROM recall_traces WHERE backend=? AND session_id=? AND mode=? ORDER BY id DESC LIMIT ?",
        (backend, session_id, mode, int(p["recent_turns"]))).fetchall()]
    recent, recent_eps = set(), set()
    if trace_ids:
        for typ, iid in conn.execute(
                f"SELECT item_type, item_id FROM injections WHERE status IN ({qs}) AND trace_id IN ({','.join('?' * len(trace_ids))})",
                (*st, *trace_ids)).fetchall():
            if typ == "episode":
                recent_eps.add(str(iid))
            elif str(iid).isdigit():
                recent.add(int(iid))

    def since(hours):
        t = (now - timedelta(hours=hours)).strftime(timeutil.STORE_FMT)
        return {int(r[0]) for r in conn.execute(
            f"SELECT item_id FROM injections WHERE status IN ({qs}) AND item_type='moment' AND created_at >= ?",
            (*st, t)).fetchall()}

    return {"recent": recent, "recent_episodes": recent_eps, "medium": since(p["medium_cooldown_h"]),
            "serendipity": since(p["serendipity_cooldown_d"] * 24)}


def load_params():
    """这一轮用的参数（Dashboard 上调过的优先，读不到就全用默认值）"""
    try:
        return app_settings.get("recall")
    except Exception as e:
        print(f"[RECALL] 读取召回参数失败，用默认值：{e}")
        return {}


async def warm_up(delay=3.0):
    """启动预热（记忆服务启动后在后台跑一次）：jieba 词典和 FTS 对账、嵌入和精排的 HTTPS 连接。
    这样重启后的第一轮召回不用再多等这 1–3 秒。出错只打印，不影响启动。返回各步耗时（毫秒）或失败原因。"""
    await asyncio.sleep(delay)
    out = {}

    async def step(name, coro):
        t = time.monotonic()
        try:
            await coro
            out[name] = int((time.monotonic() - t) * 1000)
        except Exception as e:
            out[name] = f"失败：{type(e).__name__}"

    settings = load_params()
    await step("fts", channels.fts_search_async("预热", n=1))
    await step("embed", channels.embed("你好"))
    if settings.get("rerank_enabled", True):
        await step("rerank", rerank_mod.rerank("你好", ["你好呀"], model=settings.get("rerank_model"), timeout=5))
    logging.getLogger(__name__).info(f"[RECALL] 启动预热：{out}（毫秒）")
    return out


async def compose_turn(req, collection, embed_fn=None, rerank_fn=None, fts_fn=None, rng=None, params=None):
    settings = load_params() if params is None else params
    p = decide_mod.params_from(settings)
    embed_fn = embed_fn or channels.embed
    rerank_fn = rerank_fn or functools.partial(rerank_mod.rerank, model=settings.get("rerank_model"),
                                               timeout=settings.get("rerank_timeout"),
                                               enabled=settings.get("rerank_enabled"))
    fts_fn = fts_fn or channels.fts_search_async
    rng = rng or random.Random()
    t0 = time.monotonic()
    backend = req.get("backend") or "api"
    session_id = str(req.get("session_id") or req.get("conv_id") or "")
    shadow = req.get("shadow")
    if shadow is None:
        shadow = not live_enabled()
    mode = "shadow" if shadow else "live"
    turn_key = req.get("turn_key") or f"{session_id}:{uuid.uuid4().hex[:12]}"
    user_text = str(req.get("user_text") or "").strip()
    names = req["names"] if req.get("names") is not None else address_names()
    intent = intent_mod.classify(user_text, names)
    query = build_query(intent_mod.strip_address(user_text, names), req.get("prev_user") or "", req.get("prev_assistant") or "")
    lat = {}
    chan = {"vector": [], "fts": []}
    cands = []
    eps = []
    skip = "日常报备（早安、下课了、到教室了、亲亲……）" if intent.get("routine") else "寒暄或空消息"
    rr_info = {"skipped": skip} if (intent["type"] == "greeting" or not user_text) else None

    if rr_info is None:
        async def timed(coro, key):
            t = time.monotonic()
            r = await coro
            lat[key] = int((time.monotonic() - t) * 1000)
            return r
        emb, fts = await asyncio.gather(timed(embed_fn(query), "embed"), timed(fts_fn(query), "fts"))
        vec = channels.vector_search(collection, emb, n=TOP_N)
        chan = {"vector": vec[:TOP_N], "fts": fts[:TOP_N], "embed_ok": emb is not None}
        # 时间通道：只看这一轮用户自己的话（拼上上文的检索文本里可能带着别的时间）
        ranges = timeparse.parse(user_text) if settings.get("time_enabled", True) else []
        hits = channels.time_search(ranges, n=TOP_N) if ranges else []
        if ranges:
            chan["time"] = {"ranges": [{k: r[k] for k in ("label", "from", "to")} for r in ranges],
                            "hits": [mid for mid, _ in hits]}
        if ranges and intent["type"] != "task" and episodes_enabled():
            eps = channels.episode_search(ranges, n=EPISODES_N)
            if eps:
                chan["time"]["episodes"] = [e["id"] for e in eps]
        fused = fuse(vec, fts, intent["type"], hits)
        meta = channels.load_meta([mid for mid, _ in fused])
        vec_sim = dict(vec)
        win_from = window_start(session_id, req.get("window_size"))
        in_window = []
        for mid, rrf in fused:
            if mid in meta:
                c = {**meta[mid], "rrf": round(rrf, 5), "vec_sim": vec_sim.get(mid)}
                if win_from and c.get("conv_id") == session_id and (timeutil.to_utc_str(c.get("ts")) or "") >= win_from:
                    in_window.append(mid)          # 原话还在这一轮的窗口里，不用再想起
                    continue
                if ranges and timeparse.contains(ranges, c.get("ts")):
                    c["in_time"] = True
                cands.append(c)
        if win_from:
            chan["window"] = {"from": win_from, "size": req.get("window_size"), "skipped": in_window}
        t2 = time.monotonic()
        docs = [doc_text(c) for c in cands] + [f"{e['title']}：{e['body']}"[:600] for e in eps]
        scores, rr_info = await rerank_fn(query, docs)
        lat["rerank"] = int((time.monotonic() - t2) * 1000)
        for i, c in enumerate(cands):
            c["rerank"] = scores[i] if scores else None
            c["score"] = scores[i] if scores else (c["vec_sim"] or 0.0)
        for j, e in enumerate(eps):
            e["rerank"] = scores[len(cands) + j] if scores and len(scores) > len(cands) + j else None
        cands.sort(key=lambda c: -c["score"])

    conn = get_conn()
    try:
        cooldown = load_cooldown(conn, backend, session_id, mode, params=p)
        pool = channels.serendipity_pool()
        rerank_ok = bool(rr_info and rr_info.get("ok"))
        d = decide_mod.decide(cands, intent["type"], cooldown, rerank_ok, serendipity_pool=pool, rng=rng, params=p)
        ep_picks = pick_episodes(eps, intent["type"], rerank_ok, cooldown.get("recent_episodes", set()), p)
        card = cards.format_card(d["picks"], intent["type"], episodes=ep_picks)
        lat["total"] = int((time.monotonic() - t0) * 1000)
        now = timeutil.utc_now_str()
        legacy = req.get("legacy")
        legacy_json = json.dumps(legacy, ensure_ascii=False)[:LEGACY_MAX_CHARS] if legacy is not None else None
        picks_brief = [{"id": p["id"], "tier": p["tier"], "score": p.get("score")} for p in d["picks"]]
        decision = {**{k: d[k] for k in ("eligible", "dropped", "gate", "basis")}, "picks": picks_brief, "params": p,
                    "candidates": [{"id": c["id"], "rerank": c.get("rerank"), "vec_sim": c.get("vec_sim"),
                                    "rrf": c.get("rrf"), **({"in_time": True} if c.get("in_time") else {})}
                                   for c in cands[:15]]}
        if eps:
            decision["episodes"] = [{"id": e["id"], "day": e["day"], "rerank": e.get("rerank")} for e in eps]
            decision["episode_picks"] = [e["id"] for e in ep_picks]
        cur = conn.execute(
            "INSERT INTO recall_traces (turn_key, backend, session_id, client, mode, query, user_text, intent, channels, "
            "rerank, decision, card, legacy, injected_chars, latency, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (turn_key, backend, session_id, req.get("client") or "", mode, query[:1000], user_text[:500],
             json.dumps(intent, ensure_ascii=False), json.dumps(chan, ensure_ascii=False),
             json.dumps(rr_info, ensure_ascii=False), json.dumps(decision, ensure_ascii=False), card, legacy_json,
             len(card), json.dumps(lat), now))
        trace_id = cur.lastrowid
        status = "shadow" if mode == "shadow" else "planned"
        for p in d["picks"]:
            conn.execute(
                "INSERT INTO injections (turn_key, trace_id, backend, session_id, item_type, item_id, tier, channel, "
                "status, created_at) VALUES (?, ?, ?, ?, 'moment', ?, ?, ?, ?, ?)",
                (turn_key, trace_id, backend, session_id, str(p["id"]), p["tier"],
                 "serendipity" if p["tier"] == "serendipity" else ("rerank" if d["basis"] == "rerank" else "vec_sim"),
                 status, now))
        for e in ep_picks:
            conn.execute(
                "INSERT INTO injections (turn_key, trace_id, backend, session_id, item_type, item_id, tier, channel, "
                "status, created_at) VALUES (?, ?, ?, ?, 'episode', ?, 'time', 'time', ?, ?)",
                (turn_key, trace_id, backend, session_id, str(e["id"]), status, now))
        conn.commit()
    finally:
        conn.close()
    return {"turn_key": turn_key, "trace_id": trace_id, "mode": mode, "intent": intent["type"],
            "card": card if mode == "live" else "", "shadow_card": card if mode == "shadow" else "",
            "picks": picks_brief, "episodes": [e["id"] for e in ep_picks], "latency_ms": lat}


def pick_episodes(eps, intent_type, rerank_ok, recent, p):
    """话里提到的那段时间的片段：回忆模式最多 2 段（分数 ≥ 回忆门槛），平时最多 1 段（分数 + 时间加分 ≥ 强门槛）。
    精排不可用时：回忆模式取最重要的那一段，平时不放。同一会话最近几轮放过的不再放。"""
    if not eps or intent_type in ("greeting", "task"):
        return []
    pool = [e for e in eps if str(e["id"]) not in recent]
    if not rerank_ok:
        return pool[:1] if intent_type == "recall" else []
    if intent_type == "recall":
        bar, limit, bonus = p["t_recall"], 2, 0.0
    else:
        bar, limit, bonus = p["t_high"], 1, p["time_bonus"]
    ok = [e for e in pool if e.get("rerank") is not None and e["rerank"] + bonus >= bar]
    return sorted(ok, key=lambda e: -e["rerank"])[:limit]


def confirm(turn_key):
    """这一轮的回复正常结束了：planned → delivered，记下投递时间；想起的瞬间计入展示次数和最后展示时间（冷却）。
    返回这次确认了几条。同一轮确认两次不会重复计数。"""
    if not turn_key:
        return 0
    now = timeutil.utc_now_str()
    ms = int(time.time() * 1000)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id, item_type, item_id FROM injections WHERE turn_key=? AND status='planned'",
                            (str(turn_key),)).fetchall()
        for iid, typ, item in rows:
            conn.execute("UPDATE injections SET status='delivered', delivered_at=? WHERE id=?", (now, iid))
            if typ == "moment" and str(item).isdigit():
                conn.execute("UPDATE l1_memories SET display_count = COALESCE(display_count, 0) + 1, last_shown_at = ? WHERE id = ?",
                             (ms, int(item)))
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
