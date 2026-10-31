"""020 小本子：模型自己随手记下的小事（聊天里偶然冒出来的细碎细节、有意思的小瞬间）。

和「对彼此的认识和理解」（画像，稳定的认识）、feel（自己的感受）分开：这里是一件件具体的小事，用第一人称写一两句。
- source：ai（模型用 jot 工具记的）/ user（在 Dashboard 上自己写的）。
- status：active / erased（擦掉的留着，Dashboard 上能恢复）。
- pinned：钉住的一直放进聊天（不受「最近几天」的限制）。
- conv_id、anchor_l0_id：记的时候在哪个对话、那时最新的一条消息（以后可以按它找回上下文）。
- edited_by：最后一次是谁改的（ai / user）。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'ai',
        status TEXT NOT NULL DEFAULT 'active',
        pinned INTEGER NOT NULL DEFAULT 0,
        conv_id TEXT,
        anchor_l0_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        edited_by TEXT
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_created ON notes (status, created_at)")
