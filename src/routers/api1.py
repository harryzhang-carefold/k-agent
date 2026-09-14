"""auth + agents + ext 路由 (PRD module 6/2/5/4/3)."""
import json
from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel
from core import db
from core.errors import APIError
from core.security import (current_user, require_perm, verify_password,
                           create_token, ROLE_MATRIX, PERMISSIONS)
from mcp import plugins as plugin_registry
from mcp import mcp_client

auth = APIRouter(prefix="/api/auth", tags=["auth"])
agents = APIRouter(prefix="/api/agents", tags=["agents"])
ext = APIRouter(prefix="/api/ext", tags=["ext"])


# ---------------- auth ----------------
class LoginIn(BaseModel):
    username: str
    password: str


@auth.post("/login")
async def login(body: LoginIn, request: Request):
    conn = request.app.state.db
    row = await db.fetchone(conn, "SELECT * FROM users WHERE username=?", (body.username,))
    if not row or not verify_password(body.password, row["password_hash"]):
        raise APIError(401, "用户名或密码错误")
    roles = [r["name"] for r in await db.fetchall(
        conn, """SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?""",
        (row["id"],))]
    perms = [p["code"] for p in await db.fetchall(
        conn, """SELECT DISTINCT p.code FROM permissions p
                 JOIN role_permissions rp ON rp.permission_code=p.code
                 JOIN user_roles ur ON ur.role_id=rp.role_id WHERE ur.user_id=?""", (row["id"],))]
    token = create_token(row["id"], row["username"], roles)
    return {"token": token, "user": {"id": row["id"], "username": row["username"],
                                     "roles": roles, "permissions": perms}}


@auth.get("/me")
async def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "username": user["username"],
            "roles": user["roles"], "permissions": user["permissions"]}


# ---------------- agents ----------------
class AgentIn(BaseModel):
    name: str
    description: str = ""
    system_prompt: str
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int = 1024
    top_p: float = 0.9
    bindings: list[dict] | None = None  # [{"type": "skill|mcp|plugin|rag|memory", "ref_id": str}]


async def _validate_bindings(conn, bindings: list[dict]):
    for b in bindings or []:
        btype, raw = b.get("type"), b.get("ref_id", "")
        if btype == "plugin":
            # 插件绑定用插件名（字符串），保持 str 匹配
            name = str(raw)
            if name not in plugin_registry.PLUGIN_REGISTRY:
                raise APIError(400, f"插件不存在: {name}")
        elif btype in ("skill", "mcp", "rag"):
            # ref_id 是 DB 整型主键（skills/mcp_servers/rag_knowledge.id，PG 为 BIGINT）：
            # 统一安全整数转换（BUG-004）。PG(asyncpg) 严格校验 int 类型，传 str 会
            # DataError→500；sqlite 类型亲和性会掩盖 str→int，导致双后端行为不一致。
            # 非整数 → 400，整数 → 类型安全查询（WHERE id=<int>），双后端行为一致。
            try:
                rid = int(raw)
            except (TypeError, ValueError):
                raise APIError(400, f"{btype} 绑定 ref_id 必须为整数: {raw!r}")
            table, label = {
                "skill": ("skills", "skill"),
                "mcp": ("mcp_servers", "mcp server"),
                "rag": ("rag_knowledge", "rag 知识库"),
            }[btype]
            if not await db.fetchone(conn, f"SELECT id FROM {table} WHERE id=?", (rid,)):
                raise APIError(400, f"{label} 不存在: {rid}")
        elif btype != "memory":
            raise APIError(400, f"未知绑定类型: {btype}")


@agents.get("")
async def list_agents(request: Request, user: dict = Depends(require_perm("agent:read"))):
    rows = await db.fetchall(request.app.state.db,
                             "SELECT * FROM agents ORDER BY id")
    for a in rows:
        a["bindings"] = await db.fetchall(
            request.app.state.db, "SELECT type, ref_id FROM agent_bindings WHERE agent_id=?",
            (a["id"],))
    return {"agents": rows}


@agents.get("/{agent_id}")
async def get_agent(agent_id: int, request: Request,
                    user: dict = Depends(require_perm("agent:read"))):
    a = await db.fetchone(request.app.state.db, "SELECT * FROM agents WHERE id=?", (agent_id,))
    if not a:
        raise APIError(404, "agent 不存在")
    a["bindings"] = await db.fetchall(
        request.app.state.db, "SELECT type, ref_id FROM agent_bindings WHERE agent_id=?", (agent_id,))
    return a


@agents.post("")
async def create_agent(body: AgentIn, request: Request,
                       user: dict = Depends(require_perm("agent:create"))):
    conn = request.app.state.db
    if await db.fetchone(conn, "SELECT id FROM agents WHERE name=?", (body.name,)):
        raise APIError(409, f"agent 重名: {body.name}")
    await _validate_bindings(conn, body.bindings)
    aid = await db.execute(
        conn,
        """INSERT INTO agents (name, description, system_prompt, model, temperature, max_tokens, top_p)
           VALUES (?,?,?,?,?,?,?)""",
        (body.name, body.description, body.system_prompt, body.model,
         body.temperature, body.max_tokens, body.top_p))
    for b in body.bindings or []:
        await db.execute(conn, "INSERT OR IGNORE INTO agent_bindings (agent_id, type, ref_id) VALUES (?,?,?)",
                         (aid, b["type"], str(b["ref_id"])))
    return {"id": aid, "name": body.name}


@agents.put("/{agent_id}")
async def update_agent(agent_id: int, body: AgentIn, request: Request,
                       user: dict = Depends(require_perm("agent:update"))):
    conn = request.app.state.db
    a = await db.fetchone(conn, "SELECT id FROM agents WHERE id=?", (agent_id,))
    if not a:
        raise APIError(404, "agent 不存在")
    if body.name != (await db.fetchone(conn, "SELECT name FROM agents WHERE id=?", (agent_id,)))["name"]:
        if await db.fetchone(conn, "SELECT id FROM agents WHERE name=?", (body.name,)):
            raise APIError(409, f"agent 重名: {body.name}")
    await _validate_bindings(conn, body.bindings)
    await db.execute(
        conn,
        """UPDATE agents SET name=?, description=?, system_prompt=?, model=?, temperature=?,
           max_tokens=?, top_p=?, updated_at=datetime('now') WHERE id=?""",
        (body.name, body.description, body.system_prompt, body.model,
         body.temperature, body.max_tokens, body.top_p, agent_id))
    await db.execute(conn, "DELETE FROM agent_bindings WHERE agent_id=?", (agent_id,))
    for b in body.bindings or []:
        await db.execute(conn, "INSERT OR IGNORE INTO agent_bindings (agent_id, type, ref_id) VALUES (?,?,?)",
                         (agent_id, b["type"], str(b["ref_id"])))
    return {"id": agent_id, "updated": True}


@agents.delete("/{agent_id}")
async def delete_agent(agent_id: int, request: Request,
                       user: dict = Depends(require_perm("agent:delete"))):
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM agents WHERE id=?", (agent_id,)):
        raise APIError(404, "agent 不存在")
    await db.execute(conn, "DELETE FROM agent_bindings WHERE agent_id=?", (agent_id,))
    await db.execute(conn, "DELETE FROM agents WHERE id=?", (agent_id,))
    return {"id": agent_id, "deleted": True}


@agents.get("/{agent_id}/prompt")
async def agent_prompt(agent_id: int, text: str = "患者 IgE 394 KU/L，FEV1 改善 240ml，哮喘控制情况如何？",
                       request: Request = None, user: dict = Depends(require_perm("agent:read"))):
    """调试接口：返回组装后的 messages（prefix caching 布局断言用，AC-18/24/33）。"""
    conn = request.app.state.db
    a = await db.fetchone(conn, "SELECT * FROM agents WHERE id=?", (agent_id,))
    if not a:
        raise APIError(404, "agent 不存在")
    engine = request.app.state.engine
    messages = await engine.assemble(a, text)
    return {"agent_id": agent_id, "messages": messages,
            "layout": "system(固定左: system_prompt+skills+工具schema+RAG) -> 记忆(左) -> 历史 -> 用户请求(最右)"}


# ---------------- ext: plugins / skills / mcp ----------------
@ext.get("/plugins")
async def list_plugins(user: dict = Depends(current_user)):
    return {"plugins": list(plugin_registry.PLUGIN_REGISTRY.values())}


class PluginCall(BaseModel):
    arguments: dict = {}


@ext.post("/plugins/{name}/call")
async def call_plugin(name: str, body: PluginCall, request: Request,
                      user: dict = Depends(current_user)):
    return await plugin_registry.call_plugin(name, body.arguments, {})


class SkillIn(BaseModel):
    name: str
    description: str = ""
    content: str


@ext.get("/skills")
async def list_skills(request: Request, user: dict = Depends(require_perm("ext:manage"))):
    return {"skills": await db.fetchall(request.app.state.db, "SELECT * FROM skills ORDER BY id")}


@ext.post("/skills")
async def create_skill(body: SkillIn, request: Request,
                       user: dict = Depends(require_perm("ext:manage"))):
    if not (body.content or "").strip():
        raise APIError(400, "skill 内容不能为空")
    conn = request.app.state.db
    if await db.fetchone(conn, "SELECT id FROM skills WHERE name=?", (body.name,)):
        raise APIError(409, f"skill 重名: {body.name}")
    sid = await db.execute(conn, "INSERT INTO skills (name, description, content) VALUES (?,?,?)",
                           (body.name, body.description, body.content))
    return {"id": sid, "name": body.name}


@ext.put("/skills/{skill_id}")
async def update_skill(skill_id: int, body: SkillIn, request: Request,
                       user: dict = Depends(require_perm("ext:manage"))):
    if not (body.content or "").strip():
        raise APIError(400, "skill 内容不能为空")
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM skills WHERE id=?", (skill_id,)):
        raise APIError(404, "skill 不存在")
    await db.execute(conn, "UPDATE skills SET name=?, description=?, content=? WHERE id=?",
                     (body.name, body.description, body.content, skill_id))
    return {"id": skill_id, "updated": True}


@ext.delete("/skills/{skill_id}")
async def delete_skill(skill_id: int, request: Request,
                       user: dict = Depends(require_perm("ext:manage"))):
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM skills WHERE id=?", (skill_id,)):
        raise APIError(404, "skill 不存在")
    await db.execute(conn, "DELETE FROM skills WHERE id=?", (skill_id,))
    return {"id": skill_id, "deleted": True}


# ---- MCP ----
class MCPServerIn(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict = {}


@ext.get("/mcp")
async def list_mcp(request: Request, user: dict = Depends(require_perm("ext:manage"))):
    return {"mcp_servers": [dict(r) for r in await db.fetchall(
        request.app.state.db, "SELECT * FROM mcp_servers ORDER BY id")]}


@ext.post("/mcp")
async def create_mcp(body: MCPServerIn, request: Request,
                     user: dict = Depends(require_perm("ext:manage"))):
    conn = request.app.state.db
    if await db.fetchone(conn, "SELECT id FROM mcp_servers WHERE name=?", (body.name,)):
        raise APIError(409, f"mcp server 重名: {body.name}")
    mid = await db.execute(conn,
                           "INSERT INTO mcp_servers (name, command, args, enabled) VALUES (?,?,?,true)",
                           (body.name, body.command, json.dumps(body.args)))
    return {"id": mid, "name": body.name}


@ext.get("/mcp/{mid}/tools")
async def mcp_tools(mid: int, request: Request, user: dict = Depends(require_perm("ext:manage"))):
    conn = request.app.state.db
    row = await db.fetchone(conn, "SELECT * FROM mcp_servers WHERE id=?", (mid,))
    if not row:
        raise APIError(404, "mcp server 不存在")
    try:
        async def _fn(s):
            return await s.tools_list()
        tools = await mcp_client.with_session(row["command"], json.loads(row["args"] or "[]"),
                                              fn=_fn)
        return {"server": row["name"], "tools": tools}
    except Exception as e:
        raise APIError(502, f"MCP 连接失败: {e}")


class ToolCallIn(BaseModel):
    arguments: dict = {}


@ext.post("/mcp/{mid}/tools/{tool}/call")
async def mcp_tool_call(mid: int, tool: str, body: ToolCallIn, request: Request,
                        user: dict = Depends(require_perm("ext:manage"))):
    conn = request.app.state.db
    row = await db.fetchone(conn, "SELECT * FROM mcp_servers WHERE id=?", (mid,))
    if not row:
        raise APIError(404, "mcp server 不存在")
    try:
        async def _fn(s):
            return await s.tools_call(tool, body.arguments)
        result = await mcp_client.with_session(row["command"], json.loads(row["args"] or "[]"),
                                               fn=_fn)
        return {"ok": True, "tool": tool, "result": result}
    except Exception as e:
        raise APIError(502, f"MCP 调用失败: {e}")

