"""OpenAI-compatible LLM provider abstraction (PRD module 1).

- chat(messages, **params) -> str
- chat_stream(messages, **params) -> AsyncIterator[str]  (token chunks)
- retry w/ exponential backoff; on final failure raise LLMError (caller degrades)
- api_key only from .env (DECISION-004)
"""
import asyncio
import json
import httpx
from core.config import S
from core.errors import LLMError
from core import stats


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


async def chat(messages, **params) -> str:
    """Non-streaming chat. Raises LLMError after retries (controlled degradation)."""
    url = S.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = _payload(messages, **params)
    last_err = None
    for attempt in range(S.LLM_RETRIES):
        try:
            r = await _post(url, payload, S.LLM_TIMEOUT, S.LLM_API_KEY)
            if r.status_code == 401:
                stats.bump("llm_errors")
                raise LLMError(f"LLM 401 认证失败: {r.text[:200]}")
            if r.status_code >= 500 or r.status_code == 429:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            data = r.json()
            stats.bump("llm_calls")
            return data["choices"][0]["message"]["content"] or ""
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


async def chat_stream(messages, **params):
    """Streaming chat (SSE). Yields token text chunks. Raises LLMError on failure."""
    url = S.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = _payload(messages, stream=True, **params)
    headers = {"Content-Type": "application/json"}
    if S.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {S.LLM_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=S.LLM_TIMEOUT) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code == 401:
                    stats.bump("llm_errors")
                    raise LLMError("LLM 401 认证失败")
                if r.status_code >= 400:
                    body = (await r.aread()).decode()[:200]
                    stats.bump("llm_errors")
                    raise LLMError(f"LLM HTTP {r.status_code}: {body}")
                stats.bump("llm_calls")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0].get("delta", {})
                        tok = delta.get("content")
                        if tok:
                            yield tok
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
        stats.bump("llm_errors")
        raise LLMError(f"LLM 流式不可用: {type(e).__name__}: {e}")


async def health() -> dict:
    try:
        out = await chat([{"role": "user", "content": "ping"}], max_tokens=5, temperature=0)
        return {"ok": True, "model": S.LLM_MODEL, "sample": out[:40]}
    except LLMError as e:
        return {"ok": False, "model": S.LLM_MODEL, "error": str(e)[:200]}
