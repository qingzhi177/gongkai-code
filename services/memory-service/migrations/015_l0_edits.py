"""015 L0 原文的修改记录（Dashboard 上手动修改 L0，见 app/l0_edit.py）：l0_edits。

- 每改一次记一行：改之前 old_content、改之后 new_content；
- mode：fix（只改字，连带改引用了这段原话的地方）/ reextract（意思改了，重新提取）；
- synced：连带改了哪些地方 {表: [id, ...]}（JSON）。
前端手里还是改之前的原文，存记录时靠这张表认出「这是改过的那一条」，不会把修改冲掉。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS l0_edits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        l0_id INTEGER NOT NULL,
        conv_id TEXT,
        old_content TEXT NOT NULL,
        new_content TEXT NOT NULL,
        mode TEXT NOT NULL,
        synced TEXT,
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_l0_edits_l0 ON l0_edits (l0_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_l0_edits_conv ON l0_edits (conv_id)")
