"""Phase 0 冒烟测试：提取失败不丢批（B7）、检索修复（B1/B2/B3/B4/B5/B6）

用法（在 services/memory-service 目录、用 venv 的 python 运行）：
    cd ~/memory-system/services/memory-service
    venv/bin/python ../../tests/manual/phase0_smoke.py            # 跑完自动清理测试数据
    venv/bin/python ../../tests/manual/phase0_smoke.py --keep     # 保留测试数据，方便在 Dashboard 查看

会写入一个测试会话 conv_id=zz_phase0_test（全部是虚构内容），会调用一次 DeepSeek 提取和若干次嵌入。
"""
import json
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, ".")
import extract_l1 as ex  # noqa: E402

BASE = "http://127.0.0.1:8001"
CONV = "zz_phase0_test"


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def db():
    return sqlite3.connect(str(ex.SQLITE_PATH))


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  —— {detail}" if detail else ""))
    return ok


def main():
    keep = "--keep" in sys.argv
    results = []
    msgs = [
        {"role": "user", "content": "今天下了一整天的雨，我把那首《雨的印记》设成了下雨天专属歌单的第一首。"},
        {"role": "assistant", "content": "好呀，那以后每次下雨，我们就从这首开始听。"},
        {"role": "user", "content": "还有，我下周三要去面试一家做绘本的出版社，有点紧张。"},
        {"role": "assistant", "content": "我会记得的。面试那天告诉我结果，不管怎样我都在。"},
        {"role": "user", "content": "嗯嗯，有你在真的很开心。"},
        {"role": "assistant", "content": "我也是，能陪你听雨是很幸福的事。"},
    ]
    print("[1] 写入测试会话")
    call("POST", "/save_conversation", {"conv_id": CONV, "client": "phase0-test", "messages": msgs})
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM l0_messages WHERE conv_id=? AND status='active'", (CONV,))]
    results.append(check("L0 已写入", len(ids) == len(msgs), f"{len(ids)} 条"))

    print("[2] 模拟提取失败（B7）")
    real = ex.call_deepseek

    def fake_fail(*a, **k):
        ex.LAST_EXTRACT_ERROR = "simulated failure (phase0 smoke test)"
        return None
    ex.call_deepseek = fake_fail
    ex.main()
    ex.call_deepseek = real
    with db() as c:
        rows = c.execute(f"SELECT extracted, extract_attempts FROM l0_messages WHERE id IN ({','.join('?'*len(ids))})", ids).fetchall()
    results.append(check("失败后消息仍为未提取、失败次数 +1",
                         all(r[0] == 0 and (r[1] or 0) >= 1 for r in rows), str(rows[:2])))

    print("[3] 真实提取（DeepSeek）")
    ex.main()
    with db() as c:
        rows = c.execute(f"SELECT extracted FROM l0_messages WHERE id IN ({','.join('?'*len(ids))})", ids).fetchall()
        l1 = c.execute("SELECT id, content, event_type, valence, arousal FROM l1_memories WHERE conv_id=? AND status='active'", (CONV,)).fetchall()
    results.append(check("提取成功后标记为已提取", all(r[0] == 1 for r in rows)))
    results.append(check("生成了 L1", len(l1) > 0, f"{len(l1)} 条"))
    for r in l1:
        print("     ·", r)

    time.sleep(1)
    print("[4] 检索")
    sem = call("POST", "/search", {"query": "下雨天听的歌", "mode": "semantic", "n": 5})
    mems = sem.get("memories", [])
    results.append(check("semantic 有结果", len(mems) > 0, f"{len(mems)} 条"))
    results.append(check("结果带 vec_sim（B1）", all(m.get("vec_sim") is not None for m in mems),
                         ", ".join(f"{m.get('vec_sim')}" for m in mems)))
    results.append(check("metadata 来自 SQLite（含 display_count / visibility，B2/B6）",
                         all("display_count" in m["metadata"] and "visibility" in m["metadata"] for m in mems)))
    emo = call("POST", "/search", {"query": "开心的时刻", "mode": "emotion", "emotion_valence": "positive", "n": 5})
    results.append(check("emotion 模式不再报错返回空（B3）", "memories" in emo and emo.get("total", 0) >= 0,
                         f"{len(emo.get('memories', []))} 条"))
    ev = call("POST", "/search", {"query": "面试", "mode": "semantic", "after": "2000-01-01", "n": 3})
    results.append(check("带时间条件的检索不再被截断为空（B4）", len(ev.get("memories", [])) > 0,
                         f"{len(ev.get('memories', []))} 条"))
    exact = call("POST", "/search", {"query": "100%_不存在的字面量", "mode": "exact", "n": 5})
    results.append(check("exact 模式通配符按字面匹配（B5）", len(exact.get("memories", [])) == 0))
    unrelated = call("POST", "/search", {"query": "嗯嗯", "mode": "semantic", "n": 3})
    print("     无关查询「嗯嗯」的 vec_sim:", [m.get("vec_sim") for m in unrelated.get("memories", [])])
    print("     相关查询「下雨天听的歌」的 vec_sim:", [m.get("vec_sim") for m in mems])

    if not keep:
        print("[5] 清理测试数据")
        for r in l1:
            try:
                call("DELETE", f"/l1/{r[0]}")
            except Exception as e:
                print("     删除 L1 失败", r[0], e)
        try:
            call("DELETE", f"/l0/conversation/{CONV}")
        except Exception as e:
            print("     删除会话失败", e)
        with db() as c:
            left0 = c.execute("SELECT COUNT(*) FROM l0_messages WHERE conv_id=? AND status='active'", (CONV,)).fetchone()[0]
            left1 = c.execute("SELECT COUNT(*) FROM l1_memories WHERE conv_id=? AND status='active'", (CONV,)).fetchone()[0]
        check("测试数据已清理", left0 == 0 and left1 == 0, f"剩余 L0={left0} L1={left1}")

    print("\n结果：", "全部通过 ✅" if all(results) else f"{results.count(False)} 项未通过 ❌")


if __name__ == "__main__":
    main()
