"""后台任务 worker（Phase 1，方案 7.1 节）

执行 pipeline_jobs 表里的任务：L1 提取、近期摘要、共同经历叙事、画像重建。
这些任务要调用大模型，动辄几十秒，所以放在独立进程里，不拖慢聊天路径；worker 出问题也不影响记忆服务。

怎么运行：
- 平时由记忆服务自动拉起并看护（main.py 的 lifespan），意外退出会自动重启，记忆服务重启时一起重启。
- 各类任务的逻辑在 app/pipeline/（摘要、叙事、画像）和 extract_l1.py（提取）。
  不想让记忆服务拉起时，在 .env 里设 WORKER_AUTOSTART=0，然后手动运行：venv/bin/python worker.py
- 同一时间只会有一个 worker（data/run/worker.lock 文件锁），再启动一个会直接退出（退出码 3）。

规则：
- 每 5 秒看一次队列，同时最多执行 2 个任务；同一种任务同时只执行一个（不会有两个提取同时跑）。
- 失败按 1 分钟、5 分钟、30 分钟重试，次数用完进入死信（见 jobs.py）。
- 收到 SIGTERM（记忆服务重启时）：不再取新任务，正在执行的最多再等 15 秒，没做完的放回队列，下次接着做，不算失败。
- 启动时发现上次异常退出留下的 running 任务：记一次失败，重新排队。
"""
import asyncio
import fcntl
import functools
import inspect
import logging
import os
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import jobs  # noqa: E402
import timeutil  # noqa: E402
from app import config as app_config  # noqa: E402

SQLITE_PATH = app_config.SQLITE_PATH
LOCK_PATH = app_config.DATA_DIR / "run" / "worker.lock"

POLL_SEC = float(os.getenv("WORKER_POLL_SEC", 5))
MAX_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", 2))
STOP_GRACE_SEC = float(os.getenv("WORKER_STOP_GRACE_SEC", 15))
CLEANUP_EVERY_SEC = 3600
EXIT_LOCKED = 3

logger = logging.getLogger("worker")


class JobError(Exception):
    """任务主动报告的失败（按退避时间重试；permanent=True 时直接进死信）"""

    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def acquire_lock(lock_path=LOCK_PATH):
    """拿到单实例锁返回文件对象（进程活着就一直持有），拿不到返回 None。"""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


class Worker:
    def __init__(self, db_path, handlers, poll_sec=POLL_SEC, concurrency=MAX_CONCURRENCY,
                 stop_grace_sec=STOP_GRACE_SEC):
        self.db_path = db_path
        self.handlers = handlers
        self.poll_sec = poll_sec
        self.concurrency = concurrency
        self.stop_grace_sec = stop_grace_sec
        self.worker_id = f"pid:{os.getpid()}"
        self.running = {}          # job_id -> {"kind", "task", "since"}
        self.started_at = None
        self._stop = None
        self._wake = None
        # 同步的处理程序（提取）放在自己的线程池里跑：停止时不用等它们（asyncio 默认线程池退出时会一直等）
        self.executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="job")

    def stop(self):
        if self._stop is not None:
            self._stop.set()
            self._wake.set()

    async def run(self):
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, RuntimeError, ValueError):
                pass   # 不在主线程（测试）时加不了信号处理，直接调用 stop()
        orphans = jobs.recover_orphans(self.db_path)
        if orphans:
            logger.warning(f"[WORKER] 上次异常退出留下 {len(orphans)} 个未完成的任务，已重新排队：{orphans}")
        self.started_at = timeutil.utc_now_str()
        logger.info(f"[WORKER] 已启动（pid {os.getpid()}，同时最多 {self.concurrency} 个任务）")
        last_cleanup = 0.0
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self._fill()
            except Exception as e:  # 数据库暂时锁住之类，下一轮再试
                logger.error(f"[WORKER] 取任务出错：{e}")
            self._heartbeat()
            if time.monotonic() - last_cleanup > CLEANUP_EVERY_SEC:
                last_cleanup = time.monotonic()
                try:
                    n = jobs.cleanup(self.db_path)
                    if n:
                        logger.info(f"[WORKER] 清理了 {n} 条 30 天前完成的任务记录")
                except Exception as e:
                    logger.error(f"[WORKER] 清理任务记录出错：{e}")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_sec)
            except asyncio.TimeoutError:
                pass
        await self._shutdown()

    def _fill(self):
        while len(self.running) < self.concurrency:
            busy = sorted({r["kind"] for r in self.running.values()})
            job = jobs.claim(self.db_path, self.worker_id, exclude_kinds=busy)
            if not job:
                return
            task = asyncio.create_task(self._execute(job))
            self.running[job["id"]] = {"kind": job["kind"], "task": task, "since": timeutil.utc_now_str()}
            task.add_done_callback(lambda _t, jid=job["id"]: self._on_done(jid))

    def _on_done(self, job_id):
        self.running.pop(job_id, None)
        if self._wake is not None:
            self._wake.set()   # 有空位了，马上看看还有没有任务

    def _heartbeat(self):
        try:
            jobs.heartbeat(self.db_path, os.getpid(), self.started_at,
                           [{"id": jid, "kind": r["kind"], "since": r["since"]} for jid, r in self.running.items()])
        except Exception as e:
            logger.error(f"[WORKER] 写心跳出错：{e}")

    async def _execute(self, job):
        label = f"#{job['id']} {job['kind']}"
        t0 = time.monotonic()
        logger.info(f"[JOB] {label} 开始（第 {job['attempts']} 次）")
        try:
            handler = self.handlers.get(job["kind"])
            if handler is None:
                raise JobError(f"没有处理「{job['kind']}」任务的程序", permanent=True)
            payload = job.get("payload") or {}
            if inspect.iscoroutinefunction(handler):
                result = await handler(payload, job)
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(self.executor, functools.partial(handler, payload, job))
            jobs.complete(self.db_path, job["id"], result)
            logger.info(f"[JOB] {label} 完成（{time.monotonic() - t0:.1f} 秒）")
        except asyncio.CancelledError:
            raise
        except JobError as e:
            st = jobs.fail(self.db_path, job["id"], str(e), permanent=e.permanent)
            logger.warning(f"[JOB] {label} 失败：{e} → {'进入死信' if st == 'dead' else '稍后重试'}")
        except Exception as e:
            st = jobs.fail(self.db_path, job["id"], f"{type(e).__name__}: {e}")
            logger.error(f"[JOB] {label} 出错：{type(e).__name__}: {e} → {'进入死信' if st == 'dead' else '稍后重试'}\n"
                         f"{traceback.format_exc()}")

    async def _shutdown(self):
        if self.running:
            logger.info(f"[WORKER] 正在停止，等待 {len(self.running)} 个任务（最多 {self.stop_grace_sec:.0f} 秒）")
            await asyncio.wait([r["task"] for r in self.running.values()], timeout=self.stop_grace_sec)
        for jid, r in list(self.running.items()):
            if not r["task"].done():
                jobs.release(self.db_path, jid)
                r["task"].cancel()
                logger.info(f"[WORKER] #{jid} {r['kind']} 没做完，已放回队列")
        self.executor.shutdown(wait=False, cancel_futures=True)
        try:
            jobs.worker_stopped(self.db_path)
        except Exception:
            pass
        logger.info("[WORKER] 已停止")


# ---------- 各类任务的处理程序 ----------

def _check(result):
    """生成函数出错时返回 {"status": "error"}，这里转成失败，让任务按退避时间重试"""
    if isinstance(result, dict) and result.get("status") == "error":
        raise JobError(result.get("message") or "生成失败")
    return result


def build_handlers():
    # 生成逻辑在 app/pipeline/ 里（从 main.py 拆出来的），worker 不用再导入整个 main.py
    import extract_l1
    from app.pipeline import arcs as arc_pipeline
    from app.pipeline import chapters as chapter_pipeline
    from app.pipeline import digests as digest_pipeline
    from app.pipeline import episodes as episode_pipeline
    from app.pipeline import lexicon as lexicon_pipeline
    from app.pipeline import loops as loops_pipeline
    from app.pipeline import narrative as narrative_pipeline
    from app.pipeline import profile as profile_pipeline
    from app.pipeline import state as state_pipeline
    from app.pipeline import summary as summary_pipeline

    def extract(payload, job):
        return extract_l1.main(force=bool(payload.get("force")))

    async def summary(payload, job):
        return _check(await summary_pipeline.generate_summary())

    async def narrative(payload, job):
        fields = {k: payload[k] for k in ("trigger_type", "force", "reason") if k in payload}
        return _check(await narrative_pipeline.run_narrative(narrative_pipeline.NarrativeGenerateRequest(**fields)))

    async def profile_rebuild(payload, job):
        return _check(await profile_pipeline.rebuild_profile())

    async def settle_day(payload, job):
        result = await episode_pipeline.settle_day(payload["day"], force=bool(payload.get("force")))
        try:                                  # 片段整理好了 → 归卷（主题卷）
            if arc_pipeline.cfg().get("enabled"):
                arc_pipeline.enqueue_route()
        except Exception as e:
            logger.error(f"[JOB] 投递归卷失败：{e}")
        try:                                  # → 那天的日摘（日摘写完会接着投递周摘、近七天摘要）
            if digest_pipeline.cfg().get("enabled"):
                digest_pipeline.enqueue_day(payload["day"])
        except Exception as e:
            logger.error(f"[JOB] 投递日摘失败：{e}")
        try:                                  # → 找共同语汇
            if lexicon_pipeline.cfg().get("enabled"):
                lexicon_pipeline.enqueue_scan()
        except Exception as e:
            logger.error(f"[JOB] 投递共同语汇失败：{e}")
        try:                                  # → 找未完的事
            if loops_pipeline.cfg().get("enabled"):
                loops_pipeline.enqueue_scan()
        except Exception as e:
            logger.error(f"[JOB] 投递未完的事失败：{e}")
        try:                                  # → 写章节（进行中的那章追加新片段）
            if chapter_pipeline.cfg().get("enabled"):
                chapter_pipeline.schedule()
        except Exception as e:
            logger.error(f"[JOB] 投递写章节失败：{e}")
        return result

    def _state_soon():
        try:                                  # 状态卡的材料可能变了：5 分钟后看一下（这期间别的任务还会改材料，攒一攒再写）
            if state_pipeline.cfg().get("enabled"):
                state_pipeline.enqueue(delay_sec=300)
        except Exception as e:
            logger.error(f"[JOB] 投递关系状态失败：{e}")

    async def route_arcs(payload, job):
        return await arc_pipeline.route_arcs()

    async def narrate_arc(payload, job):
        return await arc_pipeline.narrate_arc(int(payload["arc_id"]))

    async def digest_day(payload, job):
        return await digest_pipeline.digest_day(payload["day"])

    async def digest_week(payload, job):
        return await digest_pipeline.digest_week(payload["week"])

    async def digest_recent(payload, job):
        result = await digest_pipeline.digest_recent()
        _state_soon()
        return result

    async def lexicon_scan(payload, job):
        return await lexicon_pipeline.scan()

    async def loops_scan(payload, job):
        result = await loops_pipeline.scan()
        _state_soon()
        return result

    async def state_refresh(payload, job):
        return await state_pipeline.refresh(force=bool(payload.get("force")))

    async def write_chapter(payload, job):
        return await chapter_pipeline.write_chapter(int(payload["chapter_id"]), waited=int(payload.get("waited") or 0))

    async def propose_chapters(payload, job):
        result = await chapter_pipeline.propose()
        if chapter_pipeline.cfg().get("enabled"):
            # 在等这次提议的章：看不出要分、分法不合格时现在就开始写；等你确认的提议照样等着
            try:
                result["queued"] = chapter_pipeline.schedule(exclude_job=job["id"])["queued"]
            except Exception as e:
                logger.error(f"[WORKER] 分章提议以后投递写章节失败：{e}")
        return result

    return {"extract": extract, "summary": summary, "narrative": narrative, "profile_rebuild": profile_rebuild,
            "settle_day": settle_day, "route_arcs": route_arcs, "narrate_arc": narrate_arc,
            "digest_day": digest_day, "digest_week": digest_week, "digest_recent": digest_recent,
            "lexicon_scan": lexicon_scan, "loops_scan": loops_scan,
            "state_refresh": state_refresh, "write_chapter": write_chapter, "propose_chapters": propose_chapters}


def main():
    lock = acquire_lock(LOCK_PATH)
    if lock is None:
        print("[WORKER] 已有另一个 worker 在运行，退出", flush=True)
        sys.exit(EXIT_LOCKED)
    app_config.setup_logging()           # 和记忆服务一样写 data/logs/auto_trigger.log
    handlers = build_handlers()
    worker = Worker(SQLITE_PATH, handlers)
    try:
        asyncio.run(worker.run())
    finally:
        logging.shutdown()
        lock.close()
    # 超过等待时间还没做完的提取（在线程里跑）已放回队列，这里直接退出，不等线程
    os._exit(0)


if __name__ == "__main__":
    main()
