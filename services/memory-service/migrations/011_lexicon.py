"""011 共同语汇（方案 6.7 节，Phase 4 第四步）：lexicon、lexicon_scans。

- lexicon：一个只有我们懂的说法（称呼、小梗、固定说法、口头禅、歌、地方、东西）。
  term_key 是比较用的写法（去掉空白引号、小写），唯一；拒绝过、合并过的也留着，免得再提。
  status：candidate（候选）/ active（收下）/ rejected（不要）/ merged（并进了 merged_into 那个词）。
  统计按原文直接数：use_count（几条消息）、days_seen（几天）、first_seen / last_used、origin_l0_id（第一次出现的消息）、
  coined_by（谁先说的：user / ai）。evidence：扫描时模型指出的消息 id。edited：你改过含义，扫描不覆盖。
- lexicon_scans：扫过的对话块（块的指纹和每日片段一样），每块只扫一次。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS lexicon (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term TEXT NOT NULL,
        term_key TEXT NOT NULL UNIQUE,
        aliases TEXT,
        kind TEXT,
        meaning TEXT,
        status TEXT NOT NULL,
        merged_into INTEGER,
        coined_by TEXT,
        origin_l0_id INTEGER, origin_ts TEXT,
        first_seen TEXT, last_used TEXT,
        use_count INTEGER NOT NULL DEFAULT 0,
        days_seen INTEGER NOT NULL DEFAULT 0,
        evidence TEXT,
        source TEXT,
        edited INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lexicon_status ON lexicon (status)")
    conn.execute('''CREATE TABLE IF NOT EXISTS lexicon_scans (
        chunk_key TEXT PRIMARY KEY,
        day TEXT, conv_id TEXT,
        scanned_at TEXT NOT NULL,
        model TEXT, prompt_version TEXT,
        result TEXT
    )''')
