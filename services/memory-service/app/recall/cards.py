"""念头式卡片（方案 8.7 节）：沿用网关现在的写法——「（⭐ 昨天，……）」，模糊时间，只是提醒、不用逐条回应。

和网关 server.js 里的 formatMemoriesAsThoughts / toFuzzyTime 保持同样的措辞，影子模式下新旧两边才好对比；
另外加了两点：
- 回忆模式（「还记得吗」）附上原话；
- AI 自己记下的感受（author = ai_felt）标明「我记下的感受」。
"""
import timeutil

SEASONS = ["冬天", "冬末", "早春", "春天", "晚春", "初夏", "夏天", "盛夏", "初秋", "秋天", "深秋", "初冬"]


def fuzzy_time(ts, now=None):
    """精确时间 → 模糊说法（按用户时区的日历天算）"""
    mem = timeutil.to_local(ts)
    if mem is None:
        return "不知何时" if not ts else "很久之前"
    now = timeutil.to_local(now) if now is not None else timeutil.to_local(timeutil.utc_now())
    days = (now.date() - mem.date()).days
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


def thought_line(m, with_quote=False, now=None):
    t = fuzzy_time(m.get("ts"), now=now)
    core = "⭐ " if m.get("is_core") else ""
    content = (m.get("content") or "").strip()
    if m.get("author") == "ai_felt":
        content = "我记下的感受：" + content
    line = f"（{core}{t}，{content}"
    quote = (m.get("quote") or "").strip()
    if with_quote and quote:
        line += f"——原话「{quote}」"
    return line + "）"


def format_card(picks, intent_type="normal", now=None):
    """picks：[{...元数据, "tier": ...}]。没有要注入的返回空串。"""
    if not picks:
        return ""
    with_quote = intent_type == "recall"
    lines = [thought_line(m, with_quote=with_quote or m.get("tier") == "recall", now=now) for m in picks]
    return "\n" + "\n\n".join(lines) + "\n"
