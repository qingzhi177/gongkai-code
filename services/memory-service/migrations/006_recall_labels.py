"""006 黄金集标注（方案 14.1 节）：在「召回对比」里给某一轮的候选记忆标「该想起 / 不该想起」，
或者把整轮标成「什么都不该想起」。用来校准召回门槛、以后改召回逻辑时防止退步。

标注只存在这个库里（私有），不进仓库。
- item_id = 0 表示整轮的标注（label = none：这一轮什么都不该想起）；
- 其余 item_id 是 L1 记忆的 id，label = should / should_not。
- query / user_text 冗余存一份：轨迹以后清理掉，标注也还能用来重跑。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS recall_labels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id INTEGER NOT NULL,     -- recall_traces.id
        item_id INTEGER NOT NULL,      -- L1 id；0 = 整轮
        label TEXT NOT NULL,           -- should / should_not / none
        query TEXT,
        user_text TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (trace_id, item_id)
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_labels_label ON recall_labels (label)")
