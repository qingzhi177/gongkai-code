"""008 每日片段（方案 6.5 节，Phase 4 第一步）：episodes + settle_days。

- episodes：一段经历（半天以内），用「我们」的视角写成，每个具体细节都标了出处（L0 原文的行）。
  按天整理（每日结算），同一天重新整理时旧版本标成 superseded、新版本 version + 1。
  现在只生成、只在 Dashboard 上看，还不注入聊天；以后是主题卷、章节、近七天摘要的素材，也是「那天……」的召回单元。
- settle_days：每一天整理到哪了（整理时这一天的消息条数和最后一条的 id）；有新消息进来、或者删了消息，就会重新整理。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL,              -- 用户时区的日期（凌晨 4 点前算前一天）
        conv_id TEXT,
        start_ts TEXT, end_ts TEXT,     -- UTC
        title TEXT NOT NULL,
        body TEXT NOT NULL,             -- 去掉出处标记的正文
        body_with_refs TEXT NOT NULL,   -- 带出处的原稿（白盒查看用）：[#n] 是 refs 里的第 n 条
        mood TEXT,
        topics TEXT,                    -- JSON 数组
        refs TEXT,                      -- JSON：引用的 L0 id（按原文顺序）
        salience REAL,
        status TEXT NOT NULL DEFAULT 'active',   -- active / superseded
        version INTEGER NOT NULL DEFAULT 1,
        issues TEXT,                    -- JSON：校验时去掉了哪些句子、为什么
        model TEXT,
        prompt_version TEXT,
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_day ON episodes (day, status)")
    conn.execute('''CREATE TABLE IF NOT EXISTS settle_days (
        day TEXT PRIMARY KEY,
        status TEXT NOT NULL,           -- done / error
        last_l0_id INTEGER,             -- 整理时这一天最后一条消息的 id
        messages INTEGER,               -- 整理时这一天的消息条数
        sessions INTEGER, episodes INTEGER,
        detail TEXT,                    -- JSON：每段对话的处理结果
        version INTEGER NOT NULL DEFAULT 1,
        settled_at TEXT NOT NULL
    )''')
