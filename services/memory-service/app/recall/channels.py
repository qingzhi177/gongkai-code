"""检索通道（方案 8.2 节）。现在有：稠密向量、稀疏关键词（FTS5 + jieba）、时间（话里提到的日期范围，见 timeparse.py）。
字面（L0 原文）、共同语汇、结构化通道等对应的数据层（Phase 4）做出来以后再加。
"""
import asyncio
import os
from collections import OrderedDict

import httpx

import text_index
from app.config import SQLITE_PATH, get_conn

ALIBABA_API_KEY = os.getenv("ALIBABA_API_KEY")
EMBED_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
EMBED_MODEL = "text-embedding-v3"          # 和入库时用的模型一致（vector_store.py）
EMBED_CACHE_MAX = 256

_embed_cache = OrderedDict()

_clients = {}


def _client():
    """同一个事件循环里复用一个 AsyncClient（保持连接，省掉每次的 TLS 握手）"""
    loop = asyncio.get_running_loop()
    c = _clients.get(id(loop))
    if c is None or c.is_closed:
        c = httpx.AsyncClient()
        _clients.clear()
        _clients[id(loop)] = c
    return c


async def embed(text, timeout=4.0):
    """查询嵌入，相同文本走缓存（网关的旧逻辑和这里会对同一句话各算一次）。失败返回 None。"""
    key = str(text or "")
    if not key or not ALIBABA_API_KEY:
        return None
    if key in _embed_cache:
        _embed_cache.move_to_end(key)
        return _embed_cache[key]
    try:
        r = await _client().post(EMBED_URL, headers={"Authorization": f"Bearer {ALIBABA_API_KEY}"},
                                 json={"model": EMBED_MODEL, "input": key, "encoding_format": "float"}, timeout=timeout)
        r.raise_for_status()
        vec = r.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"[RECALL] 查询嵌入失败: {type(e).__name__}: {e}")
        return None
    _embed_cache[key] = vec
    if len(_embed_cache) > EMBED_CACHE_MAX:
        _embed_cache.popitem(last=False)
    return vec


def vector_search(collection, embedding, n=30):
    """返回 [(l1_id, 余弦相似度)]，相似度高的在前"""
    if collection is None or embedding is None:
        return []
    res = collection.query(query_embeddings=[embedding], n_results=n)
    out = []
    for mid, sim in zip(res["ids"][0] if res["ids"] else [], res["similarities"][0] if res["similarities"] else []):
        s = str(mid)
        if s.startswith("l1_") and s[3:].isdigit():
            out.append((int(s[3:]), round(float(sim), 4)))
    return out


def time_search(ranges, n=30):
    """时间通道：发生在这些日期范围里的瞬间。返回 [(l1_id, 1.0)]，核心的、情绪浓的排前面"""
    if not ranges:
        return []
    conn = get_conn()
    try:
        out, seen = [], set()
        for r in ranges:
            rows = conn.execute(
                "SELECT id FROM l1_memories WHERE status='active' AND COALESCE(visibility, 1.0) > 0 "
                "AND datetime(ts) >= ? AND datetime(ts) < ? "
                "ORDER BY COALESCE(is_core, 0) DESC, COALESCE(arousal, 0) DESC, id DESC LIMIT ?",
                (r["start"], r["end"], n)).fetchall()
            for (i,) in rows:
                if i not in seen:
                    seen.add(i)
                    out.append((int(i), 1.0))
        return out[:n]
    finally:
        conn.close()


def episode_search(ranges, n=6):
    """时间通道里的每日片段：开始时间落在这些范围里的，重要的排前面。返回 [{id, day, start_ts, title, body}]"""
    if not ranges:
        return []
    conn = get_conn()
    try:
        out, seen = [], set()
        for r in ranges:
            try:
                rows = conn.execute(
                    "SELECT id, day, start_ts, title, body FROM episodes WHERE status='active' "
                    "AND datetime(start_ts) >= ? AND datetime(start_ts) < ? "
                    "ORDER BY COALESCE(salience, 0) DESC, start_ts LIMIT ?", (r["start"], r["end"], n)).fetchall()
            except Exception:            # 还没有片段表（迁移前）
                return []
            for eid, day, ts, title, body in rows:
                if eid not in seen:
                    seen.add(eid)
                    out.append({"id": int(eid), "day": day, "start_ts": ts, "title": title or "", "body": body or ""})
        return out[:n]
    finally:
        conn.close()


def fts_search(query, n=30):
    """返回 [(l1_id, bm25 分数)]，分数高的在前"""
    try:
        text_index.reconcile(SQLITE_PATH)
    except Exception as e:
        print(f"[RECALL] FTS 对账失败: {e}")
    return [(lid, round(float(sc), 4)) for lid, sc in text_index.search(SQLITE_PATH, query, limit=n)]


async def fts_search_async(query, n=30):
    return await asyncio.to_thread(fts_search, query, n)


META_COLS = ("id", "content", "quote", "ts", "event_type", "tags", "is_core", "arousal", "valence",
             "visibility", "author", "evidence_status", "conv_id")


def load_meta(ids):
    """取候选的元数据（只要 active 且没被软隐藏的）。返回 {id: dict}"""
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT {', '.join(META_COLS)} FROM l1_memories WHERE status='active' "
            f"AND COALESCE(visibility, 1.0) > 0 AND id IN ({','.join('?' * len(ids))})", ids).fetchall()
    finally:
        conn.close()
    return {r[0]: dict(zip(META_COLS, r)) for r in rows}


def serendipity_pool(limit=30):
    """无由来浮现的候选：情绪浓（arousal ≥ 0.6）或核心记忆"""
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT {', '.join(META_COLS)} FROM l1_memories WHERE status='active' AND COALESCE(visibility, 1.0) > 0 "
            "AND (is_core=1 OR arousal >= 0.6) ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [dict(zip(META_COLS, r)) for r in rows]
