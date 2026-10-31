"""缓存记录（app/cache_stats.py）的测试：临时库，只用标准库。

运行：python3 tests/manual/test_cache_stats.py
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "services" / "memory-service"))

import db  # noqa: E402
from app import cache_stats as CS  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cachetest_"))
    try:
        path = tmp / "m.db"
        db.migrate(path, log=lambda *a, **k: None)
        c = sqlite3.connect(str(path))
        print("\n[1] 价格和命中率")
        check("Opus 4.6 / 4.5 按新价，Opus 4.1 按旧价", CS.price("anthropic/claude-opus-4.6") == (5.0, 25.0)
              and CS.price("claude-opus-4-5") == (5.0, 25.0) and CS.price("claude-opus-4-1") == (15.0, 75.0))
        check("认不出的模型不算钱", CS.price("deepseek-chat") is None and CS.cost({"model": "deepseek-chat", "input": 5}) is None)
        e = {"model": "anthropic/claude-opus-4.6", "ttl": "1h", "input": 100, "cache_write": 1000, "cache_read": 20000, "output": 200}
        check("一轮的钱：输入 + 写缓存（1 小时 2 倍）+ 读缓存（0.1 倍）+ 输出",
              abs(CS.cost(e) - (100 * 5 + 1000 * 10 + 20000 * 0.5 + 200 * 25) / 1e6) < 1e-9, CS.cost(e))
        check("5 分钟缓存写 1.25 倍", abs(CS.cost({**e, "ttl": "5m"}) - (100 * 5 + 1000 * 6.25 + 20000 * 0.5 + 200 * 25) / 1e6) < 1e-9)
        check("命中率 = 读缓存 / 全部输入", CS.hit_rate(e) == round(20000 / 21100, 3) and CS.hit_rate({}) is None)

        print("\n[2] 记录和汇总")
        now = "2026-10-31 12:00:00"
        CS.record(c, {"kind": "turn", "input": 1}, "2026-09-01 00:00:00")   # 一个多月以前的，后面记新的时候会被清掉
        CS.record(c, {"kind": "turn", "conv_id": "c1", "provider": "Amazon Bedrock", "relay": "openrouter",
                      "model": "anthropic/claude-opus-4.6", "ttl": "1h", "input": 99, "cache_write": 6000, "cache_read": 16000, "output": 150}, now)
        CS.record(c, {"kind": "turn", "conv_id": "c1", "provider": "Amazon Bedrock", "model": "anthropic/claude-opus-4.6",
                      "input": 99, "cache_write": 100, "cache_read": 22000, "output": 150}, "2026-10-31 12:05:00")
        CS.record(c, {"kind": "beat", "conv_id": "c1", "provider": "Amazon Bedrock", "model": "anthropic/claude-opus-4.6",
                      "input": 40, "cache_read": 22100}, "2026-10-31 12:55:00")
        CS.record(c, {"kind": "weird", "input": -5, "cache_read": "x"}, "2026-10-31 12:56:00")
        o = CS.overview(c, now="2026-10-31 13:00:00")
        ev = o["events"]
        check("最新的在前；奇怪的值当 0、kind 当 turn", ev[0]["kind"] == "turn" and ev[0]["input"] == 0 and ev[0]["cache_read"] == 0, ev[0])
        check("重新写了一大段的那轮标成没命中", [x["miss"] for x in ev if x["kind"] == "turn" and x["provider"]] == [False, True])
        d = o["day"]
        check("24 小时汇总：3 轮聊天（含奇怪的那条）、1 次心跳、1 轮没命中", d["turns"] == 3 and d["beats"] == 1 and d["misses"] == 1, d)
        check("按哪家分", d["by_provider"].get("Amazon Bedrock") == 2, d["by_provider"])
        check("心跳的钱只有读缓存（输出没算）", abs(d["beat_cost"] - (40 * 5 + 22100 * 0.5) / 1e6) < 1e-4, d["beat_cost"])
        n_old = c.execute("SELECT COUNT(*) FROM cache_events WHERE ts < '2026-10-01'").fetchone()[0]
        check("30 天以前的记录自动清掉", n_old == 0)
        CS.set_keepwarm({"armed": True, "conv_id": "c1", "secret": "不该存", "next_at": "x"})
        check("心跳状态只留认得的字段", CS.keepwarm() == {"armed": True, "conv_id": "c1", "next_at": "x"}, CS.keepwarm())
        c.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
