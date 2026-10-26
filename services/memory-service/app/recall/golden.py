"""黄金集（方案 14.1 节）：标注、门槛校准、排序质量。

标注在 Dashboard「召回对比」每一轮的详情里做（表结构见 migrations/006_recall_labels.py）：
- 某条候选记忆：「该想起」(should) /「不该想起」(should_not)；
- 整轮：「什么都不该想起」(none，item_id = 0)，比如寒暄、和过去无关的话。

校准只用轨迹里已经存下的精排分，不再调接口、不花钱：
- 命中率：标了「该想起」的记忆里，精排分 ≥ 门槛的比例（越高越好）；
- 误浮现率（按条）：「不该想起」的记忆、以及「什么都不该想起」的那些轮里分数最高的候选，精排分 ≥ 门槛的比例；
- 整轮误浮现率：不该想起任何事的轮（标了「什么都不该想起」，或者只标了「不该想起」、没有「该想起」的）里，
  有一条分数 ≥ 门槛的比例——也就是平常聊天里，这一轮会冒出一件不相干的事的概率。每轮有十几个候选，
  按条算的误浮现率很低时，按轮算可能还很高（2026-10-26：按条 6%，按轮 31%）。
建议：强门槛取整轮误浮现率不超过 10% 的最低值；中门槛取「命中率还能保住 80%」和「中层概率 × 整轮误浮现率不超过 10%」
两者中更高的那个（不超过强门槛）。不该想起的轮不够数时，强门槛退回按条算。
"""
import json
import sqlite3

import timeutil
from app.config import get_conn

LABELS = ("should", "should_not", "none")
TURN = 0                                                  # item_id = 0：整轮的标注
GRID = [round(0.30 + 0.05 * i, 2) for i in range(14)]     # 0.30 … 0.95
MIN_SHOULD = 8                                            # 至少这么多条「该想起」才给建议
MIN_NEGATIVE = 8                                          # 至少这么多条反例才给建议
TARGET_FP = 0.10                                          # 方案 Phase 2 验收：误浮现率 ≤ 10%
TARGET_HIT = 0.80
TARGET_MEDIUM_FP = 0.10                                   # 中层：平常一轮里因为「中」想起不相干的事的概率


def labels_for(conn, trace_id):
    """这一轮的标注 {item_id: label}"""
    try:
        return {int(i): lab for i, lab in conn.execute(
            "SELECT item_id, label FROM recall_labels WHERE trace_id=?", (int(trace_id),)).fetchall()}
    except sqlite3.OperationalError:     # 表还没建（迁移前）
        return {}


def label_counts(conn, trace_ids):
    """{trace_id: 标了几条}"""
    ids = [int(i) for i in trace_ids]
    if not ids:
        return {}
    try:
        return {int(t): n for t, n in conn.execute(
            f"SELECT trace_id, COUNT(*) FROM recall_labels WHERE trace_id IN ({','.join('?' * len(ids))}) "
            "GROUP BY trace_id", ids).fetchall()}
    except sqlite3.OperationalError:
        return {}


def set_label(trace_id, item_id, label):
    """标注、改标注，或者清除（label 为空）。返回这一轮现在的全部标注 {item_id: label}。
    参数不对抛 ValueError；没有这一轮抛 LookupError。"""
    trace_id, item_id = int(trace_id), int(item_id)
    if label is not None and label not in LABELS:
        raise ValueError(f"标注只能是 {' / '.join(LABELS)}")
    if label == "none" and item_id != TURN:
        raise ValueError("「什么都不该想起」是整轮的标注（item_id 填 0）")
    if label in ("should", "should_not") and item_id == TURN:
        raise ValueError("「该想起 / 不该想起」要标在某一条记忆上")
    conn = get_conn()
    try:
        row = conn.execute("SELECT query, user_text FROM recall_traces WHERE id=?", (trace_id,)).fetchone()
        if not row:
            raise LookupError(f"没有第 {trace_id} 轮的记录")
        now = timeutil.utc_now_str()
        conn.execute("BEGIN IMMEDIATE")
        if label is None:
            conn.execute("DELETE FROM recall_labels WHERE trace_id=? AND item_id=?", (trace_id, item_id))
        else:
            # 「该想起」和「整轮什么都不该想起」互相矛盾：标了一个就去掉另一个
            if label == "should":
                conn.execute("DELETE FROM recall_labels WHERE trace_id=? AND item_id=?", (trace_id, TURN))
            elif label == "none":
                conn.execute("DELETE FROM recall_labels WHERE trace_id=? AND label='should'", (trace_id,))
            conn.execute(
                "INSERT INTO recall_labels (trace_id, item_id, label, query, user_text, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(trace_id, item_id) DO UPDATE SET "
                "label = excluded.label, updated_at = excluded.updated_at",
                (trace_id, item_id, label, row[0], row[1], now, now))
        conn.commit()
        return labels_for(conn, trace_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_turns(conn):
    """{trace_id: {labels, cands, usable}}。usable：这一轮有精排分可用（精排成功，或者寒暄这类本来就不检索的）"""
    rows = conn.execute(
        "SELECT l.trace_id, l.item_id, l.label, t.decision, t.rerank FROM recall_labels l "
        "LEFT JOIN recall_traces t ON t.id = l.trace_id ORDER BY l.trace_id").fetchall()
    turns = {}
    for tid, iid, label, dec, rr in rows:
        t = turns.get(tid)
        if t is None:
            try:
                d = json.loads(dec or "{}")
            except ValueError:
                d = {}
            try:
                r = json.loads(rr or "{}")
            except ValueError:
                r = {}
            t = turns[tid] = {"labels": {}, "cands": d.get("candidates") or [],
                              "usable": dec is not None and (bool(r.get("ok")) or "skipped" in r)}
        t["labels"][int(iid)] = label
    return turns


def calibration():
    """按标注算各个门槛下的命中率和误浮现率，以及排序质量（Recall@5、MRR），够数时给出建议门槛。"""
    conn = get_conn()
    try:
        turns = _load_turns(conn)
    finally:
        conn.close()
    should, should_not, none_max, neg_turn_max = [], [], [], []
    first_ranks = []
    top5 = 0
    skipped = 0
    for t in turns.values():
        if not t["usable"]:
            skipped += 1
            continue
        score = {int(c["id"]): float(c.get("rerank") or 0.0) for c in t["cands"]}
        order = [i for i, _ in sorted(score.items(), key=lambda x: -x[1])]
        labs = t["labels"]
        if labs.get(TURN) == "none":
            none_max.append(max(score.values()) if score else 0.0)
            neg_turn_max.append(none_max[-1])
        elif labs and "should" not in labs.values():
            neg_turn_max.append(max(score.get(i, 0.0) for i, lab in labs.items() if lab == "should_not"))
        first = None
        has_should = False
        for iid, lab in labs.items():
            if iid == TURN:
                continue
            s = score.get(iid, 0.0)          # 没进候选的当 0 分：怎么调门槛都想不起来
            if lab == "should":
                has_should = True
                should.append(s)
                if iid in order[:5]:
                    top5 += 1
                if iid in order:
                    r = order.index(iid) + 1
                    first = r if first is None else min(first, r)
            elif lab == "should_not":
                should_not.append(s)
        if has_should:
            first_ranks.append(first)
    neg = len(should_not) + len(none_max)
    rows = []
    for g in GRID:
        sh = sum(s >= g for s in should)
        sn = sum(s >= g for s in should_not)
        nn = sum(s >= g for s in none_max)
        tn = sum(s >= g for s in neg_turn_max)
        rows.append({"t": g, "should_hits": sh, "should_not_hits": sn, "none_hits": nn,
                     "hit": round(sh / len(should), 3) if should else None,
                     "fp": round((sn + nn) / neg, 3) if neg else None,
                     "turn_fp": round(tn / len(neg_turn_max), 3) if neg_turn_max else None})
    counts = {"turns": len(turns), "should": len(should), "should_not": len(should_not),
              "none_turns": len(none_max), "neg_turns": len(neg_turn_max), "skipped": skipped}
    quality = {"recall_at_5": round(top5 / len(should), 3) if should else None,
               "mrr": round(sum(1.0 / r for r in first_ranks if r) / len(first_ranks), 3) if first_ranks else None}
    try:
        from app import settings as app_settings
        medium_prob = float(app_settings.get("recall")["medium_prob"])
    except Exception:
        medium_prob = 0.3
    return {"counts": counts, "quality": quality, "rows": rows, "suggest": suggest(rows, counts, medium_prob),
            "targets": {"fp": TARGET_FP, "hit": TARGET_HIT, "medium_fp": TARGET_MEDIUM_FP, "medium_prob": medium_prob,
                        "min_should": MIN_SHOULD, "min_negative": MIN_NEGATIVE}}


def suggest(rows, counts, medium_prob=0.3):
    neg = counts["should_not"] + counts["none_turns"]
    if counts["should"] < MIN_SHOULD or neg < MIN_NEGATIVE:
        return {"ok": False, "reason": f"再多标一些才给建议：至少 {MIN_SHOULD} 条「该想起」和 {MIN_NEGATIVE} 条反例"
                                       f"（「不该想起」或「这一轮什么都不该想起」），现在分别是 {counts['should']} 和 {neg}。"}
    notes = []
    by_turn = counts.get("neg_turns", 0) >= MIN_NEGATIVE
    key = "turn_fp" if by_turn else "fp"
    t_high = next((r["t"] for r in rows if r.get(key) is not None and r[key] <= TARGET_FP), None)
    if t_high is None:
        t_high = rows[-1]["t"]
        notes.append(f"门槛调到 {t_high} 误浮现率也超过 {int(TARGET_FP * 100)}%：精排分对这些反例偏高，"
                     "可能要换精排模型，或者检查这些反例标得对不对。")
    if not by_turn:
        notes.append(f"不该想起任何事的轮还不到 {MIN_NEGATIVE} 轮，强门槛先按一条条记忆算；按轮算通常要更高。")
    ok_low = [r["t"] for r in rows if r["hit"] is not None and r["hit"] >= TARGET_HIT]
    if ok_low:
        t_low = max(ok_low)
    else:
        t_low = rows[0]["t"]
        notes.append(f"门槛降到 {t_low}，「该想起」的也不到 {int(TARGET_HIT * 100)}% 能想起："
                     "很多该想起的根本没进候选，问题在检索，不在门槛。")
    if by_turn and medium_prob > 0:
        quiet = next((r["t"] for r in rows if r.get("turn_fp") is not None and medium_prob * r["turn_fp"] <= TARGET_MEDIUM_FP), None)
        if quiet is not None and quiet > t_low:
            hit = next((r["hit"] for r in rows if r["t"] == quiet), None)
            notes.append(f"中门槛取 {quiet}：再低的话，平常聊天里因为「中」冒出不相干的事的概率会超过 {int(TARGET_MEDIUM_FP * 100)}%"
                         f"（中层概率 {medium_prob} × 整轮误浮现率）；这时该想起的命中率是 {int(round((hit or 0) * 100))}%。"
                         "问「还记得吗」时用的是单独的回忆门槛，不受影响。")
            t_low = quiet
    if t_low > t_high:
        t_low = t_high
        notes.append("精排分把「该想起」和「不该想起」分得很开，中间几乎没有模糊地带，所以中门槛和强门槛取一样。")
    return {"ok": True, "t_high": t_high, "t_low": t_low, "by_turn": by_turn, "notes": notes}


def export():
    """导出黄金集（私有 JSON，不进仓库）：每一轮的话、检索文本，以及标了的记忆内容。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT l.trace_id, l.item_id, l.label, l.query, l.user_text, l.created_at, m.content, t.frozen FROM recall_labels l "
            "LEFT JOIN recall_traces t ON t.id = l.trace_id "
            "LEFT JOIN l1_memories m ON m.id = l.item_id AND l.item_id != 0 AND t.frozen IS NULL "
            "ORDER BY l.trace_id, l.item_id").fetchall()
    finally:
        conn.close()
    items = {}
    for tid, iid, label, query, user_text, created_at, content, frozen in rows:
        if frozen:          # 清空记忆库之前的一轮：用当时定格下来的记忆内容
            try:
                content = ((json.loads(frozen).get("items") or {}).get(str(iid)) or {}).get("content")
            except ValueError:
                content = None
        it = items.setdefault(tid, {"trace_id": tid, "user_text": user_text, "query": query, "nothing": False,
                                    "should": [], "should_not": [], "labeled_at": created_at})
        if label == "none":
            it["nothing"] = True
        else:
            it[label].append({"id": iid, "content": content})
    return {"version": 1, "exported_at": timeutil.utc_now_str(), "items": list(items.values())}
