"""AI Agent Platform — FastAPI 应用入口 (PRD module 6/7/11).

启动：uvicorn core.app:app --port 8099（run.sh 一键起）
- REST /api/* 全接口（auth/agents/chat/rag/memory/users/ext/longtext）
- WS /ws/chat/{agent_id}/{conv_id}（流式 token）
- /healthz 健康检查
- 挂载 static/ 提供纯静态 SPA（DECISION-001）
"""
import json
import os
import time
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
from engine.hermes_adapter import HermesAgentAdapter
from memory import backend as memory
from mcp import mcp_client
from routers.api1 import auth, agents, ext
from routers.api2 import chat, rag, mem, users, roles, lt, ws_router
from routers.api_hermes import hermes as hermes_router

app = FastAPI(title="AI Agent Platform", version="1.0.0")


# ---------------- settings 持久化（TASK-021 / F3，DECISION-007 双后端模式） ----------------
# 9 项白名单：key -> (Settings 属性, 类型)。api_key 类字段只写库、GET 永不回传明文。
SETTING_KEYS = {
    "llm_base_url": ("LLM_BASE_URL", str),
    "llm_model": ("LLM_MODEL", str),
    "llm_api_key": ("LLM_API_KEY", str),
    "llm_timeout": ("LLM_TIMEOUT", float),
    "llm_retries": ("LLM_RETRIES", int),
    "embedding_base_url": ("EMBEDDING_BASE_URL", str),
    "embedding_model": ("EMBEDDING_MODEL", str),
    "embedding_api_key": ("EMBEDDING_API_KEY", str),
    "embedding_dim": ("EMBEDDING_DIM", int),
}


def mask_key(k: str) -> dict:
    """api_key 脱敏：只回 key_set(bool) + 末 4 位（短于 4 位全打码）。"""
    return {"key_set": bool(k), "key_tail": ("" if not k else "****" + k[-4:])}


async def load_settings_from_db(conn):
    """启动加载：DB settings 覆盖 env（优先级 DB > .env，DECISION-007 模式）。"""
    try:
        rows = await dbmod.fetchall(conn, "SELECT key, value FROM settings")
    except Exception as e:
        import logging
        logging.getLogger("core.app").warning("settings 表读取失败（纯 env 配置）: %s", e)
        return
    for r in rows:
        k, raw = r.get("key"), r.get("value")
        if k not in SETTING_KEYS:
            continue
        attr, cast = SETTING_KEYS[k]
        if raw is None:
            continue
        try:
            v = json.loads(raw)
            setattr(S, attr, cast(v) if cast is not str else str(v))
        except Exception:
            pass  # 脏值跳过，保留 env 值


async def persist_setting(conn, key, value):
    """单 key 落库（JSON 标量序列化；sqlite ON CONFLICT / asyncpg 同语义）。"""
    raw = json.dumps(value)
    await dbmod.execute(
        conn,
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, raw))


@app.on_event("startup")
async def startup():
    import logging
    log = logging.getLogger("core.app")
    # 1. DB + 记忆后端（TASK-046: 连接失败在此抛错 → uvicorn 启动失败退出，
    # fail-fast 绝不无限 hang；见 core.db._pg_connect / _sqlite_connect）
    conn = await dbmod.connect()
    await dbmod.init_db()
    app.state.db = conn
    await load_settings_from_db(conn)
    await memory.init_memory()
    # 2. 引擎
    engine = AgentEngine(app)
    engine.set_conn(conn)
    app.state.engine = engine
    # 2b. Hermes 适配器（TASK-037 / 需求3）：hermes 后端对话路由。
    # 与 AgentEngine 并存，按 agent.backend 分派（见 api2.py chat/ws）。
    # CLI 是否可用在运行期逐次探测（AC-H7 双模式兼容），构造不抛错。
    hermes_adapter = HermesAgentAdapter(app)
    hermes_adapter.set_conn(conn)
    app.state.hermes_adapter = hermes_adapter
    # 3. MCP demo 脚本路径
    mcp_client.set_demo_script(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "mcp", "mcp_server_demo.py"))
    # 4. 种子数据
    from seed import seed
    await seed(conn)
    # 5. TASK-046: 明确就绪标记（供 CI 健康判断 / 日志诊断）。
    # 用 print 直出 stdout（uvicorn --log-level info 下 root logger 实际阈值
    # 为 WARNING，logging.info 在容器日志里不可见——就绪标记必须可见）。
    db_info = dbmod.backend_info()
    _db_desc = (db_info.get("path") or
                f"{db_info.get('host')}:{db_info.get('port')}/{db_info.get('database')}")
    print(f"AGP STARTUP OK backend={db_info.get('backend')} db={_db_desc} port={S.PORT}",
          flush=True)


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
    """脱敏系统配置（AC-13 展示 + F3 全字段回显）。

    兼容原有 llm/embedding 结构（前端 dashboard 依赖），另加 settings 段：
    9 项白名单全字段回显；api_key 只回 key_set(bool) + 末 4 位，永不回传明文。
    """
    return {
        "llm": {"base_url": S.LLM_BASE_URL, "model": S.LLM_MODEL, "key_set": bool(S.LLM_API_KEY)},
        "embedding": {"base_url": S.EMBEDDING_BASE_URL or None, "model": S.EMBEDDING_MODEL or None,
                      "dim": S.EMBEDDING_DIM},
        "memory_backend": S.MEMORY_BACKEND,
        "semantic_cache_threshold": S.SEMANTIC_CACHE_THRESHOLD,
        "max_tool_rounds": S.MAX_TOOL_ROUNDS,
        "settings": {
            "llm_base_url": S.LLM_BASE_URL,
            "llm_model": S.LLM_MODEL,
            "llm_api_key": mask_key(S.LLM_API_KEY),
            "llm_timeout": S.LLM_TIMEOUT,
            "llm_retries": S.LLM_RETRIES,
            "embedding_base_url": S.EMBEDDING_BASE_URL or None,
            "embedding_model": S.EMBEDDING_MODEL or None,
            "embedding_api_key": mask_key(S.EMBEDDING_API_KEY),
            "embedding_dim": S.EMBEDDING_DIM,
        },
    }


def _coerce_setting(key: str, val):
    """白名单 key 的类型转换 + 基础校验（非法 -> 400，不落库/不热更）。"""
    attr, cast = SETTING_KEYS[key]
    try:
        if cast is int:
            v = int(val)
            if v <= 0:
                raise ValueError("必须为正整数")
        elif cast is float:
            v = float(val)
            if v <= 0:
                raise ValueError("必须为正数")
        else:
            v = str(val)
            if key == "llm_base_url" and v:
                v = v.rstrip("/")
        if key in ("llm_api_key", "embedding_api_key") and not isinstance(val, str):
            v = str(val)
        return key, v
    except (TypeError, ValueError) as e:
        raise APIError(400, f"字段 {key} 非法: {e}")


@app.post("/api/system/config")
async def sys_config_update(request: Request, user: dict = Depends(require_perm("system:admin"))):
    """系统配置全字段更新（F3：9 项白名单 + 兼容 legacy semantic_cache_threshold）。

    - 提供的字段：类型校验 -> 热更新内存 Settings（即时生效）+ 落库持久化（重启不丢）。
    - 未提供的字段：不变。
    - api_key 写库但 GET 永不回传明文（见 sys_config）。
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise APIError(400, "请求体必须是 JSON 对象")
    conn = app.state.db
    changed = []
    for key, val in body.items():
        if key == "semantic_cache_threshold":  # legacy 字段（非 settings 白名单，仅内存热更）
            try:
                S.SEMANTIC_CACHE_THRESHOLD = float(val)
            except (TypeError, ValueError):
                raise APIError(400, "字段 semantic_cache_threshold 非法: 必须为正数")
            changed.append(key)
            continue
        if key not in SETTING_KEYS:
            raise APIError(400, f"未知配置字段: {key}")
        key, v = _coerce_setting(key, val)
        setattr(S, SETTING_KEYS[key][0], v)          # 热更新（即时生效，与现状一致）
        try:
            await persist_setting(conn, key, v)       # 落库持久化
        except Exception as e:
            import logging
            logging.getLogger("core.app").warning("settings 落库失败（仅内存生效）: %s", e)
        changed.append(key)
        # TASK-029: LLM 设置变更时同步 system-default 行，保持
        # "系统默认 endpoint ≡ S.LLM_* 单值" 不变式（兜底/默认选中项）
        if key in _LLM_SETTING_KEYS:
            try:
                await _sync_system_default(conn)
            except Exception as e:
                import logging
                logging.getLogger("core.app").warning("system-default 同步失败（降级）: %s", e)
    return {"updated": changed,
             "llm": {"base_url": S.LLM_BASE_URL, "model": S.LLM_MODEL},
             "settings": sys_config_view()}


# TASK-029: 影响 system-default 的 LLM 设置 key
_LLM_SETTING_KEYS = {"llm_base_url", "llm_model", "llm_api_key",
                     "llm_timeout", "llm_retries"}


async def _sync_system_default(conn):
    """把 S.LLM_* 单值快照写回 system-default 行（INSERT ON CONFLICT 幂等）。"""
    await dbmod.execute(
        conn,
        """INSERT INTO llm_endpoints (name, base_url, model, api_key, timeout, retries, is_active)
           VALUES (?,?,?,?,?,?,1)
           ON CONFLICT (name) DO UPDATE SET
             base_url=excluded.base_url, model=excluded.model,
             api_key=excluded.api_key, timeout=excluded.timeout,
             retries=excluded.retries, is_active=1, updated_at=excluded.updated_at""",
        ("system-default", S.LLM_BASE_URL, S.LLM_MODEL, S.LLM_API_KEY,
         S.LLM_TIMEOUT, S.LLM_RETRIES))


def sys_config_view() -> dict:
    """settings 白名单脱敏视图（POST 回显复用）。"""
    return {
        "llm_base_url": S.LLM_BASE_URL,
        "llm_model": S.LLM_MODEL,
        "llm_api_key": mask_key(S.LLM_API_KEY),
        "llm_timeout": S.LLM_TIMEOUT,
        "llm_retries": S.LLM_RETRIES,
        "embedding_base_url": S.EMBEDDING_BASE_URL or None,
        "embedding_model": S.EMBEDDING_MODEL or None,
        "embedding_api_key": mask_key(S.EMBEDDING_API_KEY),
        "embedding_dim": S.EMBEDDING_DIM,
    }


# 连通测试超时上限（秒）：test 端点验证"可达性"，避免用户配的 90s 让按钮卡死
_TEST_TIMEOUT = 15.0


@app.post("/api/system/config/test")
async def sys_config_test(request: Request, user: dict = Depends(require_perm("system:admin"))):
    """真实连通性测试（F3"保存前测试"按钮）。

    body: {"target": "llm"|"embedding", 可选覆盖: base_url/model/api_key}
    未覆盖的参数取当前 Settings 值（即"用当前待保存或已保存的参数"）。
    - llm:      GET {base_url}/models（OpenAI 兼容）
    - embedding: POST {base_url}/embeddings（4 字符 input）
    返回 {ok, latency_ms, detail}；失败 detail 含错误摘要，永不 500 崩溃。
    """
    body = await request.json()
    target = (body or {}).get("target")
    if target not in ("llm", "embedding"):
        raise APIError(400, "target 必须是 'llm' 或 'embedding'")
    if target == "llm":
        base = (body.get("base_url") or S.LLM_BASE_URL).rstrip("/")
        model, key = body.get("model"), (body.get("api_key") or S.LLM_API_KEY)
    else:
        base = (body.get("base_url") or S.EMBEDDING_BASE_URL or "").rstrip("/")
        model = body.get("model") or S.EMBEDDING_MODEL
        key = body.get("api_key") or S.EMBEDDING_API_KEY
    if not base:
        return {"ok": False, "latency_ms": 0, "detail": f"{target} 未配置 base_url"}

    import httpx
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            if target == "llm":
                r = await client.get(base + "/models", headers=headers)
                ok = r.status_code < 400
                detail = (f"HTTP {r.status_code}; models: "
                          f"{[m.get('id') for m in r.json().get('data', [])][:5]}") if ok \
                    else f"HTTP {r.status_code}: {r.text[:200]}"
            else:
                r = await client.post(base + "/embeddings",
                                      json={"model": model, "input": "测试"},
                                      headers=headers)
                ok = r.status_code < 400
                detail = (f"HTTP {r.status_code}; dim="
                          f"{len(r.json()['data'][0]['embedding'])}") if ok \
                    else f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
    return {"ok": ok, "latency_ms": int((time.monotonic() - t0) * 1000), "detail": detail}


# ---------------- 路由挂载 ----------------
# TASK-029: llm_endpoints 路由延迟导入（endpoints.py 依赖本模块的
# mask_key/persist_setting，放文件尾避免循环导入）
from routers.endpoints import endpoints as llm_endpoints  # noqa: E402

app.include_router(auth)
app.include_router(agents)
app.include_router(ext)
app.include_router(llm_endpoints)
app.include_router(chat)
app.include_router(rag)
app.include_router(mem)
app.include_router(users)
app.include_router(roles)
app.include_router(lt)
app.include_router(hermes_router)  # TASK-037: /api/hermes/* profile CRUD + status 探测
app.include_router(ws_router)

# 静态前端：放在所有 API/WS 路由之后，catch-all 只兜底未匹配路径
if os.path.isdir(S.STATIC_DIR):
    app.mount("/static", StaticFiles(directory=S.STATIC_DIR), name="static")
    app.mount("/", StaticFiles(directory=S.STATIC_DIR, html=True), name="root")
