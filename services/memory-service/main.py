from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
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
from cryptography.fernet import Fernet
import json

load_dotenv()

app = FastAPI(title="Memory Service")

# 功能6：配置加密。CONFIG_SECRET_KEY 用于加密存储 API Key（Fernet 对称加密）。
_config_secret = os.getenv("CONFIG_SECRET_KEY")
_fernet = Fernet(_config_secret.encode()) if _config_secret else None

def encrypt_secret(plain: str) -> str:
    """加密明文 API Key；未配置密钥时降级为明文存储（并告警）。"""
    if not plain:
        return ""
    if not _fernet:
        print("[CONFIG] 警告：未配置 CONFIG_SECRET_KEY，API Key 将明文存储")
        return plain
    return _fernet.encrypt(plain.encode()).decode()

def decrypt_secret(token: str) -> str:
    """解密 API Key；解密失败（如密钥变更）时返回空串。"""
    if not token:
        return ""
    if not _fernet:
        return token
    try:
        return _fernet.decrypt(token.encode()).decode()
    except Exception:
        print("[CONFIG] 警告：API Key 解密失败（密钥可能已变更）")
        return ""

CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", 8000))
ALIBABA_API_KEY = os.getenv("ALIBABA_API_KEY")
ALIBABA_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DATA_DIR = Path(os.getenv("DATA_DIR", "/home/qingzhi/memory-system/data"))
# 功能6：网关地址，用于配置变更后通知网关热重载
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:3000")
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
    # 功能6：供应商配置表。api_key 加密存储；models 存 JSON 数组。
    c.execute('''CREATE TABLE IF NOT EXISTS providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key_enc TEXT DEFAULT '',
        models TEXT DEFAULT '[]',
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    # 当前选中的供应商 + 模型（单行，key 固定为 1）
    c.execute('''CREATE TABLE IF NOT EXISTS active_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        provider_id INTEGER,
        model TEXT
    )''')
    # Shared Narrative 表
    c.execute('''CREATE TABLE IF NOT EXISTS shared_narrative (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        version INTEGER DEFAULT 1,
        trigger_type TEXT,
        trigger_details TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'active'
    )''')
    # Recent Summary 表
    c.execute('''CREATE TABLE IF NOT EXISTS recent_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        period_start DATETIME,
        period_end DATETIME,
        msg_count INTEGER,
        token_count INTEGER,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'active'
    )''')
    # Narrative 配置表（更新检查策略）
    c.execute('''CREATE TABLE IF NOT EXISTS narrative_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        auto_update_enabled INTEGER DEFAULT 1,
        check_threshold_turns INTEGER DEFAULT 50,
        check_threshold_l1 INTEGER DEFAULT 10,
        summary_max_turns INTEGER DEFAULT 100,
        summary_max_tokens INTEGER DEFAULT 50000
    )''')
    # 模型配置表（不同模块独立配置模型）
    c.execute('''CREATE TABLE IF NOT EXISTS model_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purpose TEXT UNIQUE NOT NULL,
        provider_id INTEGER,
        model_name TEXT,
        enabled INTEGER DEFAULT 1,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
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

            # 重构：active 兜底校验（双保险）。L1 已改硬删，但若 ChromaDB 删向量失败
            # 留下残留向量，仅靠向量检索仍可能命中。这里用 SQLite 核对该 L1 是否 active，
            # 不是（已删/不存在）就跳过，保证删掉的记忆一定搜不出来。
            try:
                _row = conn.execute(
                    "SELECT status FROM l1_memories WHERE id=?", (l1_id_str,)
                ).fetchone()
            except Exception:
                _row = None
            if not _row or _row[0] != 'active':
                continue

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


def normalize_content(content):
    """把消息 content 归一化成纯文本。

    问题2 关键修复：Kelivo 会把历史消息用 Anthropic content 块数组发回
    （如 [{"type":"thinking",...},{"type":"text","text":"..."}]）。
    直接把 list 往 SQLite TEXT 列插会抛 'type list is not supported'，
    导致整轮 save 事务 rollback、对话丢失。
    这里只取 text 块拼成纯文本，忽略 thinking/tool_use/tool_result
    （工具与思维链内容本就不该进 L0 记忆）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)

def _cascade_l1_on_version_switch(c, conv_id, superseded_old_ids):
    """修复7：旧 L0 版本切换时，联动作废其所在「提取批次」的 L1 并重置重提取。

    在 save_conversation 的事务内调用（传入同一个 cursor c），只做 SQLite 侧改动，
    返回需要从 ChromaDB 删除的 L1 id 列表（由调用方在 commit 后删向量）。

    批次定位：source_msg_id 是批次首条消息 id，批次按 id 区间不重叠切分对话。
    被改消息 old_id（已 extracted）所属批次的首 id = 该 conv active L1 中
    source_msg_id <= old_id 的最大值；批次上界 next_sid = 大于它的最小 source_msg_id。
    """
    if not superseded_old_ids:
        return []
    # 该 conv 下所有 active L1 用到的批次首 id（升序、去重）
    c.execute(
        "SELECT DISTINCT source_msg_id FROM l1_memories "
        "WHERE conv_id=? AND status='active' AND source_msg_id IS NOT NULL "
        "ORDER BY source_msg_id ASC",
        (conv_id,))
    batch_sids = [row[0] for row in c.fetchall()]
    if not batch_sids:
        return []

    affected_batches = set()   # 需要作废+重提的批次首 id
    for old_id in superseded_old_ids:
        # 找 <= old_id 的最大批次首 id（该消息所属批次）
        candidates = [s for s in batch_sids if s <= old_id]
        if candidates:
            affected_batches.add(max(candidates))

    l1_ids_to_purge = []
    for batch_sid in affected_batches:
        # 批次上界：下一个更大的批次首 id（没有则到对话末尾）
        higher = [s for s in batch_sids if s > batch_sid]
        next_sid = min(higher) if higher else None

        # 收集这批 active L1 的 id（删向量用）
        c.execute(
            "SELECT id FROM l1_memories WHERE conv_id=? AND source_msg_id=? AND status='active'",
            (conv_id, batch_sid))
        l1_ids_to_purge.extend(row[0] for row in c.fetchall())
        # 硬删这批 L1（重构：L1 统一硬删；批次定位逻辑不变，仍返回 id 列表给调用方删向量）
        c.execute(
            "DELETE FROM l1_memories "
            "WHERE conv_id=? AND source_msg_id=? AND status='active'",
            (conv_id, batch_sid))
        # 让这批 id 区间内的 active L0 重提取（被改消息旧行已 superseded，不会命中）
        if next_sid is not None:
            c.execute(
                "UPDATE l0_messages SET extracted=0 "
                "WHERE conv_id=? AND status='active' AND id>=? AND id<?",
                (conv_id, batch_sid, next_sid))
        else:
            c.execute(
                "UPDATE l0_messages SET extracted=0 "
                "WHERE conv_id=? AND status='active' AND id>=?",
                (conv_id, batch_sid))
    return l1_ids_to_purge


@app.post("/save_conversation")
async def save_conversation(req: SaveRequest):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    saved = 0
    superseded_old_ids = []   # 修复7：本轮因版本切换被 superseded 的旧 L0 id（含其 extracted 标志）
    try:
        for idx, msg in enumerate(req.messages):
            role = msg.get("role", "")
            content = normalize_content(msg.get("content", ""))
            if not content or role == "system":
                continue
            c.execute('SELECT id, content, extracted FROM l0_messages WHERE conv_id=? AND msg_idx=? AND status=?',
                      (req.conv_id, idx, 'active'))
            existing = c.fetchone()
            if existing:
                if existing[1] == content:
                    continue
                c.execute('UPDATE l0_messages SET status=? WHERE id=?', ('superseded', existing[0]))
                # 修复7：只有已提取过 L1 的旧消息才需要联动清理（extracted=1）
                if existing[2] == 1:
                    superseded_old_ids.append(existing[0])
            c.execute('INSERT INTO l0_messages (conv_id, msg_idx, role, content, client, status, extracted) VALUES (?,?,?,?,?,?,?)',
                      (req.conv_id, idx, role, content, req.client, 'active', 0))
            saved += 1

        # 修复7：L0 版本切换联动 L1（批次级）。
        # source_msg_id 是「该次 cron 提取批次首条消息的 id」，同批多条 L1 共享它，
        # 按 id 区间不重叠地切分整个对话。被改消息落在某批次内 → 作废整批 L1、
        # 让该批次的其它 active 消息一并重提取（extracted=0），旧向量从 ChromaDB 删。
        l1_ids_to_purge = _cascade_l1_on_version_switch(c, req.conv_id, superseded_old_ids)

        conn.commit()
    except Exception as e:
        conn.rollback()
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()

    # SQLite 已提交后再删向量：ChromaDB 失败不影响 L0/L1 的软删（与修复6 一致）
    if l1_ids_to_purge:
        try:
            l1_collection.delete(ids=[f"l1_{i}" for i in l1_ids_to_purge])
        except Exception as e:
            print(f"ChromaDB 版本切换清理失败 (conv_id={req.conv_id}): {e}")
            return {"status": "ok", "saved": saved,
                    "l1_purged": len(l1_ids_to_purge),
                    "warning": f"L1已软删但向量清除失败: {e}"}
    return {"status": "ok", "saved": saved, "l1_purged": len(l1_ids_to_purge)}

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
        # 修复2：显式写入 client='ai_self'、conv_id=''，与 ChromaDB metadata 对齐，
        # 让 Dashboard 能按 client 区分「AI自述」和「对话提取」的 feel。
        c.execute(
            'INSERT INTO l1_memories (content, quote, conv_id, client, event_type, tags, valence, arousal, status) VALUES (?,?,?,?,?,?,?,?,?)',
            (req.content, req.content, '', 'ai_self', 'feel', '["感受"]', req.valence, req.arousal, 'active')
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
async def get_conversations(limit: int = 500, offset: int = 0):
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
async def get_l1_list(event_type: Optional[str] = None, limit: int = 1000, offset: int = 0):
    """获取 L1 记忆列表"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    if event_type:
        c.execute(
            'SELECT id, content, quote, event_type, tags, valence, arousal, is_core, access_count, ts, client, source_msg_id FROM l1_memories WHERE status=? AND event_type=? ORDER BY ts DESC LIMIT ? OFFSET ?',
            ('active', event_type, limit, offset)
        )
    else:
        c.execute(
            'SELECT id, content, quote, event_type, tags, valence, arousal, is_core, access_count, ts, client, source_msg_id FROM l1_memories WHERE status=? ORDER BY ts DESC LIMIT ? OFFSET ?',
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
            "ts": row[9], "client": row[10],
            # 重构：有关联 L0（source_msg_id 非空）才可重提取；ai_self 的为 NULL → False
            "has_source": row[11] is not None
        })
    return {"memories": memories, "total": total}

class UpdateL1Request(BaseModel):
    tags: Optional[str] = None
    is_core: Optional[int] = None
    event_type: Optional[str] = None
    content: Optional[str] = None

@app.put("/l1/{memory_id}")
async def update_l1(memory_id: int, req: UpdateL1Request):
    """编辑 L1 记忆（标签/核心标记/类型/内容）

    修复4：支持修改 content。content 变化时，除更新 SQLite 外，
    还要重新生成 embedding 并同步 ChromaDB 的向量和文档，
    否则 recall 向量检索仍会命中旧内容。

    重构：L1 改硬删后，编辑前先校验该 id 存在且 status='active'，
    否则返回 404（防止对已删 id 编辑还返回 ok）。
    """
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    exists = c.execute("SELECT status FROM l1_memories WHERE id=?", (memory_id,)).fetchone()
    if not exists or exists[0] != 'active':
        conn.close()
        return JSONResponse(status_code=404, content={"status": "error", "detail": "L1 记忆不存在或已删除"})
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
    if req.content is not None:
        updates.append("content=?")
        params.append(req.content)
    if not updates:
        conn.close()
        return {"status": "error", "detail": "nothing to update"}
    params.append(memory_id)
    c.execute(f'UPDATE l1_memories SET {",".join(updates)} WHERE id=?', params)
    conn.commit()
    conn.close()

    # content 变化时重新生成向量并同步 ChromaDB
    if req.content is not None:
        embedding = await get_embedding(req.content)
        if embedding:
            try:
                l1_collection.update(
                    ids=[f"l1_{memory_id}"],
                    embeddings=[embedding],
                    documents=[req.content]
                )
            except Exception as e:
                print(f"ChromaDB 向量更新失败 l1_{memory_id}: {e}")
                return {"status": "ok", "warning": f"SQLite已更新但向量同步失败: {e}"}
        else:
            return {"status": "ok", "warning": "SQLite已更新但embedding生成失败，向量未同步"}
    return {"status": "ok"}

@app.delete("/l1/{memory_id}")
async def delete_l1(memory_id: int):
    """硬删 L1 记忆：SQLite DELETE + ChromaDB delete。

    重构：原来只标记 superseded，向量从不删 → 已删记忆仍被 /search 向量检索命中。
    ai_self 感受（conv_id=''、source_msg_id=NULL）只有这条删除路径，问题最明显。
    现改硬删。SQLite 先提交，再删向量；ChromaDB 失败返回 warning 不回滚
    （与 /search 的 active 兜底校验配合，残留向量也搜不出来）。
    """
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute('DELETE FROM l1_memories WHERE id=?', (memory_id,))
    conn.commit()
    conn.close()
    try:
        l1_collection.delete(ids=[f"l1_{memory_id}"])
    except Exception as e:
        print(f"ChromaDB 删除失败 l1_{memory_id}: {e}")
        return {"status": "ok", "warning": f"SQLite已删但向量清除失败: {e}"}
    return {"status": "ok"}

@app.post("/l1/{memory_id}/reextract")
async def reextract_l1(memory_id: int):
    """单条重提取：有 L0 关联的 L1 基于原文重新提炼 → 生成新 L1 → 硬删旧 L1。

    约束（不允许"旧删了新没生成"的中间态）：
    - 先校验 + 调 DeepSeek 提取，提取失败/为空 → 直接返回 error，旧 L1 原样保留。
    - 成功拿到新记忆后，SQLite 事务内「插新 + 删旧」一起提交，再写/删 ChromaDB。
    新 L1 必须沿用旧 L1 的 source_msg_id（批次锚点，绝不能丢）、conv_id、client；
    ts 用 L0 原文时间；quote/event_type/tags/valence/arousal 用新提取结果。
    ai_self（source_msg_id=NULL）会被 source_msg_id 校验自然拒绝。
    """
    # 复用 cron 提取管线的 prompt 和 DeepSeek 调用（懒加载，避免 import 时的连接副作用）
    from extract_l1 import EXTRACT_PROMPT, call_deepseek

    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    try:
        row = c.execute(
            "SELECT content, source_msg_id, conv_id, client, status FROM l1_memories WHERE id=?",
            (memory_id,)
        ).fetchone()
        if not row:
            conn.close()
            return JSONResponse(status_code=404, content={"status": "error", "detail": "L1 记忆不存在"})
        old_content, source_id, conv_id, client, status = row
        if status != 'active':
            conn.close()
            return JSONResponse(status_code=404, content={"status": "error", "detail": "L1 记忆已删除"})
        if not source_id:
            conn.close()
            return JSONResponse(status_code=400,
                                content={"status": "error", "detail": "该记忆无 L0 关联（如 ai_self 感受），不能重提取，只能编辑或删除"})

        # 取 L0 上下文（参考 get_l0_context：source_msg_id 前后各几条 active 消息），用 [你]/[我] 标注
        ctx_rows = c.execute(
            "SELECT role, content, ts FROM l0_messages "
            "WHERE conv_id=? AND status='active' AND id BETWEEN ? AND ? ORDER BY id ASC",
            (conv_id, max(0, source_id - 3), source_id + 5)
        ).fetchall()
        # 新 L1 的时间戳：用 source_msg_id 那条 L0 的原文时间（对话发生时间，非提取时间）
        src_ts_row = c.execute("SELECT ts FROM l0_messages WHERE id=?", (source_id,)).fetchone()
    except Exception as e:
        conn.close()
        return JSONResponse(status_code=500, content={"status": "error", "detail": f"读取失败: {e}"})

    if not ctx_rows:
        conn.close()
        return JSONResponse(status_code=400, content={"status": "error", "detail": "找不到关联的 L0 原文（可能已删除）"})

    # 拼提取文本（与 extract_l1 的格式一致：[你]/[我]），并附带旧记忆要求只提炼这一条
    convo = ""
    for role, content, _ts in ctx_rows:
        prefix = "你" if role == "user" else "我"
        convo += f"[{prefix}] {content}\n"
    reextract_hint = (
        f"\n（重提取任务：请只基于以上对话原文，重新提炼下面这条旧记忆对应的内容，"
        f"严格只输出一条记忆。旧记忆内容：{old_content}）\n"
    )
    # call_deepseek 内部会拼上 EXTRACT_PROMPT；这里传"对话 + 重提取说明"作为正文
    try:
        memories = call_deepseek(convo + reextract_hint)
    except Exception as e:
        conn.close()
        return JSONResponse(status_code=502, content={"status": "error", "detail": f"DeepSeek 提取失败: {e}"})
    if not memories:
        conn.close()
        return JSONResponse(status_code=422,
                            content={"status": "error", "detail": "重提取未产生记忆，旧记忆已保留（未删除）"})
    new_mem = memories[0]  # 只取一条

    new_ts = (src_ts_row[0] if src_ts_row and src_ts_row[0] else datetime.now().isoformat())
    tags_json = json.dumps(new_mem.get("tags", []), ensure_ascii=False)

    # SQLite 事务：插新 + 删旧一起提交（原子），成功后再写/删 ChromaDB
    try:
        c.execute(
            'INSERT INTO l1_memories (content, quote, source_msg_id, conv_id, client, event_type, tags, valence, arousal, status, ts) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (new_mem["content"], new_mem.get("quote", ""), source_id, conv_id, client,
             new_mem.get("event_type", "general"), tags_json,
             new_mem.get("valence"), new_mem.get("arousal"), 'active', new_ts)
        )
        new_id = c.lastrowid
        c.execute('DELETE FROM l1_memories WHERE id=?', (memory_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return JSONResponse(status_code=500, content={"status": "error", "detail": f"落库失败，旧记忆已保留: {e}"})
    finally:
        conn.close()

    # ChromaDB：写新向量 + 删旧向量。失败不回滚 SQLite（与 active 兜底校验配合），返回 warning。
    warning = None
    embedding = await get_embedding(new_mem["content"])
    if embedding:
        try:
            l1_collection.add(
                ids=[f"l1_{new_id}"],
                embeddings=[embedding],
                documents=[new_mem["content"]],
                metadatas=[{
                    "ts": new_ts,
                    "event_type": new_mem.get("event_type", "general"),
                    "tags": tags_json,
                    "quote": new_mem.get("quote", ""),
                    "conv_id": conv_id,
                    "client": client,
                    "is_core": 0,
                    "valence": new_mem.get("valence") or 0,
                    "arousal": new_mem.get("arousal") or 0
                }]
            )
        except Exception as e:
            warning = f"新 L1 已入库但向量写入失败: {e}"
    else:
        warning = "新 L1 已入库但 embedding 生成失败，向量未写入"
    try:
        l1_collection.delete(ids=[f"l1_{memory_id}"])
    except Exception as e:
        warning = (warning + "; " if warning else "") + f"旧向量清除失败: {e}"

    resp = {"status": "ok", "new_id": new_id, "old_id": memory_id,
            "content": new_mem["content"], "quote": new_mem.get("quote", ""),
            "event_type": new_mem.get("event_type", "general"), "source_msg_id": source_id}
    if warning:
        resp["warning"] = warning
    return resp

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
    """删除单条 L0 消息（联动清理 L1 + ChromaDB 向量，批次级）。

    原来只标记 L0 superseded，其对应 L1 记忆和向量残留，recall 仍会搜到。
    现复用修复7 的批次级联动：source_msg_id 是「提取批次首条消息 id」，
    删掉的消息落在某批次内 → 作废整批 L1、该批其它 active 消息重提取(extracted=0)、
    从 ChromaDB 删对应向量。只对已提取过(extracted=1)的消息联动。
    """
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    l1_ids_to_purge = []
    try:
        row = c.execute('SELECT conv_id, extracted, status FROM l0_messages WHERE id=?',
                        (msg_id,)).fetchone()
        if not row:
            return {"status": "error", "detail": "message not found"}
        conv_id, extracted, cur_status = row
        # 标记该 L0 superseded
        c.execute('UPDATE l0_messages SET status=? WHERE id=?', ('superseded', msg_id))
        # 只有已提取过 L1 的消息才需要联动（extracted=0 的没进过批次，无 L1）
        if extracted == 1 and cur_status == 'active':
            l1_ids_to_purge = _cascade_l1_on_version_switch(c, conv_id, [msg_id])
        conn.commit()
    except Exception as e:
        conn.rollback()
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()
    # SQLite 提交后再删向量：ChromaDB 失败不回滚（与修复6/7 一致）
    if l1_ids_to_purge:
        try:
            l1_collection.delete(ids=[f"l1_{i}" for i in l1_ids_to_purge])
        except Exception as e:
            print(f"ChromaDB 单条删除清理失败 (msg_id={msg_id}): {e}")
            return {"status": "ok", "l1_purged": len(l1_ids_to_purge),
                    "warning": f"L1已软删但向量清除失败: {e}"}
    return {"status": "ok", "l1_purged": len(l1_ids_to_purge)}

@app.delete("/l0/conversation/{conv_id}")
async def delete_l0_conversation(conv_id: str):
    """删除整个对话（修复6/8：联动清除 L1 + ChromaDB 向量）

    重构：L1 从软删（superseded）改为硬删（DELETE）。L0 仍保持软删。
    先收集该对话下 active L1 的 id，DELETE 后再删对应向量。
    """
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    # 1. 查出该 conv_id 下所有 active L1 的 id（删向量要用）
    c.execute("SELECT id FROM l1_memories WHERE conv_id=? AND status='active'", (conv_id,))
    l1_ids = [row[0] for row in c.fetchall()]
    # 2. 标记 L0 superseded（L0 仍软删）
    c.execute('UPDATE l0_messages SET status=? WHERE conv_id=?', ('superseded', conv_id))
    # 3. 硬删该对话下的 active L1
    c.execute("DELETE FROM l1_memories WHERE conv_id=? AND status='active'", (conv_id,))
    conn.commit()
    conn.close()
    # 4. 从 ChromaDB 删除对应向量
    if l1_ids:
        try:
            l1_collection.delete(ids=[f"l1_{i}" for i in l1_ids])
        except Exception as e:
            print(f"ChromaDB 删除失败 (conv_id={conv_id}): {e}")
            return {"status": "ok", "l1_removed": len(l1_ids), "warning": f"L1已硬删但向量清除失败: {e}"}
    return {"status": "ok", "l1_removed": len(l1_ids)}

# ============ 功能6：供应商模型配置（CRUD + 加密） ============

class ProviderData(BaseModel):
    name: str
    base_url: str
    api_key: Optional[str] = None   # 明文传入；为 None 表示不修改已存的 key
    models: List[str] = []

class ActiveConfig(BaseModel):
    provider_id: int
    model: str

def _mask_key(enc: str) -> str:
    """给 Dashboard 显示用：有 key 就返回 ***，没有返回空。"""
    return "***" if enc else ""

@app.get("/config/providers")
async def list_providers():
    """列出所有供应商。api_key 脱敏为 ***，不返回明文。"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    rows = c.execute("SELECT id, name, base_url, api_key_enc, models FROM providers ORDER BY id").fetchall()
    active = c.execute("SELECT provider_id, model FROM active_config WHERE id = 1").fetchone()
    conn.close()
    providers = []
    for r in rows:
        providers.append({
            "id": r[0], "name": r[1], "base_url": r[2],
            "api_key": _mask_key(r[3]),
            "has_key": bool(r[3]),
            "models": json.loads(r[4] or "[]"),
        })
    return {
        "providers": providers,
        "active": {"provider_id": active[0], "model": active[1]} if active else None,
    }

@app.post("/config/providers")
async def create_provider(req: ProviderData):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    enc = encrypt_secret(req.api_key) if req.api_key else ""
    c.execute(
        "INSERT INTO providers (name, base_url, api_key_enc, models) VALUES (?, ?, ?, ?)",
        (req.name, req.base_url, enc, json.dumps(req.models, ensure_ascii=False)),
    )
    pid = c.lastrowid
    conn.commit()
    conn.close()
    return {"status": "ok", "id": pid}

@app.put("/config/providers/{pid}")
async def update_provider(pid: int, req: ProviderData):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    row = c.execute("SELECT api_key_enc FROM providers WHERE id = ?", (pid,)).fetchone()
    if not row:
        conn.close()
        return {"status": "error", "message": "provider not found"}
    # api_key 为 None/空 时保留原 key（前端显示 *** 时不会回传真实 key）
    if req.api_key:
        enc = encrypt_secret(req.api_key)
    else:
        enc = row[0]
    c.execute(
        "UPDATE providers SET name = ?, base_url = ?, api_key_enc = ?, models = ? WHERE id = ?",
        (req.name, req.base_url, enc, json.dumps(req.models, ensure_ascii=False), pid),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/config/providers/{pid}")
async def delete_provider(pid: int):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute("DELETE FROM providers WHERE id = ?", (pid,))
    # 如果删的是当前选中的供应商，清空 active
    active = c.execute("SELECT provider_id FROM active_config WHERE id = 1").fetchone()
    if active and active[0] == pid:
        c.execute("DELETE FROM active_config WHERE id = 1")
    conn.commit()
    conn.close()
    return {"status": "ok"}

async def notify_gateway_reload():
    """通知网关热重载配置（localhost 内部调用，不走 nginx）。失败不影响配置保存。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{GATEWAY_URL}/reload-config")
        return True
    except Exception as e:
        print(f"[CONFIG] 通知网关重载失败：{e}")
        return False

@app.post("/config/active")
async def set_active(req: ActiveConfig):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    exists = c.execute("SELECT id FROM providers WHERE id = ?", (req.provider_id,)).fetchone()
    if not exists:
        conn.close()
        return {"status": "error", "message": "provider not found"}
    c.execute(
        "INSERT INTO active_config (id, provider_id, model) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET provider_id = excluded.provider_id, model = excluded.model",
        (req.provider_id, req.model),
    )
    conn.commit()
    conn.close()
    reloaded = await notify_gateway_reload()
    return {"status": "ok", "gateway_reloaded": reloaded}

@app.get("/config/current")
async def get_current_config():
    """给网关拉取用：返回当前选中供应商的 base_url + 解密后的真实 api_key + model。"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    active = c.execute("SELECT provider_id, model FROM active_config WHERE id = 1").fetchone()
    if not active:
        conn.close()
        return {"configured": False}
    row = c.execute("SELECT name, base_url, api_key_enc FROM providers WHERE id = ?", (active[0],)).fetchone()
    conn.close()
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "name": row[0],
        "base_url": row[1],
        "api_key": decrypt_secret(row[2]) if row[2] else "",
        "model": active[1],
    }

@app.get("/last_session")
async def get_last_session(conv_id: Optional[str] = None):
    """时间感知：返回最近一条 active L0 消息的时间戳（UTC）。

    网关用它算距上次对话的间隔，注入"好久不见"式的会话间隔感知。
    传 conv_id 时先查该对话最近一条；查不到（新开的对话窗口还没存过消息）
    再退回全局最近一条——这样"隔了一周新开窗口"也能感知到间隔（问题2 的本意）。
    不传 conv_id 直接查全局最近一条。
    ts 是 SQLite 存的 UTC（CURRENT_TIMESTAMP，"YYYY-MM-DD HH:MM:SS"）。
    """
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    try:
        row = None
        if conv_id:
            row = c.execute(
                "SELECT ts FROM l0_messages WHERE conv_id=? AND status='active' ORDER BY ts DESC LIMIT 1",
                (conv_id,)
            ).fetchone()
        if not row:
            row = c.execute(
                "SELECT ts FROM l0_messages WHERE status='active' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
    finally:
        conn.close()
    return {"last_ts": row[0] if row else None}

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/test/embedding")
async def test_embedding():
    embedding = await get_embedding("test")
    return {"status": "ok", "dimension": len(embedding)} if embedding else {"status": "error"}

# ============ Shared Narrative API ============

@app.get("/narrative/current")
async def get_current_narrative():
    """获取当前活跃的 Shared Narrative"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    row = c.execute(
        "SELECT id, content, version, trigger_type, ts FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return {"exists": False}
    return {
        "exists": True,
        "id": row[0],
        "content": row[1],
        "version": row[2],
        "trigger_type": row[3],
        "ts": row[4]
    }

@app.get("/narrative/history")
async def get_narrative_history(limit: int = 10):
    """获取历史版本"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, version, trigger_type, ts, status FROM shared_narrative ORDER BY version DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return {
        "history": [
            {"id": r[0], "version": r[1], "trigger_type": r[2], "ts": r[3], "status": r[4]}
            for r in rows
        ]
    }

class NarrativeGenerateRequest(BaseModel):
    trigger_type: str = "manual"
    force: bool = False

@app.post("/narrative/generate")
async def generate_narrative(req: NarrativeGenerateRequest):
    """生成 Shared Narrative"""
    try:
        # 1. 获取现有 narrative
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()
        existing = c.execute(
            "SELECT content FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        existing_narrative = existing[0] if existing else ""

        # 2. 获取重要的 L1 记忆（核心记忆 + 最近的高唤醒记忆）
        core_memories = c.execute(
            "SELECT content, quote, ts FROM l1_memories WHERE status='active' AND is_core=1 ORDER BY ts DESC LIMIT 20"
        ).fetchall()

        recent_memories = c.execute(
            "SELECT content, quote, ts FROM l1_memories WHERE status='active' AND arousal >= 0.5 ORDER BY ts DESC LIMIT 30"
        ).fetchall()

        conn.close()

        # 3. 获取 narrative 模型配置
        config = await get_model_config("narrative")
        if not config["configured"]:
            return {"status": "error", "message": "Narrative 模型未配置"}

        # 4. 构建提示词
        prompt = await build_narrative_prompt(existing_narrative, core_memories, recent_memories)

        # 5. 调用 LLM 生成
        new_narrative = await call_llm_for_narrative(config, prompt)

        if not new_narrative:
            return {"status": "error", "message": "生成失败"}

        # 6. 保存新版本
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()

        # 标记旧版本为 superseded
        c.execute("UPDATE shared_narrative SET status='superseded' WHERE status='active'")

        # 获取新版本号
        last_version = c.execute("SELECT MAX(version) FROM shared_narrative").fetchone()[0] or 0
        new_version = last_version + 1

        # 插入新版本
        c.execute(
            "INSERT INTO shared_narrative (content, version, trigger_type, trigger_details, status) VALUES (?, ?, ?, ?, 'active')",
            (new_narrative, new_version, req.trigger_type, json.dumps({"timestamp": datetime.now().isoformat()}))
        )

        conn.commit()
        new_id = c.lastrowid
        conn.close()

        return {
            "status": "ok",
            "id": new_id,
            "version": new_version,
            "content": new_narrative
        }

    except Exception as e:
        print(f"Narrative generation error: {e}")
        return {"status": "error", "message": str(e)}

class NarrativeUpdateRequest(BaseModel):
    content: str

@app.put("/narrative/{narrative_id}")
async def update_narrative(narrative_id: int, req: NarrativeUpdateRequest):
    """编辑 Narrative 内容"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute("UPDATE shared_narrative SET content=? WHERE id=?", (req.content, narrative_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ============ Recent Summary API ============

@app.get("/summary/current")
async def get_current_summary():
    """获取当前 Recent Summary"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    row = c.execute(
        "SELECT id, content, period_start, period_end, msg_count, ts FROM recent_summary WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return {"exists": False}
    return {
        "exists": True,
        "id": row[0],
        "content": row[1],
        "period_start": row[2],
        "period_end": row[3],
        "msg_count": row[4],
        "ts": row[5]
    }

@app.post("/summary/generate")
async def generate_summary():
    """生成 Recent Summary"""
    try:
        # 获取配置
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()
        config_row = c.execute("SELECT summary_max_turns, summary_max_tokens FROM narrative_config WHERE id=1").fetchone()
        max_turns = config_row[0] if config_row else 100

        # 获取最近的对话消息
        messages = c.execute(
            "SELECT role, content, ts FROM l0_messages WHERE status='active' ORDER BY ts DESC LIMIT ?",
            (max_turns,)
        ).fetchall()

        conn.close()

        if not messages:
            return {"status": "error", "message": "没有足够的对话内容"}

        # 获取 summary 模型配置
        model_config = await get_model_config("summary")
        if not model_config["configured"]:
            return {"status": "error", "message": "Summary 模型未配置"}

        # 构建摘要提示词
        prompt = build_summary_prompt(messages)

        # 调用 LLM 生成摘要
        summary_content = await call_llm_for_summary(model_config, prompt)

        if not summary_content:
            return {"status": "error", "message": "生成失败"}

        # 保存摘要
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()

        # 标记旧摘要为 superseded
        c.execute("UPDATE recent_summary SET status='superseded' WHERE status='active'")

        period_start = messages[-1][2] if messages else None
        period_end = messages[0][2] if messages else None

        c.execute(
            "INSERT INTO recent_summary (content, period_start, period_end, msg_count, status) VALUES (?, ?, ?, ?, 'active')",
            (summary_content, period_start, period_end, len(messages))
        )

        conn.commit()
        new_id = c.lastrowid
        conn.close()

        return {"status": "ok", "id": new_id, "content": summary_content}

    except Exception as e:
        print(f"Summary generation error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/summary/config")
async def get_summary_config():
    """获取摘要配置"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    row = c.execute("SELECT * FROM narrative_config WHERE id=1").fetchone()
    conn.close()

    if not row:
        # 返回默认值
        return {
            "auto_update_enabled": 1,
            "check_threshold_turns": 50,
            "check_threshold_l1": 10,
            "summary_max_turns": 100,
            "summary_max_tokens": 50000
        }

    return {
        "auto_update_enabled": row[1],
        "check_threshold_turns": row[2],
        "check_threshold_l1": row[3],
        "summary_max_turns": row[4],
        "summary_max_tokens": row[5]
    }

class SummaryConfigUpdate(BaseModel):
    auto_update_enabled: Optional[int] = None
    check_threshold_turns: Optional[int] = None
    check_threshold_l1: Optional[int] = None
    summary_max_turns: Optional[int] = None
    summary_max_tokens: Optional[int] = None

@app.put("/summary/config")
async def update_summary_config(req: SummaryConfigUpdate):
    """更新摘要配置"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # 确保配置行存在
    c.execute("INSERT OR IGNORE INTO narrative_config (id) VALUES (1)")

    updates = []
    params = []
    if req.auto_update_enabled is not None:
        updates.append("auto_update_enabled=?")
        params.append(req.auto_update_enabled)
    if req.check_threshold_turns is not None:
        updates.append("check_threshold_turns=?")
        params.append(req.check_threshold_turns)
    if req.check_threshold_l1 is not None:
        updates.append("check_threshold_l1=?")
        params.append(req.check_threshold_l1)
    if req.summary_max_turns is not None:
        updates.append("summary_max_turns=?")
        params.append(req.summary_max_turns)
    if req.summary_max_tokens is not None:
        updates.append("summary_max_tokens=?")
        params.append(req.summary_max_tokens)

    if updates:
        params.append(1)
        c.execute(f"UPDATE narrative_config SET {', '.join(updates)} WHERE id=?", params)

    conn.commit()
    conn.close()
    return {"status": "ok"}

# ========== 继续标记 ==========
# CONTINUATION_MARKER_1

# ============ L1 Extraction Status API ============

@app.get("/l1/extraction_status")
async def get_extraction_status():
    """获取 L1 提取状态"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # 统计总消息数
    total = c.execute("SELECT COUNT(*) FROM l0_messages WHERE status='active'").fetchone()[0]

    # 统计已提取消息数
    extracted = c.execute("SELECT COUNT(*) FROM l0_messages WHERE status='active' AND extracted=1").fetchone()[0]

    # 待提取
    pending = total - extracted

    # 获取批次信息（按日期分组）
    batches = c.execute("""
        SELECT
            DATE(ts) as date,
            MIN(id) as start_id,
            MAX(id) as end_id,
            COUNT(*) as count,
            SUM(extracted) as extracted_count
        FROM l0_messages
        WHERE status='active'
        GROUP BY DATE(ts)
        ORDER BY date DESC
        LIMIT 20
    """).fetchall()

    conn.close()

    batch_list = []
    for b in batches:
        status = "extracted" if b[4] == b[3] else "partial" if b[4] > 0 else "pending"
        batch_list.append({
            "date": b[0],
            "msg_range": f"{b[1]}-{b[2]}",
            "total": b[3],
            "extracted": b[4],
            "status": status
        })

    return {
        "total_messages": total,
        "extracted_messages": extracted,
        "pending_messages": pending,
        "batches": batch_list
    }

@app.post("/l1/extract_now")
async def extract_now():
    """手动触发 L1 提取（调用 extract_l1.py）"""
    try:
        import subprocess
        script_path = Path(__file__).parent / "extract_l1.py"
        venv_python = Path(__file__).parent / "venv" / "bin" / "python"

        # 在后台运行提取脚本
        subprocess.Popen([str(venv_python), str(script_path)])

        return {"status": "ok", "message": "提取任务已启动"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ============ Pipeline Status API ============

@app.get("/pipeline/status")
async def get_pipeline_status():
    """获取整个记忆管线状态"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # L0 统计
    l0_total = c.execute("SELECT COUNT(*) FROM l0_messages WHERE status='active'").fetchone()[0]

    # L1 统计
    l1_extracted = c.execute("SELECT COUNT(*) FROM l0_messages WHERE status='active' AND extracted=1").fetchone()[0]
    l1_total = c.execute("SELECT COUNT(*) FROM l1_memories WHERE status='active'").fetchone()[0]
    l1_percent = int((l1_extracted / l0_total * 100)) if l0_total > 0 else 0

    # Embedding 统计（从 ChromaDB 获取）
    try:
        embedding_count = l1_collection.count()
        last_l1 = c.execute("SELECT ts FROM l1_memories WHERE status='active' ORDER BY ts DESC LIMIT 1").fetchone()
        embedding_last_update = last_l1[0] if last_l1 else None
    except:
        embedding_count = 0
        embedding_last_update = None

    # Narrative 统计
    narrative = c.execute("SELECT version, ts FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1").fetchone()
    narrative_version = narrative[0] if narrative else 0
    narrative_last_update = narrative[1] if narrative else None

    conn.close()

    return {
        "l0": {"total": l0_total, "percent": 100},
        "l1": {"extracted": l1_extracted, "total": l1_total, "pending": l0_total - l1_extracted, "percent": l1_percent},
        "embedding": {"count": embedding_count, "last_update": embedding_last_update},
        "narrative": {"version": narrative_version, "last_update": narrative_last_update, "pending": False}
    }

# ============ Model Config API (扩展) ============

async def get_model_config(purpose: str):
    """获取指定模块的模型配置"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # 先查 model_configs 表
    config = c.execute(
        "SELECT provider_id, model_name FROM model_configs WHERE purpose=? AND enabled=1",
        (purpose,)
    ).fetchone()

    if not config:
        conn.close()
        return {"configured": False}

    # 查供应商信息
    provider = c.execute(
        "SELECT name, base_url, api_key_enc FROM providers WHERE id=?",
        (config[0],)
    ).fetchone()

    conn.close()

    if not provider:
        return {"configured": False}

    return {
        "configured": True,
        "provider_name": provider[0],
        "base_url": provider[1],
        "api_key": decrypt_secret(provider[2]),
        "model": config[1]
    }

@app.get("/config/models")
async def get_all_model_configs():
    """获取所有模块的模型配置"""
    purposes = ["chat", "l1_extract", "embedding", "narrative", "summary"]
    configs = {}

    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    for purpose in purposes:
        row = c.execute(
            """SELECT mc.provider_id, mc.model_name, p.name
               FROM model_configs mc
               LEFT JOIN providers p ON mc.provider_id = p.id
               WHERE mc.purpose=? AND mc.enabled=1""",
            (purpose,)
        ).fetchone()

        if row:
            configs[purpose] = {
                "provider_id": row[0],
                "provider_name": row[2],
                "model": row[1]
            }
        else:
            configs[purpose] = None

    conn.close()
    return {"configs": configs}

class ModelConfigUpdate(BaseModel):
    provider_id: int
    model_name: str

@app.put("/config/models/{purpose}")
async def update_model_config(purpose: str, req: ModelConfigUpdate):
    """更新指定模块的模型配置"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()

    # 检查供应商是否存在
    exists = c.execute("SELECT id FROM providers WHERE id=?", (req.provider_id,)).fetchone()
    if not exists:
        conn.close()
        return {"status": "error", "message": "供应商不存在"}

    # 插入或更新配置
    c.execute(
        """INSERT INTO model_configs (purpose, provider_id, model_name, enabled)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(purpose) DO UPDATE SET
           provider_id=excluded.provider_id,
           model_name=excluded.model_name,
           ts=CURRENT_TIMESTAMP""",
        (purpose, req.provider_id, req.model_name)
    )

    conn.commit()
    conn.close()

    return {"status": "ok"}

# ============ Helper Functions ============

async def build_narrative_prompt(existing_narrative: str, core_memories: list, recent_memories: list) -> str:
    """构建 Narrative 生成提示词"""
    # 读取提示词模板
    prompt_file = Path(__file__).parent.parent.parent / "Narrative生成提示词.md"
    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            template = f.read()
    except:
        template = """你是一段亲密关系的记忆守护者。维护一份连续的共同经历叙事。

## 任务
基于现有叙事和新记忆，更新叙事内容。保留时间锚点、情感转折点、共同经历。

## 现有叙事
{existing}

## 新增记忆
{memories}

请生成更新后的叙事（markdown格式）："""

    # 构建记忆列表
    memory_text = "### 核心记忆\n"
    for m in core_memories:
        memory_text += f"- {m[0]}\n"
        if m[1]:
            memory_text += f"  > {m[1]}\n"

    memory_text += "\n### 最近记忆\n"
    for m in recent_memories[:20]:  # 限制数量
        memory_text += f"- {m[0]}\n"

    prompt = template.replace("{existing}", existing_narrative or "（首次生成）")
    prompt = prompt.replace("{memories}", memory_text)

    return prompt

async def call_llm_for_narrative(config: dict, prompt: str) -> str:
    """调用 LLM 生成 Narrative"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{config['base_url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": config['model'],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4000
                }
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"LLM call error: {e}")
        return None

def build_summary_prompt(messages: list) -> str:
    """构建 Summary 提示词"""
    msg_text = ""
    for msg in reversed(messages[-100:]):  # 最近100条，倒序
        role = "用户" if msg[0] == "user" else "AI"
        msg_text += f"{role}: {msg[1][:500]}\n\n"

    prompt = f"""请为以下对话生成一份简洁的近期摘要（300字以内）：

{msg_text}

摘要要求：
- 提取关键事件和话题
- 保留时间顺序
- 突出重要信息
- 简洁明了

摘要："""

    return prompt

async def call_llm_for_summary(config: dict, prompt: str) -> str:
    """调用 LLM 生成 Summary"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{config['base_url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": config['model'],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1000
                }
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"LLM call error: {e}")
        return None

# Dashboard 静态文件路由
DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"

@app.get("/")
async def root():
    """根路径重定向到 dashboard"""
    index_file = DASHBOARD_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse({"message": "Memory Service API", "dashboard": "/dashboard/"})

@app.get("/dashboard/")
async def dashboard():
    """Dashboard 主页"""
    index_file = DASHBOARD_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse({"error": "Dashboard not found"}, status_code=404)

# 挂载静态文件目录（CSS/JS 等资源）
if DASHBOARD_DIR.exists():
    app.mount("/dashboard/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="dashboard_static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
