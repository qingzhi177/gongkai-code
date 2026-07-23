import sqlite3
import os
import json
import httpx
import chromadb
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", "/home/qingzhi/memory-system/data"))
SQLITE_PATH = DATA_DIR / "sqlite" / "memory.db"
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", 8000))
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
ALIBABA_API_KEY = os.getenv("ALIBABA_API_KEY")
ALIBABA_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

chroma_client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
l1_collection = chroma_client.get_or_create_collection(name="l1_memories")

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

def call_deepseek(text):
    try:
        response = httpx.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": EXTRACT_PROMPT + text}], "temperature": 0.1},
            timeout=60.0
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(content)
    except Exception as e:
        print(f"DeepSeek error: {e}")
        return []

def get_embedding(text):
    try:
        response = httpx.post(
            f"{ALIBABA_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {ALIBABA_API_KEY}", "Content-Type": "application/json"},
            json={"model": "text-embedding-v3", "input": text, "encoding_format": "float"},
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

def save_l1(memory, conv_id, client, source_id):
    ts = get_l0_ts(source_id) or datetime.now().isoformat()
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    try:
        c.execute(
            'INSERT INTO l1_memories (content, quote, source_msg_id, conv_id, client, event_type, tags, valence, arousal, status, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (memory["content"], memory.get("quote", ""), source_id, conv_id, client, memory.get("event_type", "general"),
             json.dumps(memory.get("tags", []), ensure_ascii=False), memory.get("valence"), memory.get("arousal"), 'active', ts)
        )
        l1_id = c.lastrowid
        conn.commit()
        embedding = get_embedding(memory["content"])
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
        memories = call_deepseek(text)
        if memories:
            print(f"  Extracted {len(memories)} memories")
            for memory in memories:
                save_l1(memory, conv_id, msgs[0][4], msgs[0][0])
        else:
            print("  No memories")
        mark_extracted(ids)
    print(f"[{datetime.now()}] Complete.")

if __name__ == "__main__":
    main()
