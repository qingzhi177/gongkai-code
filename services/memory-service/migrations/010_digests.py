"""010 日摘、周摘、近七天摘要（方案 6.11 节，Phase 4 第三步）：digests。

- kind：day（日摘，period_start 是那天）/ week（周摘，period_start 是周一）/ recent（近七天摘要，period_start 固定是 recent，
  period_end 写窗口「第一天~今天」）。
- body：去掉出处的正文（日摘一行一句；周摘、近七天是一段）；body_with_refs：带 [#n] 出处的原稿，[#n] 是 refs 里的第 n 个。
- refs：日摘 → 片段 id；周摘、近七天 → 日摘 id。
- source_key：输入的指纹（日摘是那天的片段 id；周摘、近七天是日摘 id），变了才重写。
- status：active（在用）/ superseded（被新版本替代）/ rejected（写了但没通过校验，没发布；输入不变就不再重试）。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS digests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        body TEXT NOT NULL,
        body_with_refs TEXT NOT NULL,
        refs TEXT,
        source_key TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        version INTEGER NOT NULL DEFAULT 1,
        issues TEXT,
        model TEXT, prompt_version TEXT,
        created_at TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_digests ON digests (kind, period_start, status)")
