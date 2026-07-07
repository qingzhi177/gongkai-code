from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
import chromadb
import sqlite3
import os
from dotenv import load_dotenv
import httpx
from datetime import datetime
from pathlib import Path
import jieba
from rank_bm25 import BM25Okapi

load_dotenv()

app = FastAPI(title="Memory Service")

CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", 8000))
ALIBABA_API_KEY = os.getenv("ALIBABA_API_KEY")
ALIBABA_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DATA_DIR = Path(os.getenv("DATA_DIR", "/home/qingzhi/memory-system/data"))
SQLITE_PATH = DATA_DIR / "sqlite" / "memory.db"
SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)

chroma_client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
try:
    l1_collection = chroma_client.get_or_create_collection(name="l1_memories")
except Exception as e:
    print(f"ChromaDB error: {e}")

def init_db():
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS l0_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id TEXT NOT NULL,
        msg_idx INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        source TEXT DEFAULT 'live',
        client TEXT DEFAULT 'unknown',
        status TEXT DEFAULT 'active',
        extracted INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS l1_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        quote TEXT,
        source_msg_id INTEGER,
        conv_id TEXT,
        client TEXT,
        event_type TEXT,
        tags TEXT,
        valence REAL,
        arousal REAL,
        is_core INTEGER DEFAULT 0,
        access_count INTEGER DEFAULT 0,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'active'
    )''')
    conn.commit()
    conn.close()

init_db()

class SearchRequest(BaseModel):
    query: str
    mode: str = "semantic"
    event_type: Optional[str] = None
    after: Optional[str] = None
    before: Optional[str] = None
    n: int = 10

class SaveRequest(BaseModel):
    conv_id: str
    client: str = "unknown"
    messages: List[dict]

async def get_embedding(text):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ALIBABA_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {ALIBABA_API_KEY}", "Content-Type": "application/json"},
                json={"model": "text-embedding-v3", "input": text, "encoding_format": "float"}
            )
            response.raise_for_status()
            return response.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"Embedding error: {e}")
        return None

def get_l0_context(sqlite_path, l1_id_str):
    """根据 L1 ID 查找对应的 L0 上下文"""
    try:
        conn = sqlite3.connect(str(sqlite_path))
        c = conn.cursor()
        # 先找这条 L1 对应的 conv_id
        c.execute('SELECT source_msg_id, conv_id FROM l1_memories WHERE id=?', (l1_id_str,))
        row = c.fetchone()
        if not row:
            conn.close()
            return ""
        source_id, conv_id = row
        if not conv_id:
            conn.close()
            return ""
        # 用 conv_id 查找上下文（source_msg_id 前后各2条）
        if source_id:
            c.execute(
                'SELECT role, content FROM l0_messages WHERE conv_id=? AND status=? AND msg_idx BETWEEN ? AND ? ORDER BY msg_idx ASC',
                (conv_id, 'active', max(0, source_id - 3), source_id + 1)
            )
        else:
            # 没有 source_msg_id，取对话的前5条
            c.execute(
                'SELECT role, content FROM l0_messages WHERE conv_id=? AND status=? ORDER BY msg_idx ASC LIMIT5',
                (conv_id, 'active')
            )
        rows = c.fetchall()
        conn.close()
        if not rows:
            return ""
        parts = []
        for role, content in rows:
            prefix = "你" if role == "user" else "我"
            parts.append(prefix + ": " + content[:200])
        return "\n".join(parts)
    except Exception as e:
        print(f"L0 context error: {e}")
        return ""

def detect_intent(query: str) -> dict:
    """轻量规则版意图判断"""
    query_lower = query.lower()
    
    # 时间词
    time_keywords = ["上次", "之前", "最近", "昨天", "前天", "刚才", "以前", "那时", "当时", "今天", "明天"]
    # 提问词
    question_keywords = ["什么", "哪", "怎么", "为什么", "几号", "多少", "是不是", "？", "?"]
    # 情感词
    emotion_keywords = ["心情", "开心", "难过", "累", "伤心", "想念", "舍不得", "感觉", "觉得"]
    # 总结词
    summary_keywords = ["总结", "回顾", "梳理", "整理", "汇总"]
    
    intent = {
        "type": "semantic",  # 默认语义检索
        "time_weight": 0.0,
        "keyword_weight": 0.3,  # 默认 BM25 权重
        "emotion_filter": False
    }
    
    # 检测时间类
    if any(kw in query_lower for kw in time_keywords):
        intent["type"] = "temporal"
        intent["time_weight"] = 0.3  # 增加时间权重
    
    # 检测提问类（事实查询）
    if any(kw in query_lower for kw in question_keywords):
        intent["type"] = "fact"
        intent["keyword_weight"] = 0.5  # 提高关键词权重
    
    # 检测情感类
    if any(kw in query_lower for kw in emotion_keywords):
        intent["type"] = "emotional"
        intent["emotion_filter"] = True
    
    # 检测总结类
    if any(kw in query_lower for kw in summary_keywords):
        intent["type"] = "summary"
        intent["time_weight"] = 0.5
    
    return intent

@app.post("/search")
async def search_memories(req: SearchRequest):
    """检索记忆：向量 + BM25 RF 融合"""
    try:
        if req.mode == "exact":
            # FTS5 逐字检索
            conn = sqlite3.connect(str(SQLITE_PATH))
            c = conn.cursor()
            c.execute(
                "SELECT id, content, quote, event_type, tags, ts, client, valence, arousal FROM l1_memories WHERE status='active' AND content LIKE ?",
                (f"%{req.query}%",)
            )
            rows = c.fetchall()
            conn.close()
            memories = []
            for row in rows:
                memories.append({
                    "id": f"l1_{row[0]}",
                    "l1_summary": row[1],
                    "l0_context": get_l0_context(SQLITE_PATH, str(row[0])),
                    "quote": row[2] or "",
                    "metadata": {
                        "event_type": row[3] or "",
                        "tags": row[4] or "",
                        "ts": row[5] or "",
                        "client": row[6] or "",
                        "valence": row[7] or 0,
                        "arousal": row[8] or 0,
                        "is_core": 0
                    },
                    "score": 1.0
                })
           
        # 意图判断
        intent = detect_intent(req.query)
        print(f"Intent detected: {intent}")

        # semantic和 emotion 模式：向量 + BM25 融合
        query_embedding = await get_embedding(req.query)
        if not query_embedding:
            return {"memories": [], "total": 0}

        # 1. 向量检索
        vec_results = l1_collection.query(
            query_embeddings=[query_embedding],
            n_results=min(req.n * 3, 30),
            include=["documents", "metadatas", "distances"]
        )

        # 2. BM25 关键词检索
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()
        c.execute("SELECT id, content FROM l1_memories WHERE status='active'")
        all_memories = c.fetchall()
        conn.close()

        bm25_scores = {}
        if all_memories:
            corpus = [list(jieba.cut(m[1])) for m in all_memories]
            bm25 = BM25Okapi(corpus)
            query_tokens = list(jieba.cut(req.query))
            scores = bm25.get_scores(query_tokens)
            for i, m in enumerate(all_memories):
                bm25_scores[f"l1_{m[0]}"] = scores[i]

        # 3. RF 融合
        K = 60  # RF 常数
        rrf_scores = {}

        # 向量排名分
        if vec_results["ids"] and len(vec_results["ids"][0]) > 0:
            for rank, mid in enumerate(vec_results["ids"][0]):
                # 向量权重（默认 0.7，根据意图调整）
                vec_weight = 0.7 if intent["type"] == "semantic" else 0.5
                rrf_scores[mid] = rrf_scores.get(mid, 0) + vec_weight * (1.0 / (K + rank + 1))

        # BM25 排名分
        bm25_sorted = sorted(bm25_scores.items(), key=lambda x: -x[1])
        for rank, (mid, score) in enumerate(bm25_sorted[:30]):
            if score > 0:
                # 关键词权重（根据意图动态调整）
                keyword_weight = intent["keyword_weight"]
                rrf_scores[mid] = rrf_scores.get(mid, 0) + keyword_weight * (1.0 / (K + rank + 1))

        # 4. 排序并构建结果
        sorted_ids = sorted(rrf_scores.items(), key=lambda x: -x[1])

        memories = []
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()

        for mid, rrf_score in sorted_ids[:req.n]:

            # 获取 L1 内容和元数据
            l1_id_str = mid.replace("l1_", "")

            # 从 ChromaDB 获取元数据
            try:
                chroma_result = l1_collection.get(ids=[mid], include=["documents", "metadatas"])
                if not chroma_result["ids"]:
                    continue
                doc = chroma_result["documents"][0]
                meta = chroma_result["metadatas"][0]
            except Exception:
                continue

            # 过滤条件
            if req.event_type and meta.get("event_type") != req.event_type:
                continue
            if req.after and meta.get("ts", "") < req.after:
                continue
            if req.before and meta.get("ts", "") > req.before:
                continue
            if req.mode == "emotion" and req.emotion_valence:
                v = meta.get("valence", 0)
                if req.emotion_valence == "positive" and v <= 0:
                    continue
                if req.emotion_valence == "negative" and v >= 0:
                    continue

            # 情感过滤
            if intent["emotion_filter"]:
                arousal = meta.get("arousal", 0)
                if arousal < 0.3:  # 只保留情感强度较高的
                    continue

            # 获取 L0 上下文
            l0_context = get_l0_context(SQLITE_PATH, l1_id_str)

            memories.append({
                "id": mid,
                "l1_summary": doc,
                "l0_context": l0_context,
                "quote": meta.get("quote", ""),
                "metadata": meta,
                "score": rrf_score
            })

        conn.close()

        # 核心记忆置顶 + RRF 分数 + 时间权重
        if intent["time_weight"] > 0:
            # 计算时间加成
            for m in memories:
                ts = m["metadata"].get("ts", "")
                if ts:
                    from datetime import datetime
                    try:
                        days_ago = (datetime.now() - datetime.fromisoformat(ts)).days
                        time_boost = max(0, 1 - days_ago / 365) * intent["time_weight"]
                        m["score"] = m["score"] * (1 + time_boost)
                    except:
                        pass
        
        memories.sort(key=lambda m: (-m["metadata"].get("is_core", 0), -m["score"]))

        # 更新 access_count
        try:
            conn_ac = sqlite3.connect(str(SQLITE_PATH))
            c_ac = conn_ac.cursor()
            for m in memories[:req.n]:
                lid = m["id"].replace("l1_", "")
                c_ac.execute('UPDATE l1_memories SET access_count = access_count + 1 WHERE id = ?', (lid,))
            conn_ac.commit()
            conn_ac.close()
        except Exception:
            pass

        return {"memories": memories[:req.n], "total": len(memories)}

    except Exception as e:
        print(f"Search error: {e}")
        return {"memories": [], "total": 0}


@app.post("/save_conversation")
async def save_conversation(req: SaveRequest):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    saved = 0
    try:
        for idx, msg in enumerate(req.messages):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content or role == "system":
                continue
            c.execute('SELECT id, content FROM l0_messages WHERE conv_id=? AND msg_idx=? AND status=?',
                      (req.conv_id, idx, 'active'))
            existing = c.fetchone()
            if existing:
                if existing[1] == content:
                    continue
                c.execute('UPDATE l0_messages SET status=? WHERE id=?', ('superseded', existing[0]))
            c.execute('INSERT INTO l0_messages (conv_id, msg_idx, role, content, client, status, extracted) VALUES (?,?,?,?,?,?,?)',
                      (req.conv_id, idx, role, content, req.client, 'active', 0))
            saved += 1
        conn.commit()
        return {"status": "ok", "saved": saved}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()

@app.get("/stats")
async def stats():
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM l0_messages WHERE status=?', ('active',))
    l0 = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM l1_memories WHERE status=?', ('active',))
    l1 = c.fetchone()[0]
    conn.close()
    return {"l0_messages": l0, "l1_memories": l1}

@app.get("/stats/detail")
async def stats_detail():
    """详细统计"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    # 按类型统计
    c.execute('SELECT event_type, COUNT(*) FROM l1_memories WHERE status=? GROUP BY event_type', ('active',))
    type_stats = {row[0]: row[1] for row in c.fetchall()}
    # 按来源统计
    c.execute('SELECT client, COUNT(*) FROM l0_messages WHERE status=? GROUP BY client', ('active',))
    client_stats = {row[0] or 'unknown': row[1] for row in c.fetchall()}
    # 核心记忆数
    c.execute('SELECT COUNT(*) FROM l1_memories WHERE status=? AND is_core=1', ('active',))
    core_count = c.fetchone()[0]
    # 对话数
    c.execute('SELECT COUNT(DISTINCT conv_id) FROM l0_messages WHERE status=?', ('active',))
    conv_count = c.fetchone()[0]
    conn.close()
    return {
        "type_stats": type_stats,
        "client_stats": client_stats,
        "core_count": core_count,
        "conv_count": conv_count
    }

class FeelRequest(BaseModel):
    content: str
    valence: Optional[float] = None
    arousal: Optional[float] = None

@app.post("/save_feel")
async def save_feel(req: FeelRequest):
    """保存 AI 的感受到 L1"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    try:
        # 存入 SQLite
        c.execute(
            'INSERT INTO l1_memories (content, quote, event_type, tags, valence, arousal, status) VALUES (?,?,?,?,?,?,?)',
            (req.content, req.content, 'feel', '["感受"]', req.valence, req.arousal, 'active')
        )
        l1_id = c.lastrowid
        conn.commit()
        
        # 存入 ChromaDB
        embedding = await get_embedding(req.content)
        if embedding:
            l1_collection.add(
                ids=[f"l1_{l1_id}"],
                embeddings=[embedding],
                documents=[req.content],
                metadatas=[{
                    "ts": datetime.now().isoformat(),
                    "event_type": "feel",
                    "tags": '["感受"]',
                    "quote": req.content,
                    "is_core": 0,
                    "valence": req.valence or 0,
                    "arousal": req.arousal or 0,
                    "conv_id": "",
                    "client": "ai_self"
                }]
            )
        return {"status": "ok", "id": l1_id}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()

class ImportRequest(BaseModel):
    messages: List[dict]
    client: str = "import"
    conv_date: Optional[str] = None

@app.post("/import_conversation")
async def import_conversation(req: ImportRequest):
    """导入历史对话到 L0"""
    import uuid
    conv_id = f"{req.client}_{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    saved = 0
    try:
        for idx, msg in enumerate(req.messages):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content or role == "system":
                continue
            raw_ts = msg.get("timestamp") or req.conv_date or ""
            if raw_ts:
                try:
                    from datetime import timedelta
                    dt = datetime.fromisoformat(raw_ts.replace('Z', ''))
                    dt_utc = dt - timedelta(hours=8)
                    ts = dt_utc.isoformat()
                except:
                    ts = raw_ts
            else:
                ts = datetime.utcnow().isoformat()
            c.execute(
                'INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, source, client, status, extracted) VALUES (?,?,?,?,?,?,?,?,?)',
                (conv_id, idx, role, content, ts,'import', req.client, 'active', 0)
            )
            saved += 1
        conn.commit()
        return {"status": "ok", "conv_id": conv_id, "saved": saved}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()

class ProfileData(BaseModel):
    about_user: str = ""
    about_ai: str = ""
    about_us: str = ""

@app.get("/profile")
async def get_profile():
    profile_dir = DATA_DIR / "profile"
    try:
        about_user = (profile_dir / "about_user.md").read_text() if (profile_dir / "about_user.md").exists() else ""
        about_ai = (profile_dir / "about_ai.md").read_text() if (profile_dir / "about_ai.md").exists() else ""
        about_us = (profile_dir / "about_us.md").read_text() if (profile_dir / "about_us.md").exists() else ""
        return {"about_user": about_user, "about_ai": about_ai, "about_us": about_us}
    except Exception as e:
        return {"about_user": "", "about_ai": "", "about_us": "", "error": str(e)}

@app.post("/profile")
async def save_profile(req: ProfileData):
    profile_dir = DATA_DIR / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        (profile_dir / "about_user.md").write_text(req.about_user)
        (profile_dir / "about_ai.md").write_text(req.about_ai)
        (profile_dir / "about_us.md").write_text(req.about_us)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/l0/messages")
async def get_l0_messages(conv_id: Optional[str] = None, limit: int = 5000, offset: int = 0):
    """浏览 L0 原文"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    if conv_id:
        c.execute(
            'SELECT id, conv_id, role, content, ts, source, client, status FROM l0_messages WHERE conv_id=? AND status=? ORDER BY msg_idx ASC LIMIT ? OFFSET ?',
            (conv_id, 'active', limit, offset)
        )
    else:
        c.execute(
            'SELECT id, conv_id, role, content, ts, source, client, status FROM l0_messages WHERE status=? ORDER BY ts DESC LIMIT ? OFFSET ?',
            ('active', limit, offset)
        )
    rows = c.fetchall()
    c.execute('SELECT COUNT(*) FROM l0_messages WHERE status=?', ('active',))
    total = c.fetchone()[0]
    conn.close()
    messages = []
    for row in rows:
        messages.append({
            "id": row[0], "conv_id": row[1], "role": row[2],
            "content": row[3], "ts": row[4], "source": row[5],
            "client": row[6], "status": row[7]
        })
    return {"messages": messages, "total": total}

@app.get("/l0/conversations")
async def get_conversations(limit: int = 20, offset: int = 0):
    """获取对话列表"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute('''
        SELECT conv_id, client, MIN(ts) as first_ts, MAX(ts) as last_ts, COUNT(*) as msg_count
        FROM l0_messages WHERE status='active'
        GROUP BY conv_id ORDER BY last_ts DESC LIMIT ? OFFSET ?
    ''', (limit, offset))
    rows = c.fetchall()
    conn.close()
    convs = []
    for row in rows:
        convs.append({
            "conv_id": row[0], "client": row[1],
            "first_ts": row[2], "last_ts": row[3], "msg_count": row[4]
        })
    return {"conversations": convs}

@app.get("/l1/list")
async def get_l1_list(event_type: Optional[str] = None, limit: int = 50, offset: int = 0):
    """获取 L1 记忆列表"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    if event_type:
        c.execute(
            'SELECT id, content, quote, event_type, tags, valence, arousal, is_core, access_count, ts, client FROM l1_memories WHERE status=? AND event_type=? ORDER BY ts DESC LIMIT ? OFFSET ?',
            ('active', event_type, limit, offset)
        )
    else:
        c.execute(
            'SELECT id, content, quote, event_type, tags, valence, arousal, is_core, access_count, ts, client FROM l1_memories WHERE status=? ORDER BY ts DESC LIMIT ? OFFSET ?',
            ('active', limit, offset)
        )
    rows = c.fetchall()
    c.execute('SELECT COUNT(*) FROM l1_memories WHERE status=?', ('active',))
    total = c.fetchone()[0]
    conn.close()
    memories = []
    for row in rows:
        memories.append({
            "id": row[0], "content": row[1], "quote": row[2],
            "event_type": row[3], "tags": row[4], "valence": row[5],
            "arousal": row[6], "is_core": row[7], "access_count": row[8],
            "ts": row[9], "client": row[10]
        })
    return {"memories": memories, "total": total}

class UpdateL1Request(BaseModel):
    tags: Optional[str] = None
    is_core: Optional[int] = None
    event_type: Optional[str] = None

@app.put("/l1/{memory_id}")
async def update_l1(memory_id: int, req: UpdateL1Request):
    """编辑 L1 记忆（标签/核心标记/类型）"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    updates = []
    params = []
    if req.tags is not None:
        updates.append("tags=?")
        params.append(req.tags)
    if req.is_core is not None:
        updates.append("is_core=?")
        params.append(req.is_core)
    if req.event_type is not None:
        updates.append("event_type=?")
        params.append(req.event_type)
    if not updates:
        return {"status": "error", "detail": "nothing to update"}
    params.append(memory_id)
    c.execute(f'UPDATE l1_memories SET {",".join(updates)} WHERE id=?', params)
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/l1/{memory_id}")
async def delete_l1(memory_id: int):
    """删除 L1 记忆（标记为 superseded）"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute('UPDATE l1_memories SET status=? WHERE id=?', ('superseded', memory_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

class UpdateTimestampRequest(BaseModel):
    ts: str

@app.put("/l0/{msg_id}/timestamp")
async def update_l0_timestamp(msg_id: int, req: UpdateTimestampRequest):
    """修改 L0 消息的时间戳"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute('UPDATE l0_messages SET ts=? WHERE id=?', (req.ts, msg_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/l0/message/{msg_id}")
async def delete_l0_message(msg_id: int):
    """删除单条 L0 消息"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute('UPDATE l0_messages SET status=? WHERE id=?', ('superseded', msg_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/l0/conversation/{conv_id}")
async def delete_l0_conversation(conv_id: str):
    """删除整个对话"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute('UPDATE l0_messages SET status=? WHERE conv_id=?', ('superseded', conv_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/test/embedding")
async def test_embedding():
    embedding = await get_embedding("test")
    return {"status": "ok", "dimension": len(embedding)} if embedding else {"status": "error"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0", port=8001)
