"""012 未完的事（方案 6.8 节，Phase 4 第五步）：open_loops、loop_events、loops_scans。

- open_loops：一件还没了结的事（答应的事、一起的计划、在等的结果、牵挂、没聊完的话题、定期的约定）。
  kind 种类；text 一句话（用「我」「你」写）；owner 谁的事（user / ai / both）；due_at 软期限（YYYY-MM-DD，可以没有）；
  status：open（进行中）/ done（有结果了）/ dropped（不用再管了）/ expired（自动过期）；
  follow_up_policy 跟进方式：when_relevant（相关时可以提）/ when_due（到期时才提）/ never_push（不主动提）；
  created_* / closed_*：出现和了结时的消息、时间、原话；closed_text 了结时的一句「后续」；closed_by scan / dashboard / auto；
  last_mentioned_at / mention_count：最近一次被提起、被提起几次；edited：你改过，扫描不再自动改期限。
- loop_events：每件事的经过（created / mentioned / done / dropped / expired / reopened / edited），带原话。
- loops_scans：扫过的对话块（块的指纹和每日片段一样），每块只扫一次。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS open_loops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        owner TEXT,
        due_at TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        follow_up_policy TEXT NOT NULL DEFAULT 'when_relevant',
        created_l0_id INTEGER, created_ts TEXT, created_quote TEXT,
        closed_l0_id INTEGER, closed_ts TEXT, closed_quote TEXT, closed_text TEXT, closed_by TEXT,
        last_mentioned_at TEXT,
        mention_count INTEGER NOT NULL DEFAULT 0,
        source TEXT,
        edited INTEGER NOT NULL DEFAULT 0,
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_open_loops_status ON open_loops (status)")
    conn.execute('''CREATE TABLE IF NOT EXISTS loop_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        loop_id INTEGER NOT NULL,
        event TEXT NOT NULL,
        l0_id INTEGER, ts TEXT, quote TEXT, detail TEXT,
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_events_loop ON loop_events (loop_id)")
    conn.execute('''CREATE TABLE IF NOT EXISTS loops_scans (
        chunk_key TEXT PRIMARY KEY,
        day TEXT, conv_id TEXT,
        scanned_at TEXT NOT NULL,
        model TEXT, prompt_version TEXT,
        result TEXT
    )''')
