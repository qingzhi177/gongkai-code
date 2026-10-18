"""分层决策 + 冷却（方案 8.3、8.5 节）。下面的数值都是默认值（起点），要用黄金集校准。
Dashboard「召回对比」页可以改（存在 app_settings 表，见 app/settings.py），每一轮通过 params 传进来。

| 层级     | 条件                         | 动作                                      |
|----------|------------------------------|-------------------------------------------|
| 回忆模式 | 意图是「回忆」               | 取分数 ≥ 中门槛的前 3 条，附原话，不走概率 |
| 强       | 精排分 ≥ T_HIGH（0.75）      | 一定注入，最多 2 条                        |
| 中       | T_LOW（0.5）≤ 精排分 < T_HIGH | 30% 概率注入，条数按 0/1/2 = 20%/60%/20% 抽 |
| 弱       | 精排分 < T_LOW               | 不注入                                    |
| 无由来   | 5% 概率                      | 从情绪浓 / 核心记忆里抽 1 条，冷却 7 天     |

- 精排不可用时退回向量相似度：vec_sim ≥ SURFACE_MIN_SIM（0.55）的只算「中」，不会有「强」。
- 寒暄 / 很短的回复不走相关度召回；「当下任务」把中层概率降到 10%；「情绪」给情绪浓的记忆加 0.05 分。
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

T_HIGH = float(os.getenv("RECALL_T_HIGH", 0.75))
T_LOW = float(os.getenv("RECALL_T_LOW", 0.5))
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

PARAM_KEYS = ("t_high", "t_low", "min_sim", "medium_prob", "task_medium_prob", "serendipity_prob", "max_picks",
              "recall_top", "recent_turns", "medium_cooldown_h", "serendipity_cooldown_d", "emotion_bonus")


def params_from(params=None):
    """决策要用的参数：传进来的（Dashboard 上调过的）优先，没传的用上面的默认值"""
    p = {"t_high": T_HIGH, "t_low": T_LOW, "min_sim": MIN_SIM, "medium_prob": MEDIUM_PROB,
         "task_medium_prob": TASK_MEDIUM_PROB, "serendipity_prob": SERENDIPITY_PROB, "max_picks": MAX_PICKS,
         "recall_top": RECALL_TOP, "recent_turns": RECENT_TURNS, "medium_cooldown_h": MEDIUM_COOLDOWN_H,
         "serendipity_cooldown_d": SERENDIPITY_COOLDOWN_D, "emotion_bonus": EMOTION_BONUS}
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
        scored.append({**c, "score": s})
    scored.sort(key=lambda c: -c["score"])

    def tier_of(c):
        if rerank_ok:
            if c["score"] >= p["t_high"]:
                return "strong"
            if c["score"] >= p["t_low"]:
                return "medium"
            return None
        return "medium" if (c.get("vec_sim") or 0) >= p["min_sim"] else None

    if intent_type == "greeting":
        out["gate"] = "greeting"
    elif intent_type == "recall":
        out["gate"] = "recall"
        for c in scored:
            if tier_of(c) is None:
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
