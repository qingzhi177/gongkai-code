"""实时存记录时的序列对齐（POST /save_conversation 里没有 group_id 的消息）。

前端每一轮发来的是「最近 N 条」的窗口（N 可以随时调），网关把这一轮的回复接在后面。这里把窗口和库里这个会话
现有的消息（导入的和实时存的一起，按 msg_idx 排）整体对齐：
- 对得上的：不动。比较时忽略空白和图片占位（「[图片]」「[图片：名字]」）：这一轮发图时消息里只有字，
  下一轮前端的历史里会多一个占位；窗口里多了占位、库里那条没有的，把占位补进库里那条，不算改过。
- 窗口里有、库里没有的：新消息，按位置插进去。
- 窗口里没带到的旧消息：原样保留（窗口可能只是变小了）。
- 同一个位置内容变了（重新生成、编辑）：旧的标成 superseded，新的接上。

保护（以前真出过的问题，见 docs/plan/progress-2026-10.md 10-25 那一节）：
- 只在库里最后一段（窗口长度的两倍再多 TAIL_SLACK 条）里对齐，更早的一律不动。
- 以窗口和库里重合最长的一段为准。这段之前（窗口开头）对不上的，不会把库里更早的消息整段作废：
  窗口开头那几条要么是库里没有的更早的消息（插在前面），要么是写法和库里不一样的同一条；
  只有紧挨着重合段、角色一样、内容很像（改了几个字）的，才当成编辑替换。
- 窗口和库里一条都对不上（比如窗口只带了这一轮），或者只碰巧对上一条很短的话、后面还有一大段对不上：
  新消息接在最后，不作废任何旧消息。
- 时间：新插进来的消息按位置定时间（夹在前后两条之间）；编辑、重新生成、补图片占位都沿用原来那条的时间，
  不会全变成「现在」（以前整段被换掉时，一百多条消息的时间全变成了同一刻）。在最后、内容差得多的（新说的话）用现在。
"""
import difflib
import re
from datetime import timedelta

import timeutil

IMG_RE = re.compile(r"\[图片(?:：[^\]\n]*)?\]")
WS_RE = re.compile(r"\s+")
TAIL_SLACK = 20             # 对齐时看库里最后「窗口长度 × 2 + 这么多」条
SIMILAR = 0.6               # 内容这么像，才算同一条改了几个字
WEAK_CHARS = 20             # 只对上一条、而且这么短：可能是碰巧（「嗯」「晚安」）
WEAK_TRAILING = 2           # 碰巧对上的那条后面，库里还有超过这么多条对不上，就当一条都没对上
SHORT_CONV = 4              # 一条都没对上、库里这个会话不超过这么多条、窗口盖住了整段、第一句很像：当成编辑第一句后重发


def norm(content):
    """比较用：去掉图片占位和所有空白。只发了图的消息（去掉占位就空了）按图的张数比较——
    同一张图，前端历史里可能写「[图片]」，也可能写「[图片：名字]」"""
    raw = content or ""
    s = WS_RE.sub("", IMG_RE.sub("", raw))
    if s:
        return s
    n = len(IMG_RE.findall(raw))
    return "[图片]" * n if n else WS_RE.sub("", raw)


def cmp_key(role, content):
    return f"{role or ''}\x00{norm(content)}"


def has_img(content):
    return bool(IMG_RE.search(content or ""))


def similar(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio() >= SIMILAR


def _fmt(t):
    return t.strftime(timeutil.STORE_FMT)


def ts_between(prev_ts, next_ts, now):
    """插在两条消息之间的新消息的时间：前一条之后 1 秒（不晚于后一条）；在最前面就比后一条早 1 秒；前后都没有就是现在"""
    p = timeutil.parse_ts(prev_ts) if prev_ts else None
    n = timeutil.parse_ts(next_ts) if next_ts else None
    if p:
        t = p + timedelta(seconds=1)
        return _fmt(p if (n and t > n) else t)
    if n:
        return _fmt(n - timedelta(seconds=1))
    return now


def merge(c, conv_id, client, messages, edited=None, now=None):
    """把窗口 messages（[{"role", "content"}]，按顺序）和库里这个会话对齐。c 是调用方事务里的游标。
    edited：Dashboard 上改过的消息 {改之前的比较键: 现在的比较键}（app/l0_edit.aliases）。
    返回 {"saved": 新存了几条, "superseded": [被换下来、提取过的旧 id（要连带作废记忆）], "upgraded": 补了图片占位的条数}"""
    st = {"saved": 0, "superseded": [], "upgraded": 0}
    if not messages:
        return st
    now = now or timeutil.utc_now_str()
    edited = edited or {}
    rows = c.execute("SELECT id, role, content, extracted, ts, msg_idx FROM l0_messages "
                     "WHERE conv_id=? AND status='active' ORDER BY msg_idx, id", (conv_id,)).fetchall()
    cut = max(0, len(rows) - (len(messages) * 2 + TAIL_SLACK))
    prefix, old = rows[:cut], rows[cut:]
    final = [r[0] for r in prefix]
    ts_of = {r[0]: r[4] for r in rows}

    def insert(m, ts):
        cur = c.execute("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status, extracted) "
                        "VALUES (?, -1, ?, ?, ?, ?, 'active', 0)", (conv_id, m["role"], m["content"], ts, client))
        final.append(cur.lastrowid)
        ts_of[cur.lastrowid] = ts
        st["saved"] += 1

    def supersede(r):
        c.execute("UPDATE l0_messages SET status='superseded' WHERE id=?", (r[0],))
        if r[3] == 1:
            st["superseded"].append(r[0])

    def upgrade(r, m):
        # 只在真的一样（不靠 Dashboard 修改的映射）、只是窗口里多了图片占位时补进去，免得把 Dashboard 上的修改冲掉
        if r[2] != m["content"] and has_img(m["content"]) and not has_img(r[2]) and norm(r[2]) == norm(m["content"]):
            c.execute("UPDATE l0_messages SET content=? WHERE id=?", (m["content"], r[0]))
            st["upgraded"] += 1

    def prev_ts():
        return ts_of.get(final[-1]) if final else None

    def next_ts(a):
        return old[a][4] if a < len(old) else None

    ok = [cmp_key(r[1], r[2]) for r in old]
    nk = [edited.get(k, k) for k in (cmp_key(m["role"], m["content"]) for m in messages)]
    ops = difflib.SequenceMatcher(None, ok, nk, autojunk=False).get_opcodes()
    eq = [i for i, op in enumerate(ops) if op[0] == "equal"]
    main = max(eq, key=lambda i: (ops[i][2] - ops[i][1], ops[i][1])) if eq else None     # 重合最长的一段（一样长取靠后的）
    if main is not None:
        _, a1, a2, _, _ = ops[main]
        if a2 - a1 == 1 and len(norm(old[a1][2])) < WEAK_CHARS and len(old) - a2 > WEAK_TRAILING:
            main = None

    if main is None:
        if (old and not prefix and len(old) <= SHORT_CONV and len(messages) >= len(old)
                and old[0][1] == messages[0]["role"] and similar(old[0][2], messages[0]["content"])):
            for r in old:
                supersede(r)
            for i, m in enumerate(messages):
                insert(m, old[i][4] if i < len(old) else now)
        else:
            final.extend(r[0] for r in old)
            for m in messages:
                insert(m, now)
    else:
        last = len(ops) - 1
        first_eq = eq[0]
        prev_weak = False
        for idx, (tag, a1, a2, b1, b2) in enumerate(ops):
            # 窗口开头：第一段对上的之前。另外，碰巧对上一句很短的话以后、到重合最长那段之前，
            # 库里比窗口多出好几条的那一段也按开头处理（多半是碰巧对上，不能把中间那么多条都作废）
            lead = idx < first_eq or (tag == "replace" and idx < main and prev_weak and (a2 - a1) > (b2 - b1) + WEAK_TRAILING)
            if tag == "equal":
                prev_weak = a2 - a1 == 1 and len(norm(old[a1][2])) < WEAK_CHARS
                for i, j in zip(range(a1, a2), range(b1, b2)):
                    upgrade(old[i], messages[j])
                    final.append(old[i][0])
            elif tag == "delete":
                final.extend(r[0] for r in old[a1:a2])
            elif tag == "insert":
                at_end = idx == last and a1 == len(old)
                for m in messages[b1:b2]:
                    insert(m, now if at_end else ts_between(prev_ts(), next_ts(a1), now))
            elif lead:
                # 窗口开头对不上：从紧挨着重合段的地方往前一一配对，只替换角色一样、内容很像的；其余旧的保留、新的插进去
                olds, news = old[a1:a2], messages[b1:b2]
                k = min(len(olds), len(news))
                final.extend(r[0] for r in olds[:len(olds) - k])
                pair_old, extra_new, pair_new = olds[len(olds) - k:], news[:len(news) - k], news[len(news) - k:]
                for m in extra_new:
                    insert(m, ts_between(prev_ts(), pair_old[0][4] if pair_old else next_ts(a2), now))
                for n, (r, m) in enumerate(zip(pair_old, pair_new)):
                    if r[1] == m["role"] and similar(r[2], m["content"]):
                        supersede(r)
                        insert(m, r[4])
                    else:
                        final.append(r[0])
                        insert(m, ts_between(r[4], pair_old[n + 1][4] if n + 1 < len(pair_old) else next_ts(a2), now))
            else:
                # 重合段之后同一个位置内容变了：重新生成、编辑，或者前端删掉了后面那几条
                olds, news = old[a1:a2], messages[b1:b2]
                at_end = idx == last and a2 == len(old)
                for r in olds:
                    supersede(r)
                for i, m in enumerate(news):
                    if i < len(olds) and olds[i][1] == m["role"] and (not at_end or similar(olds[i][2], m["content"])):
                        ts = olds[i][4]
                    elif at_end:
                        ts = now
                    else:
                        ts = ts_between(prev_ts(), next_ts(a2), now)
                    insert(m, ts)

    idx_of = {r[0]: r[5] for r in rows}
    for i, rid in enumerate(final):
        if idx_of.get(rid) != i:
            c.execute("UPDATE l0_messages SET msg_idx=? WHERE id=?", (i, rid))
    return st
