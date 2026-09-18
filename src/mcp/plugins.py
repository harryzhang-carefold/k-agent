"""Plugins — 平台内置可调用能力注册表 (PRD module 5, AC-25/26/27).

内置: get_time / mcp_call / echo。
插件作为 tool 暴露给 LLM（schema 注入 prompt 固定左侧），LLM 以
{"tool_call": {"name":..., "arguments":...}} JSON 块请求调用，引擎执行后喂回。
"""
import datetime
import json
from core import db
from core.errors import MCPError

PLUGIN_REGISTRY = {
    "get_time": {
        "name": "get_time",
        "description": "返回当前时间（UTC + 本地 ISO 格式）",
        "schema": {
            "type": "object",
            "properties": {"format": {"type": "string", "description": "可选 strftime 格式"}},
            "required": [],
        },
    },
    "mcp_call": {
        "name": "mcp_call",
        "description": "调用 agent 绑定的 MCP server 工具（name=工具名）",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "MCP 工具名"},
                "arguments": {"type": "object", "description": "工具参数"},
            },
            "required": ["name"],
        },
    },
    "echo": {
        "name": "echo",
        "description": "原样返回输入（调试用）",
        "schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}


async def call_plugin(name: str, arguments: dict, context: dict) -> dict:
    """context: mcp_call 路由字段（mcp_client.mcp_ctx_from_row 产出）：
    {mcp_transport, mcp_command, mcp_args, mcp_env, mcp_url, mcp_headers}。
    兼容旧引擎直接传 {mcp_command, mcp_args, mcp_env}（无 transport → 按 stdio）。
    """
    if name not in PLUGIN_REGISTRY:
        return {"ok": False, "error": f"插件不存在: {name}"}
    arguments = arguments or {}
    try:
        if name == "get_time":
            now = datetime.datetime.now()
            fmt = arguments.get("format")
            return {"ok": True, "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "local": now.strftime(fmt) if fmt else now.isoformat()}
        if name == "echo":
            return {"ok": True, "text": arguments.get("text", "")}
        if name == "mcp_call":
            from mcp import mcp_client
            transport = (context.get("mcp_transport") or "stdio").strip().lower()
            if transport not in ("stdio", "http"):
                transport = "stdio"
            if transport == "http":
                url = (context.get("mcp_url") or "").strip()
                if not url:
                    return {"ok": False, "error": "agent 绑定的 MCP server 缺少 http url"}
                async def _do_http(s):
                    return await s.tools_call(arguments.get("name", ""), arguments.get("arguments"))
                result = await mcp_client.with_http_session(url, context.get("mcp_headers"),
                                                            fn=_do_http)
                return {"ok": True, "mcp": arguments.get("name"), "result": result}
            command = context.get("mcp_command")
            margs = context.get("mcp_args", [])
            if not command:
                return {"ok": False, "error": "agent 未绑定 MCP server"}
            async def _do(s):
                return await s.tools_call(arguments.get("name", ""), arguments.get("arguments"))
            result = await mcp_client.with_session(command, margs, context.get("mcp_env"),
                                                    fn=_do)
            return {"ok": True, "mcp": arguments.get("name"), "result": result}
    except MCPError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": False, "error": f"未实现的插件: {name}"}


def schemas_for(names: list[str]) -> list[dict]:
    return [PLUGIN_REGISTRY[n] for n in names if n in PLUGIN_REGISTRY]
