"""017 事实层（方案 6.6 节，Phase 4 第八步）：facts、fact_events、facts_scans。

- facts：一条「键 → 值」的事实，比如 user.city → 杭州、user.pet.cat.name → 某个名字、us.nickname.for_ai → 某个称呼。
  fact_key 键（user. / ai. / us. 开头）；label 中文名（「住在哪个城市」）；subject 关于谁（user / ai / us）；value 值；
  status：current（现在的）/ superseded（以前的，valid_to 是被换掉的时间）/ disputed（可能变了：随口提到一个不一样的说法，等确认）/
  rejected（你说不对）；certainty 当时怎么说的：explicit（明确说的）/ vague（随口、不确定）；
  valid_from 从什么时候起（出处消息的时间）；superseded_by 被哪一条换掉；l0_id / quote 出处和原话；
  last_confirmed_at / confirm_count：后来又被说到几次；source scan / dashboard；edited 你改过（扫描不再自动换掉它，只标「可能变了」）。
- fact_events：每条事实的经过（created / confirmed / superseded / disputed / resolved / edited / rejected / restored），带原话。
- facts_scans：扫过的对话块（块的指纹和每日片段一样），每块只扫一次。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_key TEXT NOT NULL,
        label TEXT NOT NULL,
        subject TEXT NOT NULL,
        value TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'current',
        certainty TEXT,
        valid_from TEXT, valid_to TEXT,
        superseded_by INTEGER,
        l0_id INTEGER, quote TEXT,
        last_confirmed_at TEXT,
        confirm_count INTEGER NOT NULL DEFAULT 0,
        source TEXT,
        edited INTEGER NOT NULL DEFAULT 0,
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_key ON facts (fact_key, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_status ON facts (status)")
    conn.execute('''CREATE TABLE IF NOT EXISTS fact_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_id INTEGER NOT NULL,
        event TEXT NOT NULL,
        l0_id INTEGER, ts TEXT, quote TEXT, detail TEXT,
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_events_fact ON fact_events (fact_id)")
    conn.execute('''CREATE TABLE IF NOT EXISTS facts_scans (
        chunk_key TEXT PRIMARY KEY,
        day TEXT, conv_id TEXT,
        scanned_at TEXT NOT NULL,
        model TEXT, prompt_version TEXT,
        result TEXT
    )''')
