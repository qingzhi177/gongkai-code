"""L0 原文的手动修改（Dashboard「L0原文」→ 每条消息旁边的「修改」，接口 PUT /l0/{id}/content）。

为什么要有：以前 L0 不能改，有零碎的错误（错字、标点、颜文字）只能整段重新导入。

两种改法：
- 只改字（fix，默认）：错字、标点、颜文字这种不改意思的修正。不调用模型：
  原文改掉；别的地方一字不差引用过这一段的（记忆的原话、每日片段、日摘、叙事卷、章节、关系状态、未完的事、思考记录里的回复、
  旧版的共同经历叙事和近期摘要），把引用的那一小段一起改掉（带着前后几个字去找，不会改到别处）。
- 重新提取（reextract）：意思改了的时候用。这条消息所在那一批的记忆作废、重新提取；那一天的每日片段整天重新整理
  （日摘、章节这些会跟着更新）。
每次修改都记在 l0_edits（改之前、改之后、连带改了哪些地方），可以看记录、改回去。

前端（Pluvia / Kelivo）手里还是改之前的原文。下次带着原文来存记录时，序列对齐按改之前的原文也认得是这一条（aliases()），
分支会话的重合部分也认（old_contents()），不会把修改冲掉，也不会当成新消息再存一遍。
"""
import difflib
import json
import re
import sqlite3

import timeutil

MODES = {"fix": "只改字", "reextract": "重新提取"}
MAX_LEN = 100000
CONTEXTS = (8, 5, 3, 2)      # 找引用时带上前后几个字，从多到少试
MIN_MATCH = 4                # 拿去找的那一段至少这么长，太短容易改到别处
MERGE_GAP = 8                # 两处改动隔得比这近，就当成一处

# 只改字时，连带检查这些地方：(表, 普通文字列, JSON 列, 条件)
TARGETS = [
    ("l1_memories", ("content", "quote"), (), "status='active'"),
    ("episodes", ("body", "body_with_refs"), (), "status='active'"),
    ("digests", ("body", "body_with_refs"), (), "status='active'"),
    ("arcs", ("summary",), ("sections",), None),
    ("chapters", ("summary", "abstract"), ("sections",), None),
    ("relationship_states", (), ("lines",), "status='active'"),
    ("open_loops", ("text", "created_quote", "closed_quote", "closed_text"), (), None),
    ("loop_events", ("quote", "detail"), (), None),
    ("thinking_records", ("full_reply",), (), None),
    ("shared_narrative", ("content",), (), "status='active'"),
    ("recent_summary", ("content",), (), "status='active'"),
]
TARGET_CN = {"l1_memories": "记忆", "episodes": "每日片段", "digests": "日摘", "arcs": "叙事卷", "chapters": "章节",
             "relationship_states": "关系状态", "open_loops": "未完的事", "loop_events": "未完的事的经过",
             "thinking_records": "思考记录", "shared_narrative": "旧版共同经历叙事", "recent_summary": "近期摘要"}


def cmp_key(role, content):
    """和序列对齐（app/l0_merge.py）用同一个比较键：角色 + 内容，忽略空白和图片占位"""
    from app.l0_merge import cmp_key as k
    return k(role, content)


# ---------- 找出改了哪几段 ----------

def segments(old, new):
    """逐字比较改之前和改之后，每一处改动给出几种「带前后文的旧片段 → 新片段」（前后文从多到少）。
    挨得近的几处改动（中间隔着不到 MERGE_GAP 个字）合成一处，这样消息末尾加个标点这种改动也找得到。"""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    regions = []
    for tag, a1, a2, b1, b2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if regions and a1 - regions[-1][1] < MERGE_GAP:
            regions[-1] = (regions[-1][0], a2, regions[-1][2], b2)
        else:
            regions.append((a1, a2, b1, b2))
    out = []
    for a1, a2, b1, b2 in regions:
        alts = []
        for k in CONTEXTS:
            left = 0
            while left < k and a1 - left > 0 and b1 - left > 0 and old[a1 - left - 1] == new[b1 - left - 1]:
                left += 1
            right = 0
            while right < k and a2 + right < len(old) and b2 + right < len(new) and old[a2 + right] == new[b2 + right]:
                right += 1
            o, n = old[a1 - left:a2 + right], new[b1 - left:b2 + right]
            if len(o) >= MIN_MATCH and o not in [x[0] for x in alts]:
                alts.append((o, n))
        if alts:
            out.append(alts)
    return out


def apply(text, segs):
    """在一段文字里替换引用到的旧片段；返回 (新文字, 改了几处)"""
    if not isinstance(text, str) or not text:
        return text, 0
    n = 0
    for alts in segs:
        for o, new in alts:
            if o in text:
                n += text.count(o)
                text = text.replace(o, new)
                break
    return text, n


def _apply_json(value, segs):
    if isinstance(value, str):
        return apply(value, segs)
    if isinstance(value, list):
        out, n = [], 0
        for v in value:
            v2, k = _apply_json(v, segs)
            out.append(v2)
            n += k
        return out, n
    if isinstance(value, dict):
        out, n = {}, 0
        for key, v in value.items():
            v2, k = _apply_json(v, segs)
            out[key] = v2
            n += k
        return out, n
    return value, 0


def _cols(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def sync_quotes(conn, old, new):
    """把各处引用到的旧片段换成新的。返回 ({表: [id]}, 内容改了的记忆 id（要重算向量）)"""
    segs = segments(old, new)
    synced, l1_changed = {}, []
    if not segs:
        return synced, l1_changed
    for table, text_cols, json_cols, where in TARGETS:
        have = _cols(conn, table)
        tcols = [c for c in text_cols if c in have]
        jcols = [c for c in json_cols if c in have]
        if not (tcols or jcols) or "id" not in have:
            continue
        cols = tcols + jcols
        sql = f'SELECT id, {", ".join(cols)} FROM "{table}"' + (f" WHERE {where}" if where else "")
        for row in conn.execute(sql).fetchall():
            rid, vals = row[0], dict(zip(cols, row[1:]))
            updates = {}
            for c in tcols:
                v2, k = apply(vals[c], segs)
                if k:
                    updates[c] = v2
            for c in jcols:
                if not vals[c]:
                    continue
                try:
                    data = json.loads(vals[c])
                except (TypeError, ValueError):
                    continue
                d2, k = _apply_json(data, segs)
                if k:
                    updates[c] = json.dumps(d2, ensure_ascii=False)
            if updates:
                conn.execute(f'UPDATE "{table}" SET {", ".join(f"{c}=?" for c in updates)} WHERE id=?', [*updates.values(), rid])
                synced.setdefault(table, []).append(rid)
                if table == "l1_memories" and "content" in updates:
                    l1_changed.append(rid)
    return synced, l1_changed


# ---------- 改 ----------

def edit(l0_id, new_content, mode="fix", cascade=None, now=None):
    """改一条 L0。cascade(cursor, conv_id, [l0_id]) → 作废了的记忆 id（重新提取时由 main 传入，作废那一批的记忆、让那一批重新提取）。
    返回 {status, mode, synced: {表: 几处}, l1_changed, l1_purged, resettle, edit_id}"""
    if mode not in MODES:
        raise ValueError("改法只能是「只改字」或「重新提取」")
    new_content = str(new_content if new_content is not None else "")
    if not new_content.strip():
        raise ValueError("内容不能是空的（想删掉这条，用「删除」）")
    if len(new_content) > MAX_LEN:
        raise ValueError(f"内容太长了（最多 {MAX_LEN} 字）")
    from app.config import get_conn       # 用到时才导入：分支会话（app/branches.py）也用这个模块，它的测试不读配置
    now = now or timeutil.utc_now_str()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute("SELECT id, conv_id, role, content, ts, status, extracted FROM l0_messages WHERE id=?", (int(l0_id),)).fetchone()
        if not r:
            raise ValueError("没有这条消息")
        if r[5] != "active":
            raise ValueError("这条消息已经不是现在的版本了（被重新生成或者删掉了），不能改")
        old = r[3] or ""
        if new_content == old:
            raise ValueError("内容没有变")
        conn.execute("UPDATE l0_messages SET content=? WHERE id=?", (new_content, r[0]))
        synced, l1_changed, purged = {}, [], []
        if mode == "fix":
            synced, l1_changed = sync_quotes(conn, old, new_content)
        elif cascade and r[6] == 1:
            purged = list(cascade(conn.cursor(), r[1], [r[0]]) or [])
        eid = conn.execute("INSERT INTO l0_edits (l0_id, conv_id, old_content, new_content, mode, synced, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (r[0], r[1], old, new_content, mode, json.dumps({**synced, **({"l1_purged": purged} if purged else {})}), now)).lastrowid
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    resettle = []
    if mode == "reextract":
        from app.pipeline import episodes as E
        day = E.day_of(r[4]) if r[4] else None
        if day:
            resettle = E.enqueue_days([day.isoformat()], source="dashboard", skip_dead=False, force=True)
    return {"status": "ok", "edit_id": eid, "mode": mode, "synced": {t: len(ids) for t, ids in synced.items()},
            "synced_cn": "、".join(f"{TARGET_CN.get(t, t)} {len(ids)} 处" for t, ids in synced.items()),
            "l1_changed": l1_changed, "l1_purged": purged, "resettle": resettle}


def history(l0_id):
    from app.config import get_conn
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, old_content, new_content, mode, synced, created_at FROM l0_edits WHERE l0_id=? ORDER BY id DESC",
                            (int(l0_id),)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out = []
    for eid, old, new, mode, synced, at in rows:
        try:
            s = json.loads(synced or "{}")
        except ValueError:
            s = {}
        out.append({"id": eid, "old_content": old, "new_content": new, "mode": mode, "mode_cn": MODES.get(mode, mode),
                    "synced": {t: len(v) for t, v in s.items() if isinstance(v, list)}, "created_at": at})
    return out


def counts(conn, ids):
    """{l0_id: 改过几次}"""
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    try:
        return dict(conn.execute(f"SELECT l0_id, COUNT(*) FROM l0_edits WHERE l0_id IN ({','.join('?' * len(ids))}) GROUP BY l0_id",
                                 ids).fetchall())
    except sqlite3.OperationalError:
        return {}


# ---------- 前端带着改之前的原文来时，认出是同一条 ----------

def aliases(c, conv_id):
    """{改之前的比较键: 现在的比较键}（这个会话里还有效、改过的消息）"""
    try:
        rows = c.execute("SELECT e.old_content, m.role, m.content FROM l0_edits e JOIN l0_messages m ON m.id = e.l0_id "
                         "WHERE m.conv_id=? AND m.status='active'", (conv_id,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for old, role, cur in rows:
        a, b = cmp_key(role, old), cmp_key(role, cur)
        if a != b:
            out[a] = b
    return out


def old_contents(c, conv_id):
    """{l0_id: [改之前的内容, ...]}（分支会话比较重合部分时用）"""
    try:
        rows = c.execute("SELECT e.l0_id, e.old_content FROM l0_edits e JOIN l0_messages m ON m.id = e.l0_id "
                         "WHERE m.conv_id=? AND m.status='active'", (conv_id,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for lid, old in rows:
        out.setdefault(lid, []).append(old)
    return out
