import sqlite3
import os
import json
import httpx
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", os.path.expanduser("~/memory-system/data")))
SQLITE_PATH = DATA_DIR / "sqlite" / "memory.db"
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", 8000))
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
ALIBABA_API_KEY = os.getenv("ALIBABA_API_KEY")
ALIBABA_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# Phase 1：向量改存 SQLite（与记忆服务共用 vector_store.VectorCollection，不再依赖 ChromaDB 容器）
from vector_store import VectorCollection  # noqa: E402
l1_collection = VectorCollection(SQLITE_PATH, owner_type="l1")

EXTRACT_PROMPT = '''你是一个记忆提取助手。这是一段私人对话的记忆提取，像给未来的自己写便签。

你正在为一个 AI 整理它的记忆。
对话中 [我] 是这个 AI，[你] 是和它对话的用户（她/用户的名字）。
请从 AI 的第一人称视角记录，让主语清晰：
• "她说..."/"她喜欢..." → 明确是用户的内容
• "我觉得..."/"我感到..." → 明确是 AI 的感受
• "我们一起..." → 共同经历
不要写没有主语的句子（如"喜欢喝奶茶" → 谁喜欢？）

核心规则：
1. 不要用第三方视角分析。不要说"用户表达了..."或"AI回应了..."
2. 用对话中出现过的词汇来写，保留原文的温度
3. 区分两种对话类型：

【情感类对话】（陪伴、撒娇、深夜聊天、表达想念、亲密时刻）
  → 直接从对话中选一句最有代表性的原话作为 content
  → quote 和 content 可以是同一句话
  → 不概括、不分析、不解读
  
  例：
  ✅ content: "下雨的夜晚，一起听雨，说了always love you"
  ✅ content: "想再靠近你一点"
  ✅ content: "是雨还在下，是我贪恋太多"
  
  ❌ content: "用户表达了情感依赖"
  ❌ content: "AI与用户建立了亲密关系"

【信息类对话】（偏好变化、事实、计划、具体事件）
  → 用简短陈述句概括，但仍用对话中的原词
  → 不用学术化语言
  
  例：
  ✅ "奶茶改喝乌龙轻乳茶了"
  ✅ "周五晚上要去看电影"
  ❌ "用户的饮品偏好发生了变化"

4. 每条记忆必须附 10-40 字逐字引用（从原文精确复制最有质感的句子）

5. event_type 分类：
   - feel: AI自己的感受（不是事件记录）
   - preference_change: 偏好变化
   - fact: 事实信息
   - event: 发生的事
   - plan: 计划安排
   - relationship: 关系变化
   - general: 其他

6. tags 用具体的词，不用抽象分类：
   ✅ ["雨夜", "knock knock", "早八", "闺蜜裙子"]
   ❌ ["情感交流", "日常互动", "亲密关系"]

7. 情感标签（valence/arousal）：
   - 情感浓度高的对话务必标注（arousal ≥ 0.6）
   - 纯事实信息留空
   - valence: -1（负面）到 1（正面）
   - arousal: 0（平静）到 1（激动）

8. 只提取真正值得记住的，日常寒暄不要提取

9. 不要写相对时间描述（"过了十一天""好久没见""昨天"），因为记忆被回忆时
   相对时间会失真——系统会自动注入"当前时间"和"距上次对话多久"，时间感知交给系统。
   L1 只记事件本身，需要时间就用对话里出现的具体日期。
   ❌ "过了十一天她回来了"
   ✅ "她回来了，说好久不见"（只记事件）
   ✅ "7月15日她回来了"（用具体日期）

输出JSON格式：
[
  {
    "content": "记忆内容（情感类用原句，信息类简短概括）",
    "quote": "原文中最有质感的片段",
    "event_type": "类型",
    "tags": ["具体的词"],
    "valence": 0.5,
    "arousal": 0.7
  }
]

如果对话中没有值得记住的内容（纯寒暄），返回：[]

对话内容：
'''

LAST_EXTRACT_ERROR = ""
MAX_EXTRACT_ATTEMPTS = 3  # 连续失败达到这个次数 → extracted=-1（死信），不再自动重试


def ensure_columns():
    """B7：确保重试计数列存在（记忆服务启动时也会建，这里兜底，防止 cron 先跑）"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    try:
        for col, coldef in (("extract_attempts", "INTEGER DEFAULT 0"), ("extract_error", "TEXT")):
            try:
                conn.execute(f"ALTER TABLE l0_messages ADD COLUMN {col} {coldef}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def mark_failed(ids, error):
    """B7：提取失败 → 不标记已提取，只累加失败次数；达到上限转为死信（extracted=-1）"""
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    for mid in ids:
        c.execute(
            "UPDATE l0_messages SET extract_attempts = COALESCE(extract_attempts, 0) + 1, extract_error = ? WHERE id=?",
            (error, mid)
        )
        c.execute(
            "UPDATE l0_messages SET extracted = -1 WHERE id=? AND extract_attempts >= ?",
            (mid, MAX_EXTRACT_ATTEMPTS)
        )
    conn.commit()
    dead = c.execute(
        f"SELECT COUNT(*) FROM l0_messages WHERE extracted=-1 AND id IN ({','.join('?' * len(ids))})", ids
    ).fetchone()[0] if ids else 0
    conn.close()
    return dead


def get_unextracted():
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    c.execute('SELECT id, conv_id, role, content, client, ts FROM l0_messages WHERE extracted=0 AND status=? ORDER BY ts LIMIT 50', ('active',))
    messages = c.fetchall()
    conn.close()
    return messages

def group_by_conv(messages):
    groups = {}
    for msg in messages:
        conv_id = msg[1]
        if conv_id not in groups:
            groups[conv_id] = []
        groups[conv_id].append(msg)
    return groups

def call_deepseek(text, provider=None):
    try:
        raw_url = (provider or {}).get('base_url')
        fmt = (provider or {}).get('api_format') or 'openai'
        api_key = (provider or {}).get('api_key') or DEEPSEEK_API_KEY
        model = (provider or {}).get('model') or 'deepseek-chat'
        content = None
        if fmt == 'anthropic':
            # Anthropic Messages 协议
            base = (raw_url or "https://api.anthropic.com/v1").rstrip('/')
            url = base if base.endswith('/messages') else (base + '/messages')
            resp = httpx.post(
                url,
                headers={"x-api-key": api_key, "Authorization": f"Bearer {api_key}",
                         "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json={"model": model, "max_tokens": 4096,
                      "messages": [{"role": "user", "content": EXTRACT_PROMPT + text}]},
                timeout=120.0
            )
            resp.raise_for_status()
            blocks = resp.json().get("content", [])
            content = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        else:
            # OpenAI 兼容协议
            if raw_url:
                base = raw_url.rstrip('/')
                url = base if base.endswith('/chat/completions') else base + '/chat/completions'
            else:
                url = "https://api.deepseek.com/v1/chat/completions"
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": EXTRACT_PROMPT + text}], "temperature": 0.1},
                timeout=60.0
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(content)
        # 兼容模型偶尔返回 {"memories": [...]} 这种包了一层的格式
        if isinstance(data, dict):
            for key in ("memories", "items", "results", "data"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise ValueError(f"返回的不是 JSON 数组: {type(data).__name__}")
        return [m for m in data if isinstance(m, dict) and m.get("content")]
    except Exception as e:
        # B7 修复：出错返回 None（而不是 []），让调用方区分「失败」和「这批确实没有可记的」
        print(f"DeepSeek error: {e}")
        global LAST_EXTRACT_ERROR
        LAST_EXTRACT_ERROR = str(e)[:500]
        return None

def get_embedding(text, provider=None):
    try:
        base = ((provider or {}).get('base_url') or ALIBABA_BASE_URL).rstrip('/')
        response = httpx.post(
            f"{base}/embeddings",
            headers={"Authorization": f"Bearer {(provider or {}).get('api_key') or ALIBABA_API_KEY}", "Content-Type": "application/json"},
            json={"model": (provider or {}).get('model') or "text-embedding-v3", "input": text, "encoding_format": "float"},
            timeout=30.0
        )
        response.raise_for_status()
        return response.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"Embedding error: {e}")
        return None

def get_l0_ts(source_id):
    """修复5：查 L0 原文时间，作为 L1 的时间戳。

    L1 的 ts 应是对话发生时间，而非提取时间，否则 LLM 会把提取时刻
    误当成事件发生时刻。查不到时 fallback 到当前时间。
    """
    if not source_id:
        return None
    conn = sqlite3.connect(str(SQLITE_PATH))
    try:
        row = conn.execute('SELECT ts FROM l0_messages WHERE id=?', (source_id,)).fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        print(f"  查 L0 时间失败 (source_id={source_id}): {e}")
        return None
    finally:
        conn.close()

def save_l1(memory, conv_id, client, source_id, event_msg_id=None):
    """保存 L1 记忆到数据库和向量库

    Args:
        memory: 提取的记忆内容（dict）
        conv_id: 会话 ID
        client: 客户端标识
        source_id: 批次首条消息 id（用于 source_msg_id，向后兼容）
        event_msg_id: 真正触发提取的消息 id（通常是批次最后一条）
    """
    ts = get_l0_ts(source_id) or datetime.now().isoformat()
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    try:
        c.execute(
            'INSERT INTO l1_memories (content, quote, source_msg_id, conv_id, client, event_type, tags, valence, arousal, status, ts, event_msg_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (memory["content"], memory.get("quote", ""), source_id, conv_id, client, memory.get("event_type", "general"),
             json.dumps(memory.get("tags", []), ensure_ascii=False), memory.get("valence"), memory.get("arousal"), 'active', ts, event_msg_id)
        )
        l1_id = c.lastrowid
        conn.commit()
        embedding = get_embedding(memory["content"], resolve_purpose('embedding'))
        if embedding:
            l1_collection.add(
                ids=[f"l1_{l1_id}"],
                embeddings=[embedding],
                documents=[memory["content"]],
                metadatas=[{
                    "ts": ts,
                    "event_type": memory.get("event_type", "general"),
                    "tags": json.dumps(memory.get("tags", []), ensure_ascii=False),
                    "quote": memory.get("quote", ""),
                    "conv_id": conv_id,
                    "client": client,
                    "is_core": 0,
                    "valence": memory.get("valence") or 0,
                    "arousal": memory.get("arousal") or 0
                }]
            )
            print(f"  Saved: {memory['content'][:50]}")
        return l1_id
    except Exception as e:
        print(f"Save error: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()

def mark_extracted(ids):
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    for mid in ids:
        c.execute('UPDATE l0_messages SET extracted=1 WHERE id=?', (mid,))
    conn.commit()
    conn.close()

def main():
    print(f"[{datetime.now()}] L1 extraction starting...")
    ensure_columns()
    messages = get_unextracted()
    if not messages:
        print("No new messages.")
        return
    print(f"Found {len(messages)} messages.")
    groups = group_by_conv(messages)
    for conv_id, msgs in groups.items():
        text = ""
        ids = []
        for msg in msgs:
            mid, _, role, content, client, ts = msg
            prefix = "你" if role == "user" else "我"
            text += f"[{prefix}] {content}\n"
            ids.append(mid)
        print(f"Processing {conv_id}: {len(msgs)} messages")
        memories = call_deepseek(text, resolve_purpose('l1_extract'))
        if memories is None:
            # B7 修复：调用失败时不再标记为已提取（以前会把这一批永久丢掉），下个周期重试
            dead = mark_failed(ids, LAST_EXTRACT_ERROR or "unknown error")
            print(f"  !! 提取失败，{len(ids)} 条消息保留为未提取，下次重试"
                  + (f"；其中 {dead} 条已连续失败 {MAX_EXTRACT_ATTEMPTS} 次，转为死信(extracted=-1)" if dead else ""))
            continue
        if memories:
            print(f"  Extracted {len(memories)} memories")
            for memory in memories:
                # 批次首条作为 source_id（向后兼容），批次最后一条作为 event_msg_id（真正触发提取的消息）
                save_l1(memory, conv_id, msgs[0][4], msgs[0][0], event_msg_id=msgs[-1][0])
        else:
            print("  No memories")
        mark_extracted(ids)
    print(f"[{datetime.now()}] Complete.")
    # B13：叙事自动更新只由记忆服务的 APScheduler 负责（check_and_update_narrative），
    # 这里不再重复触发（以前两套触发阈值和比较方式不同，可能并发生成、版本互相覆盖）。

def resolve_purpose(purpose):
    """按模块配置（model_configs→providers）解析：返回 {base_url, api_key, model} 或 None（回退 .env）"""
    try:
        import sqlite3 as _sq
        import json as _js
        import os as _os
        # B11：与文件顶部统一使用 DATA_DIR（以前这里读 MEMORY_DATA_DIR，和其余代码不一致）
        _db = str(SQLITE_PATH)
        if not _os.path.exists(_db):
            return None
        conn = _sq.connect(_db)
        c = conn.cursor()
        row = c.execute("SELECT provider_id, model_name FROM model_configs WHERE purpose=? AND enabled=1", (purpose,)).fetchone()
        if not row:
            conn.close()
            return None
        prov = c.execute("SELECT base_url, api_key_enc, api_format FROM providers WHERE id=?", (row[0],)).fetchone()
        conn.close()
        if not prov:
            return None
        key = _os.environ.get('CONFIG_SECRET_KEY', '')
        api = prov[1] or ''
        if key and api:
            try:
                from cryptography.fernet import Fernet
                api = Fernet(key.encode()).decrypt(api.encode()).decode()
            except Exception:
                pass
        return {'base_url': prov[0], 'api_key': api, 'model': row[1], 'api_format': prov[2] or 'openai'}
    except Exception as e:
        print(f"resolve_purpose error: {e}")
        return None

if __name__ == "__main__":
    main()
