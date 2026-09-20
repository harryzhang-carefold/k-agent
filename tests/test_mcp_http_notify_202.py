"""BUG-011 回归单测：MCPSessionHTTP 通知响应的 2xx 非空 body 兼容。

BUG-011（TASK-056 交付后真实 server 暴露）：MCP Streamable HTTP 标准握手三步——
  1. initialize            → 200 + JSON-RPC 结果
  2. notifications/initialized（通知，无 id）→ 真实 server 回 **202 + body {"received":true}**
  3. tools/list            → 200 + 工具列表
原 `MCPSessionHTTP._post()` 仅跳过**空 body** 的 202（`if notify and not content.strip()`），
非空占位 body 落进 `resp.json()` → 无 `jsonrpc` 字段 → 抛
`MCP HTTP 非 JSON-RPC 响应（202）`，握手在第 2 步中断。

按 MCP Streamable HTTP 规范（2025-03-26）：通知的 2xx 响应 body **不是** JSON-RPC
响应，客户端应直接忽略、不解析。修复：notify 请求收到 status < 300 一律 return None。

本文件单测**纯函数/客户端**（httpx.MockTransport 假 server，不连真实 server、
不依赖 8099 服务、不碰 stdio 进程，仅最后一项 stdio 用本机 python3 起 demo）：
  - AC-4 核心：mock server 对 notifications/initialized 分别回
    ① 202 空体  ② 202 {"received":true}  ③ 202 {}  ④ 200 任意 body
    → 四种均完成握手且后续 tools/list 正常；
  - 非通知路径回归：404 / 500 报错格式不变（MCP HTTP 错误 <status> <url>: <body 前 200>）；
    通知收到 4xx 同样按错误处理（修复未放宽错误路径）；
  - SSE 结果帧消费不变：tools/list 走 text/event-stream 仍取到匹配 id 的结果帧；
  - id 匹配回归：非通知请求收到别 id 的 JSON-RPC 帧仍报错（id 语义未被放宽）；
  - stdio 路径零影响：MCPSession（stdio 版）initialize+tools_list 用本机 demo 跑通。

与既有 pytest 一致：src 加入 sys.path，纯单元测试。
"""
import json
import os
import subprocess
import sys

import httpx
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from core.errors import MCPError  # noqa: E402
from mcp import mcp_client  # noqa: E402
from mcp.mcp_client import MCPSession, MCPSessionHTTP, with_session  # noqa: E402

URL = "http://mock-mcp.test/mcp/"
DEMO_SCRIPT = os.path.join(SRC, "mcp", "mcp_server_demo.py")


# ---------------------------------------------------------------------------
# Mock server：按请求 method 路由，模拟真实 Streamable HTTP server 的三种响应
# ---------------------------------------------------------------------------

def make_handler(notify_status: int, notify_body: bytes,
                 tools_status: int = 200, tools_sse: bool = False,
                 extra_headers: dict | None = None):
    """返回 MockTransport 的 handler(request) -> httpx.Response。

    - initialize（带 id）：200 + JSON-RPC 结果（可带 Mcp-Session-Id 头）
    - notifications/initialized（无 id）：notify_status + notify_body（BUG-011 核心）
    - tools/list（带 id）：tools_status；tools_sse=True 时走 SSE 结果帧
    - tools/call（带 id）：200 + 简单结果
    """

    def handler(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content.decode())
        method = msg.get("method")
        req_id = msg.get("id")

        if method == "initialize":
            hdrs = dict(extra_headers or {})
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "serverInfo": {"name": "mock-mcp", "version": "0.0.1"},
                },
            }, headers=hdrs)

        if method == "notifications/initialized":
            return httpx.Response(notify_status, content=notify_body,
                                  headers={"Content-Type": "application/json"})

        if method == "tools/list":
            if tools_sse:
                frame = {"jsonrpc": "2.0", "id": req_id, "result": {
                    "tools": [
                        {"name": "login", "description": "d1", "inputSchema": {}},
                        {"name": "current_user", "description": "d2", "inputSchema": {}},
                    ]}}
                # 混入一个 notification 帧 + 一个心跳（非 JSON），验证消费器跳过
                sse = (
                    "event: message\n"
                    f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
                    "data: not-json-heartbeat\n\n"
                    "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{}}\n\n"
                )
                return httpx.Response(tools_status, content=sse.encode(),
                                      headers={"Content-Type": "text/event-stream"})
            return httpx.Response(tools_status, json={
                "jsonrpc": "2.0", "id": req_id,
                "result": {"tools": [
                    {"name": "login", "description": "d1", "inputSchema": {}},
                    {"name": "current_user", "description": "d2", "inputSchema": {}},
                ]},
            }, headers={"Content-Type": "application/json"})

        if method == "tools/call":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}],
                           "isError": False},
            }, headers={"Content-Type": "application/json"})

        # 其它方法（如别 id 回归用例）
        return httpx.Response(404, json={"error": "unknown method"},
                              headers={"Content-Type": "application/json"})

    return handler


def make_session(handler) -> MCPSessionHTTP:
    """构造 MCPSessionHTTP 并注入 MockTransport 客户端（跳过 start() 的网络部分）。"""
    s = MCPSessionHTTP(URL)
    s._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        timeout=httpx.Timeout(5.0),
    )
    return s


async def _handshake_and_list(handler):
    """initialize（含 notifications/initialized）→ tools_list。返回工具名列表。"""
    s = make_session(handler)
    try:
        await s.initialize()
        assert s.initialized is True
        tools = await s.tools_list()
        return [t["name"] for t in tools]
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# AC-4 核心：通知 2xx 四种 body 均完成握手 + tools/list 正常
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("notify_status,notify_body,case", [
    (202, b"", "202 空体"),
    (202, b'{"received": true}', "202 {'received':true}（真实 server 形态）"),
    (202, b"{}", "202 {}"),
    (200, b'{"arbitrary": "body", "x": 1}', "200 任意 body"),
])
def test_notify_2xx_handshake(notify_status, notify_body, case):
    handler = make_handler(notify_status=notify_status, notify_body=notify_body)
    import asyncio
    names = asyncio.run(_handshake_and_list(handler))
    assert names == ["login", "current_user"], f"{case}: tools/list 异常: {names}"


def test_notify_2xx_with_session_header():
    """initialize 响应带 Mcp-Session-Id 头时，握手仍通（回归：头处理在修复前，不受影响）。"""
    import asyncio
    handler = make_handler(notify_status=202,
                           notify_body=b'{"received": true}',
                           extra_headers={"Mcp-Session-Id": "sess-123"})
    names = asyncio.run(_handshake_and_list(handler))
    assert names == ["login", "current_user"]


# ---------------------------------------------------------------------------
# 非通知路径回归：4xx/5xx 报错格式不变
# ---------------------------------------------------------------------------

def test_non_notify_404_error_format():
    """tools/list 404 → MCPError 格式保持 'MCP HTTP 错误 404 <url>: <body 前200>'。"""
    import asyncio

    async def go():
        handler = make_handler(notify_status=202, notify_body=b"{}",
                               tools_status=404)
        s = make_session(handler)
        try:
            await s.initialize()
            with pytest.raises(MCPError) as ei:
                await s.tools_list()
        finally:
            await s.close()
        msg = str(ei.value)
        assert msg.startswith("MCP HTTP 错误 404"), msg
        assert URL in msg, msg

    asyncio.run(go())


def test_non_notify_500_error_format():
    import asyncio

    async def go():
        handler = make_handler(notify_status=202, notify_body=b"{}",
                               tools_status=500)
        s = make_session(handler)
        try:
            await s.initialize()
            with pytest.raises(MCPError) as ei:
                await s.tools_list()
        finally:
            await s.close()
        msg = str(ei.value)
        assert msg.startswith("MCP HTTP 错误 500"), msg
        assert URL in msg, msg

    asyncio.run(go())


def test_notify_4xx_still_errors():
    """通知收到 4xx 仍按错误处理（修复只放宽 2xx，未放宽错误路径）。"""
    import asyncio

    async def go():
        # 通知回 404 → initialize 应抛错（404 先于 notify<300 判断）
        handler = make_handler(notify_status=404, notify_body=b"not found")
        s = make_session(handler)
        try:
            with pytest.raises(MCPError) as ei:
                await s.initialize()
        finally:
            await s.close()
        msg = str(ei.value)
        assert "MCP HTTP 错误 404" in msg, msg

    asyncio.run(go())


def test_id_mismatch_still_errors():
    """非通知请求收到别 id 的 JSON-RPC 结果帧仍报错（id 匹配语义未被放宽）。"""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content.decode())
        if msg.get("method") == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "serverInfo": {"name": "m", "version": "0"}}})
        if msg.get("method") == "notifications/initialized":
            return httpx.Response(202, content=b"{}",
                                  headers={"Content-Type": "application/json"})
        if msg.get("method") == "tools/list":
            # 故意回错 id
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": 99999,
                "result": {"tools": []}},
                headers={"Content-Type": "application/json"})
        return httpx.Response(404, json={})

    async def go():
        s = make_session(handler)
        try:
            await s.initialize()
            with pytest.raises(MCPError):
                await s.tools_list()
        finally:
            await s.close()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# SSE 结果帧消费回归（非通知路径不变）
# ---------------------------------------------------------------------------

def test_sse_result_frame_consumed():
    """tools/list 走 text/event-stream：仍取到匹配 id 的结果帧，跳过通知/心跳帧。"""
    import asyncio

    async def go():
        handler = make_handler(notify_status=202,
                               notify_body=b'{"received": true}',
                               tools_sse=True)
        s = make_session(handler)
        try:
            await s.initialize()
            tools = await s.tools_list()
        finally:
            await s.close()
        assert [t["name"] for t in tools] == ["login", "current_user"]

    asyncio.run(go())


def test_sse_no_result_frame_errors():
    """SSE 流无匹配 id 结果帧 → 报错（格式保持 SSE 流未返回结果帧）。"""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content.decode())
        if msg.get("method") == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "serverInfo": {"name": "m", "version": "0"}}})
        if msg.get("method") == "notifications/initialized":
            return httpx.Response(202, content=b"{}",
                                  headers={"Content-Type": "application/json"})
        if msg.get("method") == "tools/list":
            return httpx.Response(200, content=b"event: ping\ndata: {}\n\n",
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(404, json={})

    async def go():
        s = make_session(handler)
        try:
            await s.initialize()
            with pytest.raises(MCPError) as ei:
                await s.tools_list()
        finally:
            await s.close()
        assert "SSE 流未返回结果帧" in str(ei.value), str(ei.value)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# stdio 路径零影响：MCPSession（stdio 版）用本机 demo server 跑通
# ---------------------------------------------------------------------------

def test_stdio_path_zero_impact():
    """stdio 版（MCPSession）未受 HTTP 修复影响：initialize + tools_list 用本机 demo 跑通。"""
    import asyncio

    async def go():
        tools = await with_session(
            sys.executable, [DEMO_SCRIPT],
            fn=lambda s: s.tools_list(),
        )
        names = sorted(t["name"] for t in tools)
        # demo server 暴露 get_time + get_patient_demo（见 mcp_server_demo.py）
        assert "get_time" in names, f"stdio tools 异常: {names}"
        return names

    names = asyncio.run(go())
    assert "get_time" in names


def test_mcp_client_demo_script_resolvable():
    """mcp_client 能解析 demo 脚本路径（with_session 分流前提，HTTP 修复不应破坏）。"""
    # set_demo_script 是 mcp 分流入口用；此处仅验证 demo 脚本存在且可被 python 编译
    assert os.path.exists(DEMO_SCRIPT)
    subprocess.run([sys.executable, "-m", "py_compile", DEMO_SCRIPT],
                   check=True, timeout=30)
