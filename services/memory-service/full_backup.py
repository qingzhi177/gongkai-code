"""完整备份（Dashboard「备份管理」→「下载完整备份」，接口 GET /export/full）。

把和记忆有关的数据打成一个 zip，尽量什么都不丢：

- 现在的库（current/）：
  - memory.db：整库副本，表结构不变，可以直接换回去用。只清空了密钥和模型配置（providers、model_configs、
    active_config）、任务队列和 worker 状态（pipeline_jobs、worker_state），换回去以后要重新填模型配置；
  - tables/*.json：每张表一份（全文索引、二进制向量这类能重算的除外，它们在 memory.db 里）；
  - 可读版/*.md：直接能读的版本：对话原文（含被替换掉的旧版本）、记忆、思考记录、共同经历叙事和近期摘要、
    每日片段和日摘、叙事卷、章节（和旧版叙事对比）、关系状态、共同语汇和未完的事、召回对比；
  - profile/：画像文件；chroma/：向量库。
- 旧版（archive/）：data/archive/ 里放的旧库（比如以前的记忆库），每个同样清掉密钥和模型配置后放一份，并导出可读版。
  data/archive/labels.json 可以给每个旧库写标题和说明：{"文件名": {"title": "...", "note": "...", "readable": true}}。
- 说明.md：这份备份里有什么、各有多少条、怎么用。

不包含：密钥和模型配置、任务队列、系统日志（data/logs 等）、.env。
"""
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import timeutil

CLEAR_TABLES = ("providers", "model_configs", "active_config", "pipeline_jobs", "worker_state")
SKIP_JSON = ("vectors",)                     # 二进制向量：只留在 memory.db 里
SKIP_PREFIX = ("l1_fts", "sqlite_")          # 全文索引（能重算）和 SQLite 自己的表
ROLE = {"user": "你", "assistant": "ta"}
STATUS_CN = {"superseded": "旧版本", "deleted": "已删除", "rejected": "没通过校验", "merged": "已合并", "dismissed": "已忽略",
             "closed": "已了结", "expired": "已过期", "archived": "已归档", "inactive": "已停用"}
STATE_LINES = {"rhythm": "节奏", "weather": "你的天气", "ongoing": "正在一起经历的", "close": "最近的亲近时刻"}
DIGEST_KIND = {"day": "日摘", "week": "周摘", "recent": "近七天摘要"}
LEX_KINDS = {"nickname": "称呼", "inside_joke": "小梗", "ritual": "固定说法", "catchphrase": "口头禅",
             "song": "歌", "place": "地方", "object": "东西", "other": "其他"}
LEX_STATUS = {"candidate": "候选", "active": "收下的", "rejected": "不要的", "merged": "已合并"}
LOOP_KINDS = {"promise": "答应的事", "plan": "一起的计划", "waiting": "在等的结果", "worry": "牵挂",
              "unfinished_topic": "没聊完的话题", "ritual_due": "定期的约定"}
LOOP_OWNERS = {"user": "你", "ai": "ta", "both": "你们"}
LOOP_STATUS = {"open": "进行中", "done": "有结果了", "dropped": "不用再管了", "expired": "过期了"}


# ---------- 小工具 ----------

def _t(v):
    """存的是 UTC 时间，显示成你那边的时间"""
    if not v:
        return ""
    try:
        return timeutil.local_str(v)
    except Exception:
        return str(v)


def _loads(v, default):
    if v in (None, ""):
        return default
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def _tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _has(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _rows(conn, table, order=None):
    """读一张表（不存在就是空的）；order 里只用这张表有的列，旧库少几列也能读"""
    if not _has(conn, table):
        return []
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    sql = f'SELECT * FROM "{table}"'
    if order:
        keys = [k.strip() for k in order.split(",") if k.strip().split()[0] in cols]
        if keys:
            sql += " ORDER BY " + ", ".join(keys)
    return [dict(zip(cols, r)) for r in conn.execute(sql)]


def _quote(text):
    text = str(text or "").rstrip()
    return "\n".join("> " + line if line else ">" for line in text.split("\n")) if text else "> （空）"


def _status(st, active=("active", "current", "sealed", "pending", "applied", "nochange", "open", "done", None, "")):
    return "" if st in active else f"（{STATUS_CN.get(st, st)}）"


def _json_default(v):
    if isinstance(v, (bytes, bytearray)):
        return None
    return str(v)


# ---------- 副本 ----------

def sanitized_copy(src, dst):
    """整库复制（SQLite 在线备份，不用停服务）→ 清空密钥、模型配置、任务队列 → VACUUM（删掉的数据不会留在文件里）。
    返回 {表: 清掉了几行}"""
    try:
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        s.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.Error:
        s = sqlite3.connect(str(src), timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        s.close()
    cleared = {}
    try:
        d.execute("PRAGMA journal_mode=DELETE")
        names = set(_tables(d))
        for t in CLEAR_TABLES:
            if t in names:
                cleared[t] = d.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                d.execute(f'DELETE FROM "{t}"')
        d.commit()
        d.execute("VACUUM")
    finally:
        d.close()
    return cleared


def counts(conn):
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in _tables(conn) if not t.startswith(SKIP_PREFIX)}


def tables_json(conn):
    out = {}
    for t in _tables(conn):
        if t.startswith(SKIP_PREFIX) or t in SKIP_JSON:
            continue
        out[t] = json.dumps(_rows(conn, t), ensure_ascii=False, indent=1, default=_json_default)
    return out


# ---------- 可读版 ----------

def md_dialogue(conn):
    rows = _rows(conn, "l0_messages")
    if not rows:
        return None
    branches = {r["conv_id"]: r for r in _rows(conn, "conv_branches")}
    convs = {}
    for r in rows:
        convs.setdefault(r.get("conv_id") or "", []).append(r)

    def first(items):
        return min((x.get("ts") or "") for x in items)

    out = ["# 对话原文", "",
           "按会话分开，会话按开始的时间排，每个会话里按顺序排。「你」是你说的，「ta」是 ta 说的；时间是你那边的时间。",
           "标了「旧版本」的是后来重新生成或者改过、被换下来的那一版，紧跟在它的位置上；标了「已删除」的是删掉的。", ""]
    for cid, items in sorted(convs.items(), key=lambda kv: first(kv[1])):
        items.sort(key=lambda x: (x.get("msg_idx") if x.get("msg_idx") is not None else 10 ** 9, x.get("ts") or "",
                                  x.get("version") or 0, x.get("id") or 0))
        act = [x for x in items if (x.get("status") or "active") == "active"]
        clients = sorted({x.get("client") or "" for x in items} - {""})
        meta = f"{_t(first(items))} 开始 · {len(act)} 条"
        if len(items) > len(act):
            meta += f"（另有 {len(items) - len(act)} 条旧版本或已删除）"
        if clients:
            meta += " · 来自 " + "、".join(clients)
        if cid in branches:
            meta += f" · 从会话 {branches[cid].get('parent_conv_id')} 分出来"
        out += [f"## 会话 {cid or '（没有 id）'}", "", meta, ""]
        for x in items:
            who = ROLE.get(x.get("role"), x.get("role") or "")
            out += [f"**{who}** · {_t(x.get('ts'))}{_status(x.get('status'))}", "", _quote(x.get("content")), ""]
    return "\n".join(out)


def md_memories(conn):
    rows = _rows(conn, "l1_memories", "ts, id")
    if not rows:
        return None

    def item(r):
        flags = []
        if r.get("is_core"):
            flags.append("核心")
        if r.get("event_type"):
            flags.append(str(r["event_type"]))
        head = f"- **#{r.get('id')}** · {_t(r.get('ts'))}" + (f" · {' · '.join(flags)}" if flags else "") + _status(r.get("status"))
        lines = [head, "  " + str(r.get("content") or "").replace("\n", "\n  ")]
        if r.get("quote"):
            lines.append("  原话：" + str(r["quote"]).replace("\n", " "))
        return "\n".join(lines)

    selfs = [r for r in rows if (r.get("client") or "") == "ai_self"]
    others = [r for r in rows if (r.get("client") or "") != "ai_self"]
    out = ["# 记忆（L1）", "", f"一共 {len(rows)} 条。时间是你那边的时间。", ""]
    if others:
        out += [f"## 从对话里提取的（{len(others)} 条）", ""] + [item(r) for r in others] + [""]
    if selfs:
        out += [f"## ta 自己记下的（feel，{len(selfs)} 条）", ""] + [item(r) for r in selfs] + [""]
    return "\n".join(out)


def md_thinking(conn):
    rows = _rows(conn, "thinking_records", "ts, id")
    if not rows:
        return None
    out = ["# 思考记录", "", f"ta 回复时的思考过程，一共 {len(rows)} 条。", ""]
    for r in rows:
        out += [f"## {_t(r.get('ts'))}（会话 {r.get('conv_id') or ''}）", "", "**思考**", "", _quote(r.get("thinking")), ""]
        if r.get("full_reply"):
            out += ["**回复**", "", _quote(r.get("full_reply")), ""]
    return "\n".join(out)


def md_narrative(conn):
    nar = _rows(conn, "shared_narrative", "version, id")
    summ = _rows(conn, "recent_summary", "ts, id")
    if not nar and not summ:
        return None
    out = ["# 共同经历叙事和近期摘要（旧版的写法）", ""]
    if nar:
        out += ["## 共同经历叙事（每一版）", ""]
        for r in nar:
            out += [f"### 第 {r.get('version')} 版 · {_t(r.get('ts'))}{_status(r.get('status'))}", "",
                    str(r.get("content") or "（空）"), ""]
    if summ:
        out += ["## 近期摘要（每一版）", ""]
        for r in summ:
            span = f"{_t(r.get('period_start'))} 到 {_t(r.get('period_end'))}" if r.get("period_start") else ""
            out += [f"### {_t(r.get('ts'))}{(' · ' + span) if span else ''}{_status(r.get('status'))}", "",
                    str(r.get("content") or "（空）"), ""]
    return "\n".join(out)


def md_days(conn):
    eps = _rows(conn, "episodes", "day, start_ts, id")
    digs = _rows(conn, "digests", "period_start, id")
    if not eps and not digs:
        return None
    days = sorted({r.get("day") for r in eps if r.get("day")} | {r.get("period_start") for r in digs if r.get("kind") == "day"})
    out = ["# 每日片段和日摘", "", "按天排。每天先是日摘（那天的几句话），再是那天的片段。", ""]
    for day in days:
        out += [f"## {day}", ""]
        for d in [x for x in digs if x.get("kind") == "day" and x.get("period_start") == day]:
            out += [f"**日摘** 第 {d.get('version')} 版{_status(d.get('status'))}", "", str(d.get("body") or ""), ""]
        for e in [x for x in eps if x.get("day") == day]:
            mood = f"（{e['mood']}）" if e.get("mood") else ""
            out += [f"### 《{e.get('title') or ''}》{mood} · {_t(e.get('start_ts'))}{_status(e.get('status'))}", "",
                    str(e.get("body") or ""), ""]
    others = [x for x in digs if x.get("kind") != "day"]
    if others:
        out += ["## 周摘和近七天摘要", ""]
        for d in others:
            out += [f"### {DIGEST_KIND.get(d.get('kind'), d.get('kind'))} · {d.get('period_start')} 到 {d.get('period_end')}"
                    f" · 第 {d.get('version')} 版{_status(d.get('status'))}", "", str(d.get("body") or ""), ""]
    return "\n".join(out)


def _sections(secs):
    out = []
    for s in _loads(secs, []) or []:
        if isinstance(s, dict):
            out += [f"**{s.get('title') or ''}**", "", str(s.get("body") or ""), ""]
    return out


def md_arcs(conn):
    arcs = _rows(conn, "arcs", "id")
    if not arcs:
        return None
    vers = _rows(conn, "arc_versions", "arc_id, version")
    out = ["# 叙事卷（按主题的长篇）", ""]
    for a in arcs:
        out += [f"## 《{a.get('title') or ''}》{_status(a.get('status'))}", ""]
        if a.get("theme"):
            out += [f"主题：{a['theme']}", ""]
        if a.get("summary"):
            out += [str(a["summary"]), ""]
        out += _sections(a.get("sections"))
        old = [v for v in vers if v.get("arc_id") == a.get("id")]
        if old:
            out += ["### 以前的版本", ""]
            for v in old:
                out += [f"#### 第 {v.get('version')} 版 · {_t(v.get('created_at'))}", ""]
                if v.get("summary"):
                    out += [str(v["summary"]), ""]
                out += _sections(v.get("sections"))
    return "\n".join(out)


def md_chapters(conn):
    chs = _rows(conn, "chapters", "n")
    if not chs:
        return None
    vers = _rows(conn, "chapter_versions", "chapter_id, version")
    props = _rows(conn, "chapter_proposals", "id")
    out = ["# 章节（新版的共同经历）", ""]
    for c in chs:
        rng = f"{c.get('start_day')} 到 {c.get('end_day')}" if c.get("end_day") else f"{c.get('start_day')} 至今"
        out += [f"## 第 {c.get('n')} 章" + (f"「{c['title']}」" if c.get("title") else "") + f"（{rng}，"
                + ("已封存" if c.get("status") == "sealed" else "进行中") + "）", ""]
        if c.get("abstract"):
            out += [f"摘要：{c['abstract']}", ""]
        if c.get("summary"):
            out += [f"这一章讲的是：{c['summary']}", ""]
        out += _sections(c.get("sections"))
        old = [v for v in vers if v.get("chapter_id") == c.get("id")]
        if old:
            out += ["### 每一版", ""]
            for v in old:
                out += [f"#### 第 {v.get('version')} 版 · {_t(v.get('created_at'))}", ""]
                out += _sections(v.get("sections"))
    if props:
        out += ["## 分章提议", ""]
        for p in props:
            items = "；".join(f"「{x.get('title')}」{x.get('start')} 到 {x.get('end') or '现在'}"
                             for x in (_loads(p.get("chapters"), []) or []) if isinstance(x, dict))
            out += [f"- {_t(p.get('created_at'))} · {STATUS_CN.get(p.get('status'), p.get('status'))}：{items or '（没有）'}"]
        out.append("")
    nar = [r for r in _rows(conn, "shared_narrative", "version, id") if (r.get("status") or "active") == "active"]
    if nar:
        out += ["## 对比：旧版的共同经历叙事（现在用的那一版）", "", str(nar[-1].get("content") or "（空）"), ""]
    return "\n".join(out)


def md_state(conn):
    states = _rows(conn, "relationship_states", "id")
    pols = _rows(conn, "topic_policies", "id")
    if not states and not pols:
        return None
    out = ["# 关系状态", ""]
    for s in reversed(states):
        out += [f"## 第 {s.get('version')} 版 · {_t(s.get('created_at'))}{_status(s.get('status'))}", ""]
        for ln in _loads(s.get("lines"), []) or []:
            if isinstance(ln, dict):
                out.append(f"- {STATE_LINES.get(ln.get('key'), ln.get('key'))}：{ln.get('text') or ''}")
        out.append("")
    if pols:
        names = {"normal": "正常", "explicit_only": "你提起时才提", "excluded": "不主动提起"}
        out += ["## 话题边界", ""] + [f"- {p.get('topic')}：{names.get(p.get('policy'), p.get('policy'))}"
                                    + (f"（关键词：{'、'.join(_loads(p.get('keywords'), []) or [])}）" if p.get("keywords") else "")
                                    for p in pols] + [""]
    return "\n".join(out)


def md_lexicon_loops(conn):
    lex = _rows(conn, "lexicon", "first_seen, id")
    loops = _rows(conn, "open_loops", "created_ts, id")
    events = _rows(conn, "loop_events", "ts, id")
    if not lex and not loops:
        return None
    out = ["# 共同语汇和未完的事", ""]
    if lex:
        out += ["## 共同语汇", ""]
        for x in lex:
            aliases = _loads(x.get("aliases"), []) or []
            out.append(f"- **{x.get('term')}**" + (f"（也叫 {'、'.join(map(str, aliases))}）" if aliases else "")
                       + f" · {LEX_KINDS.get(x.get('kind'), x.get('kind') or '')} · {LEX_STATUS.get(x.get('status'), x.get('status') or '')}"
                       + f"：{x.get('meaning') or ''}"
                       + (f" · 第一次 {_t(x.get('first_seen'))}，用过 {x.get('use_count') or 0} 次" if x.get("first_seen") else ""))
        out.append("")
    if loops:
        out += ["## 未完的事", ""]
        for x in loops:
            out.append(f"- **{x.get('text')}**（{LOOP_KINDS.get(x.get('kind'), x.get('kind') or '')}，"
                       f"{LOOP_OWNERS.get(x.get('owner'), x.get('owner') or '')}的事）· {LOOP_STATUS.get(x.get('status'), x.get('status') or '')}"
                       + (f" · 期限 {x['due_at']}" if x.get("due_at") else ""))
            if x.get("created_quote"):
                out.append(f"  - {_t(x.get('created_ts'))} 提起：{x['created_quote']}")
            for e in [e for e in events if e.get("loop_id") == x.get("id")]:
                out.append(f"  - {_t(e.get('ts'))} {e.get('event') or ''}：{e.get('quote') or e.get('detail') or ''}")
            if x.get("closed_text"):
                out.append(f"  - 后续：{x['closed_text']}")
        out.append("")
    return "\n".join(out)


def md_recall(conn):
    traces = _rows(conn, "recall_traces", "id")
    if not traces:
        return None
    mem = {r.get("id"): r.get("content") for r in _rows(conn, "l1_memories")}
    labels = {}
    for lb in _rows(conn, "recall_labels"):
        labels.setdefault(lb.get("trace_id"), []).append(lb)

    def name(i, frozen):
        try:
            i = int(i)
        except (TypeError, ValueError):
            return str(i)
        # 清空记忆库之前的一轮（frozen 不是 None）：用当时定格下来的记忆内容，不去现在的记忆表里按编号找
        text = (frozen.get(str(i)) or {}).get("content") if frozen is not None else mem.get(i)
        return f"#{i} {str(text or '（这条记忆已经不在了）')[:80]}"

    out = ["# 召回对比", "", "每一轮聊天时，新的召回和旧的召回各自会想起哪些记忆。", ""]
    for t in traces:
        fz = _loads(t.get("frozen"), None)
        fz = (fz.get("items") or {}) if isinstance(fz, dict) else None
        dec = _loads(t.get("decision"), {}) or {}
        leg = _loads(t.get("legacy"), {}) or {}
        out += [f"## {_t(t.get('created_at'))} · {t.get('mode') or ''}", "", "你说：" + str(t.get("user_text") or t.get("query") or "").replace("\n", " ")[:300], ""]
        picks = [p.get("id") if isinstance(p, dict) else p for p in (dec.get("picks") or [])]
        out.append("- 新的召回：" + ("；".join(name(i, fz) for i in picks) if picks else f"没有想起（{dec.get('gate') or ''}）"))
        lp = leg.get("picked") or []
        out.append("- 旧的召回：" + ("；".join(name(i, fz) for i in lp) if lp else f"没有想起（{leg.get('gate') or ''}）"))
        if t.get("card"):
            out += ["", "放进聊天的那一段：", "", _quote(t["card"])]
        for lb in labels.get(t.get("id"), []):
            out.append(f"- 你的标注：{name(lb.get('item_id'), fz)} → {lb.get('label')}")
        out.append("")
    return "\n".join(out)


FACT_SUBJECTS = {"user": "关于你", "ai": "关于我", "us": "我们之间"}
FACT_STATUS = {"current": "现在的", "superseded": "以前的", "disputed": "可能变了", "rejected": "不对"}


def md_facts(conn):
    facts = _rows(conn, "facts", "fact_key, valid_from, id")
    if not facts:
        return None
    out = ["# 事实", "", "一段时间内不会变、聊天时应该知道的事。值变过的，按时间列出以前的值。", ""]
    for subj, title in FACT_SUBJECTS.items():
        rows = [x for x in facts if x.get("subject") == subj]
        if not rows:
            continue
        out += [f"## {title}", ""]
        keys = []
        for x in rows:
            if x.get("fact_key") not in keys:
                keys.append(x.get("fact_key"))
        for k in keys:
            vs = [x for x in rows if x.get("fact_key") == k]
            cur = next((x for x in vs if x.get("status") == "current"), vs[-1])
            out.append(f"- **{cur.get('label')}**：{cur.get('value')}（{FACT_STATUS.get(cur.get('status'), '')}，{_t(cur.get('valid_from'))} 起）")
            if cur.get("quote"):
                out.append(f"  - 原话：{cur['quote']}")
            for x in vs:
                if x is cur:
                    continue
                out.append(f"  - {FACT_STATUS.get(x.get('status'), x.get('status') or '')}：{x.get('value')}（{_t(x.get('valid_from'))}"
                           + (f" 到 {_t(x.get('valid_to'))}" if x.get("valid_to") else "") + "）")
        out.append("")
    return "\n".join(out)


READABLE = [("01_对话原文.md", md_dialogue), ("02_记忆.md", md_memories), ("03_思考记录.md", md_thinking),
            ("04_共同经历叙事和近期摘要.md", md_narrative), ("05_每日片段和日摘.md", md_days), ("06_叙事卷.md", md_arcs),
            ("07_章节和旧版叙事对比.md", md_chapters), ("08_关系状态.md", md_state), ("09_共同语汇和未完的事.md", md_lexicon_loops),
            ("10_召回对比.md", md_recall), ("11_事实.md", md_facts)]


def readable(conn):
    out = {}
    for fname, fn in READABLE:
        text = fn(conn)
        if text:
            out[fname] = text
    return out


# ---------- 打包 ----------

KEY_TABLES = [("l0_messages", "对话原文（条）"), ("l1_memories", "记忆（L1）"), ("thinking_records", "思考记录"),
              ("shared_narrative", "共同经历叙事（版本）"), ("recent_summary", "近期摘要（版本）"), ("episodes", "每日片段"),
              ("digests", "日摘 / 周摘"), ("arcs", "叙事卷"), ("chapters", "章节"), ("relationship_states", "关系状态（版本）"),
              ("lexicon", "共同语汇"), ("open_loops", "未完的事"), ("recall_traces", "召回记录")]


def _count_lines(cnt):
    return [f"- {label}：{cnt[t]}" for t, label in KEY_TABLES if cnt.get(t)]


def build(db_path, data_dir, out_path, now=None):
    """打包完整备份到 out_path（zip）。返回清单（每部分有多少条）"""
    data_dir = Path(data_dir)
    now = now or datetime.now()
    manifest = {"format": "memory-full-backup", "version": 1, "created_at": timeutil.utc_now_str(), "current": {}, "archive": []}
    tmp = Path(tempfile.mkdtemp(prefix="fullbackup_"))
    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            # 现在的库
            cur = tmp / "memory.db"
            cleared = sanitized_copy(db_path, cur)
            conn = sqlite3.connect(str(cur))
            try:
                cnt = counts(conn)
                for t, text in tables_json(conn).items():
                    z.writestr(f"current/tables/{t}.json", text)
                for fname, text in readable(conn).items():
                    z.writestr(f"current/可读版/{fname}", text)
            finally:
                conn.close()
            z.write(cur, "current/memory.db")
            manifest["current"] = {"counts": cnt, "cleared": cleared}
            for sub in ("profile", "chroma"):
                base = data_dir / sub
                if base.is_dir():
                    for f in sorted(base.rglob("*")):
                        if f.is_file():
                            z.write(f, f"current/{sub}/{f.relative_to(base).as_posix()}")
            # 旧版
            arch = data_dir / "archive"
            labels = _loads((arch / "labels.json").read_text(encoding="utf-8"), {}) if (arch / "labels.json").is_file() else {}
            for f in sorted(arch.glob("*.db")) if arch.is_dir() else []:
                lab = labels.get(f.name, {}) if isinstance(labels, dict) else {}
                stem = f.stem
                dst = tmp / f.name
                cl = sanitized_copy(f, dst)
                conn = sqlite3.connect(str(dst))
                try:
                    c2 = counts(conn)
                    if lab.get("readable", True):
                        for fname, text in readable(conn).items():
                            z.writestr(f"archive/{stem}/可读版/{fname}", text)
                    for t, text in tables_json(conn).items():
                        z.writestr(f"archive/{stem}/tables/{t}.json", text)
                finally:
                    conn.close()
                z.write(dst, f"archive/{stem}/memory.db")
                manifest["archive"].append({"file": f.name, "title": lab.get("title") or stem, "note": lab.get("note") or "",
                                            "counts": c2, "cleared": cl})
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
            z.writestr("说明.md", readme(manifest, now))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return manifest


def readme(manifest, now):
    cur = manifest["current"]
    out = [f"# 记忆库完整备份（{now.strftime('%Y-%m-%d %H:%M')}）", "",
           "这份备份里是记忆库里所有和记忆有关的数据。想直接读，打开各个「可读版」文件夹里的 .md 文件就好。", "",
           "## 现在的记忆库（current/）", ""] + _count_lines(cur.get("counts", {})) + [
           "",
           "- `可读版/`：能直接读的版本。对话原文里包括后来被重新生成、被换下来的旧版本。",
           "- `memory.db`：整个库的副本，表结构不变，以后可以直接换回去用（换回去以后要重新填模型配置）。",
           "- `tables/`：每张表一份 JSON，什么都在。",
           "- `profile/`：画像文件；`chroma/`：向量库（可以重算，留着省事）。", ""]
    if manifest["archive"]:
        out += ["## 旧版（archive/）", ""]
        for a in manifest["archive"]:
            out += [f"### {a['title']}（archive/{Path(a['file']).stem}/）", ""]
            if a.get("note"):
                out += [a["note"], ""]
            out += _count_lines(a.get("counts", {})) + [""]
    out += ["## 没有放进来的", "",
            "- 密钥和模型配置（providers、model_configs、active_config 这几张表是空的）；",
            "- 任务队列和 worker 状态（pipeline_jobs、worker_state）；",
            "- 系统日志、.env。", "",
            "## 怎么用", "",
            "- 只是想留着看：保存好这个 zip，打开「可读版」里的文件就行。",
            "- 以后想恢复：把 zip 交给 Claude，或者把 `current/memory.db` 换回 `data/sqlite/memory.db`（先停服务），再在 Dashboard 里重新填模型配置。",
            "- `manifest.json` 里记着每张表有多少行。", ""]
    return "\n".join(out)
