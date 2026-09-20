"""OpenAI-compatible LLM provider abstraction (PRD module 1).

- chat(messages, **params) -> (content, usage_dict)   # TASK-057: 采集 usage
- chat_stream(messages, **params) -> (content_str, usage_dict)  # 聚合 chunk + 末尾 usage
- retry w/ exponential backoff; on final failure raise LLMError (caller degrades)
- api_key only from .env (DECISION-004)

TASK-057 / 迭代5: usage 采集（DECISION-027 决策3）
- chat() 返回 (content, usage_dict)；chat_stream() 返回 (content_str, usage_dict)。
  usage_dict 形如 {"prompt_tokens": N, "completion_tokens": M, "total_tokens": T}；
  端点无 usage（或字段缺失）时为 {}（**不 raise**，调用方按 {} 处理不报错）。
- 流式请求带 stream_options={"include_usage": true}（vLLM 已确认支持；
  端点不支持时忽略该参数，末尾无 usage chunk → usage 记 0）。
- 现有调用点（engine._tool_loop、longtext、tests、health()）同步适配。
"""
import asyncio
import json
import httpx
from core.config import S
from core.errors import LLMError
from core import stats


def _usage_from_response(data: dict) -> dict:
    """从 OpenAI 响应体提取 usage → {prompt_tokens, completion_tokens, total_tokens}。
    缺失/非数字 → 0；无 usage 字段 → {}（不 raise）。"""
    u = data.get("usage")
    if not isinstance(u, dict):
        return {}
    def _int(v):
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0
    return {
        "prompt_tokens": _int(u.get("prompt_tokens")),
        "completion_tokens": _int(u.get("completion_tokens")),
        "total_tokens": _int(u.get("total_tokens")) or
            (_int(u.get("prompt_tokens")) + _int(u.get("completion_tokens"))),
    }


async def _post(url: str, payload: dict, timeout: float, key: str) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=payload, headers=headers)


def _payload(messages, stream=False, **params):
    p = {"model": params.pop("model", S.LLM_MODEL),
         "messages": messages,
         "stream": stream}
    for k in ("temperature", "max_tokens", "top_p"):
        if k in params:
            p[k] = params.pop(k)
    return p


# TASK-029 / 需求1: 按 endpoint 覆盖的可选参数。chat()/chat_stream() 额外接受
# base_url / api_key / timeout（None = 读 S.* 默认值，向后兼容——不绑 endpoint
# 的 agent 行为不变，RISK-018）。retries 保持 S.LLM_RETRIES（端点级重试数
# 目前不单独暴露，避免过度设计）。
def _endpoint_override(params: dict) -> tuple[str | None, str | None, float | None]:
    """从 **params 提取并移除 endpoint 覆盖，返回 (base_url, api_key, timeout)。"""
    base = params.pop("base_url", None)
    key = params.pop("api_key", None)
    timeout = params.pop("timeout", None)
    return base, key, timeout


async def chat(messages, **params) -> tuple[str, dict]:
    """Non-streaming chat. 返回 (content, usage_dict)（TASK-057）。
    无 usage 时 usage_dict = {}（不 raise）。Raises LLMError after retries (degradation)."""
    base, key, timeout = _endpoint_override(params)
    url = (base or S.LLM_BASE_URL).rstrip("/") + "/chat/completions"
    key = key if key is not None else S.LLM_API_KEY
    timeout = timeout if timeout is not None else S.LLM_TIMEOUT
    payload = _payload(messages, **params)
    last_err = None
    for attempt in range(S.LLM_RETRIES):
        try:
            r = await _post(url, payload, timeout, key)
            if r.status_code == 401:
                stats.bump("llm_errors")
                raise LLMError(f"LLM 401 认证失败: {r.text[:200]}")
            if r.status_code >= 500 or r.status_code == 429:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            data = r.json()
            stats.bump("llm_calls")
            content = data["choices"][0]["message"]["content"] or ""
            return content, _usage_from_response(data)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
            last_err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(min(2 ** attempt, 8))
        except LLMError:
            raise
        except Exception as e:  # malformed response etc.
            last_err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1)
    stats.bump("llm_errors")
    raise LLMError(f"LLM 端点不可用（重试 {S.LLM_RETRIES} 次）: {last_err}")


async def chat_stream(messages, **params) -> tuple[str, dict]:
    """Streaming chat (SSE). 返回 (content_str, usage_dict)（TASK-057）。

    聚合所有 chunk 的内容 + 解析末尾 usage chunk（vLLM: 单独一条 choices=[] + usage）。
    无 usage（端点不支持 include_usage）→ usage_dict = {}（不 raise）。
    流式请求带 stream_options={"include_usage": true}；端点不支持（返回 4xx 请求错误）
    时自动去掉 stream_options 重试一次（usage 记 0，不崩溃）。
    Raises LLMError on 连接/超时/401/5xx 等真实失败。
    """
    base, key, timeout = _endpoint_override(params)
    url = (base or S.LLM_BASE_URL).rstrip("/") + "/chat/completions"
    key = key if key is not None else S.LLM_API_KEY
    timeout = timeout if timeout is not None else S.LLM_TIMEOUT
    payload = _payload(messages, stream=True, **params)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    async def _attempt(with_usage: bool) -> tuple[int, list[str], dict, str | None]:
        """返回 (status_code, parts, usage, error_text)。status_code=0 表示连接类失败已抛。"""
        p = dict(payload)
        if with_usage:
            p["stream_options"] = {"include_usage": True}
        parts: list[str] = []
        usage: dict = {}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=p, headers=headers) as r:
                    if r.status_code == 401:
                        stats.bump("llm_errors")
                        raise LLMError("LLM 401 认证失败")
                    if r.status_code >= 400:
                        body = (await r.aread()).decode()[:200]
                        if r.status_code >= 500:
                            stats.bump("llm_errors")
                            raise LLMError(f"LLM HTTP {r.status_code}: {body}")
                        # 4xx（非 5xx）：可能是端点不支持 stream_options → 返回状态码供调用方回退
                        return r.status_code, parts, usage, body
                    stats.bump("llm_calls")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        # 末尾 usage chunk（vLLM: 单独一条 choices=[] + usage）
                        if isinstance(obj.get("usage"), dict):
                            usage = _usage_from_response(obj)
                        try:
                            delta = obj["choices"][0].get("delta", {})
                            tok = delta.get("content")
                            if tok:
                                parts.append(tok)
                        except (KeyError, IndexError, TypeError):
                            continue
                    return 200, parts, usage, None
        except LLMError:
            raise
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
            stats.bump("llm_errors")
            raise LLMError(f"LLM 流式不可用: {type(e).__name__}: {e}")

    try:
        status, parts, usage, err = await _attempt(with_usage=True)
        if status != 200:
            # 端点不支持 stream_options（4xx 请求错误）→ 去掉该参数重试一次
            status, parts, usage, err = await _attempt(with_usage=False)
            if status != 200:
                stats.bump("llm_errors")
                raise LLMError(f"LLM HTTP {status}: {err}")
        return "".join(parts), usage
    except LLMError:
        raise


async def health() -> dict:
    try:
        out, _usage = await chat([{"role": "user", "content": "ping"}], max_tokens=5, temperature=0)
        return {"ok": True, "model": S.LLM_MODEL, "sample": out[:40]}
    except LLMError as e:
        return {"ok": False, "model": S.LLM_MODEL, "error": str(e)[:200]}
