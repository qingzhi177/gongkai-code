"""009 主题卷（方案 6.9 节，Phase 4 第二步）：arcs、arc_materials、arc_routing、arc_versions；episodes 加 chunk_key。

- arcs：一条跨越时间的主题线。状态：candidate（候选，还在积累）→ proposed（待你确认）→ active（进行中）/ dormant（30 天没新素材）；
  rejected（你拒绝过，不再提）。sections 是正文的小节（JSON），每节带出处（片段 id）；summary 是一两句话的目录摘要。
- arc_materials：卷的素材目录（片段 + 一句话标签）。narrated：0 还没写进正文，1 写进去了，2 写了但没通过校验。
- arc_routing：哪些片段已经归过卷（归进 0 个卷也算归过）。
- arc_versions：每次发布的正文留一份历史。
- episodes.chunk_key：这个片段来自哪一块对话（指纹）；重新整理时内容没变的块沿用原来的片段。
"""


def up(conn):
    conn.execute("ALTER TABLE episodes ADD COLUMN chunk_key TEXT")
    conn.execute('''CREATE TABLE IF NOT EXISTS arcs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        theme TEXT,                     -- 这条线讲什么（一句话，归卷时给模型看）
        status TEXT NOT NULL,           -- candidate / proposed / active / dormant / rejected
        summary TEXT,                   -- 一两句话（以后放进开窗包的卷目录）
        sections TEXT NOT NULL DEFAULT '[]',   -- JSON：[{title, body, body_with_refs, refs, created_at}]
        version INTEGER NOT NULL DEFAULT 0,
        needs_rebuild INTEGER NOT NULL DEFAULT 0,
        issues TEXT,                    -- JSON：最近一次写卷时校验删掉了什么
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, confirmed_at TEXT,
        last_material_ts TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS arc_materials (
        arc_id INTEGER NOT NULL,
        episode_id INTEGER NOT NULL,
        label TEXT,
        ts TEXT, day TEXT,
        narrated INTEGER NOT NULL DEFAULT 0,
        added_at TEXT NOT NULL,
        PRIMARY KEY (arc_id, episode_id)
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arc_materials_ep ON arc_materials (episode_id)")
    conn.execute('''CREATE TABLE IF NOT EXISTS arc_routing (
        episode_id INTEGER PRIMARY KEY,
        routed_at TEXT NOT NULL,
        model TEXT,
        result TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS arc_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        arc_id INTEGER NOT NULL,
        version INTEGER NOT NULL,
        summary TEXT, sections TEXT,
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arc_versions ON arc_versions (arc_id, version)")
