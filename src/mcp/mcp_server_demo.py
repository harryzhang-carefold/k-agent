#!/usr/bin/env python3
"""内置 demo MCP server — 真实 stdio JSON-RPC 2.0 进程 (PRD module 3, AC-19/20).

协议：LSP 风格 Content-Length 头 + JSON-RPC 2.0 body over stdio。
支持：initialize / initialized / tools/list / tools/call / ping。
demo 工具（>=2）：get_time、get_patient_demo（医疗种子数据）。
真实外部 MCP 进程为适配位：mcp_servers 表存 command/args，配置即可接入（DECISION-002）。
"""
import json
import sys
import datetime

PROTOCOL_VERSION = "2025-03-26"

TOOLS = [
    {
        "name": "get_time",
        "description": "返回当前 UTC 与本地时间（ISO 格式）",
        "inputSchema": {
            "type": "object",
            "properties": {"format": {"type": "string", "description": "可选 strftime 格式"}},
            "required": [],
        },
    },
    {
        "name": "get_patient_demo",
        "description": "返回内置医疗示例患者摘要（患者-哮喘-FEV1-IgE 种子数据）",
        "inputSchema": {
            "type": "object",
            "properties": {"patient_id": {"type": "string", "description": "患者ID，默认 P001"}},
            "required": [],
        },
    },
]


def _tool_get_time(args):
    now = datetime.datetime.now()
    fmt = (args or {}).get("format")
    return {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "local": now.strftime(fmt) if fmt else now.isoformat()}


def _tool_get_patient_demo(args):
    pid = (args or {}).get("patient_id", "P001")
    demo = {
        "P001": {
            "patient_id": "P001", "name": "王建国", "age": 52,
            "diagnosis": "支气管哮喘（中重度，控制不佳）",
            "fev1_pre": 2.31, "fev1_post": 2.55, "fev1_improvement_ml": 240,
            "fev1_improvement_pct": 10.39,
            "ige_ku_l": 394.00, "bronchodilator_test": "阳性（FEV1 改善 240ml / 10.39%）",
            "allergy_history": "尘螨、花粉",
        }
    }
    return demo.get(pid, {"patient_id": pid, "error": "未知患者（demo 仅有 P001）"})


TOOL_IMPL = {"get_time": _tool_get_time, "get_patient_demo": _tool_get_patient_demo}


def _read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        k, _, v = line.decode("utf-8").partition(":")
        headers[k.strip().lower()] = v.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def _write_message(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _resp(id_, result):
    _write_message({"jsonrpc": "2.0", "id": id_, "result": result})


def _err(id_, code, message):
    _write_message({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def main():
    while True:
        msg = _read_message()
        if msg is None:
            break
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        try:
            if method == "initialize":
                _resp(mid, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agp-demo-mcp", "version": "1.0.0"},
                })
            elif method == "notifications/initialized" or method == "initialized":
                pass
            elif method == "ping":
                _resp(mid, {})
            elif method == "tools/list":
                _resp(mid, {"tools": TOOLS})
            elif method == "tools/call":
                name = params.get("name")
                impl = TOOL_IMPL.get(name)
                if impl is None:
                    _err(mid, -32602, f"未知工具: {name}")
                    continue
                result = impl(params.get("arguments") or {})
                _resp(mid, {"content": [{"type": "text",
                                         "text": json.dumps(result, ensure_ascii=False)}],
                            "isError": False})
            else:
                _err(mid, -32601, f"方法不支持: {method}")
        except Exception as e:  # 不崩溃，回 JSON-RPC error
            _err(mid, -32603, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
