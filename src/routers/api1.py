"""auth + agents + ext 路由 (PRD module 6/2/5/4/3)."""
import io
import json
import re
import zipfile
from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel
from core import db
from core.errors import APIError
from core.security import (current_user, require_perm, verify_password,
                           create_token, ROLE_MATRIX, PERMISSIONS)
from mcp import plugins as plugin_registry
from mcp import mcp_client
from mcp.mcp_client import env_of_row as _mcp_env_of

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
    sid = await db.execute(
        conn,
        "INSERT INTO skills (name, description, content, updated_at) "
        "VALUES (?,?,?,datetime('now'))",
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
    # 改名校验：同名（排除自身）-> 409
    other = await db.fetchone(conn, "SELECT id FROM skills WHERE name=? AND id<>?",
                              (body.name, skill_id))
    if other:
        raise APIError(409, f"skill 重名: {body.name}")
    # TASK-022 / F1: PUT 刷新 updated_at
    await db.execute(
        conn,
        "UPDATE skills SET name=?, description=?, content=?, updated_at=datetime('now') WHERE id=?",
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


# ---- skills 上传导入（TASK-022 / F1） ----
# 单文件 .md/.txt 或 zip（内含多个 SKILL.md）。SKILL.md 风格解析：
# 头部 frontmatter（--- 包围，YAML 风格 key: value）取 name/description；
# 无 frontmatter 或无 name 时用文件名（去扩展名/空格/点号）命名。
# 重名（含库内已有 + 本批次内重复）-> 跳过并回报，整批 200。
MAX_UPLOAD_MB = 10


def _sanitize_skill_name(raw: str) -> str:
    """文件名/路径片段 -> 合法 skill 名（小写、连字符，去扩展名与危险字符）。"""
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    base = base.rsplit(".", 1)[0]
    base = base.strip().lower().replace("_", "-").replace(" ", "-")
    base = re.sub(r"[^\w-]", "", base, flags=re.UNICODE)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return base


def _parse_skill_file(data: bytes, fallback_name: str) -> dict:
    """SKILL.md 风格解析：frontmatter name/description + 正文。"""
    text = data.decode("utf-8", errors="replace")
    name, desc = "", ""
    body = text
    m = re.match(r"\A\ufeff?---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, re.DOTALL)
    if m:
        fm, body = m.group(1), m.group(2)
        for line in fm.splitlines():
            k, _, v = line.partition(":")
            k = k.strip().lower()
            v = v.strip().strip('"').strip("'")
            if k == "name" and v:
                name = v
            elif k == "description" and v:
                desc = v
    name = (name or "").strip() or _sanitize_skill_name(fallback_name)
    return {"name": name, "description": desc, "content": body.strip()}


def _iter_skill_files(files: list) -> list:
    """multipart 文件列表 -> [(skill_name, source_label, parsed)]，按解析序。

    不支持的扩展名 / 空 zip 以 None 占位（调用方记入 failed）。
    """
    out = []
    for f in files:
        if f is None or not f.filename:
            continue
        fname = f.filename.replace("\\", "/").rsplit("/", 1)[-1]
        lowered = fname.lower()
        if lowered.endswith(".zip"):
            raw = f.file.read()
            if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
                raise APIError(400, f"文件过大（>{MAX_UPLOAD_MB}MB）: {fname}")
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
            except zipfile.BadZipFile:
                raise APIError(400, f"非法 zip 文件: {fname}")
            inner = [n for n in zf.namelist()
                     if not n.startswith(("/", "__MACOSX")) and not n.split("/")[-1].startswith(".")]
            md_files = [n for n in inner if n.lower().endswith((".md", ".txt"))]
            if not md_files:
                out.append(None)  # 标记：空 zip
                continue
            for n in sorted(md_files):
                data = zf.read(n)
                if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
                    raise APIError(400, f"zip 内文件过大: {n}")
                parsed = _parse_skill_file(data, n)
                out.append((parsed["name"], f"{fname}:{n}", parsed))
        elif lowered.endswith((".md", ".txt")):
            raw = f.file.read()
            if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
                raise APIError(400, f"文件过大（>{MAX_UPLOAD_MB}MB）: {fname}")
            parsed = _parse_skill_file(raw, fname)
            out.append((parsed["name"], fname, parsed))
        else:
            out.append(None)  # 标记：不支持的扩展名
    return out


@ext.post("/skills/upload")
async def upload_skills(request: Request,
                        user: dict = Depends(require_perm("ext:manage"))):
    """批量导入 skills（multipart: files[]，单文件 .md/.txt 或 zip 目录）。

    返回 {created:[{name,id}], skipped:[{name,reason}], failed:[{file,reason}]}。
    重名（库内已有 / 批次内重复 / 空内容）跳过并回报，整批 200。
    """
    form = await request.form()
    files = form.getlist("files")
    if not files:
        raise APIError(400, "未收到文件（multipart 字段名: files）")
    if len(files) > 50:
        raise APIError(400, "单次最多上传 50 个文件")
    conn = request.app.state.db
    existing = {r["name"] for r in await db.fetchall(conn, "SELECT name FROM skills")}
    created, skipped, failed = [], [], []
    for item in _iter_skill_files(files):
        if item is None:
            failed.append({"file": None, "reason": "不支持的文件类型或空 zip（仅 .md/.txt 或含此类文件的 zip）"})
            continue
        name, source, parsed = item
        if not parsed["content"]:
            skipped.append({"name": name, "reason": f"空内容（{source}）"})
            continue
        if name in existing:
            skipped.append({"name": name, "reason": f"重名，库内已存在（{source}）"})
            continue
        sid = await db.execute(
            conn,
            "INSERT INTO skills (name, description, content, updated_at) "
            "VALUES (?,?,?,datetime('now'))",
            (name, parsed["description"], parsed["content"]))
        existing.add(name)
        created.append({"name": name, "id": sid, "source": source})
    return {"created": created, "skipped": skipped, "failed": failed,
            "counts": {"created": len(created), "skipped": len(skipped), "failed": len(failed)}}


# ---- MCP ----
class MCPServerIn(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict = {}
    enabled: bool = True


def _norm_env(env) -> dict:
    """env 值统一为 str（MCP 子进程 env 只能是字符串；数字/布尔 400 由调用方控制）。"""
    return {str(k): str(v) for k, v in (env or {}).items()}


@ext.get("/mcp")
async def list_mcp(request: Request, user: dict = Depends(require_perm("ext:manage"))):
    return {"mcp_servers": [_mcp_row_out(r) for r in await db.fetchall(
        request.app.state.db, "SELECT * FROM mcp_servers ORDER BY id")]}


def _mcp_row_out(row: dict) -> dict:
    """mcp_servers 行 -> API 视图：args/env 反序列化为 JSON，enabled 归一为 bool。"""
    r = dict(row)
    try:
        r["args"] = json.loads(r.get("args") or "[]")
    except (json.JSONDecodeError, TypeError):
        r["args"] = []
    r["env"] = _mcp_env_of(row)
    r["enabled"] = bool(r.get("enabled"))
    return r


@ext.post("/mcp")
async def create_mcp(body: MCPServerIn, request: Request,
                     user: dict = Depends(require_perm("ext:manage"))):
    conn = request.app.state.db
    if await db.fetchone(conn, "SELECT id FROM mcp_servers WHERE name=?", (body.name,)):
        raise APIError(409, f"mcp server 重名: {body.name}")
    # TASK-022 / F1: env 写入（此前丢失）；统一双后端为 JSON 文本，读取时反序列化
    mid = await db.execute(
        conn,
        "INSERT INTO mcp_servers (name, command, args, env, enabled) VALUES (?,?,?,?,?)",
        (body.name, body.command, json.dumps(body.args),
         json.dumps(_norm_env(body.env), ensure_ascii=False), body.enabled))
    return {"id": mid, "name": body.name}


@ext.put("/mcp/{mid}")
async def update_mcp(mid: int, body: MCPServerIn, request: Request,
                     user: dict = Depends(require_perm("ext:manage"))):
    """TASK-022 / F1: MCP server 更新（name/command/args/env/enabled）。"""
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM mcp_servers WHERE id=?", (mid,)):
        raise APIError(404, "mcp server 不存在")
    other = await db.fetchone(conn, "SELECT id FROM mcp_servers WHERE name=? AND id<>?",
                              (body.name, mid))
    if other:
        raise APIError(409, f"mcp server 重名: {body.name}")
    await db.execute(
        conn,
        "UPDATE mcp_servers SET name=?, command=?, args=?, env=?, enabled=? WHERE id=?",
        (body.name, body.command, json.dumps(body.args),
         json.dumps(_norm_env(body.env), ensure_ascii=False), body.enabled, mid))
    return {"id": mid, "updated": True}


@ext.delete("/mcp/{mid}")
async def delete_mcp(mid: int, request: Request,
                     user: dict = Depends(require_perm("ext:manage"))):
    """TASK-022 / F1: 删除 MCP server；有 agent_bindings 引用时 409 提示先解绑。"""
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM mcp_servers WHERE id=?", (mid,)):
        raise APIError(404, "mcp server 不存在")
    refs = await db.fetchall(
        conn,
        "SELECT b.agent_id, a.name FROM agent_bindings b "
        "JOIN agents a ON a.id=b.agent_id WHERE b.type='mcp' AND b.ref_id=?",
        (str(mid),))
    if refs:
        names = ", ".join(r["name"] for r in refs)
        raise APIError(409, f"该 mcp server 仍被 agent 引用（{names}），请先解绑")
    await db.execute(conn, "DELETE FROM mcp_servers WHERE id=?", (mid,))
    return {"id": mid, "deleted": True}


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
                                              env=_mcp_env_of(row),
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
                                               env=_mcp_env_of(row),
                                               fn=_fn)
        return {"ok": True, "tool": tool, "result": result}
    except Exception as e:
        raise APIError(502, f"MCP 调用失败: {e}")

