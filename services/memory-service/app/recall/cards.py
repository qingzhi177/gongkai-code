"""念头式卡片（方案 8.7 节）：这一轮自动浮现的记忆，放在用户消息最前面的「记忆系统自动附注」里（附注的外框由网关加）。

和以前（「（初秋，……）」）不一样的地方：
- 时间写成「大约一个月前（今年初秋）：」——相对时间 + 季节，后面接冒号。以前只写「初秋，」，
  模型可能把「初秋」当成一个称呼；
- 分小标题：〈自动浮现的记忆〉（按这句话的相关度找出来的）、〈想起来的〉（在问过去的事）、
  〈忽然想起〉（无由来浮现，和这句话无关）、〈那天〉（话里提到的那段时间的每日片段）；
- 回忆模式（「还记得吗」）附上当时的原话；AI 自己记下的感受标明「我记下的感受」。
"""
import timeutil

SEASONS = ["冬天", "冬末", "早春", "春天", "晚春", "初夏", "夏天", "盛夏", "初秋", "秋天", "深秋", "初冬"]
CN_NUM = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十", 11: "十一"}
PERIODS = ((5, "凌晨"), (8, "早上"), (11, "上午"), (13, "中午"), (17, "下午"), (19, "傍晚"), (23, "晚上"), (24, "深夜"))
EPISODE_CHARS = 160
CONTENT_CHARS = 200
QUOTE_CHARS = 80
CLOSING = "（只是自然浮现的念头，合适才提，不用逐条回应。）"


def _days(ts, now):
    mem = timeutil.to_local(ts)
    if mem is None:
        return None, None, None
    now = timeutil.to_local(now) if now is not None else timeutil.to_local(timeutil.utc_now())
    return mem, now, (now.date() - mem.date()).days


def fuzzy_time(ts, now=None):
    """旧的说法（只有一个词：昨天、前阵子、初秋……），轨迹里的旧记录和对比还在用"""
    mem, now, days = _days(ts, now)
    if mem is None:
        return "不知何时" if not ts else "很久之前"
    if days <= 0:
        return "今天早些时候"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    if days < 7:
        return "最近"
    if days < 14:
        return "前阵子"
    if days < 30:
        return "不久之前"
    season = SEASONS[mem.month - 1]
    if days < 365:
        return season
    years = now.year - mem.year
    if years <= 1:
        return f"去年{season}"
    return f"{years}年前的{season}"


def when_label(ts, now=None):
    """记忆发生的大概时间，比如「昨天」「一个多星期前」「大约一个月前（今年初秋）」「大约一年前（去年春天）」"""
    mem, now, days = _days(ts, now)
    if mem is None:
        return "不记得是什么时候" if not ts else "很久以前"
    if days <= 0:
        return "今天早些时候"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    if days < 7:
        return "几天前"
    if days < 14:
        return "一个多星期前"
    if days < 30:
        return "两三个星期前"
    season = SEASONS[mem.month - 1]
    which = "今年" if mem.year == now.year else ("去年" if now.year - mem.year == 1 else f"{mem.year} 年")
    if days < 365:
        months = max(1, round(days / 30))
        return f"大约{CN_NUM.get(months, str(months))}个月前（{which}{season}）"
    years = max(1, days // 365)
    return f"大约{CN_NUM.get(years, str(years))}年前（{which}{season}）"


def day_label(ts, day=None):
    """片段开始的日期和时段，比如「10 月 19 日凌晨」"""
    t = timeutil.to_local(ts) if ts else None
    if not t:
        return str(day or "")
    period = next(name for end, name in PERIODS if t.hour < end)
    return f"{t.month} 月 {t.day} 日{period}"


def _cut(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "……"


def _squash(t):
    return "".join(str(t or "").split())


def thought_line(m, with_quote=False, now=None):
    """一条记忆一行：时间在冒号前；太长截断；原话和正文差不多时不重复附"""
    when = when_label(m.get("ts"), now=now)
    content = _cut(m.get("content"), CONTENT_CHARS)
    head = f"{when}，我记下的感受" if m.get("author") == "ai_felt" else when
    line = f"· {'⭐ ' if m.get('is_core') else ''}{head}：{content}"
    quote = (m.get("quote") or "").strip()
    q, c = _squash(quote), _squash(m.get("content"))
    if with_quote and q and q not in c and c not in q:
        line += f"——当时的原话「{_cut(quote, QUOTE_CHARS)}」"
    return line


def episode_line(e):
    body = " ".join(str(e.get("body") or "").split())
    if len(body) > EPISODE_CHARS:
        body = body[:EPISODE_CHARS] + "……"
    return f"· {day_label(e.get('start_ts'), e.get('day'))}，《{e.get('title') or '那天'}》：{body}"


def format_card(picks, intent_type="normal", now=None, episodes=()):
    """picks：[{...元数据, "tier": ...}]；episodes：话里提到的那段时间的每日片段。没有要注入的返回空串。"""
    recall = intent_type == "recall"
    related = [m for m in picks if m.get("tier") != "serendipity"]
    sudden = [m for m in picks if m.get("tier") == "serendipity"]
    lines = []
    if related:
        lines.append("〈想起来的〉（在问过去的事，这是按这句话找到的）" if recall else "〈自动浮现的记忆〉")
        lines += [thought_line(m, with_quote=recall or m.get("tier") == "recall", now=now) for m in related]
    if episodes:
        lines.append("〈那天〉（话里提到的那段时间）")
        lines += [episode_line(e) for e in episodes]
    if sudden:
        lines.append("〈忽然想起〉（和这句话无关）")
        lines += [thought_line(m, now=now) for m in sudden]
    if not lines:
        return ""
    return "\n".join(lines + [CLOSING])
