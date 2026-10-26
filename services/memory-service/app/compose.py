"""Context Composer 的 system 部分（方案 8.6 节）：API 模式下放进聊天的三块 system 内容，给网关用。

网关的 system 分三段，每段都带 1 小时缓存断点；内容只在底下的数据变了时才变，同一天里多轮聊天都能命中缓存：
- 静态块：人设与原则、记忆使用说明、人格画像（md）——网关自己拼；这里给它接在画像后面的「事实」档案几行（profile_extra）；
- 半静态块（semi）：章节目录 + 进行中的这一章、进行中的主题线、共同语汇；
- 日更块（daily）：关系状态卡（「我们最近」）、近七天摘要、最近几天的片段标题、还没了结的事。

每一层都能在 Dashboard 上单独关掉（app_settings 的 compose.*）。章节或新版摘要关掉、或者还没有时，放回旧的
「共同经历叙事」「近期摘要」，保证不会突然少一大块。
这里的内容不能随「现在几点」变（比如「上次聊天：3 小时前」），不然每一轮缓存都失效：时间头和间隔在每一轮的附注里。
碰到「轻放」话题关键词的主题线、语汇、片段标题不放。
"""
import json
from datetime import date

from app import settings as app_settings
from app.config import get_conn
from app.pipeline import arcs as AR
from app.pipeline import chapters as C
from app.pipeline import facts as F
from app.pipeline import lexicon as LX
from app.pipeline import loops as LP
from app.pipeline import state as S

LEXICON_MAX = 1600
LEXICON_MEANING = 50
ARCS_MAX = 900
CHAPTER_MAX = 2500
CHAPTER_INDEX_MAX = 1200
DIGEST_MAX = 900
EPISODE_DAYS = 2
EPISODE_TITLES_MAX = 400
WEEKDAYS = "一二三四五六日"

SEMI_INTRO = "以下是我们一起走过的时间和共同的东西（「我」是 AI，「你」是和我说话的人）。它们是认识的底子，不用主动复述。"
DAILY_INTRO = "以下是我们最近的样子（每天整理一次）。"


def cfg():
    return app_settings.get("compose")


def _cap(text, limit):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "……"


def _lines_within(lines, limit):
    out, size = [], 0
    for line in lines:
        if size + len(line) > limit:
            break
        out.append(line)
        size += len(line) + 1
    return out


# ---------- 静态块的补充：事实 ----------

def facts_text():
    lines = F.profile_lines()
    if not lines:
        return ""
    return ("[档案 · 事实]（从我们的对话里记下的、不常变的事；标着「随口提到的」不一定准，标着「可能变了」的合适时可以自然地确认一下）\n"
            + "\n".join(lines))


# ---------- 半静态块：章节、主题线、共同语汇 ----------

def chapter_index(chs):
    """章节目录：每章一行。太长时去掉最早的几章（整行去掉，不从中间截断）"""
    lines = []
    for ch in chs:
        name = f"第 {ch['n']} 章" + (f"「{ch['title']}」" if ch["title"] else "")
        if ch["status"] == "sealed":
            lines.append(f"· {name}（{C._range_label(ch)}）：{ch['abstract'] or '（摘要还没写）'}")
        else:
            lines.append(f"· {name}（{C._range_label(ch)}，进行中）：{ch['summary'] or '（还没写）'}")
    kept, size = [], 0
    for line in reversed(lines):
        if kept and size + len(line) > CHAPTER_INDEX_MAX:
            break
        kept.insert(0, line)
        size += len(line) + 1
    if len(kept) < len(lines):
        kept.insert(0, f"· （更早的 {len(lines) - len(kept)} 章略，需要时可以用 recall 去找）")
    return "\n".join(kept)


def chapters_text(conn):
    chs = C._chapters(conn)
    if not chs:
        return ""
    parts = ["[我们的章节]（按时间分的阶段；旧的章节只记得大概）\n" + chapter_index(chs)]
    cur = next((c for c in reversed(chs) if c["status"] == "current"), None)
    if cur:
        body = "\n\n".join(s["body"].strip() for s in cur["sections"] if (s.get("body") or "").strip())
        if body:
            if len(body) > CHAPTER_MAX:
                body = "……" + body[-CHAPTER_MAX:]
            name = f"第 {cur['n']} 章" + (f"「{cur['title']}」" if cur["title"] else "")
            parts.append(f"[进行中的这一章 · {name}（{C._range_label(cur)}）]\n{body}")
    return "\n\n".join(parts)


def arcs_text(conn, kws):
    arcs, _ = AR.catalog(conn)
    lines = []
    for a in arcs:
        if a["status"] != "active" or S.touches(f"{a['title']} {a['summary']}", kws):
            continue
        lines.append(f"· {a['title']}" + (f"：{a['summary'].strip()}" if a["summary"].strip() else ""))
    lines = _lines_within(lines, ARCS_MAX)
    return ("[我们的主题线]（一直延续着的几条线）\n" + "\n".join(lines)) if lines else ""


def lexicon_text(conn, kws):
    rows = sorted(LX._all(conn, ("active",)), key=lambda r: str(r["last_used"] or ""), reverse=True)
    rows.sort(key=lambda r: (r["kind"] != "nickname", -(r["use_count"] or 0)))      # 称呼在前，常用的在前，一样多时最近用过的在前
    lines = []
    for r in rows:
        meaning = (r.get("meaning") or LX.KINDS.get(r.get("kind"), "")).strip()
        if S.touches(f"{r['term']} {' '.join(r['aliases'])} {meaning}", kws):
            continue
        name = f"《{r['term']}》" if r.get("kind") == "song" else f"「{r['term']}」"
        alias = f"（也叫{'、'.join(r['aliases'][:4])}）" if r["aliases"] else ""
        lines.append(f"· {name}{alias}：{_cap(meaning, LEXICON_MEANING)}")
    lines = _lines_within(lines, LEXICON_MAX)
    return ("[我们的共同语汇]（我们之间的称呼、小梗和固定说法）\n" + "\n".join(lines)) if lines else ""


def legacy_narrative(conn):
    r = conn.execute("SELECT content FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1").fetchone()
    return f"[共同经历叙事]\n\n{r[0]}" if r and (r[0] or "").strip() else ""


def legacy_summary(conn):
    r = conn.execute("SELECT content FROM recent_summary WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    return f"[近期阶段摘要]\n\n{r[0]}" if r and (r[0] or "").strip() else ""


# ---------- 日更块：关系状态、近七天、最近几天、还没了结的事 ----------

def state_text(conn, pols):
    r = conn.execute("SELECT lines FROM relationship_states WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    lines = json.loads(r[0]) if r and r[0] else []
    card = S.render(lines, pols, None).split("\n")[1:]      # 不带「上次聊天」：那一行每轮都变，放在附注里
    return ("[我们最近]\n" + "\n".join(card)) if card else ""


def digest_text(conn):
    r = conn.execute("SELECT body FROM digests WHERE kind='recent' AND status='active' ORDER BY id DESC LIMIT 1").fetchone()
    return f"[近七天]\n{_cap(r[0], DIGEST_MAX)}" if r and (r[0] or "").strip() else ""


def _day_name(day):
    d = date.fromisoformat(day)
    return f"{d.month} 月 {d.day} 日（周{WEEKDAYS[d.weekday()]}）"


def episodes_text(conn, kws):
    days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM episodes WHERE status='active' ORDER BY day DESC LIMIT ?",
                                       (EPISODE_DAYS,))]
    lines = []
    for day in sorted(days):
        titles = [t for (t,) in conn.execute("SELECT title FROM episodes WHERE status='active' AND day=? ORDER BY start_ts", (day,))
                  if t and not S.touches(t, kws)]
        if titles:
            lines.append(f"· {_day_name(day)}：{'；'.join(titles)}")
    text = "\n".join(lines)
    if len(text) > EPISODE_TITLES_MAX:
        text = "……" + text[-EPISODE_TITLES_MAX:]
    return ("[最近几天的片段]（每段一个标题，细节记不清时可以用 recall 去找）\n" + text) if text else ""


def loops_text(now=None):
    pack = LP.pack(now=now)
    if not pack:
        return ""
    return "[还没了结的事]（相关时可以自然地接上，不用每次都提）\n" + "\n".join(f"· {x['line']}" for x in pack)


# ---------- 组装 ----------

def system_blocks(now=None):
    """返回 {recall_live, profile_extra, semi, daily, parts: {名字: 字数}, legacy: [...]}。
    parts 里只列真的放进去的；legacy 列出哪几块用了旧的（叙事 / 近期摘要）。"""
    c = cfg()
    conn = get_conn()
    try:
        pols = S.policies(conn)
        kws = S.light_keywords(pols)
        parts, legacy = {}, []

        def use(name, on, fn):
            if not on:
                return ""
            try:
                text = fn()
            except Exception as e:           # 某一层出错不影响别的层
                print(f"[COMPOSE] {name} 出错：{type(e).__name__}: {e}")
                return ""
            if text:
                parts[name] = len(text)
            return text

        profile_extra = use("facts", c["facts"], facts_text)
        chapters = use("chapters", c["chapters"], lambda: chapters_text(conn))
        if not chapters:
            chapters = use("legacy_narrative", True, lambda: legacy_narrative(conn))
            if chapters:
                legacy.append("narrative")
        semi = [chapters,
                use("arcs", c["arcs"], lambda: arcs_text(conn, kws)),
                use("lexicon", c["lexicon"], lambda: lexicon_text(conn, kws))]
        digest = use("digest", c["digest"], lambda: digest_text(conn))
        if not digest:
            digest = use("legacy_summary", True, lambda: legacy_summary(conn))
            if digest:
                legacy.append("summary")
        daily = [use("state", c["state"], lambda: state_text(conn, pols)),
                 digest,
                 use("episodes", c["episodes"], lambda: episodes_text(conn, kws)),
                 use("loops", c["loops"], lambda: loops_text(now))]
    finally:
        conn.close()
    semi = [x for x in semi if x]
    daily = [x for x in daily if x]
    return {"recall_live": c["recall_live"], "profile_extra": profile_extra,
            "semi": "\n\n".join([SEMI_INTRO] + semi) if semi else "",
            "daily": "\n\n".join([DAILY_INTRO] + daily) if daily else "",
            "parts": parts, "legacy": legacy}
