"""分层决策 + 冷却（方案 8.3、8.5 节）。下面的数值都是起点，要用黄金集校准。

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


def decide(cands, intent_type, cooldown, rerank_ok, serendipity_pool=(), rng=None):
    """cands：[{id, score(精排分或向量相似度), vec_sim, arousal, is_core, ...}]，按分数从高到低。
    cooldown：{"recent": set(最近 5 轮出现过的 id), "medium": set(24 小时内出现过的), "serendipity": set(7 天内无由来出现过的)}
    返回 {"picks": [...], "eligible": {...}, "dropped": [...], "gate": ...}"""
    rng = rng or random.Random()
    recent, medium_cd, seren_cd = cooldown.get("recent", set()), cooldown.get("medium", set()), cooldown.get("serendipity", set())
    out = {"picks": [], "eligible": {"recall": [], "strong": [], "medium": []}, "dropped": [], "gate": None,
           "basis": "rerank" if rerank_ok else "vec_sim"}

    scored = []
    for c in cands:
        s = c["score"]
        if intent_type == "emotion" and (c.get("arousal") or 0) >= 0.6:
            s = round(s + EMOTION_BONUS, 4)
        scored.append({**c, "score": s})
    scored.sort(key=lambda c: -c["score"])

    def tier_of(c):
        if rerank_ok:
            if c["score"] >= T_HIGH:
                return "strong"
            if c["score"] >= T_LOW:
                return "medium"
            return None
        return "medium" if (c.get("vec_sim") or 0) >= MIN_SIM else None

    if intent_type == "greeting":
        out["gate"] = "greeting"
    elif intent_type == "recall":
        out["gate"] = "recall"
        for c in scored:
            if tier_of(c) is None:
                continue
            if c["id"] in recent:
                out["dropped"].append({"id": c["id"], "reason": "最近 5 轮出现过"})
                continue
            out["eligible"]["recall"].append(c["id"])
            if len(out["picks"]) < RECALL_TOP:
                out["picks"].append({**c, "tier": "recall"})
    else:
        strong, medium = [], []
        for c in scored:
            t = tier_of(c)
            if t is None:
                continue
            if c["id"] in recent:
                out["dropped"].append({"id": c["id"], "reason": "最近 5 轮出现过"})
                continue
            if t == "medium" and c["id"] in medium_cd:
                out["dropped"].append({"id": c["id"], "reason": "中层 24 小时冷却"})
                continue
            (strong if t == "strong" else medium).append(c)
        out["eligible"]["strong"] = [c["id"] for c in strong]
        out["eligible"]["medium"] = [c["id"] for c in medium]
        out["picks"] = [{**c, "tier": "strong"} for c in strong[:MAX_PICKS]]
        room = MAX_PICKS - len(out["picks"])
        if room > 0 and medium:
            prob = TASK_MEDIUM_PROB if intent_type == "task" else MEDIUM_PROB
            if rng.random() < prob:
                n = min(room, _weighted_choice(SHOW_COUNT_WEIGHTS, rng))
                out["gate"] = f"medium_open:{n}"
                out["picks"] += [{**c, "tier": "medium"} for c in _weighted_sample(medium, n, rng)]
            else:
                out["gate"] = "medium_closed"

    # 无由来浮现：5% 概率，和相关度无关（寒暄时也可能有）
    limit = RECALL_TOP if intent_type == "recall" else MAX_PICKS
    if serendipity_pool and rng.random() < SERENDIPITY_PROB:
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
