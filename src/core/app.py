"""AI Agent Platform — FastAPI 应用入口 (PRD module 6/7/11).

启动：uvicorn core.app:app --port 8099（run.sh 一键起）
- REST /api/* 全接口（auth/agents/chat/rag/memory/users/ext/longtext）
- WS /ws/chat/{agent_id}/{conv_id}（流式 token）
- /healthz 健康检查
- 挂载 static/ 提供纯静态 SPA（DECISION-001）
"""
import os
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from core.config import S
from core import db as dbmod
from core import stats
from core.errors import APIError, GraphError
from core.security import require_perm, current_user
from core.stats import COUNTERS
from engine.agent_engine import AgentEngine
from memory import backend as memory
from mcp import mcp_client
from routers.api1 import auth, agents, ext
from routers.api2 import chat, rag, mem, users, roles, lt, ws_router

app = FastAPI(title="AI Agent Platform", version="1.0.0")


@app.on_event("startup")
async def startup():
    # 1. DB + 记忆后端
    conn = await dbmod.connect()
    await dbmod.init_db()
    app.state.db = conn
    await memory.init_memory()
    # 2. 引擎
    engine = AgentEngine(app)
    engine.set_conn(conn)
    app.state.engine = engine
    # 3. MCP demo 脚本路径
    mcp_client.set_demo_script(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "mcp", "mcp_server_demo.py"))
    # 4. 种子数据
    from seed import seed
    await seed(conn)


@app.on_event("shutdown")
async def shutdown():
    if getattr(app.state, "db", None):
        await dbmod.close(app.state.db)


# ---------------- 统一错误处理 ----------------
@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    return JSONResponse(status_code=exc.status_code, content=exc.to_json())


@app.exception_handler(GraphError)
async def graph_error_handler(request: Request, exc: GraphError):
    return JSONResponse(status_code=400,
                        content={"code": 400, "message": str(exc), "detail": None})


# ---------------- 健康检查 + 系统 ----------------
@app.get("/healthz")
async def healthz():
    from llm import embedding as emb
    from llm import provider as llm
    llm_ok = None
    try:
        # 轻量探活：不发真实 LLM（healthz 应保持快速）——只校验配置
        llm_ok = bool(S.LLM_API_KEY)
        llm_detail = {"configured": llm_ok, "base_url": S.LLM_BASE_URL, "model": S.LLM_MODEL}
    except Exception as e:
        llm_ok = False
        llm_detail = {"error": str(e)[:100]}
    return {
        "status": "ok",
        "service": "ai-agent-platform",
        "port": S.PORT,
        "db": dbmod.backend_info(),
        "llm": llm_detail,
        "memory_backend": memory.get_memory_backend().info(),
        "embedding": emb.backend_info(),
        "counters": stats.snapshot(),
        "semantic_cache_threshold": S.SEMANTIC_CACHE_THRESHOLD,
    }


@app.get("/api/system/config")
async def sys_config(user: dict = Depends(current_user)):
    """脱敏系统配置（不含任何密钥值）。"""
    return {
        "llm": {"base_url": S.LLM_BASE_URL, "model": S.LLM_MODEL, "key_set": bool(S.LLM_API_KEY)},
        "embedding": {"base_url": S.EMBEDDING_BASE_URL or None, "model": S.EMBEDDING_MODEL or None,
                      "dim": S.EMBEDDING_DIM},
        "memory_backend": S.MEMORY_BACKEND,
        "semantic_cache_threshold": S.SEMANTIC_CACHE_THRESHOLD,
        "max_tool_rounds": S.MAX_TOOL_ROUNDS,
    }


@app.post("/api/system/config")
async def sys_config_update(request: Request, user: dict = Depends(require_perm("system:admin"))):
    """系统配置（模型底座/全局参数）— 运行时热更新（AC-13 可切换 base_url/model）。"""
    body = await request.json()
    changed = []
    if "llm_base_url" in body and body["llm_base_url"]:
        S.LLM_BASE_URL = body["llm_base_url"].rstrip("/")
        changed.append("llm_base_url")
    if "llm_model" in body and body["llm_model"]:
        S.LLM_MODEL = body["llm_model"]
        changed.append("llm_model")
    if "semantic_cache_threshold" in body:
        S.SEMANTIC_CACHE_THRESHOLD = float(body["semantic_cache_threshold"])
        changed.append("semantic_cache_threshold")
    return {"updated": changed,
             "llm": {"base_url": S.LLM_BASE_URL, "model": S.LLM_MODEL}}


# ---------------- 路由挂载 ----------------
app.include_router(auth)
app.include_router(agents)
app.include_router(ext)
app.include_router(chat)
app.include_router(rag)
app.include_router(mem)
app.include_router(users)
app.include_router(roles)
app.include_router(lt)
app.include_router(ws_router)

# 静态前端：放在所有 API/WS 路由之后，catch-all 只兜底未匹配路径
if os.path.isdir(S.STATIC_DIR):
    app.mount("/static", StaticFiles(directory=S.STATIC_DIR), name="static")
    app.mount("/", StaticFiles(directory=S.STATIC_DIR, html=True), name="root")
