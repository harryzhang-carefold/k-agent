"""MCP 客户端：stdio（LSP 帧 JSON-RPC）+ Streamable HTTP（TASK-053 迭代4）。

stdio：拉起外部进程（asyncio.create_subprocess_exec），Content-Length 帧。
http：MCP Streamable HTTP 协议——JSON-RPC over POST 单端点，响应可为 JSON 或
SSE 流（text/event-stream），处理 Mcp-Session-Id 会话头。
每次会话：initialize 一次，之后可多次 tools/call，会话结束关闭。
"""
import asyncio
import json

import httpx
from core import stats
from core.errors import MCPError

_DEMO_SCRIPT = None  # 延迟填充（相对 src 的路径）


def set_demo_script(path: str):
    global _DEMO_SCRIPT
    _DEMO_SCRIPT = path


def env_of_row(row: dict) -> dict:
    """DB 行的 env 列 -> dict（TASK-022 / F1）。

    双后端兼容：sqlite 存 JSON 文本 / PG 存 JSONB（asyncpg 已反序列化为 dict）/
    旧行 NULL 均归一为 dict；JSON 文本损坏 -> {}（不抛错）。
    """
    env = row.get("env")
    if isinstance(env, (bytes, str)):
        try:
            return json.loads(env or "{}")
        except json.JSONDecodeError:
            return {}
    return env if isinstance(env, dict) else {}


def headers_of_row(row: dict) -> dict:
    """DB 行的 headers 列 -> dict（TASK-053 迭代4，同 env_of_row 语义）。"""
    return env_of_row({"env": row.get("headers")})


def transport_of_row(row: dict) -> str:
    """DB 行传输类型：'stdio'（默认/存量行）| 'http'。非法值按 stdio 归一。"""
    t = (row.get("transport") or "stdio").strip().lower()
    return t if t in ("stdio", "http") else "stdio"


def mcp_ctx_from_row(row: dict) -> dict:
    """mcp_servers 行 -> mcp_call 插件上下文（transport 分流所需全字段）。

    引擎侧只需把绑定行原样传入；plugins 按 transport 分流 stdio/http。
    """
    if not row:
        return {}
    return {
        "mcp_transport": transport_of_row(row),
        "mcp_command": row.get("command"),
        "mcp_args": json.loads(row.get("args") or "[]"),
        "mcp_env": env_of_row(row),
        "mcp_url": (row.get("url") or "").strip(),
        "mcp_headers": {str(k): str(v) for k, v in headers_of_row(row).items()},
    }


class MCPSession:
    def __init__(self, command: str, args: list[str], env: dict | None = None,
                 timeout: float = 10.0):
        self.command = command
        self.args = args
        self.env = env
        self.timeout = timeout
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self.initialized = False

    async def start(self):
        import os, sys
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except Exception as e:
            raise MCPError(f"MCP server 启动失败: {e}")
        await self.initialize()

    def _next_id(self):
        self._id += 1
        return self._id

    async def _send(self, method: str, params: dict, notify: bool = False):
        if self.proc is None or self.proc.returncode is not None:
            raise MCPError("MCP server 进程未运行")
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            msg["id"] = self._next_id()
        data = json.dumps(msg, ensure_ascii=False).encode()
        frame = f"Content-Length: {len(data)}\r\n\r\n".encode() + data
        self.proc.stdin.write(frame)
        await self.proc.stdin.drain()
        if notify:
            return None
        return await self._recv(self.timeout)

    async def _recv(self, timeout: float):
        try:
            async with asyncio.timeout(timeout):
                headers = {}
                while True:
                    line = await self.proc.stdout.readline()
                    if not line or line.strip() == b"":
                        break
                    k, _, v = line.decode().partition(":")
                    headers[k.strip().lower()] = v.strip()
                length = int(headers.get("content-length", "0"))
                body = await self.proc.stdout.readexactly(length)
                return json.loads(body.decode())
        except asyncio.TimeoutError:
            raise MCPError(f"MCP server 响应超时（{timeout}s）")
        except Exception as e:
            raise MCPError(f"MCP stdio 读取失败: {type(e).__name__}: {e}")

    async def initialize(self):
        r = await self._send("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "agp-platform", "version": "1.0.0"},
        })
        if "error" in r:
            raise MCPError(f"initialize 失败: {r['error']}")
        self.initialized = True
        await self._send("notifications/initialized", {}, notify=True)
        return r["result"]

    async def tools_list(self):
        r = await self._send("tools/list", {})
        if "error" in r:
            raise MCPError(f"tools/list 失败: {r['error']}")
        return r["result"]["tools"]

    async def tools_call(self, name: str, arguments: dict | None = None):
        r = await self._send("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in r:
            raise MCPError(f"tools/call 失败: {r['error']}")
        result = r["result"]
        if result.get("isError"):
            raise MCPError(f"工具 {name} 执行错误: {result}")
        text = ""
        for c in result.get("content", []):
            if c.get("type") == "text":
                text += c.get("text", "")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except Exception:
                self.proc.kill()


async def with_session(command, args, env=None, timeout=10.0, fn=None):
    """Open session, call fn(session) -> result, always close."""
    s = MCPSession(command, args, env, timeout)
    await s.start()
    try:
        return await fn(s) if fn else s
    finally:
        await s.close()


# ============================ Streamable HTTP 传输（TASK-053 迭代4） ============================
# MCP Streamable HTTP 协议（spec 2025-03-26）：
#   - JSON-RPC over POST 单端点；响应 Content-Type 可为 application/json（单帧）
#     或 text/event-stream（SSE 流，帧 {"jsonrpc":"2.0","id":N,"result":...}）。
#   - initialize 响应头 Mcp-Session-Id 标识会话，后续请求必须回带（server 要求时）。
#   - 客户端需接受两种 Content-Type（Accept: application/json, text/event-stream）。


def _sse_message(data: str) -> dict:
    """解析 SSE data 字段（可能多行拼接）为一个 JSON-RPC 消息。"""
    lines = [l for l in data.splitlines() if l.startswith("data:")]
    payload = "".join(l[5:].strip() for l in lines)
    return json.loads(payload)


class MCPSessionHTTP:
    """Streamable HTTP MCP 会话（initialize 一次，多次 tools/call）。

    超时：默认 10s（与 stdio 版对齐），connect 超时单独 5s。
    错误：端点不可达 / 4xx / 5xx / 非 JSON-RPC 响应 -> MCPError（含 URL + status
    + body 前 200 字符）。
    """

    def __init__(self, url: str, headers: dict | None = None,
                 timeout: float = 10.0, connect_timeout: float = 5.0):
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.session_id: str | None = None
        self.initialized = False
        self._client = None
        self._id = 0

    async def start(self):
        import httpx
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        # 用户自定义 headers（Authorization 等）优先于默认值
        base_headers.update(self.headers)
        self._client = httpx.AsyncClient(
            headers=base_headers,
            timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout))
        await self.initialize()

    def _req_headers(self):
        h = {}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _next_id(self):
        self._id += 1
        return self._id

    async def _post(self, method: str, params: dict, notify: bool = False) -> dict | None:
        if self._client is None:
            raise MCPError("MCP HTTP 会话未启动")
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            msg["id"] = self._next_id()
        try:
            resp = await self._client.post(self.url, json=msg,
                                           headers=self._req_headers())
        except httpx.ConnectError as e:
            raise MCPError(f"MCP HTTP 端点不可达 {self.url}: {type(e).__name__}: {e}")
        except httpx.ConnectTimeout:
            raise MCPError(f"MCP HTTP 连接超时（{self.connect_timeout}s）: {self.url}")
        except httpx.ReadTimeout:
            raise MCPError(f"MCP HTTP 响应超时（{self.timeout}s）: {self.url}")
        except Exception as e:
            raise MCPError(f"MCP HTTP 请求失败 {self.url}: {type(e).__name__}: {e}")
        # 会话头更新（server 可能更换 session id）
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        ct = resp.headers.get("content-type", "").lower()
        if resp.status_code >= 400:
            body = (resp.text or "")[:200]
            raise MCPError(f"MCP HTTP 错误 {resp.status_code} {self.url}: {body}")
        if notify and resp.status_code < 300:
            # 通知（notifications/*，无 id）：按 MCP Streamable HTTP 规范
            # （2025-03-26），通知的 2xx 响应 body 不是 JSON-RPC 响应，
            # 客户端应直接忽略、不解析。实践中大量 server 在 202/200 里放
            # 占位 JSON（如 {"received":true}、{}）——一律视为成功。
            return None
        if "text/event-stream" in ct:
            return await self._consume_sse(resp, expect_id=msg.get("id"))
        try:
            r = resp.json()
        except Exception:
            raise MCPError(
                f"MCP HTTP 非 JSON 响应（{resp.status_code}）{self.url}: "
                f"{(resp.text or '')[:200]}")
        if not isinstance(r, dict) or r.get("jsonrpc") != "2.0":
            raise MCPError(
                f"MCP HTTP 非 JSON-RPC 响应（{resp.status_code}）{self.url}: "
                f"{json.dumps(r, ensure_ascii=False)[:200]}")
        if notify or r.get("id") != msg.get("id"):
            # 通知：server 可能回 202 空体或单帧通知，无结果
            if "error" in r:
                raise MCPError(f"{method} 失败: {r['error']}")
            return None
        return r

    async def _consume_sse(self, resp, expect_id) -> dict:
        """消费 SSE 流，取出与 expect_id 对应的 JSON-RPC 结果帧。

        流内可能混有 notification / ping 帧——跳过；无匹配结果帧则报错。
        """
        try:
            async for line in resp.aiter_lines():
                line = (line or "").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    frame = json.loads(payload)
                except json.JSONDecodeError:
                    continue  # 非 JSON 帧（如心跳），忽略
                if isinstance(frame, dict) and frame.get("id") == expect_id:
                    return frame
        except Exception as e:
            raise MCPError(f"MCP HTTP SSE 流读取失败 {self.url}: {type(e).__name__}: {e}")
        raise MCPError(f"MCP HTTP SSE 流未返回结果帧 {self.url}（expect id={expect_id}）")

    async def _send(self, method: str, params: dict, notify: bool = False):
        r = await self._post(method, params, notify=notify)
        if r is not None and "error" in r:
            raise MCPError(f"{method} 失败: {r['error']}")
        return r

    async def initialize(self):
        r = await self._post("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "agp-platform", "version": "1.0.0"},
        })
        if not r or "result" not in r:
            raise MCPError(f"initialize 未返回结果: {self.url}")
        self.initialized = True
        await self._post("notifications/initialized", {}, notify=True)
        return r["result"]

    async def tools_list(self):
        r = await self._post("tools/list", {})
        if not r or "result" not in r:
            raise MCPError(f"tools/list 未返回结果: {self.url}")
        return r["result"]["tools"]

    async def tools_call(self, name: str, arguments: dict | None = None):
        r = await self._post("tools/call", {"name": name, "arguments": arguments or {}})
        if not r or "result" not in r:
            raise MCPError(f"tools/call 未返回结果: {self.url}")
        result = r["result"]
        if result.get("isError"):
            raise MCPError(f"工具 {name} 执行错误: {result}")
        text = ""
        for c in result.get("content", []):
            if c.get("type") == "text":
                text += c.get("text", "")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    async def close(self):
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None


async def with_http_session(url, headers=None, timeout=10.0, connect_timeout=5.0, fn=None):
    """Open Streamable HTTP session, call fn(session) -> result, always close.

    与 with_session 同语义：initialize 一次，fn 内可多次 tools_call，退出必关闭。
    """
    s = MCPSessionHTTP(url, headers, timeout, connect_timeout)
    await s.start()
    try:
        return await fn(s) if fn else s
    finally:
        await s.close()
