"""分层决策 + 冷却（方案 8.3、8.5 节）。下面的数值是默认值，Dashboard「召回对比」页可以改
（存在 app_settings 表，见 app/settings.py），每一轮通过 params 传进来。

| 层级     | 条件                           | 动作                                       |
|----------|--------------------------------|--------------------------------------------|
| 回忆模式 | 意图是「回忆」                 | 取分数 ≥ 回忆门槛（0.5）的前 3 条，附原话，不走概率 |
| 强       | 精排分 ≥ T_HIGH（0.85）        | 一定注入，最多 2 条                         |
| 中       | T_LOW（0.75）≤ 精排分 < T_HIGH | 30% 概率注入，条数按 0/1/2 = 20%/60%/20% 抽  |
| 弱       | 精排分 < T_LOW                 | 不注入                                     |
| 无由来   | 5% 概率                        | 从情绪浓 / 核心记忆里抽 1 条，冷却 7 天      |

门槛是 2026-10-26 用黄金集（57 轮标注）校准的：起点 0.75 / 0.5 时，单看一条条记忆，强门槛的误浮现率只有 6%，
但按整轮算，平常聊天里每 3 轮就有 1 轮会一定想起一件不相干的事（精排分对「同一个称呼、同样的日常」很高），
中层 0.5 以上几乎每轮都有候选。改成 0.85 / 0.75（再加上日常报备不召回）以后，标注过的「不该想起」的 52 轮里
只有 2 轮会一定想起；问「还记得吗」时单独用回忆门槛，不受影响。

- 精排不可用时（超时、出错，大约每 20 轮 1 轮）：平时不想起——向量相似度分不开该不该想起（黄金集里 0.55 以上的
  「不该想起」有 87%）；只有问「还记得吗」时改看向量相似度 ≥ SURFACE_MIN_SIM（0.55）的。
- 寒暄 / 很短的回复不走相关度召回；「当下任务」把中层概率降到 10%；「情绪」给情绪浓的记忆加 0.05 分；
  话里提到了时间（昨天、上周三、去年夏天……），发生在那段时间里的记忆加 0.08 分。
- 不回填：一条因为冷却被拿掉，不会拿下一条更弱的来凑数。
- 每轮最多 2 条（回忆模式 3 条）。

冷却（API 模式；只看投递账本 injections，影子模式下看影子自己的记录，和网关旧逻辑互不干扰）：
- 同一会话最近 5 轮出现过的不再出现；
- 中层额外冷却 24 小时（跨会话）；
- 强层过了 5 轮以后，如果依然强相关，可以再出现（API 模式下模型已经看不到它了）；
- 无由来冷却 7 天。
"""
import os
import random

T_HIGH = float(os.getenv("RECALL_T_HIGH", 0.85))
T_LOW = float(os.getenv("RECALL_T_LOW", 0.75))
T_RECALL = float(os.getenv("RECALL_T_RECALL", 0.5))      # 回忆模式（还记得吗）的门槛
MIN_SIM = float(os.getenv("SURFACE_MIN_SIM", 0.55))      # 精排不可用时的向量相似度门槛（和网关同一个变量）
MEDIUM_PROB = 0.3
TASK_MEDIUM_PROB = 0.1
SHOW_COUNT_WEIGHTS = [(0, 0.2), (1, 0.6), (2, 0.2)]
SERENDIPITY_PROB = 0.05
MAX_PICKS = 2
RECALL_TOP = 3
RECENT_TURNS = 5
MEDIUM_COOLDOWN_H = 24
SERENDIPITY_COOLDOWN_D = 7
EMOTION_BONUS = 0.05
TIME_BONUS = 0.08

PARAM_KEYS = ("t_high", "t_low", "t_recall", "min_sim", "medium_prob", "task_medium_prob", "serendipity_prob", "max_picks",
              "recall_top", "recent_turns", "medium_cooldown_h", "serendipity_cooldown_d", "emotion_bonus", "time_bonus")


def params_from(params=None):
    """决策要用的参数：传进来的（Dashboard 上调过的）优先，没传的用上面的默认值"""
    p = {"t_high": T_HIGH, "t_low": T_LOW, "t_recall": T_RECALL, "min_sim": MIN_SIM, "medium_prob": MEDIUM_PROB,
         "task_medium_prob": TASK_MEDIUM_PROB, "serendipity_prob": SERENDIPITY_PROB, "max_picks": MAX_PICKS,
         "recall_top": RECALL_TOP, "recent_turns": RECENT_TURNS, "medium_cooldown_h": MEDIUM_COOLDOWN_H,
         "serendipity_cooldown_d": SERENDIPITY_COOLDOWN_D, "emotion_bonus": EMOTION_BONUS, "time_bonus": TIME_BONUS}
    for k, v in (params or {}).items():
        if k in p and v is not None:
            p[k] = v
    return p


def _num(x):
    return int(x) if float(x).is_integer() else x


def _weighted_choice(options, rng):
    total = sum(w for _, w in options)
    x = rng.random() * total
    for v, w in options:
        x -= w
        if x <= 0:
            return v
    return options[-1][0]


def _weighted_sample(items, n, rng):
    pool = list(items)
    out = []
    while pool and len(out) < n:
        total = sum(max(c["score"], 1e-6) for c in pool)
        x = rng.random() * total
        for i, c in enumerate(pool):
            x -= max(c["score"], 1e-6)
            if x <= 0:
                out.append(pool.pop(i))
                break
        else:
            out.append(pool.pop())
    return out


def decide(cands, intent_type, cooldown, rerank_ok, serendipity_pool=(), rng=None, params=None):
    """cands：[{id, score(精排分或向量相似度), vec_sim, arousal, is_core, ...}]，按分数从高到低。
    cooldown：{"recent": set(最近 5 轮出现过的 id), "medium": set(24 小时内出现过的), "serendipity": set(7 天内无由来出现过的)}
    params：可调参数（见 params_from），不传就用默认值。
    返回 {"picks": [...], "eligible": {...}, "dropped": [...], "gate": ...}"""
    rng = rng or random.Random()
    p = params_from(params)
    recent_reason = f"最近 {_num(p['recent_turns'])} 轮出现过"
    medium_reason = f"中层 {_num(p['medium_cooldown_h'])} 小时冷却"
    recent, medium_cd, seren_cd = cooldown.get("recent", set()), cooldown.get("medium", set()), cooldown.get("serendipity", set())
    out = {"picks": [], "eligible": {"recall": [], "strong": [], "medium": []}, "dropped": [], "gate": None,
           "basis": "rerank" if rerank_ok else "vec_sim"}

    scored = []
    for c in cands:
        s = c["score"]
        if intent_type == "emotion" and (c.get("arousal") or 0) >= 0.6:
            s = round(s + p["emotion_bonus"], 4)
        if c.get("in_time"):
            s = round(s + p["time_bonus"], 4)
        scored.append({**c, "score": s})
    scored.sort(key=lambda c: -c["score"])

    def tier_of(c):
        if rerank_ok:
            if c["score"] >= p["t_high"]:
                return "strong"
            if c["score"] >= p["t_low"]:
                return "medium"
            return None
        return None          # 精排不可用：平时不想起（向量相似度分不开），见上面的说明

    def recallable(c):
        if rerank_ok:
            return c["score"] >= p["t_recall"]
        return (c.get("vec_sim") or 0) >= p["min_sim"]

    if intent_type == "greeting":
        out["gate"] = "greeting"
    elif intent_type == "recall":
        out["gate"] = "recall"
        for c in scored:
            if not recallable(c):
                continue
            if c["id"] in recent:
                out["dropped"].append({"id": c["id"], "reason": recent_reason})
                continue
            out["eligible"]["recall"].append(c["id"])
            if len(out["picks"]) < p["recall_top"]:
                out["picks"].append({**c, "tier": "recall"})
    else:
        strong, medium = [], []
        for c in scored:
            t = tier_of(c)
            if t is None:
                continue
            if c["id"] in recent:
                out["dropped"].append({"id": c["id"], "reason": recent_reason})
                continue
            if t == "medium" and c["id"] in medium_cd:
                out["dropped"].append({"id": c["id"], "reason": medium_reason})
                continue
            (strong if t == "strong" else medium).append(c)
        out["eligible"]["strong"] = [c["id"] for c in strong]
        out["eligible"]["medium"] = [c["id"] for c in medium]
        out["picks"] = [{**c, "tier": "strong"} for c in strong[:p["max_picks"]]]
        room = p["max_picks"] - len(out["picks"])
        if room > 0 and medium:
            prob = p["task_medium_prob"] if intent_type == "task" else p["medium_prob"]
            if rng.random() < prob:
                n = min(room, _weighted_choice(SHOW_COUNT_WEIGHTS, rng))
                out["gate"] = f"medium_open:{n}"
                out["picks"] += [{**c, "tier": "medium"} for c in _weighted_sample(medium, n, rng)]
            else:
                out["gate"] = "medium_closed"

    # 无由来浮现：默认 5% 概率，和相关度无关（寒暄时也可能有）
    limit = p["recall_top"] if intent_type == "recall" else p["max_picks"]
    if limit > 0 and serendipity_pool and rng.random() < p["serendipity_prob"]:
        picked_ids = {p["id"] for p in out["picks"]}
        pool = [m for m in serendipity_pool if m["id"] not in seren_cd and m["id"] not in recent and m["id"] not in picked_ids]
        if pool:
            weights = [((m.get("arousal") or 0) + (0.5 if m.get("is_core") else 0)) or 0.1 for m in pool]
            total = sum(weights)
            x = rng.random() * total
            choice = pool[-1]
            for m, w in zip(pool, weights):
                x -= w
                if x <= 0:
                    choice = m
                    break
            out["picks"] = ([{**choice, "tier": "serendipity", "score": None}] + out["picks"])[:limit]
    return out
