"""016 召回对比定格：recall_traces 加 frozen。

召回对比的每一轮只记了候选记忆的编号，看的时候再按编号去记忆表里取内容。
清空记忆库（Dashboard「清空所有数据」）时召回对比留着，但记忆没了、编号以后还可能指到别的记忆上，
所以清空之前先把每一轮用到的记忆内容存一份在这里（JSON：{"at": 定格时间, "items": {编号: {content, ts, event_type, author}}}），
以后看这一轮就用这份，不再去记忆表里取。见 app/recall/traces.py freeze()。
"""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recall_traces)")}
    if "frozen" not in cols:
        conn.execute("ALTER TABLE recall_traces ADD COLUMN frozen TEXT")
