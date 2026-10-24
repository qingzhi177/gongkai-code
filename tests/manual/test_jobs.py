"""后台任务队列 + worker 测试（jobs.py、worker.py）

用法（在 services/memory-service 目录下）：
    venv/bin/python ../../tests/manual/test_jobs.py

只用临时库和假的任务处理程序，不调用大模型，不碰真实数据。
"""
import asyncio
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE_DIR = HERE.parent.parent / "services" / "memory-service"
sys.path.insert(0, str(SERVICE_DIR))

import db  # noqa: E402
import jobs  # noqa: E402
import timeutil  # noqa: E402
import worker as worker_mod  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def quiet(*a, **k):
    pass


def fresh_db(tmp, name):
    p = tmp / name / "memory.db"
    db.migrate(p, log=quiet)
    return p


def make_due(p, job_id):
    """把重试时间改成现在（测试里不真等 1 分钟）"""
    conn = sqlite3.connect(p)
    conn.execute("UPDATE pipeline_jobs SET next_run_at=? WHERE id=?", (timeutil.utc_now_str(), job_id))
    conn.commit()
    conn.close()


def seconds_until(ts):
    return (timeutil.parse_ts(ts) - timeutil.utc_now()).total_seconds()


# ---------- 1. 队列基本操作 ----------
def test_queue(tmp):
    print("\n[1] 投递、去重、取任务")
    p = fresh_db(tmp, "q")
    j1, c1 = jobs.enqueue(p, "extract", {"force": False}, dedupe_key="extract")
    j2, c2 = jobs.enqueue(p, "extract", {"force": False}, dedupe_key="extract")
    check("同一个 key 不会重复投递", c1 and not c2 and j1["id"] == j2["id"])
    j3, _ = jobs.enqueue(p, "extract", {"force": True}, dedupe_key="extract", source="dashboard", update_if_queued=True)
    check("排队中的任务可以被手动触发覆盖（改成 force）", j3["id"] == j1["id"] and j3["payload"] == {"force": True}
          and j3["source"] == "dashboard")
    jobs.enqueue(p, "summary", None, dedupe_key="summary")
    jobs.enqueue(p, "narrative", {"x": 1}, dedupe_key="narrative", delay_sec=3600)
    a = jobs.claim(p, "w1")
    check("按到期时间取任务，attempts 加 1", a and a["id"] == j1["id"] and a["attempts"] == 1 and a["status"] == "running")
    b = jobs.claim(p, "w1", exclude_kinds=["summary"])
    check("exclude_kinds 生效；还没到时间的不会被取到", b is None)
    b = jobs.claim(p, "w1")
    check("取到第二个任务", b and b["kind"] == "summary")
    j4, c4 = jobs.enqueue(p, "extract", {"force": True}, dedupe_key="extract", update_if_queued=True)
    check("执行中的任务不会被覆盖，也不会新建", not c4 and j4["id"] == j1["id"] and j4["status"] == "running")
    jobs.complete(p, a["id"], {"status": "ok", "segments": 2})
    done = jobs.get(p, a["id"])
    check("完成后记下结果", done["status"] == "done" and done["result"]["segments"] == 2 and done["finished_at"])
    j5, c5 = jobs.enqueue(p, "extract", None, dedupe_key="extract")
    check("完成以后同一个 key 可以再投递", c5 and j5["id"] != j1["id"])
    pc = fresh_db(tmp, "cont")
    jn, _ = jobs.enqueue(pc, "scan", None, dedupe_key="scan:next")
    jobs.claim(pc, "w1", kinds=["scan"])
    _, c6 = jobs.enqueue(pc, "scan", None, dedupe_key="scan:next")
    j7, c7 = jobs.enqueue(pc, "scan", None, dedupe_key="scan:next", continuation=True)
    _, c8 = jobs.enqueue(pc, "scan", None, dedupe_key="scan:next", continuation=True)
    check("接着做剩下的（continuation）：自己正在跑不算重复，能接着投；已经排着一个就不再投",
          not c6 and c7 and j7["id"] != jn["id"] and not c8, (c6, c7, c8))
    jobs.complete(pc, jn["id"], {"status": "ok"})
    jobs.claim(pc, "w1", kinds=["scan"])
    j9, c9 = jobs.enqueue(pc, "scan", None, dedupe_key="scan:next", continuation=True)
    check("接着投的那个跑起来以后还能再接着投（两个 key 轮流用）", c9 and j9["dedupe_key"] == "scan:next"
          and j7["dedupe_key"] == "scan:next~2", (j7["dedupe_key"], j9["dedupe_key"]))
    s = jobs.stats(p)
    check("统计", s["queued"] == 2 and s["running"] == 1 and s["dead"] == 0 and s["done_24h"] == 1, s)


# ---------- 2. 失败重试与死信 ----------
def test_retry(tmp):
    print("\n[2] 失败重试、死信、重新排队、忽略")
    p = fresh_db(tmp, "r")
    j, _ = jobs.enqueue(p, "summary", None, dedupe_key="summary")
    waits = []
    for i in range(4):
        make_due(p, j["id"])
        c = jobs.claim(p, "w1")
        st = jobs.fail(p, c["id"], f"第{i + 1}次错误")
        cur = jobs.get(p, j["id"])
        if st == "queued":
            waits.append(round(seconds_until(cur["next_run_at"])))
    check("退避时间 1 分钟、5 分钟、30 分钟", [abs(w - e) <= 2 for w, e in zip(waits, [60, 300, 1800])] == [True] * 3, waits)
    cur = jobs.get(p, j["id"])
    check("第 4 次失败进入死信", cur["status"] == "dead" and cur["attempts"] == 4 and cur["last_error"] == "第4次错误")
    check("统计里有死信", jobs.stats(p)["dead"] == 1)
    check("描述", "已停止重试" in jobs.describe(cur))
    j2, c2 = jobs.enqueue(p, "summary", None, dedupe_key="summary")
    check("死信不挡同类新任务", c2)
    ok, msg = jobs.retry(p, j["id"])
    check("有同类任务在排队时不能重试死信", not ok and "同类" in msg, msg)
    jobs.complete(p, jobs.claim(p, "w1")["id"], {"status": "ok"})
    ok, msg = jobs.retry(p, j["id"])
    cur = jobs.get(p, j["id"])
    check("重试：回到排队，次数清零", ok and cur["status"] == "queued" and cur["attempts"] == 0, msg)
    c = jobs.claim(p, "w1")
    jobs.fail(p, c["id"], "又错了", permanent=True)
    check("permanent 失败直接进死信", jobs.get(p, j["id"])["status"] == "dead")
    check("忽略死信", jobs.dismiss(p, j["id"]) and jobs.get(p, j["id"])["status"] == "dismissed")
    check("不能忽略非死信", not jobs.dismiss(p, c2 and j2["id"]))
    retrying = jobs.enqueue(p, "narrative", None, dedupe_key="narrative")[0]
    jobs.fail(p, jobs.claim(p, "w1")["id"], "网络错误")
    lst = jobs.list_jobs(p, status="retrying")
    check("「等待重试」筛选", [x["id"] for x in lst] == [retrying["id"]], lst)
    check("等待重试的描述", "自动重试" in jobs.describe(lst[0]), jobs.describe(lst[0]))


# ---------- 3. 中断恢复 ----------
def test_recover(tmp):
    print("\n[3] worker 中断后的恢复")
    p = fresh_db(tmp, "o")
    a, _ = jobs.enqueue(p, "extract", None, dedupe_key="extract")
    b, _ = jobs.enqueue(p, "summary", None, dedupe_key="summary")
    jobs.claim(p, "w1")
    jobs.claim(p, "w1")
    jobs.release(p, a["id"])
    ra = jobs.get(p, a["id"])
    check("正常停止放回队列：不算失败", ra["status"] == "queued" and ra["attempts"] == 0)
    ids = jobs.recover_orphans(p)
    rb = jobs.get(p, b["id"])
    check("异常退出留下的任务：记一次失败、立即重排", ids == [b["id"]] and rb["status"] == "queued"
          and rb["attempts"] == 1 and seconds_until(rb["next_run_at"]) <= 1 and "中断" in rb["last_error"])
    conn = sqlite3.connect(p)
    conn.execute("UPDATE pipeline_jobs SET status='running', attempts=max_attempts WHERE id=?", (b["id"],))
    conn.commit()
    conn.close()
    jobs.recover_orphans(p)
    check("次数用完的中断任务进死信", jobs.get(p, b["id"])["status"] == "dead")


# ---------- 4. 清理 ----------
def test_cleanup(tmp):
    print("\n[4] 清理旧记录")
    p = fresh_db(tmp, "c")
    old, _ = jobs.enqueue(p, "extract", None)
    new, _ = jobs.enqueue(p, "extract", None)
    dead, _ = jobs.enqueue(p, "extract", None, max_attempts=1)
    for j in (old, new):
        jobs.complete(p, jobs.claim(p, "w")["id"])
    jobs.fail(p, jobs.claim(p, "w")["id"], "x")
    conn = sqlite3.connect(p)
    conn.execute("UPDATE pipeline_jobs SET updated_at='2020-01-01 00:00:00' WHERE id IN (?, ?)", (old["id"], dead["id"]))
    conn.commit()
    conn.close()
    n = jobs.cleanup(p, keep_days=30)
    left = {j["id"] for j in jobs.list_jobs(p)}
    check("只删 30 天前完成的；死信保留", n == 1 and left == {new["id"], dead["id"]}, (n, left))


# ---------- 5. worker ----------
def run_worker(p, handlers, until, timeout=15, **kw):
    """在后台线程里跑 worker，直到 until() 为真或超时，然后停止。返回 (worker, 是否在超时前满足条件)。"""
    w = worker_mod.Worker(p, handlers, poll_sec=0.2, **kw)
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=lambda: loop.run_until_complete(w.run()), daemon=True)
    th.start()
    t0 = time.time()
    ok = False
    while time.time() - t0 < timeout:
        if until():
            ok = True
            break
        time.sleep(0.1)
    loop.call_soon_threadsafe(w.stop)
    th.join(30)
    return w, ok


def test_worker(tmp):
    print("\n[5] worker 执行任务")
    p = fresh_db(tmp, "w")
    calls = []
    active = {"n": 0, "max": 0, "extract": 0, "extract_max": 0}
    lock = threading.Lock()

    def enter(kind):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            if kind == "extract":
                active["extract"] += 1
                active["extract_max"] = max(active["extract_max"], active["extract"])

    def leave(kind):
        with lock:
            active["n"] -= 1
            if kind == "extract":
                active["extract"] -= 1

    def extract(payload, job):          # 同步处理程序（像 extract_l1.main）
        enter("extract")
        time.sleep(0.4)
        leave("extract")
        calls.append(("extract", payload))
        return {"status": "ok", "segments": 1}

    async def summary(payload, job):    # 异步处理程序（像 generate_summary）
        enter("summary")
        await asyncio.sleep(0.4)
        leave("summary")
        calls.append(("summary", payload))
        return {"status": "ok"}

    async def narrative(payload, job):
        enter("narrative")
        await asyncio.sleep(0.4)
        leave("narrative")
        return {"status": "ok"}

    async def flaky(payload, job):
        raise worker_mod.JobError("模型返回为空")

    def boom(payload, job):
        raise ValueError("意外错误")

    handlers = {"extract": extract, "summary": summary, "narrative": narrative, "flaky": flaky, "boom": boom}
    e1, _ = jobs.enqueue(p, "extract", {"force": False})
    e2, _ = jobs.enqueue(p, "extract", {"force": True})
    s1, _ = jobs.enqueue(p, "summary", None)
    n1, _ = jobs.enqueue(p, "narrative", None)
    f1, _ = jobs.enqueue(p, "flaky", None)
    b1, _ = jobs.enqueue(p, "boom", None)
    u1, _ = jobs.enqueue(p, "unknown_kind", None)

    def all_settled():
        return all(jobs.get(p, j["id"])["status"] != "running" and
                   (jobs.get(p, j["id"])["status"] != "queued" or jobs.get(p, j["id"])["attempts"] > 0)
                   for j in (e1, e2, s1, n1, f1, b1, u1))

    w, ok = run_worker(p, handlers, all_settled, concurrency=2)
    check("所有任务都处理过了", ok)
    st = {j["id"]: jobs.get(p, j["id"]) for j in (e1, e2, s1, n1, f1, b1, u1)}
    check("同步、异步处理程序都能完成", all(st[j["id"]]["status"] == "done" for j in (e1, e2, s1, n1)),
          {k: v["status"] for k, v in st.items()})
    check("结果写进任务表", st[e1["id"]]["result"] == {"status": "ok", "segments": 1})
    check("payload 传给处理程序", ("extract", {"force": True}) in calls)
    check("同时最多 2 个任务", active["max"] <= 2 and active["max"] == 2, active)
    check("同一种任务不会同时执行", active["extract_max"] == 1, active)
    check("JobError → 等待重试", st[f1["id"]]["status"] == "queued" and st[f1["id"]]["attempts"] == 1
          and st[f1["id"]]["last_error"] == "模型返回为空")
    check("意外异常 → 等待重试，记下异常类型", st[b1["id"]]["status"] == "queued" and "ValueError" in st[b1["id"]]["last_error"])
    check("没有处理程序的任务直接进死信", st[u1["id"]]["status"] == "dead")
    s = jobs.stats(p)
    check("worker 停止后显示离线", s["worker"] and not s["worker"]["online"], s["worker"])


def test_worker_stop(tmp):
    print("\n[6] worker 停止时没做完的任务放回队列")
    p = fresh_db(tmp, "s")
    started = threading.Event()

    def slow(payload, job):
        started.set()
        time.sleep(3)
        return {"status": "ok"}

    j, _ = jobs.enqueue(p, "extract", None)
    seen = {}

    def until():
        if started.is_set():
            seen["stats"] = jobs.stats(p)
            return True
        return False

    w, ok = run_worker(p, {"extract": slow}, until, stop_grace_sec=0.5)
    cur = jobs.get(p, j["id"])
    check("执行中时心跳显示在线、正在执行的任务", ok and seen["stats"]["worker"]["online"]
          and seen["stats"]["worker"]["running"] and seen["stats"]["worker"]["running"][0]["kind"] == "extract",
          seen.get("stats"))
    check("超过等待时间：放回队列，次数不变", cur["status"] == "queued" and cur["attempts"] == 0, cur)


def test_lock(tmp):
    print("\n[7] 单实例锁")
    lp = tmp / "run" / "worker.lock"
    a = worker_mod.acquire_lock(lp)
    b = worker_mod.acquire_lock(lp)
    check("第二个 worker 拿不到锁", a is not None and b is None)
    a.close()
    c = worker_mod.acquire_lock(lp)
    check("第一个退出后可以拿到", c is not None)
    c.close()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="jobtest_"))
    try:
        test_queue(tmp)
        test_retry(tmp)
        test_recover(tmp)
        test_cleanup(tmp)
        test_worker(tmp)
        test_worker_stop(tmp)
        test_lock(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
