"""018 换窗衔接（方案第 16 章，Phase 5 第一步）：window_bridges、window_state、conv_windows，conv_branches 加 kind。

- window_bridges：滑出窗口那段的小结。一块（同一段对话里大约 16 条）写一两句，加一个「那会儿」的温度词。
  chunk_key 是这一块的指纹（消息 id + 内容，和每日片段一样），内容变了（改了、重新生成了）就是另一块；
  conv_id 是哪个会话的窗口用到的，src_convs 是这一块的消息实际存在哪些会话（分支 / 搬家的会话会带着原会话的消息）；
  status：active（有小结）/ empty（只是寒暄、太短，或者没写出能用的）。
- window_state：每个会话的窗口开头那行附注，按窗口的第一条消息（first_row_id）定格：窗口没挪，这一行就不变，缓存才能命中。
- conv_windows：会话从哪来。kind：new（新开的，prev_conv_id 是那时最近聊过的另一个会话）/ branch（从这里分出去）/
  forged（挑几段搬过来）/ unknown（第一次见到时已经带着历史，比如在别的设备上开的）。
- conv_branches.kind：branch（开头连续一段和原会话一样）/ forged（挑选式搬家：带过来的几段在原会话里可以不连续）。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS window_bridges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chunk_key TEXT NOT NULL UNIQUE,
        conv_id TEXT NOT NULL,
        src_convs TEXT,
        first_id INTEGER, last_id INTEGER,
        start_ts TEXT, end_ts TEXT,
        n INTEGER NOT NULL DEFAULT 0,
        gist TEXT, mood TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        issues TEXT,
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_window_bridges_conv ON window_bridges (conv_id, start_ts)")
    conn.execute('''CREATE TABLE IF NOT EXISTS window_state (
        conv_id TEXT PRIMARY KEY,
        first_row_id INTEGER,
        sig TEXT,
        note TEXT,
        pending INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS conv_windows (
        conv_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        prev_conv_id TEXT,
        created_at TEXT NOT NULL
    )''')
    cols = [r[1] for r in conn.execute("PRAGMA table_info(conv_branches)")]
    if cols and "kind" not in cols:
        conn.execute("ALTER TABLE conv_branches ADD COLUMN kind TEXT NOT NULL DEFAULT 'branch'")
