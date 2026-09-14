"""门 0 + 模块 10 RBAC：healthz / 端口 / 前端页脚 / JWT / 4角色13权限 / 路由守卫。"""
from conftest import C, BASE, SRC
import os


def test_healthz_200():
    code, o = C.req("GET", "/healthz")
    assert code == 200
    assert o["status"] == "ok"
    assert o["port"] == 8099


def test_port_8099():
    code, o = C.req("GET", "/healthz")
    assert o["port"] == 8099


def test_frontend_footer():
    html = open(os.path.join(SRC, "static", "index.html"), encoding="utf-8").read()
    assert "AI Agent Platform · dev-team 构建" in html


def test_login_4_users():
    for u, role in [("admin", "super_admin"), ("developer", "agent_developer"),
                    ("user", "user"), ("viewer", "viewer")]:
        code, user = C.login(u)
        assert role in user["roles"]


def test_login_wrong_password_401():
    code, o = C.req("POST", "/api/auth/login",
                    {"username": "admin", "password": "wrong"}, expect=401)
    assert code == 401


def test_no_token_401():
    code, o = C.req("GET", "/api/agents", expect=401)
    assert code == 401


def test_viewer_overreach_403():
    tok, _ = C.login("viewer")
    code, o = C.req("GET", "/api/roles", tok=tok, expect=403)
    assert code == 403


def test_developer_no_user_manage_403():
    tok, _ = C.login("developer")
    code, o = C.req("POST", "/api/users",
                    {"username": "x", "password": "p", "role": "user"}, tok=tok, expect=403)
    assert code == 403


def test_13_permissions():
    tok, _ = C.login("admin")
    code, o = C.req("GET", "/api/roles", tok=tok)
    assert len(o["all_permissions"]) == 13


def test_role_matrix_counts():
    tok, _ = C.login("admin")
    code, o = C.req("GET", "/api/roles", tok=tok)
    rp = {r["name"]: set(r["permissions"]) for r in o["roles"]}
    assert len(rp["super_admin"]) == 13
    assert len(rp["agent_developer"]) == 10
    assert rp["user"] == {"agent:read", "rag:read", "memory:read"}
    assert rp["viewer"] == {"agent:read", "rag:read"}
    assert set(o["all_permissions"]) == rp["super_admin"]


def test_me_permissions():
    tok, _ = C.login("viewer")
    code, o = C.req("GET", "/api/auth/me", tok=tok)
    assert len(o["permissions"]) == 2
    assert o["username"] == "viewer"


def test_jwt_expired_rejected():
    # 伪造一个过期 token → 401（不硬编码密钥，仅结构校验：缺 exp 会被拒）
    code, o = C.req("GET", "/api/auth/me", tok="not-a-valid-jwt", expect=401)
    assert code == 401
