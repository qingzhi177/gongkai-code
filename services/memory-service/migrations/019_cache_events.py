"""019 缓存记录：网关每一轮聊天、每一次缓存心跳的用量（Dashboard「缓存与心跳」页用）。

- kind：turn（一轮聊天，工具多轮合在一起）/ beat（缓存心跳：重放上一轮的前缀，让缓存不过期）。
- provider：上游实际是哪家在回答（OpenRouter 会在 Anthropic、Google Vertex、Amazon Bedrock 之间分配，
  每家的缓存是分开的；从回复 id 的前缀看出来：msg_bdrk_ / msg_vrtx_ / msg_01…）。
- relay：中转的名字（Dashboard 里配置的名字）；ttl：这一轮消息部分的缓存时长（1h / 5m）。
- 只存用量数字，不存内容。保留 30 天。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS cache_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        conv_id TEXT,
        provider TEXT,
        relay TEXT,
        model TEXT,
        ttl TEXT,
        input INTEGER NOT NULL DEFAULT 0,
        cache_write INTEGER NOT NULL DEFAULT 0,
        cache_read INTEGER NOT NULL DEFAULT 0,
        output INTEGER NOT NULL DEFAULT 0,
        note TEXT
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_events_ts ON cache_events (ts)")
