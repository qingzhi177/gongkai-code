"""007 分支会话（Pluvia「从这里分出去」，接入约定第 12 节）：conv_branches。

分支会话第一次存记录时登记：从哪个会话分出来、和原会话重合的那段历史（每条消息的指纹和它在原会话里的位置）。
之后这个分支只存分支以后的新内容，重合部分只在原会话名下存一份，不会被提取成两份记忆。详见 app/branches.py。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS conv_branches (
        conv_id TEXT PRIMARY KEY,         -- 分支会话
        parent_conv_id TEXT NOT NULL,     -- 从哪个会话分出来
        parent_turn_id TEXT,              -- 前端给的分支点那一轮（只记录，不参与计算）
        prefix TEXT NOT NULL,             -- JSON：[[指纹, 存在哪个会话, 行 id], ...]
        shared INTEGER NOT NULL,          -- 第一次登记时重合了几条
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_parent ON conv_branches (parent_conv_id)")
