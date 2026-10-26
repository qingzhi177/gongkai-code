"""运行时可调参数（Dashboard 上能改的那些）。

存在 app_settings 表（迁移 005）：「组名.参数名」→ JSON 值。没改过的项用默认值（.env 或代码里的常量），
「恢复默认」就是删掉那一行。召回参数（recall.*）、聊天里放什么（compose.*），以及各层的开关。

召回编排每一轮读一次 get("recall")：有 5 秒缓存，保存 / 恢复默认后立即失效，不用重启。
"""
import json
import sqlite3
import threading
import time

import timeutil
from app.config import get_conn
from app.recall import decide as D
from app.recall import rerank as R

CACHE_TTL = 5.0


def _item(key, typ, default, lo, hi, step, section, label, help_text):
    return {"key": key, "type": typ, "default": default, "min": lo, "max": hi, "step": step,
            "section": section, "label": label, "help": help_text}


SPECS = {
    "recall": [
        _item("t_high", float, D.T_HIGH, 0.0, 1.0, 0.01, "门槛", "强门槛",
              "精排分 ≥ 这个值：一定会想起（受「每轮最多几条」限制）。调低会想起得更多、更频繁。"),
        _item("t_low", float, D.T_LOW, 0.0, 1.0, 0.01, "门槛", "中门槛",
              "精排分在中门槛和强门槛之间：按「中层概率」偶尔想起；低于中门槛的不会想起。不能高于强门槛。"),
        _item("t_recall", float, D.T_RECALL, 0.0, 1.0, 0.01, "门槛", "回忆门槛",
              "问「还记得吗」「上次……」时（回忆模式），精排分 ≥ 这个值的取前几条，附原话，不走概率。比平时的门槛低：你在主动问。"),
        _item("min_sim", float, D.MIN_SIM, 0.0, 1.0, 0.01, "门槛", "向量兜底门槛",
              "精排暂时不可用（超时、出错、关掉了）时：平时不想起；问「还记得吗」时改看向量相似度，≥ 这个值的取前几条。"),
        _item("medium_prob", float, D.MEDIUM_PROB, 0.0, 1.0, 0.01, "概率", "中层概率",
              "有「中」候选时，这一轮真的想起的概率（0.3 = 30%）。"),
        _item("task_medium_prob", float, D.TASK_MEDIUM_PROB, 0.0, 1.0, 0.01, "概率", "做事时的中层概率",
              "识别成「当下任务」（帮我写、帮我查……）时改用这个概率，默认更低，专心做事。"),
        _item("serendipity_prob", float, D.SERENDIPITY_PROB, 0.0, 1.0, 0.01, "概率", "无由来浮现概率",
              "每一轮和话题无关、忽然想起一件情绪浓或核心的事的概率（0.05 = 5%）。"),
        _item("emotion_bonus", float, D.EMOTION_BONUS, 0.0, 0.3, 0.01, "概率", "情绪加分",
              "聊到情绪（难过、想你……）时，给情绪浓的记忆额外加的分。"),
        _item("max_picks", int, D.MAX_PICKS, 0, 5, 1, "条数和冷却", "每轮最多几条",
              "平时每一轮最多想起几条（0 = 不想起）。"),
        _item("recall_top", int, D.RECALL_TOP, 1, 6, 1, "条数和冷却", "回忆时最多几条",
              "识别成「回忆」（还记得吗、上次……）时最多取几条。"),
        _item("recent_turns", int, D.RECENT_TURNS, 0, 50, 1, "条数和冷却", "最近几轮不重复",
              "同一个对话里，最近这么多轮出现过的不再出现。"),
        _item("medium_cooldown_h", float, D.MEDIUM_COOLDOWN_H, 0.0, 720.0, 1, "条数和冷却", "中层冷却（小时）",
              "因为「中」被想起过的，这么久之内不再因为「中」出现（跨对话）。"),
        _item("serendipity_cooldown_d", float, D.SERENDIPITY_COOLDOWN_D, 0.0, 90.0, 1, "条数和冷却", "无由来冷却（天）",
              "无由来浮现过的，这么多天内不再无由来出现。"),
        _item("time_enabled", bool, True, None, None, None, "时间", "启用时间通道",
              "话里提到时间（昨天、上周三、去年夏天、10 月 15 号……）时，把发生在那段时间里的记忆也找出来。"),
        _item("time_bonus", float, D.TIME_BONUS, 0.0, 0.3, 0.01, "时间", "时间加分",
              "发生在话里提到的那段时间里的记忆，精排分额外加这么多。"),
        _item("rerank_enabled", bool, R.RERANK_ENABLED, None, None, None, "精排", "启用精排",
              "关掉以后平时都不会想起，只有问「还记得吗」时按向量相似度找。"),
        _item("rerank_model", str, R.RERANK_MODEL, None, None, None, "精排", "精排模型",
              "阿里云百炼的文本排序模型，比如 qwen3.7-text-rerank、qwen3-rerank、gte-rerank-v2。"),
        _item("rerank_timeout", float, R.RERANK_TIMEOUT, 0.3, 10.0, 0.1, "精排", "精排超时（秒）",
              "超过这个时间就放弃精排、改用向量相似度。"),
    ],
    "compose": [
        _item("recall_live", bool, True, None, None, None, "召回", "用新的召回（正式）",
              "开：每一轮由记忆服务决定想起什么（分层、冷却、日常报备不召回），放进聊天；"
              "关：回到以前网关的随机浮现，新逻辑只在旁边记录（影子模式），方便对比或者出问题时退回去。"),
        _item("facts", bool, True, None, None, None, "放进聊天的内容", "事实",
              "「事实」页的档案几行（关于你 / 关于我 / 我们之间），接在人格画像后面。"),
        _item("chapters", bool, True, None, None, None, "放进聊天的内容", "章节",
              "章节目录 + 进行中的这一章，代替以前整篇的「共同经历叙事」。关掉就放回旧的叙事。"),
        _item("arcs", bool, True, None, None, None, "放进聊天的内容", "主题线",
              "进行中的主题卷：每条一行（标题 + 一句话）。"),
        _item("lexicon", bool, True, None, None, None, "放进聊天的内容", "共同语汇",
              "收下的称呼、梗、固定说法和它们的意思（常用的在前，有长度上限）。"),
        _item("state", bool, True, None, None, None, "放进聊天的内容", "关系状态",
              "「我们最近」这张卡（节奏、你的天气、正在一起经历的、最近的亲近时刻、轻放的话题）。"),
        _item("digest", bool, True, None, None, None, "放进聊天的内容", "近七天摘要",
              "新版的近七天摘要，代替以前的「近期摘要」。关掉就放回旧的。"),
        _item("episodes", bool, True, None, None, None, "放进聊天的内容", "最近几天的片段",
              "最近两个聊过天的日子里每日片段的标题；话里提到某段时间（昨天、上周三……）时，那段时间的片段也会浮现。"),
        _item("loops", bool, True, None, None, None, "放进聊天的内容", "还没了结的事",
              "「未完的事」的开窗几件（「不主动提」的不放）。"),
    ],
    "episodes": [
        _item("enabled", bool, True, None, None, None, "整理", "自动整理每日片段",
              "每天把对话整理成片段（会调用模型，花一点点钱）。关掉以后只能手动点「整理」。"),
    ],
    "arcs": [
        _item("enabled", bool, True, None, None, None, "归卷", "自动归卷和写卷",
              "新片段整理好以后自动归进主题卷，确认过的卷有新素材时自动写（会调用模型，花一点点钱）。"),
        _item("propose_min_episodes", int, 3, 2, 20, 1, "归卷", "几个片段才提议开卷",
              "一个新主题积累到这么多个片段（并且跨了足够的天数），才在 Dashboard 上请你确认。"),
        _item("propose_min_days", int, 2, 1, 30, 1, "归卷", "跨几天才提议开卷",
              "一个新主题的片段至少分布在这么多天，才请你确认（避免一次聊天就开一个卷）。"),
        _item("narrate_min_new", int, 2, 1, 20, 1, "写卷", "攒几个新片段写一次",
              "确认过的卷，攒够这么多个还没写进去的新片段，就追加一小节。"),
        _item("dormant_days", int, 30, 7, 365, 1, "写卷", "多少天没新素材算休眠",
              "这么多天没有新素材的卷转为「休眠」，有新素材时自动醒来。"),
    ],
    "digests": [
        _item("enabled", bool, True, None, None, None, "摘要", "自动写日摘、周摘和近七天摘要",
              "每日片段整理好以后自动写那天的日摘，一周结束写周摘，近七天摘要跟着更新（会调用模型，花一点点钱）。"),
    ],
    "lexicon": [
        _item("enabled", bool, True, None, None, None, "语汇", "自动找共同语汇",
              "每日片段整理好以后，扫一遍那天的对话找新的称呼、小梗、固定说法（会调用模型，花一点点钱）。"),
        _item("auto_activate_days", int, 2, 1, 10, 1, "语汇", "出现几天就自动收下",
              "候选的词在这么多天的对话里都出现过，就自动收下（1 = 找到就收下）。没到的留在候选里等你看。"),
    ],
    "loops": [
        _item("enabled", bool, True, None, None, None, "未完的事", "自动找未完的事",
              "每日片段整理好以后，扫一遍那天的对话：记下新的约定、计划、在等的结果、牵挂，也看看以前的事有没有下文（会调用模型，花一点点钱）。"),
        _item("expire_after_days", int, 14, 1, 120, 1, "未完的事", "过了期限几天自动过期",
              "有软期限的事，过了期限这么多天、之后也没再提，就自动标成「过期了」（随时可以重新打开）。"),
        _item("stale_days", int, 45, 7, 365, 1, "未完的事", "多少天没提自动过期",
              "没有期限的事，这么多天都没再提，就自动标成「过期了」（随时可以重新打开）。"),
        _item("pack_size", int, 5, 1, 10, 1, "未完的事", "开窗时最多列几件",
              "以后每次开始聊天时，最多把这么多件还没了结的事告诉我（「不主动提」的不算）。"),
    ],
    "facts": [
        _item("enabled", bool, True, None, None, None, "事实", "自动找事实",
              "每日片段整理好以后，扫一遍那天的对话：记下住在哪、在读什么、家里的猫叫什么、我们之间的称呼这类不常变的事，"
              "说法变了能分清是真变了还是随口一提（会调用模型，花一点点钱）。"),
    ],
    "state": [
        _item("enabled", bool, True, None, None, None, "关系状态", "自动更新关系状态卡",
              "日摘、未完的事、主题卷、话题边界有变化时，自动重写「我们最近」这张卡（会调用模型，花一点点钱）。"),
    ],
    "chapters": [
        _item("enabled", bool, True, None, None, None, "章节", "自动写章节",
              "每日片段整理好以后，把新片段写进进行中的那一章；封存的章自动写摘要（会调用模型，花一点点钱）。"),
        _item("seal_after_days", int, 30, 7, 365, 1, "章节", "一章跨了多少天就提议分章",
              "进行中的这一章跨了这么多天、而且写得够长了，就请模型提议在哪里分开（要你确认才生效）。"),
        _item("seal_min_chars", int, 2500, 500, 20000, 100, "章节", "一章写了多少字就提议分章",
              "和上一项一起满足才提议。"),
        _item("min_chapter_days", int, 7, 1, 90, 1, "章节", "每章至少几天",
              "提议分章时，每一章至少跨这么多天（进行中的最后一章除外）。"),
    ],
}

TYPE_NAMES = {float: "float", int: "int", bool: "bool", str: "str"}

_lock = threading.Lock()
_cache = {}   # 组名 -> (时间, {参数: 值})


def _spec(group):
    if group not in SPECS:
        raise ValueError(f"没有这组参数：{group}")
    return {s["key"]: s for s in SPECS[group]}


def _fmt(x):
    return int(x) if isinstance(x, float) and x.is_integer() else x


def coerce(spec, v):
    """把传进来的值转成参数的类型并检查范围；不合法时抛 ValueError（中文说明）"""
    t, label = spec["type"], spec["label"]
    if t is bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        if isinstance(v, str) and v.strip().lower() in ("1", "true", "on", "yes", "0", "false", "off", "no"):
            return v.strip().lower() in ("1", "true", "on", "yes")
        raise ValueError(f"「{label}」只能是开或关")
    if t is str:
        s = str(v if v is not None else "").strip()
        if not s or len(s) > 100:
            raise ValueError(f"「{label}」不能为空，也不能超过 100 个字符")
        return s
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"「{label}」要填一个数字")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"「{label}」要填一个数字")
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError(f"「{label}」要填一个数字")
    if t is int:
        if not x.is_integer():
            raise ValueError(f"「{label}」要填整数")
        x = int(x)
    if (spec["min"] is not None and x < spec["min"]) or (spec["max"] is not None and x > spec["max"]):
        raise ValueError(f"「{label}」要在 {_fmt(spec['min'])} 到 {_fmt(spec['max'])} 之间")
    return x


def _check_recall(vals):
    if vals["t_low"] > vals["t_high"]:
        raise ValueError(f"中门槛（{vals['t_low']}）不能高于强门槛（{vals['t_high']}）")


CHECKS = {"recall": _check_recall}


def _overrides(conn, group):
    """{参数: (值, 更新时间)}；表还没建（迁移前）时返回空"""
    try:
        rows = conn.execute("SELECT key, value, updated_at FROM app_settings WHERE key LIKE ?",
                            (group + ".%",)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for k, v, ts in rows:
        try:
            out[k[len(group) + 1:]] = (json.loads(v), ts)
        except Exception:
            pass
    return out


def _effective(group, ov):
    vals = {}
    for s in SPECS[group]:
        v = s["default"]
        if s["key"] in ov:
            try:
                v = coerce(s, ov[s["key"]][0])
            except ValueError:
                pass   # 库里的值不合法（手改过？）就当没改过
        vals[s["key"]] = v
    return vals


def invalidate(group=None):
    with _lock:
        if group:
            _cache.pop(group, None)
        else:
            _cache.clear()


def get(group, fresh=False):
    """这一组参数的当前值（改过的用改过的，其余用默认值）"""
    _spec(group)
    now = time.monotonic()
    if not fresh:
        with _lock:
            hit = _cache.get(group)
            if hit and now - hit[0] < CACHE_TTL:
                return dict(hit[1])
    conn = get_conn()
    try:
        vals = _effective(group, _overrides(conn, group))
    finally:
        conn.close()
    with _lock:
        _cache[group] = (now, vals)
    return dict(vals)


def describe(group):
    """给 Dashboard 用：每个参数的名称、说明、范围、默认值、当前值、是否改过"""
    conn = get_conn()
    try:
        ov = _overrides(conn, group)
    finally:
        conn.close()
    vals = _effective(group, ov)
    items = []
    for s in SPECS[group]:
        items.append({**{k: s[k] for k in ("key", "label", "help", "section", "min", "max", "step", "default")},
                      "type": TYPE_NAMES[s["type"]], "value": vals[s["key"]],
                      "overridden": s["key"] in ov, "updated_at": ov[s["key"]][1] if s["key"] in ov else None})
    times = [ov[k][1] for k in ov]
    return {"group": group, "items": items, "updated_at": max(times) if times else None}


def update(group, values):
    """保存一批参数（只存传进来的）。任何一个不合法就整批不存，抛 ValueError。返回保存后的当前值。"""
    spec = _spec(group)
    clean = {}
    for k, v in (values or {}).items():
        if k not in spec:
            raise ValueError(f"没有这个参数：{k}")
        clean[k] = coerce(spec[k], v)
    if not clean:
        return get(group, fresh=True)
    now = timeutil.utc_now_str()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        merged = {**_effective(group, _overrides(conn, group)), **clean}
        if group in CHECKS:
            CHECKS[group](merged)
        for k, v in clean.items():
            conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (f"{group}.{k}", json.dumps(v, ensure_ascii=False), now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        invalidate(group)
    return get(group, fresh=True)


def reset(group, keys=None):
    """恢复默认：删掉这些参数（不传就是整组）。恢复后如果和别的改过的参数冲突（比如中门槛高于强门槛），抛 ValueError、不改。"""
    spec = _spec(group)
    keys = list(keys or [])
    for k in keys:
        if k not in spec:
            raise ValueError(f"没有这个参数：{k}")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if keys:
            conn.executemany("DELETE FROM app_settings WHERE key = ?", [(f"{group}.{k}",) for k in keys])
        else:
            conn.execute("DELETE FROM app_settings WHERE key LIKE ?", (group + ".%",))
        merged = _effective(group, _overrides(conn, group))
        if group in CHECKS:
            CHECKS[group](merged)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        invalidate(group)
    return get(group, fresh=True)
