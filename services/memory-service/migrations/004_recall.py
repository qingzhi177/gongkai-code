"""004 召回编排（方案 8.5 节、附录 C）：召回轨迹 recall_traces + 投递账本 injections。

- recall_traces：每一轮聊天一行——用户说了什么、识别成什么意图、各通道检索到什么、精排分、最后决定注入哪些、
  耗时；影子模式下还记着网关旧逻辑这一轮的决定（legacy），方便对比。
- injections：每条要注入的记忆一行（planned → delivered / void；影子模式是 shadow）。冷却只看这张表。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS recall_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_key TEXT,
        backend TEXT,                 -- api / cc
        session_id TEXT,              -- API 模式下是 conv_id
        client TEXT,
        mode TEXT,                    -- shadow / live
        query TEXT,                   -- 实际用来检索的文本（短消息会拼上上文）
        user_text TEXT,               -- 这一轮用户的话
        intent TEXT,                  -- JSON
        channels TEXT,                -- JSON：各通道的候选和分数
        rerank TEXT,                  -- JSON：精排模型、是否成功、耗时
        decision TEXT,                -- JSON：分层决策（候选、可选、被冷却拿掉的、最终注入的）
        card TEXT,                    -- 会注入的念头式文字
        legacy TEXT,                  -- JSON：网关旧逻辑这一轮的决定（影子对比用）
        injected_chars INTEGER,
        latency TEXT,                 -- JSON：各步耗时（毫秒）
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_session ON recall_traces (backend, session_id, mode, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_created ON recall_traces (created_at)")

    conn.execute('''CREATE TABLE IF NOT EXISTS injections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_key TEXT NOT NULL,
        trace_id INTEGER,
        backend TEXT NOT NULL,
        session_id TEXT,
        item_type TEXT,               -- moment（现在的 L1）/ 以后：episode / feel / lexicon / loop / arc
        item_id TEXT,
        tier TEXT,                    -- recall / strong / medium / serendipity …
        channel TEXT,
        status TEXT DEFAULT 'planned',   -- planned / delivered / void / shadow
        created_at TEXT NOT NULL,
        delivered_at TEXT
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inj_trace ON injections (trace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inj_item ON injections (item_type, status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inj_turn ON injections (turn_key)")
