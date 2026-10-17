"""002 常用查询的索引。

之前整个库一个索引都没有：按会话取 L0、找未提取的消息、按状态筛 L1，每次都是全表扫描。
现在几百行感觉不到，到几万行就会明显变慢。这里只加索引，不改任何数据。
"""

INDEXES = [
    # 按会话取对话、序列对齐（save_conversation）、上次对话时间（last_session）
    "CREATE INDEX IF NOT EXISTS idx_l0_conv ON l0_messages (conv_id, status, msg_idx)",
    # 提取：找未提取的消息
    "CREATE INDEX IF NOT EXISTS idx_l0_extract ON l0_messages (status, extracted)",
    # group 语义的版本替换
    "CREATE INDEX IF NOT EXISTS idx_l0_group ON l0_messages (conv_id, group_id)",
    # L1 按状态 / 类型筛选
    "CREATE INDEX IF NOT EXISTS idx_l1_status ON l1_memories (status, event_type)",
    # L1 按会话 + 来源批次（批次定位、溯源）
    "CREATE INDEX IF NOT EXISTS idx_l1_conv ON l1_memories (conv_id, status, source_msg_id)",
    # 思考链按会话
    "CREATE INDEX IF NOT EXISTS idx_thinking_conv ON thinking_records (conv_id)",
]


def up(conn):
    for sql in INDEXES:
        conn.execute(sql)
