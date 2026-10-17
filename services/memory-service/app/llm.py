"""大模型调用（从 main.py 原样搬来）

- get_model_config(purpose)：按 Dashboard 里的「模块模型配置」（model_configs → providers）取出地址、密钥、模型名；
- call_llm(config, prompt, max_tokens)：通用调用（claude 系列走 Anthropic /messages，其余走 /chat/completions）；
- call_llm_for_narrative(config, prompt_parts)：叙事专用（system + user 两段，输出被截断时自动续写）。

以后提取（extract_l1.py 里的 call_deepseek / resolve_purpose）和嵌入也会并到这里，统一重试和用量记录。
"""
import logging

import httpx

from app.config import get_conn
from app.crypto import decrypt_secret

logger = logging.getLogger(__name__)


async def get_model_config(purpose: str):
    """获取指定模块的模型配置"""
    conn = get_conn()
    c = conn.cursor()

    # 先查 model_configs 表
    config = c.execute(
        "SELECT provider_id, model_name FROM model_configs WHERE purpose=? AND enabled=1",
        (purpose,)
    ).fetchone()

    if not config:
        conn.close()
        return {"configured": False}

    # 查供应商信息
    provider = c.execute(
        "SELECT name, base_url, api_key_enc, api_format FROM providers WHERE id=?",
        (config[0],)
    ).fetchone()
    conn.close()
    if not provider:
        return {"configured": False}
    return {
        "configured": True,
        "provider_name": provider[0],
        "base_url": provider[1],
        "api_key": decrypt_secret(provider[2]),
        "model": config[1],
        "api_format": provider[3] or 'openai'
    }


async def call_llm_for_narrative(config: dict, prompt_parts: dict) -> str:
    """调用 LLM 生成 Narrative（system+user 两段式），支持多轮续写（输出上限截断时自动继续）"""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            is_claude = (config['model'].startswith("claude")
                         or "anthropic/" in config['model']
                         or "claude-" in config['model']
                         or "claude." in config['model'])
            if is_claude:
                system_text = prompt_parts["system"]
                user_text = prompt_parts["user"]
                full = ""
                for _round in range(6):
                    response = await client.post(
                        f"{config['base_url']}/messages",
                        headers={
                            "x-api-key": config['api_key'],
                            "Authorization": f"Bearer {config['api_key']}",
                            "anthropic-version": "2023-06-01",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": config['model'],
                            "system": system_text,
                            "messages": [{"role": "user", "content": user_text}],
                            "max_tokens": 8000
                        }
                    )
                    response.raise_for_status()
                    data = response.json()
                    content = data.get("content", [])
                    part_text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                    full += part_text
                    if data.get("stop_reason") not in ("max_tokens", "long_context", "length"):
                        break
                    # 到输出上限 → 续写
                    user_text = "请接着上一段继续写完整的叙事（不要重复，直接续写，保持语气与结构一致）："
                return full
            else:
                # 其他模型走 /chat/completions（含多轮续写）
                full2 = ""
                user_text2 = prompt_parts["user"]
                for _round in range(6):
                    response = await client.post(
                        f"{config['base_url']}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {config['api_key']}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": config['model'],
                            "messages": [
                                {"role": "system", "content": prompt_parts["system"]},
                                {"role": "user", "content": user_text2}
                            ],
                            "max_tokens": 8000
                        }
                    )
                    response.raise_for_status()
                    data2 = response.json()
                    msg = data2["choices"][0]["message"]
                    full2 += msg.get("content") or ""
                    fr = data2["choices"][0].get("finish_reason")
                    if fr != "length" and str(fr) != "max_tokens":
                        break
                    user_text2 = "请接着上一段继续写完整的叙事（不要重复，直接续写，保持语气与结构一致）："
                return full2
    except Exception as e:
        logger.error(f"LLM call error in narrative generation: {e}")
        return None


async def call_llm(config: dict, prompt: str, max_tokens: int) -> str:
    """通用 LLM 调用。claude 系列走 Anthropic 原生 /messages（与网关约定一致，
    部分中转的令牌不开放 OpenAI 兼容端点），其余走 /chat/completions。"""
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            if config['model'].startswith("claude"):
                response = await client.post(
                    f"{config['base_url']}/messages",
                    headers={
                        "x-api-key": config['api_key'],
                        "Authorization": f"Bearer {config['api_key']}",
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": config['model'],
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens
                    }
                )
                response.raise_for_status()
                return "".join(
                    block.get("text", "") for block in response.json()["content"]
                    if block.get("type") == "text"
                )
            response = await client.post(
                f"{config['base_url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": config['model'],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens
                }
            )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            content = choice["message"].get("content")
            if not content and choice.get("finish_reason") == "length":
                print(f"LLM call error: 输出被 max_tokens={max_tokens} 截断，正文为空（推理模型的思考也算在里面）")
            return content
    except httpx.HTTPStatusError as e:
        print(f"LLM call error: {e} body={e.response.text[:300]}")
        return None
    except Exception as e:
        print(f"LLM call error: {e}")
        return None
