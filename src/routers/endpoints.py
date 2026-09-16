"""LLM endpoint 多维护 (TASK-029 / 需求1).

- 新表 llm_endpoints（双后端幂等建表：schema_sqlite.sql / db.py _pg_init）。
- GET/POST/PUT/DELETE /api/llm-endpoints + POST /api/llm-endpoints/test
  + POST /api/llm-endpoints/{name}/set-default。
- 写操作需 system:admin（参照 sys_config_update）；GET 登录即可
  （Agent 表单模型下拉需要列 enabled 端点）。
- api_key 永不回明文：只回 key_set + 末 4 位（复用 mask_key，DECISION-004）。
- 系统默认 endpoint（固定名 system-default）：seed 幂等补建（seed.py），
  与 S.LLM_* 保持一致（app.py sys_config_update 联动同步），禁止删除（409），
  避免清空后无可用模型。Agent 的 model 字段存 endpoint **name**。
"""
import time
from fastapi import APIRouter, Request, Depends
from core import db
from core.config import S
from core.errors import APIError
from core.security import require_perm, current_user
from core.app import mask_key, persist_setting

endpoints = APIRouter(prefix="/api/llm-endpoints", tags=["llm-endpoints"])

DEFAULT_NAME = "system-default"

# 连通测试超时上限（秒）：与 /api/system/config/test 一致
_TEST_TIMEOUT = 15.0

_FIELDS = ("base_url", "model", "api_key", "timeout", "retries", "is_active")


def _view(row: dict) -> dict:
    """脱敏视图：api_key 只回 key_set + 末 4 位，永不回明文。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "model": row["model"],
        "key_set": bool(row.get("api_key")),
        "key_tail": ("" if not row.get("api_key") else "****" + row["api_key"][-4:]),
        "timeout": row["timeout"],
        "retries": row["retries"],
        "is_active": bool(row["is_active"]),
        "is_system_default": row["name"] == DEFAULT_NAME,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def _get_row(conn, name) -> dict:
    row = await db.fetchone(conn, "SELECT * FROM llm_endpoints WHERE name=?", (name,))
    if not row:
        raise APIError(404, f"endpoint 不存在: {name}")
    return row


def _coerce(body: dict, partial: bool) -> dict:
    """字段校验/类型转换。partial=True 时（PUT）未提供的字段不动。"""
    out = {}
    if "name" in body:
        name = str(body["name"] or "").strip()
        if not name:
            raise APIError(400, "name 必填")
        if any(c in name for c in " \t\n/\\"):
            raise APIError(400, "name 不能含空白或 / \\")
        out["name"] = name
    if "base_url" in body:
        v = str(body["base_url"] or "").strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise APIError(400, "base_url 必须以 http:// 或 https:// 开头")
        out["base_url"] = v.rstrip("/")
    if "model" in body:
        v = str(body["model"] or "").strip()
        if not v:
            raise APIError(400, "model 必填")
        out["model"] = v
    if "api_key" in body:
        v = body["api_key"]
        out["api_key"] = "" if v is None else str(v)
    if "timeout" in body:
        try:
            v = float(body["timeout"])
            if v <= 0:
                raise ValueError("必须为正数")
            out["timeout"] = v
        except (TypeError, ValueError) as e:
            raise APIError(400, f"timeout 非法: {e}")
    if "retries" in body:
        try:
            v = int(body["retries"])
            if v <= 0:
                raise ValueError("必须为正整数")
            out["retries"] = v
        except (TypeError, ValueError) as e:
            raise APIError(400, f"retries 非法: {e}")
    if "is_active" in body:
        out["is_active"] = 1 if body["is_active"] else 0
    return out


@endpoints.get("")
async def list_endpoints(request: Request, user: dict = Depends(current_user)):
    """全部 endpoint（脱敏）。Agent 表单按 is_active 过滤。"""
    rows = await db.fetchall(request.app.state.db,
                             "SELECT * FROM llm_endpoints ORDER BY id")
    return {"endpoints": [_view(r) for r in rows],
            "default_name": DEFAULT_NAME}


@endpoints.post("")
async def create_endpoint(request: Request,
                          user: dict = Depends(require_perm("system:admin"))):
    body = await request.json()
    if not isinstance(body, dict):
        raise APIError(400, "请求体必须是 JSON 对象")
    f = _coerce(body, partial=False)
    if "name" not in f or "base_url" not in f or "model" not in f:
        raise APIError(400, "name / base_url / model 必填")
    conn = request.app.state.db
    if await db.fetchone(conn, "SELECT id FROM llm_endpoints WHERE name=?", (f["name"],)):
        raise APIError(409, f"endpoint 名称已存在: {f['name']}")
    row_id = await db.execute(
        conn,
        """INSERT INTO llm_endpoints (name, base_url, model, api_key, timeout, retries, is_active)
           VALUES (?,?,?,?,?,?,?)""",
        (f["name"], f["base_url"], f["model"], f.get("api_key", "") or S.LLM_API_KEY,
         f.get("timeout", 90.0), f.get("retries", 3), f.get("is_active", 0)))
    row = await db.fetchone(conn, "SELECT * FROM llm_endpoints WHERE id=?", (row_id,))
    if not row:
        raise APIError(500, "endpoint 创建后读取失败")
    return _view(row)


@endpoints.put("/{name}")
async def update_endpoint(name: str, request: Request,
                          user: dict = Depends(require_perm("system:admin"))):
    """部分更新：只改提供的字段；api_key 留空/未提供 = 不改动已存 key。"""
    body = await request.json()
    if not isinstance(body, dict):
        raise APIError(400, "请求体必须是 JSON 对象")
    f = _coerce(body, partial=True)
    conn = request.app.state.db
    row = await _get_row(conn, name)
    if "name" in f and f["name"] != name:
        raise APIError(400, "不支持改名（name 是 Agent 绑定标识，用 DELETE+POST 重建）")
    if "api_key" in f and f["api_key"] == "":
        f.pop("api_key")  # 留空 = 不改动
    if "name" in f:
        f.pop("name")     # 不支持改名（见上），剩余字段即待更新集
    if not f:
        raise APIError(400, "没有可更新的字段")
    sets = ", ".join(f"{k}=?" for k in f)
    params = list(f.values()) + [name]
    await db.execute(
        conn,
        (f"UPDATE llm_endpoints SET {sets}, "
         f"updated_at=datetime('now') WHERE name=?"),
        params)
    return _view(await _get_row(conn, name))


@endpoints.delete("/{name}")
async def delete_endpoint(name: str, request: Request,
                          user: dict = Depends(require_perm("system:admin"))):
    if name == DEFAULT_NAME:
        raise APIError(409, "系统默认 endpoint 不可删除（兜底/默认选中项）")
    conn = request.app.state.db
    await _get_row(conn, name)
    await db.execute(conn, "DELETE FROM llm_endpoints WHERE name=?", (name,))
    return {"deleted": name}


@endpoints.post("/test")
async def test_endpoint(request: Request,
                        user: dict = Depends(require_perm("system:admin"))):
    """连通性测试（复用 sys_config_test 逻辑：GET {base_url}/models，15s 上限，失败不 500）。

    body: {"name": <已存 endpoint>} 或 {"base_url", "api_key"(可选), "model"(可选)}
    未提供参数时 name 必填（用已存值测）。
    """
    body = await request.json() or {}
    name = body.get("name")
    if name:
        row = await _get_row(request.app.state.db, name)
        base = row["base_url"].rstrip("/")
        key = row.get("api_key") or S.LLM_API_KEY  # 与引擎同语义：空 key 回退共享 key
    else:
        base = str(body.get("base_url") or "").strip().rstrip("/")
        key = str(body.get("api_key") or "")
    if not base:
        return {"ok": False, "latency_ms": 0, "detail": "未提供 endpoint name 或 base_url"}

    import httpx
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            r = await client.get(base + "/models", headers=headers)
            ok = r.status_code < 400
            detail = (f"HTTP {r.status_code}; models: "
                      f"{[m.get('id') for m in r.json().get('data', [])][:5]}") if ok \
                else f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
    return {"ok": ok, "latency_ms": int((time.monotonic() - t0) * 1000), "detail": detail}


@endpoints.post("/{name}/set-default")
async def set_default_endpoint(name: str, request: Request,
                               user: dict = Depends(require_perm("system:admin"))):
    """设为系统默认：endpoint 值同步到 S.LLM_*（热更+落库）+ system-default 行，
    并启用该 endpoint（is_active=1）。与 app.py sys_config_update 的联动规则一致：
    system-default 行始终 ≡ S.LLM_* 单值。"""
    conn = request.app.state.db
    row = await _get_row(conn, name)
    # 1. 热更内存 Settings + 落库（重启不丢，DB>.env）
    pairs = {
        "LLM_BASE_URL": row["base_url"], "LLM_MODEL": row["model"],
        "LLM_API_KEY": row.get("api_key") or "",
        "LLM_TIMEOUT": float(row["timeout"]), "LLM_RETRIES": int(row["retries"]),
    }
    for attr, v in pairs.items():
        setattr(S, attr, v)
        try:
            await persist_setting(conn, _SETTING_KEY[attr], v)
        except Exception:
            import logging
            logging.getLogger("routers.endpoints").warning(
                "set-default 落库失败（仅内存生效）: %s", attr)
    # 2. system-default 行同步（兜底/默认选中项 ≡ S.LLM_*）
    await db.execute(
        conn,
        """INSERT INTO llm_endpoints (name, base_url, model, api_key, timeout, retries, is_active)
           VALUES (?,?,?,?,?,?,1)
           ON CONFLICT (name) DO UPDATE SET
             base_url=excluded.base_url, model=excluded.model,
             api_key=excluded.api_key, timeout=excluded.timeout,
             retries=excluded.retries, is_active=1, updated_at=excluded.updated_at""",
        (DEFAULT_NAME, row["base_url"], row["model"], row.get("api_key") or "",
         float(row["timeout"]), int(row["retries"])))
    # 3. 该 endpoint 启用
    await db.execute(conn, "UPDATE llm_endpoints SET is_active=1 WHERE name=?", (name,))
    return {"ok": True, "default_name": DEFAULT_NAME,
            "llm": {"base_url": S.LLM_BASE_URL, "model": S.LLM_MODEL}}


# S.LLM_* 设置 key 映射（供 set-default 落库复用 persist_setting 白名单）
_SETTING_KEY = {
    "LLM_BASE_URL": "llm_base_url", "LLM_MODEL": "llm_model",
    "LLM_API_KEY": "llm_api_key", "LLM_TIMEOUT": "llm_timeout",
    "LLM_RETRIES": "llm_retries",
}
