"""003 后台任务队列（方案 7.1 节）：pipeline_jobs + worker_state。

提取、近期摘要、共同经历叙事、画像重建这些要调用大模型的任务，以后都进这张表，由 worker.py 执行：
失败自动重试，重试次数用完进入死信，Dashboard 上能看到。说明见 jobs.py。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS pipeline_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,                  -- extract / summary / narrative / profile_rebuild ……
        dedupe_key TEXT,                     -- 同一个 key 同时只能有一个未完成的任务
        payload TEXT,                        -- JSON
        status TEXT NOT NULL DEFAULT 'queued',   -- queued / running / done / dead / dismissed
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 4,
        next_run_at TEXT NOT NULL,           -- UTC；重试时是下次执行的时间
        last_error TEXT,
        result TEXT,                         -- JSON
        source TEXT,                         -- auto / dashboard / api
        locked_by TEXT,                      -- 正在执行它的 worker
        prompt_version TEXT,
        model TEXT,
        tokens_in INTEGER,
        tokens_out INTEGER,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_due ON pipeline_jobs (status, next_run_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_kind ON pipeline_jobs (kind, id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe_active ON pipeline_jobs (dedupe_key) "
                 "WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running')")

    # worker 心跳（单行）：Dashboard 据此显示 worker 是否在线、正在做什么
    conn.execute('''CREATE TABLE IF NOT EXISTS worker_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        pid INTEGER,
        started_at TEXT,
        heartbeat_at TEXT,
        running TEXT                         -- JSON：正在执行的任务
    )''')
