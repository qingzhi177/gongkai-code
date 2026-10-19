import sqlite3
import os
import json
import re
import difflib
import unicodedata
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
import timeutil  # noqa: E402  存储一律 UTC，给模型看时换算成用户时区
import db  # noqa: E402  统一的数据库连接 + 迁移
from app import branches as conv_branches  # noqa: E402  分支会话的上文
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

10. 对话每行开头的 [#数字] 是行号。标着【只读上下文】的部分只用来理解前因后果，
   不要从中提取记忆；只从【待提取】部分提取。
   每条记忆在 msg_refs 里写出它依据的行号（1~3 个，必须是【待提取】部分的行号）。
   如果对话没有行号，msg_refs 写 []。

11. quote 必须从 msg_refs 指向的那几行里逐字复制，不要改写、不要把不同行拼在一起。

12. author 表示这条记忆"是谁的"：
   - user_said：她说的话、她的事、她的偏好
   - ai_said：我说过的话、我答应过的事
   - co_created：我们一起形成的约定、梗、称呼、共同经历
   - ai_felt：我的感受（通常对应 event_type=feel）

输出JSON格式：
[
  {
    "content": "记忆内容（情感类用原句，信息类简短概括）",
    "quote": "原文中最有质感的片段（逐字复制）",
    "event_type": "类型",
    "tags": ["具体的词"],
    "valence": 0.5,
    "arousal": 0.7,
    "msg_refs": [3, 4],
    "author": "user_said"
  }
]

如果对话中没有值得记住的内容（纯寒暄），返回：[]

对话内容：
'''

LAST_EXTRACT_ERROR = ""
MAX_EXTRACT_ATTEMPTS = 3  # 连续失败达到这个次数 → extracted=-1（死信），不再自动重试


def ensure_columns():
    """表结构由迁移统一管理（db.py + migrations/）。cron 可能比记忆服务先跑，所以这里也执行一次；
    已经执行过的迁移不会重复执行，平时这一步什么都不做。"""
    db.migrate(SQLITE_PATH)


def mark_failed(ids, error):
    """B7：提取失败 → 不标记已提取，只累加失败次数；达到上限转为死信（extracted=-1）"""
    conn = db.connect(SQLITE_PATH)
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
    conn = db.connect(SQLITE_PATH)
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
    conn = db.connect(SQLITE_PATH)
    try:
        row = conn.execute('SELECT ts FROM l0_messages WHERE id=?', (source_id,)).fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        print(f"  查 L0 时间失败 (source_id={source_id}): {e}")
        return None
    finally:
        conn.close()

def save_l1(memory, conv_id, client, source_id, event_msg_id=None, evidence_ids=None,
            evidence_status=None, ts=None):
    """保存 L1 记忆到数据库和向量库

    Args:
        memory: 提取的记忆内容（dict，quote 已经过校验/纠正）
        conv_id: 会话 ID
        client: 客户端标识
        source_id: 分段首条消息 id（source_msg_id，版本切换级联按它定位"提取批次"，语义不能变）
        event_msg_id: 最后一条依据消息的 id（叙事校验用它取原文）
        evidence_ids: 这条记忆真正依据的 L0 id 列表（v2：来自 msg_refs）
        evidence_status: verified / verified_fuzzy / unverified / no_quote
        ts: 记忆时间（v2：第一条依据消息的时间；缺省用分段首条消息的时间）
    """
    ts = ts or get_l0_ts(source_id) or timeutil.utc_now_str()
    author = memory.get("author")
    if author not in ("user_said", "ai_said", "co_created", "ai_felt"):
        author = None
    conn = db.connect(SQLITE_PATH)
    c = conn.cursor()
    try:
        c.execute(
            'INSERT INTO l1_memories (content, quote, source_msg_id, conv_id, client, event_type, tags, valence, arousal, '
            'status, ts, event_msg_id, evidence_json, evidence_status, author) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (memory["content"], memory.get("quote", ""), source_id, conv_id, client, memory.get("event_type", "general"),
             json.dumps(memory.get("tags", []), ensure_ascii=False), memory.get("valence"), memory.get("arousal"),
             'active', ts, event_msg_id,
             json.dumps(evidence_ids) if evidence_ids else None, evidence_status, author)
        )
        l1_id = c.lastrowid
        conn.commit()
        embedding = get_embedding(memory["content"], resolve_purpose('embedding'))
        if embedding:
            l1_collection.add(ids=[f"l1_{l1_id}"], embeddings=[embedding], documents=[memory["content"]])
        else:
            print("  （向量生成失败，记忆服务的定时对账会自动补上）")
        print(f"  Saved[{evidence_status}]: {memory['content'][:50]}")
        return l1_id
    except Exception as e:
        print(f"Save error: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def mark_extracted(ids):
    conn = db.connect(SQLITE_PATH)
    c = conn.cursor()
    for mid in ids:
        c.execute('UPDATE l0_messages SET extracted=1 WHERE id=?', (mid,))
    conn.commit()
    conn.close()

# ================= 提取流水线 v2：分段 · 行号 · 证据 · 原句校验 =================
QUIET_MINUTES = int(os.getenv("EXTRACT_QUIET_MINUTES", 10))       # 会话安静这么久之后才提取（避免聊到一半就提取）
BACKLOG_MINUTES = int(os.getenv("EXTRACT_BACKLOG_MINUTES", 60))   # 但最早的未提取消息超过这么久，就不再等
GAP_MINUTES = int(os.getenv("EXTRACT_GAP_MINUTES", 30))           # 相邻消息间隔超过这么久，切成新的一段
MAX_SEG_MESSAGES = int(os.getenv("EXTRACT_MAX_SEG_MESSAGES", 40))  # 每段最多多少条（超出后在下一条 user 消息处切开）
CONTEXT_MESSAGES = 6                                              # 每段前面附带几条只读上下文
MAX_SEGMENTS_PER_RUN = int(os.getenv("EXTRACT_MAX_SEGMENTS_PER_RUN", 6))


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "").replace("T", " ")[:19])
    except ValueError:
        return None


def get_pending_segments(force=False):
    """按会话取未提取消息并切段。返回 [(conv_id, client, context_rows, segment_rows)]，row=(id, role, content, ts)

    - 只处理"已经安静了 QUIET_MINUTES 分钟"的会话；最早的未提取消息超过 BACKLOG_MINUTES 时不再等。force=True 忽略等待
    - 同一会话按 id 顺序（与版本切换级联的批次定位一致），相邻间隔 > GAP_MINUTES 或段长达到上限时切段
    """
    conn = db.connect(SQLITE_PATH)
    try:
        convs = conn.execute(
            "SELECT conv_id, MIN(ts), MAX(ts) FROM l0_messages WHERE extracted=0 AND status='active' GROUP BY conv_id"
        ).fetchall()
        segments = []
        for conv_id, oldest_ts, _ in convs:
            if not force:
                last_ts = conn.execute(
                    "SELECT MAX(ts) FROM l0_messages WHERE conv_id=? AND status='active'", (conv_id,)
                ).fetchone()[0]
                # datetime() 统一归一化时间格式（L0 的 ts 可能是 "YYYY-MM-DD HH:MM:SS" 也可能是带 T / Z 的 ISO 串）
                quiet = conn.execute("SELECT datetime(?) <= datetime('now', ?)", (last_ts, f"-{QUIET_MINUTES} minutes")).fetchone()[0]
                backlog = conn.execute("SELECT datetime(?) <= datetime('now', ?)", (oldest_ts, f"-{BACKLOG_MINUTES} minutes")).fetchone()[0]
                if not (quiet or backlog):
                    continue
            rows = conn.execute(
                "SELECT id, role, content, ts, client FROM l0_messages "
                "WHERE conv_id=? AND extracted=0 AND status='active' ORDER BY id ASC", (conv_id,)
            ).fetchall()
            if not rows:
                continue
            client = rows[0][4]
            cur = []
            for r in rows:
                row = r[:4]
                if cur:
                    prev_t, this_t = _parse_ts(cur[-1][3]), _parse_ts(row[3])
                    gap = (this_t - prev_t).total_seconds() / 60 if (prev_t and this_t) else 0
                    too_long = len(cur) >= MAX_SEG_MESSAGES and row[1] == "user"
                    if gap > GAP_MINUTES or too_long or len(cur) >= MAX_SEG_MESSAGES * 2:
                        segments.append((conv_id, client, cur))
                        cur = []
                cur.append(row)
            if cur:
                segments.append((conv_id, client, cur))
        out = []
        for conv_id, client, seg in segments[:MAX_SEGMENTS_PER_RUN]:
            ctx = conn.execute(
                "SELECT id, role, content, ts FROM l0_messages WHERE conv_id=? AND status='active' AND id < ? "
                "ORDER BY id DESC LIMIT ?", (conv_id, seg[0][0], CONTEXT_MESSAGES)
            ).fetchall()[::-1]
            if len(ctx) < CONTEXT_MESSAGES:
                # 分支会话的开头：自己前面没几句，从原会话的分支点往前补上文（app/branches.py）
                anc = conv_branches.anchor(conn, conv_id)
                if anc:
                    ctx = conn.execute(
                        "SELECT id, role, content, ts FROM l0_messages WHERE conv_id=? AND status='active' AND id <= ? "
                        "ORDER BY id DESC LIMIT ?", (anc[0], anc[1], CONTEXT_MESSAGES - len(ctx))
                    ).fetchall()[::-1] + ctx
            out.append((conv_id, client, ctx, seg))
        return out
    finally:
        conn.close()


def build_segment_text(ctx, seg):
    """带行号的提取文本。返回 (text, line_map)，line_map: 行号 → (l0_id, content)，只含待提取部分"""
    lines, line_map, n = [], {}, 1
    # 这段对话的时间（用户时区）：提示词第 9 条要求记忆里用具体日期，模型得知道「今天」是哪天
    if seg:
        t0 = timeutil.local_str(seg[0][3])
        t1 = timeutil.local_str(seg[-1][3])
        if t0:
            span = t0 if (not t1 or t1 == t0) else t0 + " 至 " + (t1[11:] if t1[:10] == t0[:10] else t1)
            lines.append(f"【这段对话发生在 {span}（{timeutil.USER_TZ_LABEL}）。对话里的「今天」「昨晚」指这个时间附近；"
                         f"记忆里要写时间，就换算成具体日期】")
    if ctx:
        lines.append("【只读上下文：仅供理解，不要从这里提取】")
        for mid, role, content, ts in ctx:
            lines.append(f"[#{n}][{'你' if role == 'user' else '我'}] {content}")
            n += 1
        lines.append("【待提取】")
    for mid, role, content, ts in seg:
        lines.append(f"[#{n}][{'你' if role == 'user' else '我'}] {content}")
        line_map[n] = (mid, content)
        n += 1
    return "\n".join(lines) + "\n", line_map


def _norm(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


def verify_quote(quote, ref_texts, all_texts):
    """quote 校验（B9）：归一化后是原文子串 → verified；否则在原文里找最相近的片段，相似度 ≥ 0.9 → 用原文片段替换
    （verified_fuzzy）；都不行 → 清空 quote（unverified）。返回 (quote, status, matched_text)"""
    q = (quote or "").strip()
    if not q:
        return "", "no_quote", None
    nq = _norm(q)
    if not nq:
        return "", "no_quote", None
    for t in list(ref_texts) + list(all_texts):
        if nq in _norm(t):
            return q, "verified", t
    best = (0.0, None, None)
    for t in (ref_texts or all_texts):
        L = len(q)
        if len(t) < max(4, L // 2):
            continue
        for i in range(0, max(1, len(t) - L + 1)):
            w = t[i:i + L]
            sm = difflib.SequenceMatcher(None, q, w)
            if sm.quick_ratio() < 0.9:
                continue
            r = sm.ratio()
            if r > best[0]:
                best = (r, w, t)
    if best[0] >= 0.9:
        return best[1], "verified_fuzzy", best[2]
    return "", "unverified", None


EXTRACT_LOCK = DATA_DIR / "run" / "extract.lock"


def _acquire_extract_lock():
    """同一时间只允许一个提取（worker、手动运行脚本都走这里），防止同一段对话被提取两次"""
    import fcntl
    EXTRACT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(EXTRACT_LOCK, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def main(force=False):
    """提取一轮，返回结果摘要（由 worker 记进任务表）。平时由 worker 执行，也可以手动运行：python extract_l1.py [--force]"""
    lock = _acquire_extract_lock()
    if lock is None:
        print(f"[{datetime.now()}] 已有提取在进行，这次跳过")
        return {"status": "skipped", "message": "已有提取在进行"}
    try:
        return _extract_round(force)
    finally:
        lock.close()


def _extract_round(force):
    print(f"[{datetime.now()}] L1 extraction starting (v2{', force' if force else ''})...")
    ensure_columns()
    segments = get_pending_segments(force=force)
    if not segments:
        print("No segments ready.")
        return {"status": "ok", "segments": 0, "memories": 0, "failed_segments": 0}
    summary = {"status": "ok", "segments": len(segments), "memories": 0, "failed_segments": 0, "errors": []}
    for conv_id, client, ctx, seg in segments:
        ids = [r[0] for r in seg]
        text, line_map = build_segment_text(ctx, seg)
        print(f"Processing {conv_id}: {len(seg)} messages (+{len(ctx)} context)")
        memories = call_deepseek(text, resolve_purpose('l1_extract'))
        if memories is None:
            # B7：调用失败时不标记已提取，下个周期重试；连续失败转死信
            err = LAST_EXTRACT_ERROR or "unknown error"
            dead = mark_failed(ids, err)
            summary["failed_segments"] += 1
            if str(err)[:300] not in summary["errors"]:
                summary["errors"].append(str(err)[:300])
            print(f"  !! 提取失败，{len(ids)} 条消息保留为未提取，下次重试"
                  + (f"；其中 {dead} 条已连续失败 {MAX_EXTRACT_ATTEMPTS} 次，转为死信(extracted=-1)" if dead else ""))
            continue
        seg_texts = [r[2] for r in seg]
        ts_by_id = {r[0]: r[3] for r in seg}
        stats = {}
        for memory in memories:
            refs = memory.get("msg_refs") or []
            ref_ids, ref_texts = [], []
            for x in refs if isinstance(refs, list) else []:
                try:
                    k = int(x)
                except (TypeError, ValueError):
                    continue
                if k in line_map and line_map[k][0] not in ref_ids:
                    ref_ids.append(line_map[k][0])
                    ref_texts.append(line_map[k][1])
            quote, status, matched = verify_quote(memory.get("quote", ""), ref_texts, seg_texts)
            memory["quote"] = quote
            if not ref_ids and matched is not None:
                # 模型没给（或给错了）行号，但原句能在某一行里找到 → 用那一行作为证据
                for mid, content in line_map.values():
                    if content == matched:
                        ref_ids = [mid]
                        break
            stats[status] = stats.get(status, 0) + 1
            first_ts = ts_by_id.get(ref_ids[0]) if ref_ids else None
            save_l1(memory, conv_id, client, ids[0],
                    event_msg_id=(max(ref_ids) if ref_ids else ids[-1]),
                    evidence_ids=sorted(ref_ids) or None, evidence_status=status, ts=first_ts)
        print(f"  Extracted {len(memories)} memories {stats if stats else ''}")
        summary["memories"] += len(memories)
        mark_extracted(ids)
    print(f"[{datetime.now()}] Complete.")
    # B13：叙事自动更新只由记忆服务的定时检查负责（投递到任务队列），这里不再重复触发。
    if not summary["errors"]:
        summary.pop("errors")
    return summary

def resolve_purpose(purpose):
    """按模块配置（model_configs→providers）解析：返回 {base_url, api_key, model} 或 None（回退 .env）"""
    try:
        import json as _js
        import os as _os
        # B11：与文件顶部统一使用 DATA_DIR（以前这里读 MEMORY_DATA_DIR，和其余代码不一致）
        _db = str(SQLITE_PATH)
        if not _os.path.exists(_db):
            return None
        conn = db.connect(_db)
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
    import sys as _sys
    main(force="--force" in _sys.argv)
