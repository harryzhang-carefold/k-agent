"""/api/trace/* 路由（TASK-057 / 迭代5）：链路追踪查询。

端点：
  GET /api/trace/conversations?agent_id=&limit=&offset= → 会话聚合列表（含 token/模型/工具摘要）
  GET /api/trace/conversations/{conv_id}                → 聚合 + 全部 spans（按 seq 排序）

鉴权（DECISION-027 决策7，安全优先）：沿用现有 current_user；**数据不跨 agent
泄露**——拿不准就全部要求 admin（褚岩倾向后者），故两个端点均要求 `system:admin`。
非 admin 一律 403，不返回任何 trace 数据（README 注明）。

保留策略：trace 数据保留 N 天（默认 30，settings 表 trace_retention_days 可配），
启动时清理过期行（幂等，见 core.trace.clean_expired；启动清理在 core.app 执行）。
"""
import json

from fastapi import APIRouter, Request, Depends, Query
from core import db
from core import trace as trace_mod
from core.errors import APIError
from core.security import current_user, require_perm

trace = APIRouter(prefix="/api/trace", tags=["trace"])


def _load_json(raw):
    """DB 里的 JSON 数组/对象字符串 → python（脏值 → None）。"""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _conv_row(r: dict) -> dict:
    """trace_conversations 行 → 视图（JSON 字段反序列化，便于前端直接用）。"""
    return {
        "id": r["id"],
        "agent_id": r["agent_id"],
        "agent_name": r.get("agent_name"),
        "backend": r.get("backend"),
        "started_at": r.get("started_at"),
        "ended_at": r.get("ended_at"),
        "total_tokens_in": r.get("total_tokens_in"),
        "total_tokens_out": r.get("total_tokens_out"),
        "total_llm_calls": r.get("total_llm_calls"),
        "models": _load_json(r.get("models")) or [],
        "tools_called": _load_json(r.get("tools_called")) or [],
        "skills_used": _load_json(r.get("skills_used")) or [],
        "rag_kb_used": _load_json(r.get("rag_kb_used")) or [],
        "files_created": _load_json(r.get("files_created")) or [],
        "duration_ms": r.get("duration_ms"),
        "status": r.get("status"),
        "error": r.get("error"),
    }


def _span_row(r: dict) -> dict:
    """trace_spans 行 → 视图（JSON 字段反序列化）。"""
    return {
        "id": r["id"],
        "conv_id": r["conv_id"],
        "seq": r["seq"],
        "ts": r["ts"],
        "span_type": r["span_type"],
        "name": r.get("name"),
        "input": r.get("input"),
        "output": r.get("output"),
        "tokens_in": r.get("tokens_in"),
        "tokens_out": r.get("tokens_out"),
        "model": r.get("model"),
        "rag_chunks": _load_json(r.get("rag_chunks")),
        "duration_ms": r.get("duration_ms"),
        "status": r.get("status"),
        "error": r.get("error"),
    }


@trace.get("/conversations")
async def list_conversations(
    request: Request,
    agent_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(require_perm("system:admin")),
):
    """会话聚合列表（按 started_at 倒序，最新在前）。

    - agent_id 可选：过滤指定 agent 的 trace。
    - limit/offset 分页（limit 上限 500，防大查询）。
    - 权限：system:admin（DECISION-027 决策7 安全优先，不跨 agent 泄露）。
    """
    conn = request.app.state.db
    sql = "SELECT * FROM trace_conversations"
    params: list = []
    if agent_id is not None:
        sql += " WHERE agent_id=?"
        params.append(agent_id)
    sql += " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = await db.fetchall(conn, sql, tuple(params))
    return {"conversations": [_conv_row(r) for r in rows],
            "count": len(rows), "limit": limit, "offset": offset,
            "retention_days": await trace_mod.retention_days(conn)}


@trace.get("/conversations/{conv_id}")
async def conversation_detail(
    conv_id: str,
    request: Request,
    user: dict = Depends(require_perm("system:admin")),
):
    """会话详情：聚合行 + 全部 spans（按 seq 升序）。

    不存在 → 404。权限：system:admin（DECISION-027 决策7）。
    """
    conn = request.app.state.db
    row = await db.fetchone(conn,
                            "SELECT * FROM trace_conversations WHERE id=?",
                            (conv_id,))
    if row is None:
        raise APIError(404, "trace 会话不存在")
    spans = await db.fetchall(
        conn,
        "SELECT * FROM trace_spans WHERE conv_id=? ORDER BY seq ASC, id ASC",
        (conv_id,))
    return {
        "conversation": _conv_row(row),
        "spans": [_span_row(s) for s in spans],
        "span_count": len(spans),
    }
