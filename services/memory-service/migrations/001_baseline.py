"""001 基线：现有的表结构（2026-10-18 之前散落在 main.py 的 init_db() 和 extract_l1.py 的 ensure_columns() 里）。

对已经在用的库来说这是空操作（表和列都已存在），只是记下「版本 1」；对全新的空库，会建出完整的结构。
向量表（vector_store.py）和 L1 全文索引（text_index.py）仍由各自的模块在启动时建，不在这里。

注意：迁移在一个事务里执行，up() 里不要调用 conn.commit() 或 conn.executescript()（会把事务提前提交）。
"""
from db import add_column


def up(conn):
    # L0：对话原文
    conn.execute('''CREATE TABLE IF NOT EXISTS l0_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id TEXT NOT NULL,
        msg_idx INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        source TEXT DEFAULT 'live',
        client TEXT DEFAULT 'unknown',
        status TEXT DEFAULT 'active',
        extracted INTEGER DEFAULT 0
    )''')
    add_column(conn, 'l0_messages', 'group_id', 'TEXT')                       # 方案C：group 语义
    add_column(conn, 'l0_messages', 'version', 'INTEGER DEFAULT 0')
    add_column(conn, 'l0_messages', 'group_anchor', 'INTEGER')
    add_column(conn, 'l0_messages', 'extract_attempts', 'INTEGER DEFAULT 0')  # B7：提取失败重试计数
    add_column(conn, 'l0_messages', 'extract_error', 'TEXT')

    # L1：提取出的记忆
    conn.execute('''CREATE TABLE IF NOT EXISTS l1_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        quote TEXT,
        source_msg_id INTEGER,
        conv_id TEXT,
        client TEXT,
        event_type TEXT,
        tags TEXT,
        valence REAL,
        arousal REAL,
        is_core INTEGER DEFAULT 0,
        access_count INTEGER DEFAULT 0,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'active'
    )''')
    add_column(conn, 'l1_memories', 'anchor_json', 'TEXT')                # 和弦情绪锚点
    add_column(conn, 'l1_memories', 'display_count', 'INTEGER DEFAULT 0') # 浮现次数
    add_column(conn, 'l1_memories', 'last_shown_at', 'INTEGER')           # 上次浮现（毫秒时间戳）
    add_column(conn, 'l1_memories', 'visibility', 'REAL DEFAULT 1.0')     # 软隐藏
    add_column(conn, 'l1_memories', 'supersedes', 'INTEGER')              # 被修正的旧 feel
    add_column(conn, 'l1_memories', 'event_msg_id', 'INTEGER')            # 触发提取的 L0 消息
    add_column(conn, 'l1_memories', 'evidence_json', 'TEXT')              # 提取 v2：证据 L0 id 列表
    add_column(conn, 'l1_memories', 'evidence_status', 'TEXT')            # verified / verified_fuzzy / unverified / no_quote
    add_column(conn, 'l1_memories', 'author', 'TEXT')                     # user_said / ai_said / co_created / ai_felt

    # 供应商配置（api_key 加密存储；models 是 JSON 数组）
    conn.execute('''CREATE TABLE IF NOT EXISTS providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key_enc TEXT DEFAULT '',
        models TEXT DEFAULT '[]',
        role TEXT DEFAULT 'chat',
        api_format TEXT DEFAULT 'openai',
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    add_column(conn, 'providers', 'role', "TEXT DEFAULT 'chat'")
    add_column(conn, 'providers', 'api_format', "TEXT DEFAULT 'openai'")

    # 当前选中的供应商 + 模型（单行）
    conn.execute('''CREATE TABLE IF NOT EXISTS active_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        provider_id INTEGER,
        model TEXT
    )''')

    # 共同叙事、近期摘要
    conn.execute('''CREATE TABLE IF NOT EXISTS shared_narrative (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        version INTEGER DEFAULT 1,
        trigger_type TEXT,
        trigger_details TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'active'
    )''')
    add_column(conn, 'shared_narrative', 'last_l1_id', 'INTEGER DEFAULT 0')   # 增量水位线
    conn.execute('''CREATE TABLE IF NOT EXISTS recent_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        period_start DATETIME,
        period_end DATETIME,
        msg_count INTEGER,
        token_count INTEGER,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'active'
    )''')
    add_column(conn, 'recent_summary', 'last_l0_id', 'INTEGER DEFAULT 0')

    # 叙事配置（单行，必须存在，否则自动检查读不到配置会静默跳过）
    conn.execute('''CREATE TABLE IF NOT EXISTS narrative_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        auto_update_enabled INTEGER DEFAULT 1,
        check_threshold_turns INTEGER DEFAULT 50,
        check_threshold_l1 INTEGER DEFAULT 10,
        summary_max_turns INTEGER DEFAULT 100,
        summary_max_tokens INTEGER DEFAULT 50000
    )''')
    conn.execute("INSERT OR IGNORE INTO narrative_config (id) VALUES (1)")

    # 各模块独立的模型配置
    conn.execute('''CREATE TABLE IF NOT EXISTS model_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purpose TEXT UNIQUE NOT NULL,
        provider_id INTEGER,
        model_name TEXT,
        enabled INTEGER DEFAULT 1,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    # 叙事增量状态
    conn.execute('''CREATE TABLE IF NOT EXISTS narrative_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_processed_l1_id INTEGER DEFAULT 0,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    # 自定义交互偏好
    conn.execute('''CREATE TABLE IF NOT EXISTS custom_prompts (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        content TEXT DEFAULT '',
        enabled INTEGER DEFAULT 1,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    # 会话配置（'__default__' 为全局默认）
    conn.execute('''CREATE TABLE IF NOT EXISTS conv_settings (
        id TEXT PRIMARY KEY,
        window_mode TEXT DEFAULT 'all',
        window_n INTEGER DEFAULT 50,
        window_tokens INTEGER DEFAULT 8000,
        effort TEXT DEFAULT 'medium',
        model TEXT DEFAULT '',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute("INSERT OR IGNORE INTO conv_settings (id) VALUES ('__default__')")

    # 思考链（不进 L0 正文，按会话关联）
    conn.execute('''CREATE TABLE IF NOT EXISTS thinking_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id TEXT NOT NULL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        thinking TEXT NOT NULL,
        answer_ref TEXT
    )''')
    add_column(conn, 'thinking_records', 'full_reply', 'TEXT')

    # 用户显式删除过的 L0 消息黑名单（防止客户端全量上报时「复活」）
    conn.execute('''CREATE TABLE IF NOT EXISTS l0_deleted_marks (
        mark_key TEXT PRIMARY KEY,
        conv_id TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
