"""时间通道（方案 8.2 节）：把话里的中文时间说法换算成日期范围，用来找「那段时间」的记忆。

支持的说法（按用户时区的日历天算，「现在」默认取当前时间）：
- 今天 / 昨天（昨晚）/ 前天 / 大前天
- N 天前、几天前、前几天、这几天
- 这周 / 上周 / 上上周，上周三、周三（最近的那个周三）、周末 / 上周末
- 这个月 / 上个月 / 上上个月，N 周前、N 个月前、N 年前
- 三月 / 3 月份（最近的那个三月）、去年三月、10 月 15 号（最近的那天）、去年 10 月 15 日
- 今年 / 去年 / 前年，去年夏天、夏天（最近的那个夏天；冬天 = 12 月到次年 2 月）
说未来的（明天、下周三、明年三月……）不算；「那天」「上次」这类要靠上下文的不算。

parse(text) 返回 [{label, from, to, start, end}]：from / to 是用户时区的日期（含两头），start / end 是 UTC 存储格式的
左闭右开区间，可以直接和库里的 ts 比较。只用标准库和 timeutil。
"""
import re
from datetime import date, datetime, time, timedelta, timezone

import timeutil

CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
SEASON = {"春": (3, 3), "夏": (6, 3), "秋": (9, 3), "冬": (12, 3)}      # 起始月、几个月
NUM = r"(?:\d{1,3}|[零〇一二两三四五六七八九十]{1,4})"


def cn_num(s):
    """'12' / '十二' / '二十五' / '两' → int；解析不了返回 None"""
    s = str(s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        tens = CN_DIGIT.get(a, 1) if a else 1
        ones = CN_DIGIT.get(b, 0) if b else 0
        if (a and a not in CN_DIGIT) or (b and b not in CN_DIGIT):
            return None
        return tens * 10 + ones
    if len(s) == 1 and s in CN_DIGIT:
        return CN_DIGIT[s]
    return None


def _month_start(y, m):
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return date(y, m, 1)


def _month_range(y, m, n=1):
    a = _month_start(y, m)
    b = _month_start(a.year, a.month + n)
    return a, b


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _year_of(prefix, today):
    return {"去年": today.year - 1, "今年": today.year, "前年": today.year - 2}.get(prefix or "")


# ---- 规则：每条 (正则, 处理函数)。处理函数返回 (起始日期, 结束日期(不含)) 或 None（认出来了但不算范围，比如未来）----

def _future(m, today):
    return None


def _date_md(m, today):
    y = _year_of(m.group(1), today)
    mo, d = cn_num(m.group(2)), cn_num(m.group(3))
    if not mo or not d or mo > 12 or d > 31:
        return None
    if y is None:
        y = today.year
        dd = _safe_date(y, mo, d)
        if dd and dd > today:
            y -= 1
    dd = _safe_date(y, mo, d)
    return (dd, dd + timedelta(days=1)) if dd else None


def _season(m, today):
    y = _year_of(m.group(1), today)
    start_m, n = SEASON[m.group(2)]
    if y is not None:
        return _month_range(y, start_m, n)
    for yy in (today.year, today.year - 1):      # 最近的那个（正在这个季节里，就是这一季）
        a, b = _month_range(yy, start_m, n)
        if a <= today:
            return a, b
    return None


def _month(m, today):
    y = _year_of(m.group(1), today)
    mo = cn_num(m.group(2))
    if not mo or mo > 12:
        return None
    if y is None:
        y = today.year if mo <= today.month else today.year - 1
    return _month_range(y, mo)


def _ago(m, today):
    raw, unit = m.group(1), m.group(2)
    if raw == "几":
        lo, hi = 2, 6
    else:
        n = cn_num(raw)
        if n is None or n <= 0 or n > 400:
            return None
        lo = hi = n
    if unit in ("天", "日"):
        return today - timedelta(days=hi), today - timedelta(days=lo) + timedelta(days=1)
    if unit in ("周", "个星期", "星期", "个礼拜", "礼拜"):
        a = _week_start(today) - timedelta(weeks=hi)
        return a, _week_start(today) - timedelta(weeks=lo) + timedelta(days=7)
    if unit in ("个月", "月"):
        a, _ = _month_range(today.year, today.month - hi)
        _, b = _month_range(today.year, today.month - lo)
        return a, b
    if unit == "年":
        return date(today.year - hi, 1, 1), date(today.year - lo + 1, 1, 1)
    return None


def _few_days(m, today):
    if m.group(0) in ("这几天", "最近几天", "这两天", "最近两天"):
        return today - timedelta(days=6 if "几" in m.group(0) else 2), today + timedelta(days=1)
    return today - timedelta(days=7), today      # 前几天：不含今天


def _day(m, today):
    off = {"大前天": 3, "前天": 2, "昨": 1, "今": 0}
    w = m.group(0)
    k = "大前天" if w.startswith("大前天") else ("前天" if w.startswith("前天") else w[0])
    d = today - timedelta(days=off[k])
    return d, d + timedelta(days=1)


def _week(m, today):
    word, wd = m.group(1), m.group(2)
    if "上上" in word:
        base = _week_start(today) - timedelta(weeks=2)
    elif "上" in word:
        base = _week_start(today) - timedelta(weeks=1)
    else:
        base = _week_start(today)
    if wd == "末":
        return base + timedelta(days=5), base + timedelta(days=7)
    if wd:
        d = base + timedelta(days=WEEKDAY[wd])
        return d, d + timedelta(days=1)
    return base, base + timedelta(days=7)


def _weekend(m, today):
    sat = _week_start(today) + timedelta(days=5)
    if sat > today:
        sat -= timedelta(days=7)
    return sat, sat + timedelta(days=2)


def _bare_weekday(m, today):
    wd = WEEKDAY[m.group(1)]
    d = today - timedelta(days=(today.weekday() - wd) % 7)
    return d, d + timedelta(days=1)


def _month_rel(m, today):
    w = m.group(0)
    off = 2 if "上上" in w else (1 if "上" in w else 0)
    return _month_range(today.year, today.month - off)


def _year(m, today):
    y = _year_of(m.group(0), today)
    return date(y, 1, 1), date(y + 1, 1, 1)


RULES = [
    # 未来的说法先吃掉，免得「下周三」里的「周三」、「明年三月」里的「三月」被当成过去
    (re.compile(rf"(?:明年|后年){NUM}月(?:{NUM}[日号])?|(?:明年|后年)|明天|后天|明早|明晚|下下?(?:个)?(?:周|星期|礼拜)[一二三四五六日天末]?|下下?个?月"), _future),
    (re.compile(rf"(去年|今年|前年)?({NUM})月({NUM})[日号]"), _date_md),
    (re.compile(r"(去年|今年|前年)?(?:这个|那个)?(春|夏|秋|冬)(?:天|季)"), _season),
    (re.compile(rf"(去年|今年|前年)?(?<![个上下这本])({NUM})月份?(?!{NUM}[日号]|前|之前|以前)"), _month),
    (re.compile(r"(\d{1,3}|[一二两三四五六七八九十]{1,3}|几)\s*(天|日|个星期|星期|个礼拜|礼拜|周|个月|月|年)(?:前|之前|以前)"), _ago),
    (re.compile(r"前几天|这几天|最近几天|这两天|最近两天"), _few_days),
    (re.compile(r"大前天|前天|昨天|昨晚|昨夜|昨日|今天|今早|今晚|今日"), _day),
    (re.compile(r"(上上(?:个)?(?:周|星期|礼拜)|上(?:个)?(?:周|星期|礼拜)|这(?:个)?(?:周|星期|礼拜)|本周)([一二三四五六日天末])?"), _week),
    (re.compile(r"周末"), _weekend),
    (re.compile(r"(?<![一二两三四五六七八九十几每\d])(?:周|星期|礼拜)([一二三四五六日天])"), _bare_weekday),
    (re.compile(r"上上个月|上个月|上月|这个月|本月"), _month_rel),
    (re.compile(r"去年|今年|前年"), _year),
]


def _to_utc(d):
    return datetime.combine(d, time(0, 0), tzinfo=timeutil.USER_TZ).astimezone(timezone.utc).strftime(timeutil.STORE_FMT)


def parse(text, now=None):
    """[{label, from, to, start, end}]，按在话里出现的先后排序；同一个范围只留一个"""
    text = str(text or "")
    if not text:
        return []
    now_local = timeutil.to_local(now) if now is not None else timeutil.to_local(timeutil.utc_now())
    today = now_local.date()
    taken = []
    found = []
    for rx, fn in RULES:
        for m in rx.finditer(text):
            a, b = m.span()
            if any(a < y and x < b for x, y in taken):
                continue
            taken.append((a, b))
            r = fn(m, today)
            if not r or not r[0] or not r[1] or r[1] <= r[0]:
                continue
            start, end = r
            if start > today:      # 整个在未来的，不算
                continue
            found.append((a, {"label": m.group(0), "from": start.isoformat(),
                              "to": (end - timedelta(days=1)).isoformat(),
                              "start": _to_utc(start), "end": _to_utc(end)}))
    found.sort(key=lambda x: x[0])
    out, seen = [], set()
    for _, r in found:
        k = (r["start"], r["end"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def contains(ranges, ts):
    """ts（库里的 UTC 时间）落在任何一个范围里"""
    t = timeutil.to_utc_str(ts) if ts else None
    if not t:
        return False
    return any(r["start"] <= t < r["end"] for r in ranges)
