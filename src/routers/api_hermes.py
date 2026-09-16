"""/api/hermes/* 路由（TASK-037 / 需求1）：Hermes profile 增删改查 + 状态探测。

权限：profile CRUD 需 `ext:manage` 或 `agent:create`（任务书 §四.2「agent:write 或
ext:manage」——本项目无 agent:write，用 agent:create 承载「创建 hermes agent 前置
profile」语义；GET 用 agent:read 兜底）。统一结构化 JSON；hermes CLI 不可用时
全部返回 503 + 明确提示（AC-H7 双模式兼容）；所有响应/日志已脱敏（AC-H9）。

端点（与 hermes CLI 一一对应）：
  GET    /api/hermes/status          → 前端探测：是否显示 hermes 选项
  GET    /api/hermes/profiles        → `hermes profile list` 解析为 JSON
  POST   /api/hermes/profiles        → create {name, description?, clone_from?}
  GET    /api/hermes/profiles/{name} → `hermes profile show`
  PUT    /api/hermes/profiles/{name} → `hermes profile describe --text`
  DELETE /api/hermes/profiles/{name} → `hermes profile delete -y`
"""
from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel
from core import hermes_cli
from core.errors import APIError
from core.security import current_user, require_perm

hermes = APIRouter(prefix="/api/hermes", tags=["hermes"])

_UNAVAILABLE_MSG = "hermes CLI 不可用（容器未安装/未挂载宿主 ~/.hermes）。hermes 后端功能优雅降级。"


def _has_perm(user: dict, *codes: str) -> bool:
    return any(c in user.get("permissions", []) for c in codes)


def _require(user: dict, *codes: str):
    if not _has_perm(user, *codes):
        raise APIError(403, f"无权限：需要 {'/'.join(codes)}",
                       {"required": list(codes), "username": user.get("username")})
    return user


def _guard_available():
    """CLI 不可用 → 503（AC-H7）。"""
    if not hermes_cli.hermes_available():
        raise APIError(503, _UNAVAILABLE_MSG)


@hermes.get("/status")
async def status(user: dict = Depends(current_user)):
    """前端探测端点（任务书 §四.2「GET /api/hermes/status 供前端判断是否显示
    hermes 选项」）。available=true 时创建页才显示 Hermes Agent 选项（前端属
    TASK-038）；available=false 时 hermes 功能优雅不可用。"""
    return {"available": hermes_cli.hermes_available(),
            "message": None if hermes_cli.hermes_available() else _UNAVAILABLE_MSG}


@hermes.get("/profiles")
async def list_profiles(user: dict = Depends(current_user)):
    _require(user, "agent:read", "ext:manage")
    _guard_available()
    try:
        rows = await hermes_cli.list_profiles()
    except hermes_cli.HermesCLIError as e:
        raise APIError(502, f"hermes profile list 失败: {e}")
    return {"profiles": rows, "count": len(rows)}


class ProfileCreateIn(BaseModel):
    name: str
    description: str | None = None
    clone_from: str | None = None


@hermes.post("/profiles")
async def create_profile(body: ProfileCreateIn, user: dict = Depends(current_user)):
    _require(user, "agent:create", "ext:manage")
    _guard_available()
    try:
        res = await hermes_cli.create_profile(
            body.name, body.description, body.clone_from)
    except ValueError as e:
        raise APIError(400, str(e))
    except hermes_cli.HermesCLIError as e:
        # profile 已存在 / 重名等 → 409 或 400
        msg = str(e)
        if "already exists" in msg.lower() or "exists" in msg.lower():
            raise APIError(409, f"profile 已存在: {body.name}")
        raise APIError(400, f"创建 profile 失败: {msg}")
    return res


@hermes.get("/profiles/{name}")
async def show_profile(name: str, user: dict = Depends(current_user)):
    _require(user, "agent:read", "ext:manage")
    _guard_available()
    try:
        res = await hermes_cli.show_profile(name)
    except ValueError as e:
        raise APIError(400, str(e))
    except hermes_cli.HermesCLIError as e:
        msg = str(e).lower()
        if ("not found" in msg or "no such" in msg or "unknown" in msg
                or "does not exist" in msg):
            raise APIError(404, f"profile 不存在: {name}")
        raise APIError(400, f"读取 profile 失败: {e}")
    return res


class ProfileUpdateIn(BaseModel):
    text: str


@hermes.put("/profiles/{name}")
async def update_profile(name: str, body: ProfileUpdateIn, user: dict = Depends(current_user)):
    _require(user, "agent:update", "ext:manage")
    _guard_available()
    try:
        res = await hermes_cli.update_profile(name, body.text)
    except ValueError as e:
        raise APIError(400, str(e))
    except hermes_cli.HermesCLIError as e:
        raise APIError(400, f"更新 profile 失败: {e}")
    return res


@hermes.delete("/profiles/{name}")
async def delete_profile(name: str, user: dict = Depends(current_user)):
    _require(user, "agent:delete", "ext:manage")
    _guard_available()
    try:
        res = await hermes_cli.delete_profile(name)
    except ValueError as e:
        raise APIError(400, str(e))
    except hermes_cli.HermesCLIError as e:
        raise APIError(400, f"删除 profile 失败: {e}")
    return res
