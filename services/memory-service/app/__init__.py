"""记忆服务的模块（方案 4.3 节）。

按「绞杀者模式」一步步从 main.py 里拆出来：新模块一个个长出来，main.py 里的旧接口保留为转调新模块的薄壳，
任何时候系统都能用。目前已经拆出：

- config.py          所有环境变量、路径、日志配置的唯一入口；get_conn()
- crypto.py          API Key 加解密
- llm.py             大模型调用、按模块取模型配置
- pipeline/summary.py    近期摘要
- pipeline/narrative.py  共同经历叙事
- pipeline/profile.py    画像重建

还在 main.py 里、之后再拆的：检索和浮现（→ recall/）、L0 / L1 的读写（→ store/）、各类接口（→ api/）、提示词（→ prompts/）。
同目录下的 db.py、timeutil.py、jobs.py、vector_store.py、text_index.py、extract_l1.py、worker.py 暂时保持原位，
以后再挪进来。
"""
