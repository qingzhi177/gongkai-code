"""014 章节（方案 6.10 节，Phase 4 第七步）：chapters、chapter_episodes、chapter_versions、chapter_proposals。

- chapters：一章。n 第几章；start_day / end_day 日期范围（进行中的那章 end_day 为空）；status：current（进行中）/ sealed（已封存）；
  sections：正文小节 [{title, body, body_with_refs, refs: 片段 id}]；summary：进行中这章的一句话；abstract：封存后两三句话的摘要；
  needs_rebuild：要整章重写；abstract_version：摘要是按哪一版写的（负数：这一版试过没写成）。
  一章的素材按日期算（日期落在这一章里的片段），不另外分配。
- chapter_episodes：写进了哪一章的片段（status 1 写进去了 / 2 没通过校验、跳过）。
- chapter_versions：每次写完留一份历史。
- chapter_proposals：分章提议 chapters：[{title, start, end, why}]；status：pending（等你确认）/ applied / dismissed /
  nochange（看不出要分）/ rejected（模型给的分法不合格）。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS chapters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        n INTEGER NOT NULL,
        title TEXT, abstract TEXT, summary TEXT,
        start_day TEXT NOT NULL, end_day TEXT,
        status TEXT NOT NULL,
        sections TEXT NOT NULL DEFAULT '[]',
        version INTEGER NOT NULL DEFAULT 0,
        needs_rebuild INTEGER NOT NULL DEFAULT 0,
        abstract_version INTEGER NOT NULL DEFAULT 0,
        issues TEXT,
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sealed_at TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chapter_episodes (
        chapter_id INTEGER NOT NULL,
        episode_id INTEGER NOT NULL,
        status INTEGER NOT NULL,
        PRIMARY KEY (chapter_id, episode_id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chapter_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chapter_id INTEGER NOT NULL,
        version INTEGER NOT NULL,
        title TEXT, abstract TEXT, summary TEXT, sections TEXT,
        start_day TEXT, end_day TEXT,
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chapter_versions ON chapter_versions (chapter_id, version)")
    conn.execute('''CREATE TABLE IF NOT EXISTS chapter_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chapter_id INTEGER,
        chapters TEXT,
        status TEXT NOT NULL,
        issues TEXT,
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL, decided_at TEXT
    )''')
