"""pytest 公共 fixture：对已运行的 8099 服务做真实集成测试。

前置条件：服务已由 `02-development/run.sh` 一键启动（healthz 200）。
本套件为**真实 LLM / 真实 MCP stdio / 真实 WebSocket** 的集成测试，
不 mock——目的是复现 BRIEF 第七节"开发可运行 + 可演示闭环"。

若 8099 未运行，所有测试 skip（避免误判为失败）。
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import pytest

BASE = os.environ.get("AGP_BASE", "http://localhost:8099")
HERE = os.path.dirname(__file__)
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
ENV_PATH = os.path.join(SRC, ".env")


def _service_up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/healthz", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _load_env():
    vals = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def _reset_cache():
    """清 L1 语义缓存 + L0，保证'首跑必走 LLM'断言可重复（幂等前置）。

    按 DB_BACKEND 分派（TASK-015）：
    - sqlite: 直接 aiosqlite 清 src/data/agp.db（默认路径）；
    - postgres: 走 asyncpg 直连 pg-unified（agp schema）清表。
    连不上则跳过（不阻塞测试；缓存残留只影响"首跑走 LLM"类断言的稳定性）。
    """
    import asyncio

    env = _load_env()
    backend = env.get("DB_BACKEND", "sqlite").lower()

    async def _do():
        if backend == "postgres":
            import asyncpg
            dsn = env.get("DB_DSN")
            if not dsn:
                user = env.get("DB_USER", "agp_user")
                pwd = env.get("AGP_DB_PASSWORD", "")
                host = env.get("DB_HOST", "pg-unified")
                port = env.get("DB_PORT", "5432")
                db = env.get("DB_NAME", "postgres")
                dsn = f"postgresql://{user}:{pwd}@{host}:{port}/{db}"
            conn = await asyncpg.connect(dsn, server_settings={"search_path": "agp, public"})
            try:
                for t in ("memory_l1_cache", "memory_l1_image", "memory_l0_raw"):
                    await conn.execute(f"DELETE FROM {t}")
            finally:
                await conn.close()
        else:
            import aiosqlite
            path = env.get("SQLITE_PATH", os.path.join(SRC, "data", "agp.db"))
            if not os.path.isabs(path):
                path = os.path.abspath(path)
            if not os.path.exists(path):
                return
            conn = await aiosqlite.connect(path)
            try:
                for t in ("memory_l1_cache", "memory_l1_image", "memory_l0_raw"):
                    await conn.execute(f"DELETE FROM {t}")
                await conn.commit()
            finally:
                await conn.close()

    try:
        asyncio.run(_do())
    except Exception:
        pass  # DB 不可达不阻塞测试


@pytest.fixture(scope="session", autouse=True)
def server():
    """整个会话只起一次前置检查；不满足则 skip。"""
    if not _service_up():
        pytest.skip("8099 服务未运行。请先执行: cd 02-development && ./run.sh")
    # 会话级缓存重置：让"首跑走 LLM"类断言从干净基线开始
    _reset_cache()
    yield


class C:
    """极简 REST 客户端（urllib，无外部依赖）。"""

    @staticmethod
    def req(method, path, body=None, tok=None, expect=200, base=BASE):
        r = urllib.request.Request(base + path, method=method)
        r.add_header("Content-Type", "application/json")
        if tok:
            r.add_header("Authorization", "Bearer " + tok)
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(r, data=data, timeout=180) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw}

    @staticmethod
    def login(username, password="admin123"):
        code, o = C.req("POST", "/api/auth/login",
                        {"username": username, "password": password})
        assert code == 200, f"login {username} failed: {o}"
        return o["token"], o["user"]
