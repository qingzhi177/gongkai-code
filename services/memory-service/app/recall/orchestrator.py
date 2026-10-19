"""召回编排：每一轮聊天调一次 compose_turn（方案 8.1、8.3 节）。

影子模式（shadow=True，现在的默认）：照常算出「这一轮会想起什么」，写进轨迹和投递账本（状态 shadow），
但不返回给网关注入，冷却也只看影子自己的记录，和网关旧逻辑互不干扰。
"""
import asyncio
import functools
import json
import random
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
SHORT_QUERY = 6          # 少于 6 个字的消息，拼上上文再检索（「然后呢」的意思全在上下文里）
LEGACY_MAX_CHARS = 4000


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
    recent = set()
    if trace_ids:
        recent = {int(r[0]) for r in conn.execute(
            f"SELECT item_id FROM injections WHERE status IN ({qs}) AND trace_id IN ({','.join('?' * len(trace_ids))})",
            (*st, *trace_ids)).fetchall()}

    def since(hours):
        t = (now - timedelta(hours=hours)).strftime(timeutil.STORE_FMT)
        return {int(r[0]) for r in conn.execute(
            f"SELECT item_id FROM injections WHERE status IN ({qs}) AND item_type='moment' AND created_at >= ?",
            (*st, t)).fetchall()}

    return {"recent": recent, "medium": since(p["medium_cooldown_h"]),
            "serendipity": since(p["serendipity_cooldown_d"] * 24)}


def load_params():
    """这一轮用的参数（Dashboard 上调过的优先，读不到就全用默认值）"""
    try:
        return app_settings.get("recall")
    except Exception as e:
        print(f"[RECALL] 读取召回参数失败，用默认值：{e}")
        return {}


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
    mode = "shadow" if req.get("shadow", True) else "live"
    turn_key = req.get("turn_key") or f"{session_id}:{uuid.uuid4().hex[:12]}"
    user_text = str(req.get("user_text") or "").strip()
    intent = intent_mod.classify(user_text)
    query = build_query(user_text, req.get("prev_user") or "", req.get("prev_assistant") or "")
    lat = {}
    chan = {"vector": [], "fts": []}
    cands = []
    rr_info = {"skipped": "寒暄或空消息"} if (intent["type"] == "greeting" or not user_text) else None

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
        fused = fuse(vec, fts, intent["type"], hits)
        meta = channels.load_meta([mid for mid, _ in fused])
        vec_sim = dict(vec)
        for mid, rrf in fused:
            if mid in meta:
                c = {**meta[mid], "rrf": round(rrf, 5), "vec_sim": vec_sim.get(mid)}
                if ranges and timeparse.contains(ranges, c.get("ts")):
                    c["in_time"] = True
                cands.append(c)
        t2 = time.monotonic()
        scores, rr_info = await rerank_fn(query, [doc_text(c) for c in cands])
        lat["rerank"] = int((time.monotonic() - t2) * 1000)
        for i, c in enumerate(cands):
            c["rerank"] = scores[i] if scores else None
            c["score"] = scores[i] if scores else (c["vec_sim"] or 0.0)
        cands.sort(key=lambda c: -c["score"])

    conn = get_conn()
    try:
        cooldown = load_cooldown(conn, backend, session_id, mode, params=p)
        pool = channels.serendipity_pool()
        d = decide_mod.decide(cands, intent["type"], cooldown, bool(rr_info and rr_info.get("ok")),
                              serendipity_pool=pool, rng=rng, params=p)
        card = cards.format_card(d["picks"], intent["type"])
        lat["total"] = int((time.monotonic() - t0) * 1000)
        now = timeutil.utc_now_str()
        legacy = req.get("legacy")
        legacy_json = json.dumps(legacy, ensure_ascii=False)[:LEGACY_MAX_CHARS] if legacy is not None else None
        picks_brief = [{"id": p["id"], "tier": p["tier"], "score": p.get("score")} for p in d["picks"]]
        decision = {**{k: d[k] for k in ("eligible", "dropped", "gate", "basis")}, "picks": picks_brief, "params": p,
                    "candidates": [{"id": c["id"], "rerank": c.get("rerank"), "vec_sim": c.get("vec_sim"),
                                    "rrf": c.get("rrf"), **({"in_time": True} if c.get("in_time") else {})}
                                   for c in cands[:15]]}
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
        conn.commit()
    finally:
        conn.close()
    return {"turn_key": turn_key, "trace_id": trace_id, "mode": mode, "intent": intent["type"],
            "card": card if mode == "live" else "", "shadow_card": card if mode == "shadow" else "",
            "picks": picks_brief, "latency_ms": lat}
