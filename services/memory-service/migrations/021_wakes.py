"""021 自动醒来：对方不在的时候，ta 偶尔醒来一下（随机的，或者 ta 自己约的时间）。

- wake_plans：ta 用 schedule_wake 约的时间（也可以在 Dashboard 上替 ta 约 / 取消）。
  status：pending（等着）/ done（醒了，run_id 是那一次）/ cancelled（取消了）/ skipped（到点没醒，note 写原因）。
  mentioned：没醒的那些，下次聊天时告诉过 ta 没有。
- wake_runs：每一次醒来。outcome：running / spoke（说了话）/ silent（没说话）/ error / interrupted（对方正好回来了）。
  说了话的（delivery）：pending（等前端来取）/ delivered（前端取走、显示了，l0_id 是存进聊天记录的那一行）/
  missed（对方先发来了消息，前端还没取：不再给前端，下次聊天时告诉 ta）/ dropped（前端说不显示了）。
  anchor_l0_id：醒来时这个对话最新的一条，说的话存进聊天记录时接在它后面。
  mentioned：下次聊天时告诉过 ta 没有（「你不在的时候我醒来过两次」）。
- wake_state：掷骰子的时间这类小状态。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS wake_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id TEXT,
        due_at TEXT NOT NULL,
        reason TEXT,
        source TEXT NOT NULL DEFAULT 'ai',
        status TEXT NOT NULL DEFAULT 'pending',
        note TEXT,
        run_id INTEGER,
        mentioned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wake_plans_due ON wake_plans (status, due_at)")
    conn.execute('''CREATE TABLE IF NOT EXISTS wake_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id TEXT,
        kind TEXT NOT NULL,
        plan_id INTEGER,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT NOT NULL DEFAULT 'running',
        text TEXT,
        aside TEXT,
        thinking TEXT,
        tools TEXT,
        usage TEXT,
        note TEXT,
        last_real_at TEXT,
        anchor_l0_id INTEGER,
        delivery TEXT,
        delivered_at TEXT,
        l0_id INTEGER,
        push TEXT,
        mentioned INTEGER NOT NULL DEFAULT 0
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wake_runs_started ON wake_runs (started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wake_runs_delivery ON wake_runs (delivery, conv_id)")
    conn.execute('''CREATE TABLE IF NOT EXISTS wake_state (
        k TEXT PRIMARY KEY,
        v TEXT,
        updated_at TEXT
    )''')
