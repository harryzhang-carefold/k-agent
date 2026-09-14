"""JWT auth + RBAC route guards (PRD module 10, PD-001/PD-002)."""
import hashlib
import secrets
import time
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from core.config import S
from core.errors import APIError

# 13 permissions (PD-002). chat:use is baseline for all authenticated users, NOT in this list.
PERMISSIONS = [
    ("agent:read", "查看 agent"),
    ("agent:create", "创建 agent"),
    ("agent:update", "修改 agent"),
    ("agent:delete", "删除 agent"),
    ("rag:read", "查看/检索知识库"),
    ("rag:write", "上传/添加文档"),
    ("rag:delete", "删除文档/知识库"),
    ("memory:read", "查看记忆"),
    ("memory:write", "写入/管理记忆"),
    ("ext:manage", "管理 MCP/Skills/Plugins"),
    ("user:manage", "管理用户"),
    ("role:manage", "管理角色/授权"),
    ("system:admin", "系统配置（模型底座/全局参数）"),
]

ROLE_MATRIX = {
    "super_admin": [c for c, _ in PERMISSIONS],  # all 13
    "agent_developer": [
        "agent:read", "agent:create", "agent:update", "agent:delete",
        "rag:read", "rag:write", "rag:delete",
        "memory:read", "memory:write",
        "ext:manage",
    ],  # 10
    "user": ["agent:read", "rag:read", "memory:read"],  # 3
    "viewer": ["agent:read", "rag:read"],  # 2
}

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    return f"pbkdf2$100000${salt}${dk}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters)).hex()
        return secrets.compare_digest(calc, dk)
    except Exception:
        return False


def create_token(user_id: int, username: str, roles: list[str]) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "roles": roles,
        "iat": int(time.time()),
        "exp": int(time.time()) + S.JWT_TTL_HOURS * 3600,
    }
    return jwt.encode(payload, S.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, S.JWT_SECRET, algorithms=["HS256"])


async def _permissions_for(conn, user_id: int) -> list[str]:
    from core import db
    rows = await db.fetchall(
        conn,
        """SELECT DISTINCT p.code FROM permissions p
           JOIN role_permissions rp ON rp.permission_code = p.code
           JOIN user_roles ur ON ur.role_id = rp.role_id
           WHERE ur.user_id = ?""",
        (user_id,),
    )
    return [r["code"] for r in rows]


async def current_user(request: Request,
                       credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    if credentials is None:
        raise APIError(401, "未认证：缺少 Bearer token")
    try:
        payload = decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise APIError(401, "token 已过期")
    except jwt.InvalidTokenError:
        raise APIError(401, "无效 token")
    conn = request.app.state.db
    from core import db as _db
    row = await _db.fetchone(conn, "SELECT id, username FROM users WHERE id=?", (int(payload["sub"]),))
    if row is None:
        raise APIError(401, "用户不存在")
    user = {
        "id": row["id"],
        "username": row["username"],
        "roles": payload.get("roles", []),
        "permissions": await _permissions_for(conn, row["id"]),
    }
    return user


def require_perm(code: str):
    async def dep(request: Request, user: dict = Depends(current_user)) -> dict:
        if code not in user["permissions"]:
            raise APIError(403, f"无权限：需要 {code}", {"required": code, "username": user["username"]})
        return user
    return dep
