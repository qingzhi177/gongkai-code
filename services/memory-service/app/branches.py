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
    """{parent, parent_turn_id, prefix: [[指纹, 会话, 行 id], ...], shared, created_at}；不是分支返回 None"""
    try:
        row = c.execute("SELECT parent_conv_id, parent_turn_id, prefix, shared, created_at FROM conv_branches "
                        "WHERE conv_id=?", (conv_id,)).fetchone()
    except sqlite3.OperationalError:     # 表还没建（迁移前）
        return None
    if not row:
        return None
    try:
        prefix = json.loads(row[2] or "[]")
    except ValueError:
        prefix = []
    return {"conv_id": conv_id, "parent": row[0], "parent_turn_id": row[1], "prefix": prefix,
            "shared": row[3], "created_at": row[4]}


def history(c, conv_id):
    """一个会话的完整历史：[[指纹, 存在哪个会话, 那一行的 id], ...]（分支 = 重合部分 + 自己存的 active 记录）"""
    b = get(c, conv_id)
    out = list(b["prefix"]) if b else []
    rows = c.execute("SELECT id, role, content FROM l0_messages WHERE conv_id=? AND status='active' "
                     "ORDER BY msg_idx ASC, id ASC", (conv_id,)).fetchall()
    out.extend([fingerprint(r[1], r[2]), conv_id, r[0]] for r in rows)
    return out


def _common(msgs, entries):
    n = 0
    for (role, content), e in zip(msgs, entries):
        if fingerprint(role, content) != e[0]:
            break
        n += 1
    return n


def register(c, conv_id, branch_of, msgs, now):
    """第一次看到分支时登记；已经登记过的不再改（重合部分以第一次为准）。
    msgs：这次请求里归一化后、去掉 system 和空消息的 [(role, content)]。返回登记记录，不是分支返回 None。"""
    b = get(c, conv_id)
    if b is not None:
        return b
    branch_of = branch_of if isinstance(branch_of, dict) else {}
    parent = str(branch_of.get("conversation_id") or "").strip()
    if not parent or parent == conv_id:
        return None
    entries = history(c, parent)
    n = _common(msgs, entries)
    turn_id = branch_of.get("turn_id")
    c.execute("INSERT OR IGNORE INTO conv_branches (conv_id, parent_conv_id, parent_turn_id, prefix, shared, created_at) "
              "VALUES (?, ?, ?, ?, ?, ?)",
              (conv_id, parent, None if turn_id is None else str(turn_id)[:200], json.dumps(entries[:n]), n, now))
    return get(c, conv_id)


def shared_len(c, conv_id, msgs):
    """这次请求开头有几条是和原会话重合的（存记录时跳过）"""
    b = get(c, conv_id)
    if not b or not b["prefix"]:
        return 0
    return _common(msgs, b["prefix"])


def anchor(c, conv_id):
    """分支点：重合部分最后一条实际存在哪个会话、哪一行 → (会话, 行 id)；没有重合返回 None"""
    b = get(c, conv_id)
    if not b or not b["prefix"]:
        return None
    last = b["prefix"][-1]
    return last[1], last[2]
