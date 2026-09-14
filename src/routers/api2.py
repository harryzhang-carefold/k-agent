"""chat / ws / rag / memory / users / longtext 路由 (PRD module 7/9/12/15)."""
import base64
import json
import uuid
from fastapi import APIRouter, Request, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from core import db
from core.errors import APIError, GraphError
from core.security import current_user, require_perm, decode_token, hash_password, ROLE_MATRIX
from core import stats
from memory import backend as memory
from memory.graph import validate_edge_type
from services import longtext

chat = APIRouter(prefix="/api/chat", tags=["chat"])
rag = APIRouter(prefix="/api/rag", tags=["rag"])
mem = APIRouter(prefix="/api/memory", tags=["memory"])
users = APIRouter(prefix="/api/users", tags=["users"])
roles = APIRouter(prefix="/api/roles", tags=["roles"])
lt = APIRouter(prefix="/api/longtext", tags=["longtext"])


# ---------------- chat ----------------
class ChatIn(BaseModel):
    message: str
    conv_id: str | None = None
    images: list[dict] | None = None  # [{"b64":..., "content_type":...}]


async def _history(conn, conv_id: str) -> list[dict]:
    rows = await db.fetchall(conn,
                             "SELECT role, content FROM messages WHERE conv_id=? ORDER BY id",
                             (conv_id,))
    return [{"role": r["role"], "content": r["content"]} for r in rows]


@chat.post("/{agent_id}")
async def chat_sync(agent_id: int, body: ChatIn, request: Request,
                    user: dict = Depends(current_user)):
    conn = request.app.state.db
    agent = await db.fetchone(conn, "SELECT * FROM agents WHERE id=?", (agent_id,))
    if not agent:
        raise APIError(404, "agent 不存在")
    conv_id = body.conv_id or f"c{uuid.uuid4().hex[:12]}"
    if not await db.fetchone(conn, "SELECT id FROM conversations WHERE id=?", (conv_id,)):
        await db.execute(conn, "INSERT INTO conversations (id, agent_id) VALUES (?,?)",
                         (conv_id, agent_id))
    history = await _history(conn, conv_id)
    result = await request.app.state.engine.run(agent, body.message, body.images, history)
    # 持久化消息
    await db.execute(conn, "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                     (conv_id, "user", body.message or "[image]"))
    await db.execute(conn, "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                     (conv_id, "assistant", result["answer"] or ""))
    return {
        "conv_id": conv_id,
        "answer": result["answer"],
        "cache_hit": result.get("cache_hit"),
        "llm_calls": result.get("llm_calls", 0),
        "route_events": result.get("route_events", []),
        "sub_requests": result.get("sub_requests", []),
        "tool_calls": result.get("tool_calls", []),
        "degraded": result.get("degraded", False),
        "counters": stats.snapshot(),
    }


# ---------------- websocket ----------------
ws_router = APIRouter(tags=["ws"])


@ws_router.websocket("/ws/chat/{agent_id}/{conv_id}")
async def ws_chat(ws: WebSocket, agent_id: int, conv_id: str):
    token = ws.query_params.get("token") or (
        (ws.headers.get("authorization") or "").replace("Bearer ", "").strip())
    if not token:
        await ws.close(code=4401, reason="未认证：缺少 token")
        return
    try:
        payload = decode_token(token)
    except Exception:
        await ws.close(code=4401, reason="无效/过期 token")
        return
    await ws.accept()
    conn = ws.app.state.db
    agent = await db.fetchone(conn, "SELECT * FROM agents WHERE id=?", (agent_id,))
    if not agent:
        await ws.send_json({"type": "error", "content": "agent 不存在"})
        await ws.close()
        return
    if not await db.fetchone(conn, "SELECT id FROM conversations WHERE id=?", (conv_id,)):
        await db.execute(conn, "INSERT INTO conversations (id, agent_id) VALUES (?,?)",
                         (conv_id, agent_id))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                msg = {"message": raw}
            text = msg.get("message") or ""
            images = msg.get("images")
            history = await _history(conn, conv_id)
            # 流式执行（缓存路由前置；命中直发，否则逐 token）
            out = await ws.app.state.engine.run_stream(agent, text, images, history)
            if out.get("cache_hit"):
                await ws.send_json({"type": "cache_hit", "content": out["answer"],
                                    "cache_hit": out["cache_hit"],
                                    "route_events": out.get("route_events", [])})
            for tok in out.pop("_tokens", []):
                await ws.send_json({"type": "token", "content": tok})
            if out.get("degraded"):
                await ws.send_json({"type": "token", "content": out["answer"]})
            await db.execute(conn, "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                             (conv_id, "user", text or "[image]"))
            await db.execute(conn, "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                             (conv_id, "assistant", out.get("answer") or ""))
            await ws.send_json({
                "type": "done",
                "content": out.get("answer") or "",
                "cache_hit": out.get("cache_hit"),
                "llm_calls": out.get("llm_calls", 0),
                "route_events": out.get("route_events", []),
                "sub_requests": out.get("sub_requests", []),
                "degraded": out.get("degraded", False),
            })
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "content": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
        await ws.close()


# ---------------- RAG ----------------
class RagIn(BaseModel):
    name: str


class DocIn(BaseModel):
    text: str
    chunk_chars: int | None = None


class SearchIn(BaseModel):
    query: str
    top_k: int = 3


@rag.get("/knowledge")
async def list_kb(request: Request, user: dict = Depends(require_perm("rag:read"))):
    rows = await db.fetchall(request.app.state.db, "SELECT * FROM rag_knowledge ORDER BY id")
    for k in rows:
        r = await db.fetchone(request.app.state.db,
                              "SELECT COUNT(*) c FROM rag_chunks WHERE knowledge_id=?", (k["id"],))
        k["chunks"] = r["c"]
    return {"knowledge": rows}


@rag.post("/knowledge")
async def create_kb(body: RagIn, request: Request,
                    user: dict = Depends(require_perm("rag:write"))):
    conn = request.app.state.db
    if await db.fetchone(conn, "SELECT id FROM rag_knowledge WHERE name=?", (body.name,)):
        raise APIError(409, f"知识库重名: {body.name}")
    kid = await db.execute(conn, "INSERT INTO rag_knowledge (name) VALUES (?)", (body.name,))
    return {"id": kid, "name": body.name}


@rag.delete("/knowledge/{kid}")
async def delete_kb(kid: int, request: Request, user: dict = Depends(require_perm("rag:delete"))):
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM rag_knowledge WHERE id=?", (kid,)):
        raise APIError(404, "知识库不存在")
    await db.execute(conn, "DELETE FROM rag_chunks WHERE knowledge_id=?", (kid,))
    await db.execute(conn, "DELETE FROM agent_bindings WHERE type='rag' AND ref_id=?", (str(kid),))
    await db.execute(conn, "DELETE FROM rag_knowledge WHERE id=?", (kid,))
    return {"id": kid, "deleted": True}


@rag.post("/knowledge/{kid}/documents")
async def add_doc(kid: int, body: DocIn, request: Request,
                  user: dict = Depends(require_perm("rag:write"))):
    from rag import rag as ragmod
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM rag_knowledge WHERE id=?", (kid,)):
        raise APIError(404, "知识库不存在")
    return await ragmod.add_document(conn, kid, body.text, body.chunk_chars)


@rag.get("/knowledge/{kid}/chunks")
async def list_chunks(kid: int, request: Request, user: dict = Depends(require_perm("rag:read"))):
    conn = request.app.state.db
    rows = await db.fetchall(conn, "SELECT id, seq, text, token_est FROM rag_chunks WHERE knowledge_id=? ORDER BY seq", (kid,))
    return {"chunks": rows}


@rag.post("/knowledge/{kid}/search")
async def search_kb(kid: int, body: SearchIn, request: Request,
                    user: dict = Depends(require_perm("rag:read"))):
    from rag import rag as ragmod
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM rag_knowledge WHERE id=?", (kid,)):
        raise APIError(404, "知识库不存在")
    res = await ragmod.search(conn, kid, body.query, body.top_k)
    return {"query": body.query, "top_k": body.top_k, "results": res,
            "context": ragmod.build_context(res)}


# ---------------- memory ----------------
@mem.get("/l0")
async def l0_list(agent_id: int | None = None, limit: int = 20,
                  request: Request = None, user: dict = Depends(require_perm("memory:read"))):
    mb = memory.get_memory_backend()
    rows = await mb.l0_recent(limit)
    if agent_id is not None:
        rows = [r for r in rows if r["agent_id"] == agent_id]
    return {"l0": rows, "count": len(rows)}


@mem.get("/l1")
async def l1_list(agent_key: str | None = None, request: Request = None,
                  user: dict = Depends(require_perm("memory:read"))):
    mb = memory.get_memory_backend()
    rows = await mb.l1_list(agent_key)
    return {"l1": rows, "count": len(rows)}


class L1SearchIn(BaseModel):
    agent_key: str
    text: str
    threshold: float = 0.95


@mem.post("/l1/search")
async def l1_search(body: L1SearchIn, request: Request = None,
                    user: dict = Depends(require_perm("memory:read"))):
    mb = memory.get_memory_backend()
    hit = await mb.l1_search(body.agent_key, body.text, body.threshold)
    return {"hit": hit, "threshold": body.threshold}


@mem.get("/l2/nodes")
async def l2_nodes(request: Request = None, user: dict = Depends(require_perm("memory:read"))):
    mb = memory.get_memory_backend()
    q = await mb.l2_query({"nodes": 1})
    return {"nodes": q["nodes"], "counts": q["counts"]}


@mem.get("/l2/edges")
async def l2_edges(request: Request = None, user: dict = Depends(require_perm("memory:read"))):
    mb = memory.get_memory_backend()
    q = await mb.l2_query({"edges": 1})
    return {"edges": q["edges"], "counts": q["counts"]}


class EdgeIn(BaseModel):
    src: str
    dst: str
    type: str
    props: dict = {}
    src_label: str = "Entity"
    dst_label: str = "Entity"


@mem.post("/l2/edge")
async def l2_add_edge(body: EdgeIn, request: Request = None,
                      user: dict = Depends(require_perm("memory:write"))):
    # BUG-003 修复：先校验边类型/语义，通过后才落库节点+写边，
    # 非法边返回 400 且零图副作用（不持久化任何孤儿节点/边）。
    # （原实现先 l2_node_or_create(src/dst) 再校验，400 时已落库孤儿节点，非原子写。）
    try:
        validate_edge_type(body.type)
    except GraphError as e:
        raise APIError(400, str(e))
    mb = memory.get_memory_backend()
    try:
        await mb.l2_node_or_create(body.src, body.src_label)
        await mb.l2_node_or_create(body.dst, body.dst_label)
        e = await mb.l2_merge_edge(body.src, body.dst, body.type, body.props)
        return {"edge": e.to_dict()}
    except GraphError as e:
        raise APIError(400, str(e))


@mem.get("/l2/subgraph")
async def l2_subgraph(start: str, hops: int = 2, request: Request = None,
                      user: dict = Depends(require_perm("memory:read"))):
    mb = memory.get_memory_backend()
    q = await mb.l2_query({"from": start, "hops": hops, "subgraph": start})
    # 无向子图（出边+入边邻接）：center 自身 + N 跳内全部节点/边
    # start 不存在 -> 空（不报错、不伪造节点）
    return {"start": start, "hops": hops,
            "nodes": q.get("subgraph_nodes", []),
            "edges": q.get("subgraph_edges", []),
            "bfs": q["bfs"], "path": q["path"],
            "summary": q.get("subgraph", "")}


@mem.get("/l2/events")
async def l2_events(request: Request = None, user: dict = Depends(require_perm("memory:read"))):
    mb = memory.get_memory_backend()
    q = await mb.l2_query({"nodes": 1, "edges": 1})
    visits = [n for n in q["nodes"] if n["label"] == "Visit"]
    out = []
    for v in visits:
        related = {}
        for e in q["edges"]:
            if e["src"] == v["id"]:
                related.setdefault(e["type"], []).append({"node": e["dst"], "props": e["props"]})
        out.append({"visit": v, "relations": related})
    return {"events": out}


@mem.get("/l2/bucket")
async def l2_bucket(node: str, dim: str, key: str | None = None,
                    request: Request = None, user: dict = Depends(require_perm("memory:read"))):
    mb = memory.get_memory_backend()
    recs = await mb.l2_bucket_read(node, dim, key)
    return {"node": node, "dim": dim, "key": key, "records": recs}


@mem.get("/backend")
async def mem_backend(request: Request = None, user: dict = Depends(current_user)):
    mb = memory.get_memory_backend()
    return {"current": mb.info(), "adapters": memory.list_adapters()}


# ---------------- users / roles ----------------
class UserIn(BaseModel):
    username: str
    password: str
    role: str


@users.get("")
async def list_users(request: Request, user: dict = Depends(require_perm("user:manage"))):
    rows = await db.fetchall(request.app.state.db, "SELECT id, username, created_at FROM users")
    for u in rows:
        u["roles"] = [r["name"] for r in await db.fetchall(
            request.app.state.db,
            "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?",
            (u["id"],))]
    return {"users": rows}


@users.post("")
async def create_user(body: UserIn, request: Request,
                      user: dict = Depends(require_perm("user:manage"))):
    if body.role not in ROLE_MATRIX:
        raise APIError(400, f"未知角色: {body.role}（可选 {list(ROLE_MATRIX)}）")
    conn = request.app.state.db
    if await db.fetchone(conn, "SELECT id FROM users WHERE username=?", (body.username,)):
        raise APIError(409, f"用户重名: {body.username}")
    uid = await db.execute(conn, "INSERT INTO users (username, password_hash) VALUES (?,?)",
                           (body.username, hash_password(body.password)))
    role = await db.fetchone(conn, "SELECT id FROM roles WHERE name=?", (body.role,))
    await db.execute(conn, "INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)",
                     (uid, role["id"]))
    return {"id": uid, "username": body.username, "role": body.role}


@users.delete("/{uid}")
async def delete_user(uid: int, request: Request,
                      user: dict = Depends(require_perm("user:manage"))):
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM users WHERE id=?", (uid,)):
        raise APIError(404, "用户不存在")
    if uid == user["id"]:
        raise APIError(400, "不能删除当前登录用户")
    await db.execute(conn, "DELETE FROM user_roles WHERE user_id=?", (uid,))
    await db.execute(conn, "DELETE FROM users WHERE id=?", (uid,))
    return {"id": uid, "deleted": True}


@roles.get("")
async def list_roles(request: Request, user: dict = Depends(require_perm("role:manage"))):
    out = []
    for r in await db.fetchall(request.app.state.db, "SELECT * FROM roles ORDER BY id"):
        perms = [p["code"] for p in await db.fetchall(
            request.app.state.db,
            "SELECT permission_code code FROM role_permissions WHERE role_id=?", (r["id"],))]
        out.append({"id": r["id"], "name": r["name"], "permissions": perms})
    from core.security import PERMISSIONS as _P
    return {"roles": out, "all_permissions": [c for c, _ in _P], "permission_count": len(_P)}


class RolePermsIn(BaseModel):
    permissions: list[str]


@roles.put("/{rid}/permissions")
async def set_role_perms(rid: int, body: RolePermsIn, request: Request,
                         user: dict = Depends(require_perm("role:manage"))):
    conn = request.app.state.db
    if not await db.fetchone(conn, "SELECT id FROM roles WHERE id=?", (rid,)):
        raise APIError(404, "角色不存在")
    valid = set(p for p, _ in __import__("core.security", fromlist=["PERMISSIONS"]).PERMISSIONS)
    bad = [p for p in body.permissions if p not in valid]
    if bad:
        raise APIError(400, f"无效权限码: {bad}")
    await db.execute(conn, "DELETE FROM role_permissions WHERE role_id=?", (rid,))
    for p in body.permissions:
        await db.execute(conn, "INSERT OR IGNORE INTO role_permissions (role_id, permission_code) VALUES (?,?)",
                         (rid, p))
    return {"role_id": rid, "permissions": body.permissions}


# ---------------- longtext 4 策略 ----------------
class LTTextIn(BaseModel):
    text: str
    center: str | None = None


@lt.post("/map-reduce")
async def lt_map_reduce(body: LTTextIn, request: Request, user: dict = Depends(current_user)):
    if not (body.text or "").strip():
        raise APIError(400, "空文本")
    return await longtext.map_reduce(request.app.state.db, body.text)


@lt.post("/incremental-graph")
async def lt_incr(body: LTTextIn, request: Request, user: dict = Depends(current_user)):
    if not (body.text or "").strip():
        raise APIError(400, "空文本")
    return await longtext.incremental_graph(request.app.state.db, body.text, body.center or "")


class CRIn(BaseModel):
    text: str
    max_rounds: int = 5


@lt.post("/critique-refine")
async def lt_critique(body: CRIn, request: Request, user: dict = Depends(current_user)):
    if not (body.text or "").strip():
        raise APIError(400, "空文本")
    return await longtext.critique_refine(request.app.state.db, body.text, body.max_rounds)


@lt.get("/hitl")
async def lt_hitl(request: Request, user: dict = Depends(require_perm("memory:read"))):
    rows = await db.fetchall(request.app.state.db, "SELECT * FROM hitl_queue ORDER BY id DESC LIMIT 50")
    return {"hitl": rows, "count": len(rows)}


class CSVIn(BaseModel):
    csv: str
    llm_convert: bool = True


@lt.post("/preprocess")
async def lt_preprocess(body: CSVIn, request: Request, user: dict = Depends(current_user)):
    if not (body.csv or "").strip():
        raise APIError(400, "空 CSV")
    return await longtext.preprocess_csv(body.csv, body.llm_convert)
