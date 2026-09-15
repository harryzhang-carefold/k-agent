"""TASK-021 (F3 后端) settings 持久化 + config 全字段 + /config/test 连通测试。

自包含：不起 8099 线上服务（那是旧镜像）；在本文件内用 TestClient 起独立 app 实例，
SQLite 库放 05-temp/t021/（铁律：临时文件禁止 /tmp），并用本地 mock LLM/embedding
server（http.server 线程）验证 /config/test 的 ok/fail 两分支。

覆盖（任务书交付要求）：
- 全字段更新（9 白名单 + 兼容 legacy semantic_cache_threshold）
- key 脱敏回显（key_set + 末 4 位，永不回传明文）
- test 端点 ok / fail 两分支（真实 HTTP 请求，非 mock 函数）
- settings 落库 + 重启后 DB 覆盖 env（优先级 DB > .env）
- RBAC：无 token 401 / viewer 403 / admin 200
- 双后端 DDL：sqlite schema 幂等（executescript 两遍）；PG 建表 SQL 走 pg-unified 隔离验证
  （PG 运行时验证见 05-temp/t021/pg_selftest.sh，pytest 层只保证 sqlite 链路 + DDL 等价）
"""
import json
import os
import re
import shutil
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
TEMP = os.path.abspath(os.path.join(HERE, "..", "..", "05-temp", "t021"))
os.makedirs(TEMP, exist_ok=True)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# 独立 SQLite 库（05-temp，非 /tmp；每个进程一份避免并行互扰）
DB_PATH = os.path.join(TEMP, f"test_config_db_{os.getpid()}.db")


# ---------------- 本地 mock LLM/embedding server（OpenAI 兼容） ----------------
class _MockHandler(BaseHTTPRequestHandler):
    auth_required = False
    good_key = "sk-test-good"

    def _check(self):
        if self.server.auth_required and \
                self.headers.get("Authorization") != f"Bearer {self.server.good_key}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "bad key"}')
            return False
        return True

    def do_GET(self):
        if not self._check():
            return
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({"data": [{"id": "mock-llm"}, {"id": "mock-llm-2"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self._check():
            return
        if self.path.rstrip("/") == "/v1/embeddings":
            body = json.dumps({"data": [{"index": 0, "embedding": [0.1] * 512}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):  # 静默
        pass


@pytest.fixture()
def mock_server():
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    srv.auth_required = False
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


# ---------------- app 实例 fixture（独立 SQLite + 05-temp 库） ----------------
@pytest.fixture()
def app_env(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", DB_PATH)
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")  # 默认不可达（fail 分支）
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("LLM_API_KEY", "env-key-secret-9999")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("EMBEDDING_MODEL", "")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_DIM", "512")

    for f in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(f):
            os.unlink(f)

    # 清掉可能残留的单例连接（db.py 是模块级全局；上一用例的 TestClient 已 shutdown，
    # 连接已关闭，直接置 None 即可）
    import core.db as dbmod
    dbmod._SQLITE_CONN = None
    dbmod._POOL = None

    # 关键：core.config.S 是被各模块 `from core.config import S` 共享的**同一对象**，
    # 且只在建模块时从 env 求值一次——monkeypatch.setenv 对已建的 S 无效。
    # 全量套件里 core.config 会被更早导入（读到 .env 的 DB_BACKEND=postgres），
    # 所以必须**就地**把后端分派 + 9 白名单 + legacy 字段全部还原到测试基准，
    # 保证 fixture 与导入顺序无关（否则误连 asyncpg/pg-unified，closed-loop 报错）。
    from core.config import S
    S.DB_BACKEND = "sqlite"
    S.SQLITE_PATH = DB_PATH
    S.LLM_BASE_URL = "http://127.0.0.1:1/v1"
    S.LLM_MODEL = "env-model"
    S.LLM_API_KEY = "env-key-secret-9999"
    S.LLM_TIMEOUT = 90.0
    S.LLM_RETRIES = 3
    S.EMBEDDING_BASE_URL = ""
    S.EMBEDDING_MODEL = ""
    S.EMBEDDING_API_KEY = ""
    S.EMBEDDING_DIM = 512
    S.SEMANTIC_CACHE_THRESHOLD = 0.95

    from fastapi.testclient import TestClient
    from core import app as appmod
    with TestClient(appmod.app) as client:
        yield client, S
    # shutdown 已在 with 退出时执行；再清单例保证下一用例干净
    dbmod._SQLITE_CONN = None
    dbmod._POOL = None
    if os.path.exists(DB_PATH):
        os.unlink(DB_PATH)


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _adm(client):
    return _login(client, "admin")


def _viewer(client):
    return _login(client, "viewer")


# ================= 1. GET 脱敏回显 =================

def test_get_config_no_token_401(app_env):
    client, _ = app_env
    r = client.get("/api/system/config")
    assert r.status_code == 401


def test_get_config_masking(app_env):
    client, S = app_env
    r = client.get("/api/system/config", headers={"Authorization": f"Bearer {_viewer(client)}"})
    assert r.status_code == 200
    body = r.json()
    st = body["settings"]
    # 兼容旧结构
    assert body["llm"]["key_set"] is True
    assert body["llm"]["model"] == "env-model"
    # 9 项全字段回显
    assert set(st.keys()) == {
        "llm_base_url", "llm_model", "llm_api_key", "llm_timeout", "llm_retries",
        "embedding_base_url", "embedding_model", "embedding_api_key", "embedding_dim"}
    # api_key 永不回传明文
    lk, ek = st["llm_api_key"], st["embedding_api_key"]
    assert lk == {"key_set": True, "key_tail": "****9999"}
    assert ek == {"key_set": False, "key_tail": ""}
    for v in json.dumps(body).split('"'):
        assert "env-key-secret-9999" not in v
    # 其余字段与 Settings 一致
    assert st["llm_base_url"] == S.LLM_BASE_URL
    assert st["embedding_dim"] == S.EMBEDDING_DIM


# ================= 2. POST 全字段更新 =================

def test_post_config_viewer_403(app_env):
    client, _ = app_env
    r = client.post("/api/system/config", json={"llm_model": "x"},
                    headers={"Authorization": f"Bearer {_viewer(client)}"})
    assert r.status_code == 403


def test_post_config_all_fields(app_env):
    client, S = app_env
    r = client.post("/api/system/config", json={
        "llm_base_url": "http://new-host:8000/v1/",
        "llm_model": "new-model",
        "llm_api_key": "new-key-1234",
        "llm_timeout": 30,
        "llm_retries": 2,
        "embedding_base_url": "http://emb-host:9000/v1",
        "embedding_model": "emb-model",
        "embedding_api_key": "emb-key-abcd",
        "embedding_dim": 256,
    }, headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["updated"]) == set({
        "llm_base_url", "llm_model", "llm_api_key", "llm_timeout", "llm_retries",
        "embedding_base_url", "embedding_model", "embedding_api_key", "embedding_dim"})
    # 热更新即时生效（内存 Settings）
    assert S.LLM_MODEL == "new-model"
    assert S.LLM_BASE_URL == "http://new-host:8000/v1"      # rstrip('/')
    assert S.LLM_TIMEOUT == 30.0
    assert S.LLM_RETRIES == 2
    assert S.EMBEDDING_DIM == 256
    assert S.EMBEDDING_API_KEY == "emb-key-abcd"
    # 回显脱敏
    assert body["settings"]["llm_api_key"] == {"key_set": True, "key_tail": "****1234"}
    assert body["settings"]["embedding_api_key"] == {"key_set": True, "key_tail": "****abcd"}
    # 明文不回传
    raw = r.text
    assert "new-key-1234" not in raw and "emb-key-abcd" not in raw


def test_post_config_partial_keeps_rest(app_env):
    client, S = app_env
    r = client.post("/api/system/config", json={"llm_model": "partial-model"},
                    headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 200
    assert r.json()["updated"] == ["llm_model"]
    assert S.LLM_MODEL == "partial-model"
    assert S.LLM_BASE_URL == "http://127.0.0.1:1/v1"  # 未提供字段不变


def test_post_config_unknown_field_400(app_env):
    client, _ = app_env
    r = client.post("/api/system/config", json={"bogus_field": 1},
                    headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 400


def test_post_config_bad_type_400(app_env):
    client, S = app_env
    r = client.post("/api/system/config", json={"llm_retries": "abc"},
                    headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 400
    assert S.LLM_RETRIES == 3  # 未生效


def test_post_config_legacy_threshold(app_env):
    client, S = app_env
    r = client.post("/api/system/config", json={"semantic_cache_threshold": 0.8},
                    headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 200
    assert S.SEMANTIC_CACHE_THRESHOLD == 0.8
    assert r.json()["updated"] == ["semantic_cache_threshold"]


# ================= 3. /config/test 两分支（真实 HTTP） =================

def test_config_test_llm_ok(app_env, mock_server):
    client, _ = app_env
    r = client.post("/api/system/config/test", json={
        "target": "llm", "base_url": mock_server},
        headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    assert "mock-llm" in body["detail"]


def test_config_test_embedding_ok(app_env, mock_server):
    client, _ = app_env
    r = client.post("/api/system/config/test", json={
        "target": "embedding", "base_url": mock_server, "model": "emb-model"},
        headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "dim=512" in body["detail"]


def test_config_test_fail_no_collapse(app_env):
    """不可达 base_url -> ok=false + 错误摘要，永不 500。"""
    client, _ = app_env
    r = client.post("/api/system/config/test", json={"target": "llm"},
                    headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["detail"]  # 错误摘要非空


def test_config_test_embedding_unconfigured(app_env):
    client, _ = app_env
    r = client.post("/api/system/config/test", json={"target": "embedding"},
                    headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "base_url" in r.json()["detail"]


def test_config_test_bad_target_400(app_env):
    client, _ = app_env
    r = client.post("/api/system/config/test", json={"target": "rag"},
                    headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 400


def test_config_test_rbac(app_env):
    client, _ = app_env
    r = client.post("/api/system/config/test", json={"target": "llm"})
    assert r.status_code == 401
    r = client.post("/api/system/config/test", json={"target": "llm"},
                    headers={"Authorization": f"Bearer {_viewer(client)}"})
    assert r.status_code == 403


# ================= 4. 落库持久化 + 重启加载（DB > env） =================

def test_settings_persist_and_reload(app_env, mock_server):
    """POST 写库 -> 新 app 实例（同库）启动后 DB 值覆盖 env（优先级 DB > .env）。"""
    client, S = app_env
    r = client.post("/api/system/config", json={
        "llm_base_url": mock_server, "llm_model": "persisted-model",
        "llm_api_key": "persisted-key-7777"},
        headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 200

    # 直读 SQLite 验证落库（本次写了 3 个 key，updated_at 列存在）
    import sqlite3
    con = sqlite3.connect(DB_PATH)
    rows = {k: v for k, v in con.execute("SELECT key, value FROM settings")}
    colnames = [c[1] for c in con.execute("PRAGMA table_info(settings)")]
    con.close()
    assert rows["llm_model"] == '"persisted-model"'
    assert rows["llm_base_url"] == json.dumps(mock_server)
    assert json.loads(rows["llm_api_key"]) == "persisted-key-7777"
    assert colnames == ["key", "value", "updated_at"]

    # ---- 重启：同库新 app 实例，env 仍是旧值，DB 应覆盖 ----
    # 模拟重启：关闭 DB 单例 + 把 S 就地还原到 env 基准（与 app_env 相同），
    # 再进 TestClient 触发 startup -> init_db + load_settings_from_db（DB 值生效）。
    import core.db as dbmod
    dbmod._SQLITE_CONN = None
    dbmod._POOL = None
    from core.config import S
    S.DB_BACKEND = "sqlite"
    S.SQLITE_PATH = DB_PATH
    S.LLM_BASE_URL = "http://127.0.0.1:1/v1"
    S.LLM_MODEL = "env-model"
    S.LLM_API_KEY = "env-key-secret-9999"
    S.LLM_TIMEOUT = 90.0
    S.LLM_RETRIES = 3
    S.EMBEDDING_BASE_URL = ""
    S.EMBEDDING_MODEL = ""
    S.EMBEDDING_API_KEY = ""
    S.EMBEDDING_DIM = 512
    # env 基准值确认（重启前）
    assert S.LLM_MODEL == "env-model"

    from fastapi.testclient import TestClient
    from core import app as appmod
    with TestClient(appmod.app) as client2:
        r2 = client2.get("/api/system/config",
                         headers={"Authorization": f"Bearer {_login(client2, 'viewer')}"})
        assert r2.status_code == 200
        st2 = r2.json()["settings"]
        # DB 覆盖 env：persisted 值生效
        assert st2["llm_model"] == "persisted-model"
        assert st2["llm_base_url"] == mock_server
        # api_key 脱敏回显（末 4 位）
        assert st2["llm_api_key"] == {"key_set": True, "key_tail": "****7777"}
        # 未写过的字段仍取 env
        assert st2["llm_timeout"] == 90.0


# ================= 5. 双后端 DDL 等价 =================

def test_sqlite_schema_idempotent():
    """schema_sqlite.sql 含 settings 表且 executescript 幂等（两遍不报错）。"""
    schema = os.path.join(SRC, "core", "schema_sqlite.sql")
    sql = open(schema, encoding="utf-8").read()
    assert re.search(r"CREATE TABLE IF NOT EXISTS settings \(", sql)
    import aiosqlite, asyncio, os as _os
    p = os.path.join(TEMP, f"schema_idem_{os.getpid()}.db")
    for f in (p, p + "-wal", p + "-shm"):
        if _os.path.exists(f):
            _os.unlink(f)

    async def _run():
        conn = await aiosqlite.connect(p)
        await conn.executescript(sql)
        await conn.commit()
        await conn.executescript(sql)   # 第二遍：幂等
        await conn.commit()
        cols = await conn.execute("PRAGMA table_info(settings)")
        names = [r[1] for r in await cols.fetchall()]
        await conn.close()
        return names

    names = asyncio.run(_run())
    assert names == ["key", "value", "updated_at"]
    for f in (p, p + "-wal", p + "-shm"):
        if os.path.exists(f):
            os.unlink(f)


def test_pg_ddl_matches_sqlite_semantics():
    """PG 建表 SQL（db._pg_init 内联）与 sqlite settings 表同构：key PK / value / updated_at。

    PG 运行时验证（真实建表 + 读写）由 05-temp/t021/pg_selftest.sh 完成；
    此处做 DDL 结构断言，防双端漂移。
    """
    import core.db as dbmod
    src = open(os.path.join(SRC, "core", "db.py"), encoding="utf-8").read()
    # DDL 由字符串字面量拼接（多行），按标记截取窗口断言，防双端漂移
    idx = src.index("CREATE TABLE IF NOT EXISTS settings")
    ddl = src[idx:idx + 600]
    assert "key TEXT PRIMARY KEY" in ddl
    assert "value TEXT NOT NULL" in ddl
    assert "updated_at TEXT" in ddl
    assert "to_char(now()" in ddl  # PG 时间戳默认（UTC 格式，与 sqlite datetime('now') 一致）
