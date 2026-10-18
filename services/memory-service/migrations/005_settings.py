"""005 运行时可调参数：app_settings（键值表）。

Dashboard 上能改的参数（比如召回门槛）存在这里；没改过的项用 .env / 代码里的默认值（见 app/settings.py）。
键名按组加前缀，比如 recall.t_high；值统一存成 JSON 文本。「恢复默认」就是删掉这一行。
"""


def up(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,         -- 组名.参数名，例如 recall.t_high
        value TEXT NOT NULL,          -- JSON
        updated_at TEXT NOT NULL
    )''')
