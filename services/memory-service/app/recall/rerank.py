"""精排（方案 8.3 节）：把「这句话 + 候选记忆」成对交给精排模型打分（0~1），比向量相似度准，适合做门槛。

用阿里云百炼（DashScope）的文本排序接口，和嵌入用同一个 ALIBABA_API_KEY。
- 模型：.env 里的 RERANK_MODEL，默认 qwen3.7-text-rerank（也可以用 qwen3-rerank、gte-rerank-v2）；
- 超过 RERANK_TIMEOUT 秒（默认 1.5）或者出错时返回 None，由调用方退回到向量相似度门槛；
- RERANK_ENABLED=0 可以关掉。
分数只在同一次请求里互相比较才有意义；门槛（0.75 / 0.5）是起点，要用黄金集校准。
"""
import os
import time

from app import config  # noqa: F401  先加载 .env
from app.recall.channels import _client

ALIBABA_API_KEY = os.getenv("ALIBABA_API_KEY")
RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
RERANK_MODEL = os.getenv("RERANK_MODEL", "qwen3.7-text-rerank")
RERANK_TIMEOUT = float(os.getenv("RERANK_TIMEOUT", 1.5))
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "1") != "0"


async def rerank(query, docs, timeout=None):
    """返回 (scores, info)。scores 和 docs 一一对应（0~1）；失败时 scores 为 None。"""
    info = {"model": RERANK_MODEL, "ok": False, "ms": 0}
    if not RERANK_ENABLED or not ALIBABA_API_KEY:
        info["error"] = "未启用"
        return None, info
    if not docs:
        info["ok"] = True
        return [], info
    t0 = time.monotonic()
    try:
        r = await _client().post(RERANK_URL, headers={"Authorization": f"Bearer {ALIBABA_API_KEY}"},
                                 json={"model": RERANK_MODEL, "input": {"query": query, "documents": docs},
                                       "parameters": {"return_documents": False}},
                                 timeout=timeout or RERANK_TIMEOUT)
        info["ms"] = int((time.monotonic() - t0) * 1000)
        d = r.json()
        if r.status_code != 200:
            info["error"] = f"HTTP {r.status_code} {d.get('code', '')} {str(d.get('message', ''))[:120]}"
            return None, info
        scores = [None] * len(docs)
        for x in (d.get("output") or {}).get("results") or []:
            i = x.get("index")
            if isinstance(i, int) and 0 <= i < len(docs):
                scores[i] = round(float(x.get("relevance_score") or 0.0), 4)
        scores = [s if s is not None else 0.0 for s in scores]
        info["ok"] = True
        info["tokens"] = (d.get("usage") or {}).get("total_tokens")
        return scores, info
    except Exception as e:
        info["ms"] = int((time.monotonic() - t0) * 1000)
        info["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return None, info
