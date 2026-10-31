"""分支会话（Pluvia「从这里分出去」，接入约定第 12 节）。

前端把「到某一行为止」的历史复制成一段新会话（新的 conversation_id）。在分支里说话时，请求里带着那段和原会话
一模一样的历史。不处理的话，这段历史会以新会话的名义再存一遍、再提取一遍，同一段经历就有两份记忆。

做法：
- 第一次看到分支（save_conversation 带 branch_of）时，把请求里的历史和原会话的完整历史逐条对比，
  开头连续一样的那段就是「重合部分」。记下重合部分每条消息的指纹（角色 + 去掉换行后的内容，取哈希），
  以及它在原会话里对应哪一行。
- 以后这个分支每次存记录，先去掉开头和这串指纹一样的部分，只存分支以后的新内容。
- 指纹是分支那一刻记下的：原会话后来改了、删了都不影响。删原会话时如果勾了「同时删除记忆」，
  重合部分的记忆跟着原会话一起没了，分支也不会再把它存回来。
- 分支的分支也一样：一个会话的完整历史 = 它自己的重合部分 + 它自己存的记录。
- 提取分支开头的新内容时，从分支点往前取几句当只读上文（extract_l1.py 用 anchor()）。

挑选式搬家（接入约定第 14 节，forged_from）也用这一套，kind='forged'：前端从旧会话里挑几轮带进新会话，
带过来的几段在原会话里可以不连续。登记时按顺序在原会话的完整历史里往后找（每条找它之后第一条一样的），
找不到的那条和它之后的都算新会话自己的内容。之后和分支一样：带过来的只在原会话名下存一份。

前端发的是滑动窗口（最近 N 条，攒够几条一起往后挪）：分支 / 搬家的对话聊长了以后，开头那段重合的会滑出窗口，
请求里的历史从重合部分的中间开始，甚至一开始（从很长的原会话分出来时）就只带着重合部分的后一截。所以对齐时不要求
从第一条开始：窗口开头那一串，和重合部分（登记时是原会话的历史）里连续的一段一样，就算重合。

这个模块只用标准库，调用方传入数据库连接（main.py 的 save_conversation、extract_l1.py 都用它）。
"""
import hashlib
import json
import re
import sqlite3


def fingerprint(role, content):
    """和 L0 序列对齐用的比较键一致：角色 + 内容（忽略换行差异）"""
    key = (role or "") + "\x00" + re.sub(r"[\r\n]", "", content or "")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def get(c, conv_id):
    """{parent, parent_turn_id, prefix: [[指纹, 会话, 行 id], ...], shared, created_at, kind}；不是分支 / 搬家返回 None"""
    try:
        row = c.execute("SELECT parent_conv_id, parent_turn_id, prefix, shared, created_at, kind FROM conv_branches "
                        "WHERE conv_id=?", (conv_id,)).fetchone()
    except sqlite3.OperationalError:     # 表还没建，或者还没有 kind 列（迁移前）
        try:
            row = c.execute("SELECT parent_conv_id, parent_turn_id, prefix, shared, created_at, 'branch' FROM conv_branches "
                            "WHERE conv_id=?", (conv_id,)).fetchone()
        except sqlite3.OperationalError:
            return None
    if not row:
        return None
    try:
        prefix = json.loads(row[2] or "[]")
    except ValueError:
        prefix = []
    return {"conv_id": conv_id, "parent": row[0], "parent_turn_id": row[1], "prefix": prefix,
            "shared": row[3], "created_at": row[4], "kind": row[5] or "branch"}


def history(c, conv_id):
    """一个会话的完整历史：[[指纹, 存在哪个会话, 那一行的 id(, 改之前的指纹列表)], ...]（分支 = 重合部分 + 自己存的 active 记录）。
    在 Dashboard 上改过的消息多带一个「改之前的指纹」：前端手里还是改之前的原文，也要认得是同一条。"""
    from app import l0_edit
    b = get(c, conv_id)
    out = list(b["prefix"]) if b else []
    olds = l0_edit.old_contents(c, conv_id)
    rows = c.execute("SELECT id, role, content FROM l0_messages WHERE conv_id=? AND status='active' "
                     "ORDER BY msg_idx ASC, id ASC", (conv_id,)).fetchall()
    for r in rows:
        e = [fingerprint(r[1], r[2]), conv_id, r[0]]
        if r[0] in olds:
            e.append([fingerprint(r[1], o) for o in olds[r[0]]])
        out.append(e)
    return out


def _matches(fp, e):
    return fp == e[0] or (len(e) > 3 and fp in (e[3] or []))


def _align(msgs, entries, to_end=False):
    """窗口开头那一串和 entries 里哪一段连续一样 → (起点 j, 条数 n)；对不上返回 (0, 0)。
    从第一条开始对得上的（j=0）照旧算；从中间开始的（窗口滑过去了）要连着对上至少 2 条，或者一直对到 entries 的最后一条
    （to_end：重合部分的结尾——窗口里重合的那截后面紧接着就是新会话自己的内容）。一样长时取靠前的起点。
    窗口开头误认几条只会少存几条本来就存过的（新的话总在窗口最后），所以这里宁可多认。"""
    if not msgs or not entries:
        return 0, 0
    fps = [fingerprint(r, t) for r, t in msgs]
    best = (0, 0)
    for j in range(len(entries)):
        if not _matches(fps[0], entries[j]):
            continue
        n = 1
        while n < len(fps) and j + n < len(entries) and _matches(fps[n], entries[j + n]):
            n += 1
        ok = j == 0 or n >= 2 or (to_end and j + n == len(entries))
        if ok and n > best[1]:
            best = (j, n)
    return best


def _picked(msgs, entries):
    """挑选式搬家：msgs 开头那些在原会话历史里按顺序找得到的（可以不连续）→ 对应的历史条目。
    每条从上一条对上的位置往后找第一条一样的；有一条找不到，它和它后面的都算新会话自己的。"""
    out, pos = [], 0
    for role, content in msgs:
        fp = fingerprint(role, content)
        k = next((j for j in range(pos, len(entries)) if _matches(fp, entries[j])), None)
        if k is None:
            break
        out.append(entries[k])
        pos = k + 1
    return out


def register(c, conv_id, branch_of, msgs, now, kind="branch"):
    """第一次看到分支 / 搬家时登记；已经登记过的不再改（重合部分以第一次为准）。
    msgs：这次请求里归一化后、去掉 system 和空消息的 [(role, content)]。返回登记记录，不是分支返回 None。
    kind：branch（开头连续一段和原会话一样）/ forged（挑选式搬家，带过来的几段在原会话里可以不连续）。"""
    b = get(c, conv_id)
    if b is not None:
        return b
    branch_of = branch_of if isinstance(branch_of, dict) else {}
    parent = str(branch_of.get("conversation_id") or "").strip()
    if not parent or parent == conv_id:
        return None
    entries = history(c, parent)
    if kind == "forged":
        # 这一轮（最后一条用户消息和它之后的）一定是新说的；前端说了带过来几条（carried）的，最多认这么多
        last_user = max((i for i, (role, _) in enumerate(msgs) if role == "user"), default=len(msgs))
        cand = msgs[:last_user]
        try:
            carried = int(branch_of.get("carried"))
            cand = cand[:max(0, carried)]
        except (TypeError, ValueError):
            pass
        shared = _picked(cand, entries)
    else:
        # 从很长的原会话分出来时，第一次请求里可能已经只带着后一截（窗口装不下全部）：重合部分 = 原会话到对上的最后一条为止
        j, n = _align(msgs, entries)
        shared = entries[:j + n] if n else []
    turn_id = branch_of.get("turn_id")
    try:
        c.execute("INSERT OR IGNORE INTO conv_branches (conv_id, parent_conv_id, parent_turn_id, prefix, shared, created_at, kind) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (conv_id, parent, None if turn_id is None else str(turn_id)[:200], json.dumps(shared), len(shared), now,
                   "forged" if kind == "forged" else "branch"))
    except sqlite3.OperationalError:     # 还没有 kind 列（迁移前）
        c.execute("INSERT OR IGNORE INTO conv_branches (conv_id, parent_conv_id, parent_turn_id, prefix, shared, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?)",
                  (conv_id, parent, None if turn_id is None else str(turn_id)[:200], json.dumps(shared), len(shared), now))
    return get(c, conv_id)


def shared_len(c, conv_id, msgs):
    """这次请求开头有几条是和原会话重合的（存记录时跳过）"""
    b = get(c, conv_id)
    if not b or not b["prefix"]:
        return 0
    # 窗口往后滑了以后，请求里的历史从重合部分的中间开始（开头几条已经不在窗口里）
    return _align(msgs, b["prefix"], to_end=True)[1]


def anchor(c, conv_id):
    """分支点：重合部分最后一条实际存在哪个会话、哪一行 → (会话, 行 id)；没有重合返回 None"""
    b = get(c, conv_id)
    if not b or not b["prefix"]:
        return None
    last = b["prefix"][-1]
    return last[1], last[2]
