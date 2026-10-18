"""召回编排测试（app/recall/）

用法（在 services/memory-service 目录下）：
    venv/bin/python ../../tests/manual/test_recall.py

用临时库、假的嵌入 / 精排 / 向量检索，不调用任何外部接口，不碰真实数据。
"""
import asyncio
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="recalltest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["RERANK_ENABLED"] = "1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "services" / "memory-service"))

import db  # noqa: E402
from app import config  # noqa: E402
from app.recall import cards, decide as D, intent, orchestrator, traces  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


# ---------- 1. 意图 ----------
def test_intent():
    print("\n[1] 意图识别")
    cases = {
        "你还记得我们第一次聊天吗": "recall", "上次那首歌叫什么来着": "recall", "我们之前说过要去看海": "recall",
        "睡觉之前想跟你说说话": "normal", "我叫什么名字呀": "fact", "我的生日是哪天你知道吗": "fact",
        "帮我写一段代码": "task", "今天好累啊，有点难过": "emotion", "嗯嗯": "greeting", "哈哈哈哈": "greeting",
        "晚安～": "greeting", "好呀": "greeting", "今天去公园看到很多银杏叶": "normal", "然后呢": "followup",
        "真的吗？": "followup",
    }
    bad = {t: (intent.classify(t)["type"], want) for t, want in cases.items() if intent.classify(t)["type"] != want}
    check(f"{len(cases)} 句全部判对", not bad, bad)
    check("短消息拼上上文", orchestrator.build_query("然后呢", "我今天去面试了", "怎么样？") == "我今天去面试了 怎么样？ 然后呢")
    check("长消息不拼", orchestrator.build_query("今天去公园看到很多银杏叶", "a", "b") == "今天去公园看到很多银杏叶")


# ---------- 2. 模糊时间与卡片 ----------
def test_cards():
    print("\n[2] 模糊时间、念头式卡片")
    now = "2026-10-18 04:00:00"            # 北京时间 10-18 12:00
    check("北京时间前一天晚上 → 昨天", cards.fuzzy_time("2026-10-17 15:30:00", now=now) == "昨天")
    check("UTC 前一天、北京时间当天凌晨 → 今天早些时候", cards.fuzzy_time("2026-10-17 16:30:00", now=now) == "今天早些时候")
    check("十天前 → 前阵子", cards.fuzzy_time("2026-10-08 04:00:00", now=now) == "前阵子")
    check("三个月前 → 季节", cards.fuzzy_time("2026-07-10 04:00:00", now=now) == "夏天")
    check("去年 → 去年+季节", cards.fuzzy_time("2025-04-10 04:00:00", now=now) == "去年春天")
    check("没有时间", cards.fuzzy_time(None) == "不知何时")
    picks = [{"id": 1, "content": "一起听过晴天", "quote": "下雨就听这首吧", "ts": "2026-10-17 12:00:00", "is_core": 1, "tier": "strong"},
             {"id": 2, "content": "那次她说累，我很想多陪她一会儿", "ts": "2026-10-17 12:00:00", "author": "ai_felt", "tier": "medium"}]
    c = cards.format_card(picks, "normal", now=now)
    check("核心记忆有星标、感受有标签、平时不带原话",
          "（⭐ 昨天，一起听过晴天）" in c and "我记下的感受：" in c and "原话" not in c, c)
    c2 = cards.format_card(picks[:1], "recall", now=now)
    check("回忆模式附原话", "——原话「下雨就听这首吧」" in c2, c2)
    check("没有要注入的返回空串", cards.format_card([], "normal") == "")


# ---------- 3. 融合与决策 ----------
def C(i, score, vec=None, arousal=0.3):
    return {"id": i, "score": score, "vec_sim": vec if vec is not None else score, "arousal": arousal, "content": f"m{i}"}


def test_decide():
    print("\n[3] 融合、分层决策、冷却")
    fused = orchestrator.fuse([(1, 0.9), (2, 0.8), (3, 0.7)], [(3, 5.0), (4, 4.0)], "normal")
    check("RRF：两个通道都排在前面的最靠前", fused[0][0] == 3 and {m for m, _ in fused} == {1, 2, 3, 4}, fused)
    nocd = {"recent": set(), "medium": set(), "serendipity": set()}
    r = D.decide([C(1, 0.9), C(2, 0.8), C(3, 0.78), C(4, 0.6)], "normal", nocd, True, rng=random.Random(1))
    check("强层一定注入，最多 2 条", [p["id"] for p in r["picks"]] == [1, 2] and all(p["tier"] == "strong" for p in r["picks"]), r["picks"])
    r = D.decide([C(4, 0.6), C(5, 0.55)], "normal", nocd, True, rng=random.Random(0))
    check("只有中层时按 30% 概率开放（这次关闭）", r["picks"] == [] and r["gate"] == "medium_closed" and r["eligible"]["medium"] == [4, 5], r)
    opened = 0
    for seed in range(200):
        rr = D.decide([C(4, 0.6), C(5, 0.55)], "normal", nocd, True, rng=random.Random(seed))
        opened += bool(rr["gate"] and rr["gate"].startswith("medium_open"))
    check("中层开放比例约 30%", 40 <= opened <= 85, opened)
    r = D.decide([C(1, 0.3), C(2, 0.2)], "normal", nocd, True, rng=random.Random(1))
    check("弱层不注入", r["picks"] == [] and r["eligible"]["strong"] == [] and r["eligible"]["medium"] == [])
    r = D.decide([C(1, 0.9), C(2, 0.6), C(3, 0.55), C(4, 0.52)], "recall", nocd, True, rng=random.Random(1))
    check("回忆模式：前 3 条、不走概率", [p["id"] for p in r["picks"]] == [1, 2, 3] and r["gate"] == "recall")
    r = D.decide([C(1, 0.95)], "greeting", nocd, True, rng=random.Random(1))
    check("寒暄不走相关度", r["picks"] == [] and r["gate"] == "greeting")
    cd = {"recent": {1}, "medium": {4}, "serendipity": set()}
    r = D.decide([C(1, 0.9), C(2, 0.8), C(4, 0.6)], "normal", cd, True, rng=random.Random(1))
    check("最近 5 轮出现过的拿掉；不回填弱的", [p["id"] for p in r["picks"]] == [2]
          and {x["id"] for x in r["dropped"]} == {1, 4}, r)
    r = D.decide([C(1, 0.9, vec=0.9), C(2, 0.3, vec=0.5)], "normal", nocd, False, rng=random.Random(5))
    check("精排不可用：退回向量门槛，只算中层", r["basis"] == "vec_sim" and r["eligible"]["strong"] == []
          and r["eligible"]["medium"] == [1], r["eligible"])
    r = D.decide([C(1, 0.72, arousal=0.8), C(2, 0.74)], "emotion", nocd, True, rng=random.Random(1))
    check("情绪意图给情绪浓的加分（0.72+0.05 进入强层）", r["picks"][0]["id"] == 1 and r["picks"][0]["tier"] == "strong"
          and r["eligible"]["strong"] == [1], r["picks"])
    hits = 0
    pool = [{"id": 9, "arousal": 0.9, "is_core": 1, "content": "x"}]
    for seed in range(400):
        rr = D.decide([], "greeting", nocd, True, serendipity_pool=pool, rng=random.Random(seed))
        hits += any(p["tier"] == "serendipity" for p in rr["picks"])
    check("无由来浮现约 5%（寒暄时也可能有）", 8 <= hits <= 40, hits)
    hits = 0
    for seed in range(400):
        rr = D.decide([], "normal", {"recent": set(), "medium": set(), "serendipity": {9}}, True,
                      serendipity_pool=pool, rng=random.Random(seed))
        hits += bool(rr["picks"])
    check("无由来 7 天冷却", hits == 0)


# ---------- 4. 端到端（假嵌入 / 假精排 / 假向量）----------
class FakeCollection:
    def __init__(self, sims):
        self.sims = sims

    def query(self, query_embeddings, n_results=10, include=None):
        items = sorted(self.sims.items(), key=lambda x: -x[1])[:n_results]
        return {"ids": [[f"l1_{i}" for i, _ in items]], "similarities": [[s for _, s in items]]}


def seed_l1(rows):
    conn = config.get_conn()
    for i, content, quote, arousal in rows:
        conn.execute("INSERT INTO l1_memories (id, content, quote, ts, event_type, arousal, status) "
                     "VALUES (?, ?, ?, '2026-10-17 12:00:00', 'event', ?, 'active')", (i, content, quote, arousal))
    conn.commit()
    conn.close()


def test_e2e():
    print("\n[4] 端到端：写轨迹、写影子账本、冷却生效")
    db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
    seed_l1([(1, "下雨天一起听晴天", "下雨就听这首吧", 0.7), (2, "她说加班很累", "", 0.4),
             (3, "讨论前端部署", "", 0.2), (4, "第一次叫我小雨伞", "小雨伞", 0.9)])
    calls = {"embed": 0, "rerank": 0}

    async def fake_embed(q):
        calls["embed"] += 1
        return [0.1] * 4

    async def fake_fts(q, n=30):
        return [(1, 3.0), (3, 1.0)]

    async def fake_rerank(q, docs):
        calls["rerank"] += 1
        table = {"下雨天一起听晴天（原话：下雨就听这首吧）": 0.9, "她说加班很累": 0.6, "讨论前端部署": 0.1}
        return [table.get(d, 0.2) for d in docs], {"model": "fake", "ok": True, "ms": 5}

    coll = FakeCollection({1: 0.82, 2: 0.66, 3: 0.4})
    req = {"session_id": "conv_a", "user_text": "下雨了，想起那首歌", "backend": "api", "shadow": True, "client": "pluvia",
           "legacy": {"gate": "open", "picked": [2], "candidates": [[2, 0.66]]}}
    run = lambda r, seed=1: asyncio.run(orchestrator.compose_turn(r, coll, embed_fn=fake_embed, rerank_fn=fake_rerank,
                                                                 fts_fn=fake_fts, rng=random.Random(seed)))
    res = run(req)
    check("强相关的那条被选中", [p["id"] for p in res["picks"]][:1] == [1] and res["picks"][0]["tier"] == "strong", res)
    check("影子模式不返回给网关注入", res["card"] == "" and "晴天" in res["shadow_card"])
    conn = config.get_conn()
    tr = conn.execute("SELECT mode, intent, legacy, injected_chars FROM recall_traces WHERE id=?", (res["trace_id"],)).fetchone()
    inj = conn.execute("SELECT item_id, tier, status FROM injections WHERE trace_id=?", (res["trace_id"],)).fetchall()
    conn.close()
    check("轨迹写入（带旧逻辑的决定）", tr[0] == "shadow" and '"picked": [2]' in tr[2] and tr[3] > 0, tr)
    check("影子账本写入", ("1", "strong", "shadow") in inj, inj)
    res2 = run(req, seed=2)
    check("同一会话下一轮：刚出现过的不再出现", 1 not in [p["id"] for p in res2["picks"]], res2["picks"])
    res3 = run({**req, "session_id": "conv_b"}, seed=3)
    check("换一个会话：强相关的可以出现", 1 in [p["id"] for p in res3["picks"]], res3["picks"])
    before = dict(calls)
    res4 = run({**req, "user_text": "嗯嗯"})
    check("寒暄不调用嵌入和精排", calls == before and res4["intent"] == "greeting")

    async def broken_rerank(q, docs):
        return None, {"model": "fake", "ok": False, "error": "timeout"}
    res5 = asyncio.run(orchestrator.compose_turn({**req, "session_id": "conv_c"}, coll, embed_fn=fake_embed,
                                                 rerank_fn=broken_rerank, fts_fn=fake_fts, rng=random.Random(0)))
    t5 = traces.get_trace(res5["trace_id"])
    check("精排失败时退回向量相似度", t5["decision"]["basis"] == "vec_sim" and t5["rerank"]["ok"] is False)
    check("轨迹详情带候选内容", t5["snippets"].get("1", {}).get("content", "").startswith("下雨天"))
    lst = traces.list_traces(limit=10)
    first = [x for x in lst if x["id"] == res["trace_id"]][0]
    check("列表：新旧两边的选择都在", [p["id"] for p in first["new"]][:1] == [1]
          and [p["id"] for p in first["legacy"]["picked"]] == [2] and first["legacy"]["picked"][0]["content"] == "她说加班很累")
    st = traces.stats(days=7)
    check("统计", st["turns"] == 5 and st["legacy_inject_turns"] == 5 and st["intents"].get("greeting") == 1
          and st["rerank_tried_turns"] == 4 and st["rerank_ok_turns"] == 3, st)


def main():
    try:
        test_intent()
        test_cards()
        test_decide()
        test_e2e()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
