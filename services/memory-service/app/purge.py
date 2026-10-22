"""清空所有数据（Dashboard「设置」→「危险操作」→「清空所有数据」，接口 POST /admin/purge_all）。

以前是一张写死的表名单，后来加的每日片段、叙事卷、共同语汇、未完的事、关系状态、章节都没加进去，清空时漏掉了。
现在反过来：除了 KEEP 里的（模型和供应商配置、各项设置、召回对比），别的表全部清空，以后加了新的层也会一起清掉。
- 召回对比留着：清空之前先把每一轮用到的记忆内容定格（app/recall/traces.py freeze()），以后看的还是当时的记忆。
- 自增编号不重置：新导入的原文、新提取的记忆接着往后编号，留下来的召回对比和标注不会指到新记忆上。
- 关键词索引（l1_fts*）由 text_index 重建，向量由 vector_store 清空（都在 main.py 里做）。
- 正在跑的后台任务清不掉：它做完以后可能还会写进一点结果，所以最好等任务都停了再清空。
"""

KEEP = frozenset({
    "schema_migrations", "sqlite_sequence",
    "providers", "model_configs", "active_config",          # 模型和供应商配置
    "app_settings", "narrative_config", "topic_policies",   # 各项设置、话题边界
    "vector_meta", "index_meta", "worker_state",            # 向量、关键词索引、任务队列自己的状态
    "recall_traces", "recall_labels", "injections",         # 召回对比、黄金集标注
})


def tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def to_clear(conn):
    return [t for t in tables(conn) if t not in KEEP and not t.startswith(("l1_fts", "sqlite_"))]


def purge(conn):
    """定格召回对比 → 清空 KEEP 以外的表。事务由调用方管（要在同一个事务里）。
    返回 {"cleared": {表: 删了几行}, "frozen": 定格了几轮召回对比}"""
    from app.recall import traces
    frozen = traces.freeze(conn)
    cleared = {t: conn.execute(f'DELETE FROM "{t}"').rowcount for t in to_clear(conn)}
    return {"cleared": cleared, "frozen": frozen}
