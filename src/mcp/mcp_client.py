"""MCP stdio JSON-RPC 客户端 (PRD module 3): initialize -> tools/list -> tools/call.

拉起 stdio 外部进程（asyncio.create_subprocess_exec），LSP 风格 Content-Length 帧。
每次会话：initialize 一次，之后可多次 tools/call，会话结束 kill 进程。
外部真实 MCP 进程 = 适配位（mcp_servers 表配置 command/args 即可接入）。
"""
import asyncio
import json
from core import stats
from core.errors import MCPError

_DEMO_SCRIPT = None  # 延迟填充（相对 src 的路径）


def set_demo_script(path: str):
    global _DEMO_SCRIPT
    _DEMO_SCRIPT = path


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
