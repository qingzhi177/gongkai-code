"""013 关系状态卡和话题边界（方案 6.12 节，Phase 4 第六步）：relationship_states、topic_policies。

- relationship_states：一版状态卡。lines：[{key, text, with_refs, refs}]，key 是 rhythm / weather / ongoing / close；
  refs：卡片里 [#n] 对应的材料 [{kind: digest / episode / loop / arc, id}]；signals：统计信号（只给 Dashboard）；
  source_key：材料的指纹，变了才重写；status：active / superseded / rejected（没通过校验，不发布）。
  「轻放」和「上次聊天」两行不存，看的时候现算。
- topic_policies：话题边界。topic 话题名；keywords 关键词（JSON 数组）；
  policy：normal（正常）/ explicit_only（你提起时才提）/ excluded（不主动提起）。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS relationship_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL,
        lines TEXT NOT NULL,
        refs TEXT,
        signals TEXT,
        source_key TEXT,
        status TEXT NOT NULL,
        version INTEGER NOT NULL,
        issues TEXT,
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_relationship_states_status ON relationship_states (status)")
    conn.execute('''CREATE TABLE IF NOT EXISTS topic_policies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL UNIQUE,
        keywords TEXT,
        policy TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )''')
