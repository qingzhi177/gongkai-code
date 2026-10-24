"""后台任务队列（Phase 1，方案 7.1 节）

pipeline_jobs 表里的一行就是一个任务。记忆服务（定时检查、Dashboard 按钮）只负责往里投递，
worker.py 负责取出来执行。

状态：
- queued     排队中（attempts > 0 表示失败过、正在等下一次重试，next_run_at 是重试时间）
- running    执行中
- done       完成（result 里是结果）
- dead       失败次数用完（死信），Dashboard 上可以一键重试或忽略
- dismissed  死信被手动忽略

去重：同一个 dedupe_key 同时只能有一个未完成（queued / running）的任务；完成以后可以再投递。
重试：失败后分别等 1 分钟、5 分钟、30 分钟再试；重试 3 次还失败（一共 4 次）就进入死信。
所有时间都是 UTC 的 'YYYY-MM-DD HH:MM:SS'。
"""
import json
import sqlite3
from datetime import timedelta

import db
import timeutil

BACKOFF_SEC = [60, 300, 1800]
DEFAULT_MAX_ATTEMPTS = 4
ERROR_MAX_CHARS = 2000
ACTIVE = ("queued", "running")

COLUMNS = ("id, kind, dedupe_key, payload, status, attempts, max_attempts, next_run_at, last_error, result, "
           "source, locked_by, created_at, started_at, finished_at, updated_at")


def _now():
    return timeutil.utc_now_str()


def _later(seconds):
    return (timeutil.utc_now() + timedelta(seconds=seconds)).strftime(timeutil.STORE_FMT)


def _row(r):
    if r is None:
        return None
    d = dict(zip([c.strip() for c in COLUMNS.split(",")], r))
    for k in ("payload", "result"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d


def _dumps(obj):
    return None if obj is None else json.dumps(obj, ensure_ascii=False, default=str)


def get(db_path, job_id):
    conn = db.connect(db_path)
    try:
        return _row(conn.execute(f"SELECT {COLUMNS} FROM pipeline_jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def enqueue(db_path, kind, payload=None, dedupe_key=None, delay_sec=0, max_attempts=DEFAULT_MAX_ATTEMPTS,
            source="auto", update_if_queued=False, continuation=False):
    """投递任务。返回 (任务, 是否新建)。

    dedupe_key 相同、而且已有未完成的任务时不会新建，直接返回那个任务；
    continuation=True（任务自己投「接着做剩下的」）时，投它的任务自己正在跑不算重复：已经排着一个就不再投，
    否则在 dedupe_key 和 dedupe_key~2 里挑一个没在跑的用（同一个 key 同时只能有一个未完成的任务，两个轮流用）。
    以前「:next」任务跑的时候再投「:next」会被自己挡掉，只能接一次，剩下的要等下一次自动检查；
    update_if_queued=True 时，如果那个任务还在排队（没开始执行），用这次的 payload 覆盖它并立即执行
    （例如自动提取排着队时点了「立即提取」，就把它改成 force）。
    """
    now = _now()
    run_at = _later(delay_sec) if delay_sec else now
    conn = db.connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if dedupe_key and continuation:
                keys = (dedupe_key, dedupe_key + "~2")
                active = [_row(r) for r in conn.execute(
                    f"SELECT {COLUMNS} FROM pipeline_jobs WHERE dedupe_key IN (?, ?) AND status IN ('queued','running') ORDER BY id",
                    keys)]
                queued = [j for j in active if j["status"] == "queued"]
                free = [k for k in keys if k not in {j["dedupe_key"] for j in active}]
                if queued or not free:
                    conn.execute("COMMIT")
                    return (queued or active)[0], False
                dedupe_key = free[0]
            elif dedupe_key:
                r = conn.execute(f"SELECT {COLUMNS} FROM pipeline_jobs WHERE dedupe_key=? AND status IN ('queued','running')",
                                 (dedupe_key,)).fetchone()
                if r:
                    job = _row(r)
                    if update_if_queued and job["status"] == "queued":
                        conn.execute("UPDATE pipeline_jobs SET payload=?, source=?, next_run_at=?, updated_at=? WHERE id=?",
                                     (_dumps(payload), source, run_at, now, job["id"]))
                        job = _row(conn.execute(f"SELECT {COLUMNS} FROM pipeline_jobs WHERE id=?", (job["id"],)).fetchone())
                    conn.execute("COMMIT")
                    return job, False
            cur = conn.execute(
                "INSERT INTO pipeline_jobs (kind, dedupe_key, payload, status, attempts, max_attempts, next_run_at, "
                "source, created_at, updated_at) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)",
                (kind, dedupe_key, _dumps(payload), max_attempts, run_at, source, now, now))
            job = _row(conn.execute(f"SELECT {COLUMNS} FROM pipeline_jobs WHERE id=?", (cur.lastrowid,)).fetchone())
            conn.execute("COMMIT")
            return job, True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def claim(db_path, worker_id, exclude_kinds=(), kinds=None):
    """取一个到期的任务并标记为 running（attempts + 1）。没有可执行的任务时返回 None。"""
    now = _now()
    conn = db.connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            sql = f"SELECT {COLUMNS} FROM pipeline_jobs WHERE status='queued' AND next_run_at <= ?"
            args = [now]
            if exclude_kinds:
                sql += f" AND kind NOT IN ({','.join('?' * len(exclude_kinds))})"
                args += list(exclude_kinds)
            if kinds is not None:
                if not kinds:
                    conn.execute("COMMIT")
                    return None
                sql += f" AND kind IN ({','.join('?' * len(kinds))})"
                args += list(kinds)
            sql += " ORDER BY next_run_at, id LIMIT 1"
            r = conn.execute(sql, args).fetchone()
            if not r:
                conn.execute("COMMIT")
                return None
            job = _row(r)
            conn.execute("UPDATE pipeline_jobs SET status='running', attempts=attempts+1, locked_by=?, started_at=?, "
                         "finished_at=NULL, updated_at=? WHERE id=?", (worker_id, now, now, job["id"]))
            conn.execute("COMMIT")
            job.update(status="running", attempts=job["attempts"] + 1, locked_by=worker_id, started_at=now)
            return job
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def complete(db_path, job_id, result=None):
    now = _now()
    conn = db.connect(db_path)
    try:
        conn.execute("UPDATE pipeline_jobs SET status='done', result=?, locked_by=NULL, finished_at=?, updated_at=? "
                     "WHERE id=?", (_dumps(result), now, now, job_id))
        conn.commit()
    finally:
        conn.close()


def fail(db_path, job_id, error, permanent=False):
    """记一次失败。还有次数就按退避时间重新排队，否则进入死信。返回新状态（'queued' 或 'dead'）。"""
    now = _now()
    error = (str(error) or "未知错误")[:ERROR_MAX_CHARS]
    conn = db.connect(db_path)
    try:
        r = conn.execute("SELECT attempts, max_attempts FROM pipeline_jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            return None
        attempts, max_attempts = r
        if permanent or attempts >= max_attempts:
            conn.execute("UPDATE pipeline_jobs SET status='dead', last_error=?, locked_by=NULL, finished_at=?, "
                         "updated_at=? WHERE id=?", (error, now, now, job_id))
            status = "dead"
        else:
            wait = BACKOFF_SEC[min(max(attempts, 1) - 1, len(BACKOFF_SEC) - 1)]
            conn.execute("UPDATE pipeline_jobs SET status='queued', last_error=?, locked_by=NULL, next_run_at=?, "
                         "updated_at=? WHERE id=?", (error, _later(wait), now, job_id))
            status = "queued"
        conn.commit()
        return status
    finally:
        conn.close()


def release(db_path, job_id):
    """worker 正常退出时把还没做完的任务放回队列，这次不算失败。"""
    now = _now()
    conn = db.connect(db_path)
    try:
        conn.execute("UPDATE pipeline_jobs SET status='queued', attempts=MAX(attempts-1, 0), locked_by=NULL, "
                     "next_run_at=?, updated_at=? WHERE id=? AND status='running'", (now, now, job_id))
        conn.commit()
    finally:
        conn.close()


def recover_orphans(db_path, reason="worker 意外退出，任务被中断"):
    """worker 启动时调用：上一个 worker 没来得及收尾的 running 任务，记一次失败后重新排队（或进死信）。"""
    now = _now()
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT id, attempts, max_attempts FROM pipeline_jobs WHERE status='running'").fetchall()
        for job_id, attempts, max_attempts in rows:
            if attempts >= max_attempts:
                conn.execute("UPDATE pipeline_jobs SET status='dead', last_error=?, locked_by=NULL, finished_at=?, "
                             "updated_at=? WHERE id=?", (reason, now, now, job_id))
            else:
                conn.execute("UPDATE pipeline_jobs SET status='queued', last_error=?, locked_by=NULL, next_run_at=?, "
                             "updated_at=? WHERE id=?", (reason, now, now, job_id))
        conn.commit()
        return [r[0] for r in rows]
    finally:
        conn.close()


def retry(db_path, job_id):
    """死信（或已忽略的）任务重新排队，次数清零。返回 (ok, 说明)。"""
    now = _now()
    conn = db.connect(db_path)
    try:
        r = conn.execute("SELECT status, dedupe_key FROM pipeline_jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            return False, "任务不存在"
        status, key = r
        if status not in ("dead", "dismissed"):
            return False, "只有失败的任务可以重试"
        if key and conn.execute("SELECT 1 FROM pipeline_jobs WHERE dedupe_key=? AND status IN ('queued','running')",
                                (key,)).fetchone():
            return False, "已经有一个同类任务在排队或执行中"
        conn.execute("UPDATE pipeline_jobs SET status='queued', attempts=0, next_run_at=?, finished_at=NULL, "
                     "source=?, updated_at=? WHERE id=?", (now, "dashboard", now, job_id))
        conn.commit()
        return True, "已重新排队"
    except sqlite3.IntegrityError:
        return False, "已经有一个同类任务在排队或执行中"
    finally:
        conn.close()


def dismiss(db_path, job_id):
    now = _now()
    conn = db.connect(db_path)
    try:
        n = conn.execute("UPDATE pipeline_jobs SET status='dismissed', updated_at=? WHERE id=? AND status='dead'",
                         (now, job_id)).rowcount
        conn.commit()
        return n > 0
    finally:
        conn.close()


def latest(db_path, kind):
    conn = db.connect(db_path)
    try:
        return _row(conn.execute(f"SELECT {COLUMNS} FROM pipeline_jobs WHERE kind=? ORDER BY id DESC LIMIT 1",
                                 (kind,)).fetchone())
    finally:
        conn.close()


def list_jobs(db_path, status=None, kind=None, limit=50):
    sql = f"SELECT {COLUMNS} FROM pipeline_jobs WHERE 1=1"
    args = []
    if status == "retrying":
        sql += " AND status='queued' AND attempts > 0"
    elif status:
        sql += " AND status=?"
        args.append(status)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    conn = db.connect(db_path)
    try:
        return [_row(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def stats(db_path):
    conn = db.connect(db_path)
    try:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM pipeline_jobs GROUP BY status").fetchall())
        retrying = conn.execute("SELECT COUNT(*) FROM pipeline_jobs WHERE status='queued' AND attempts > 0").fetchone()[0]
        day_ago = (timeutil.utc_now() - timedelta(days=1)).strftime(timeutil.STORE_FMT)
        done_24h = conn.execute("SELECT COUNT(*) FROM pipeline_jobs WHERE status='done' AND finished_at >= ?",
                                (day_ago,)).fetchone()[0]
        w = conn.execute("SELECT pid, started_at, heartbeat_at, running FROM worker_state WHERE id=1").fetchone()
    finally:
        conn.close()
    worker = None
    if w:
        age = None
        if w[2]:
            age = int((timeutil.utc_now() - timeutil.parse_ts(w[2])).total_seconds())
        try:
            running = json.loads(w[3]) if w[3] else []
        except ValueError:
            running = []
        worker = {"pid": w[0], "started_at": w[1], "heartbeat_at": w[2], "heartbeat_age_sec": age,
                  "online": age is not None and age <= 30, "running": running}
    return {
        "queued": counts.get("queued", 0) - retrying,
        "retrying": retrying,
        "running": counts.get("running", 0),
        "dead": counts.get("dead", 0),
        "done_24h": done_24h,
        "worker": worker,
    }


def heartbeat(db_path, pid, started_at, running):
    now = _now()
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT INTO worker_state (id, pid, started_at, heartbeat_at, running) VALUES (1, ?, ?, ?, ?) "
                     "ON CONFLICT(id) DO UPDATE SET pid=excluded.pid, started_at=excluded.started_at, "
                     "heartbeat_at=excluded.heartbeat_at, running=excluded.running",
                     (pid, started_at, now, _dumps(running)))
        conn.commit()
    finally:
        conn.close()


def worker_stopped(db_path):
    """worker 正常退出：清掉心跳，Dashboard 马上显示离线"""
    conn = db.connect(db_path)
    try:
        conn.execute("UPDATE worker_state SET heartbeat_at=NULL, running='[]' WHERE id=1")
        conn.commit()
    finally:
        conn.close()


def cleanup(db_path, keep_days=30):
    """删掉 keep_days 天以前完成 / 已忽略的任务（死信不删，等人处理）。返回删除条数。"""
    cutoff = (timeutil.utc_now() - timedelta(days=keep_days)).strftime(timeutil.STORE_FMT)
    conn = db.connect(db_path)
    try:
        n = conn.execute("DELETE FROM pipeline_jobs WHERE status IN ('done','dismissed') AND updated_at < ?",
                         (cutoff,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def describe(job):
    """给人看的一句话状态（Dashboard / 状态接口用）"""
    if not job:
        return ""
    st = job["status"]
    if st == "queued" and job["attempts"] > 0:
        return f"第 {job['attempts']} 次失败，{timeutil.local_str(job['next_run_at'], '%H:%M')} 自动重试：{job.get('last_error') or ''}"
    if st == "dead":
        return f"失败 {job['attempts']} 次，已停止重试：{job.get('last_error') or ''}"
    return {"queued": "排队中", "running": "执行中", "done": "完成", "dismissed": "已忽略"}.get(st, st)
