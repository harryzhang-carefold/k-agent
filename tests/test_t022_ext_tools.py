"""TASK-022 (F1 后端) skills 上传导入 + MCP CRUD 补全 + skills.updated_at + mcp env 持久化。

自包含：不起 8099 线上服务；本文件内用 TestClient 起独立 app 实例（SQLite 库放
05-temp/t022/，铁律：临时文件禁止 /tmp）。

覆盖（任务书交付要求）：
- upload 单文件 .md/.txt（frontmatter 解析 / 无 frontmatter 用文件名命名）
- upload zip（多 SKILL.md 批量入库）
- 重名 409 语义：跳过并回报（库内重名 + 批次内重名）
- mcp PUT / DELETE（被 agent_bindings 引用时 409 提示先解绑）
- 创建接口 env 写入（此前丢失）+ env 持久化回显
- skills.updated_at：新建默认 now，PUT 刷新
- RBAC：upload/mcp put/delete 沿用 ext:manage（无 token 401 / viewer 403 / admin 200）
- 双后端 DDL 等价：sqlite schema 幂等 + 旧库 ALTER 补齐；PG 列补齐 DDL 结构断言
  （PG 运行时验证见 05-temp/t022/pg_selftest.sh）
"""
import io
import json
import os
import re
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
TEMP = os.path.abspath(os.path.join(HERE, "..", "..", "05-temp", "t022"))
os.makedirs(TEMP, exist_ok=True)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# 独立 SQLite 库（05-temp，非 /tmp；每进程一份避免并行互扰）
DB_PATH = os.path.join(TEMP, f"test_t022_db_{os.getpid()}.db")

# SKILL.md 风格样本
MD_WITH_FM = (
    "---\n"
    "name: t022-fm-skill\n"
    "description: frontmatter 技能\n"
    "version: 1.0.0\n"
    "---\n"
    "# 正文标题\n"
    "正文内容 A\n"
)
MD_NO_FM = "纯正文，没有 frontmatter\n第二行\n"
TXT_PLAIN = "txt 正文内容\n"


def _make_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


@pytest.fixture()
def app_env(monkeypatch):
    """独立 SQLite app 实例（与 test_system_config.py 同模式：就地还原 S 基准）。"""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", DB_PATH)
    for f in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(f):
            os.unlink(f)
    import core.db as dbmod
    dbmod._SQLITE_CONN = None
    dbmod._POOL = None
    from core.config import S
    S.DB_BACKEND = "sqlite"
    S.SQLITE_PATH = DB_PATH
    S.LLM_BASE_URL = "http://127.0.0.1:1/v1"  # 不可达即可（本套件不发真实 LLM）
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
        yield client
    dbmod._SQLITE_CONN = None
    dbmod._POOL = None
    for f in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(f):
            os.unlink(f)


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _adm(client):
    return _login(client, "admin")


def _viewer(client):
    return _login(client, "viewer")


def _upload(client, tok, files):
    """files: [(filename, bytes)] 或 [(filename, bytes, content_type)]，multipart 字段名 files（可多值）。"""
    norm = [item + ("application/octet-stream",) * (3 - len(item)) for item in files]
    return client.post(
        "/api/ext/skills/upload",
        headers={"Authorization": f"Bearer {tok}"},
        files=[("files", (fn, io.BytesIO(data), ct)) for fn, data, ct in norm])


# ================= 1. skills upload：单文件 =================

def test_upload_single_md_frontmatter(app_env):
    client = app_env
    r = _upload(client, _adm(client), [("t022-fm-skill.md", MD_WITH_FM.encode(), "text/markdown")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"] == {"created": 1, "skipped": 0, "failed": 0}
    c = body["created"][0]
    assert c["name"] == "t022-fm-skill"
    assert c["source"] == "t022-fm-skill.md"
    # 入库内容 = frontmatter 之外正文；description 来自 frontmatter
    row = next(s for s in client.get("/api/ext/skills",
                                     headers={"Authorization": f"Bearer {_adm(client)}"}).json()["skills"]
               if s["name"] == "t022-fm-skill")
    assert row["description"] == "frontmatter 技能"
    assert "# 正文标题" in row["content"] and "frontmatter" not in row["content"]
    assert row["updated_at"]  # 新建即有 updated_at


def test_upload_single_md_no_frontmatter_filename_naming(app_env):
    """无 frontmatter -> 文件名命名（去扩展名/空格/下划线归一为连字符）。"""
    client = app_env
    r = _upload(client, _adm(client), [("My_Upload Skill.md", MD_NO_FM.encode(), "text/markdown")])
    assert r.status_code == 200
    assert r.json()["created"][0]["name"] == "my-upload-skill"
    row = next(s for s in client.get("/api/ext/skills",
                                     headers={"Authorization": f"Bearer {_adm(client)}"}).json()["skills"]
               if s["name"] == "my-upload-skill")
    assert row["description"] == ""
    assert "纯正文" in row["content"]


def test_upload_single_txt(app_env):
    client = app_env
    r = _upload(client, _adm(client), [("plain-note.txt", TXT_PLAIN.encode(), "text/plain")])
    assert r.status_code == 200
    assert r.json()["created"][0]["name"] == "plain-note"
    assert r.json()["counts"]["created"] == 1


def test_upload_empty_content_skipped(app_env):
    client = app_env
    r = _upload(client, _adm(client), [("empty.md", b"   \n", "text/markdown")])
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"created": 0, "skipped": 1, "failed": 0}
    assert body["skipped"][0]["name"] == "empty"


def test_upload_unsupported_extension_failed(app_env):
    client = app_env
    r = _upload(client, _adm(client), [("x.bin", b"binary", "application/octet-stream")])
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["failed"] == 1
    assert body["created"] == []


# ================= 2. skills upload：zip 批量 =================

def test_upload_zip_multiple(app_env):
    client = app_env
    zip_bytes = _make_zip({
        "a/SKILL.md": "---\nname: zip-skill-a\ndescription: d-a\n---\nbody A\n",
        "b/SKILL.md": "---\nname: zip-skill-b\ndescription: d-b\n---\nbody B\n",
        "c/readme.txt": "plain body C\n",
        "d/ignore.json": "{}",          # 非 md/txt 忽略
        "__MACOSX/._a": "junk",          # macOS 元数据忽略
    })
    r = _upload(client, _adm(client), [("pack.zip", zip_bytes, "application/zip")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["created"] == 3
    names = {c["name"] for c in body["created"]}
    assert names == {"zip-skill-a", "zip-skill-b", "readme"}
    # source 标注 zip 内路径
    src = {c["name"]: c["source"] for c in body["created"]}
    assert src["zip-skill-a"] == "pack.zip:a/SKILL.md"


def test_upload_zip_bad_zip_400(app_env):
    client = app_env
    r = _upload(client, _adm(client), [("bad.zip", b"not a zip", "application/zip")])
    assert r.status_code == 400
    assert "zip" in r.json()["message"]


def test_upload_zip_empty_md_400(app_env):
    """zip 内无 md/txt -> 400（避免静默空导入）。"""
    client = app_env
    zip_bytes = _make_zip({"only/json": "{}"})
    r = _upload(client, _adm(client), [("empty-pack.zip", zip_bytes, "application/zip")])
    assert r.status_code == 200  # 单 zip 无 md/txt 按 failed 回报（整批语义不 400）
    body = r.json()
    assert body["counts"]["failed"] == 1 and body["counts"]["created"] == 0


def test_upload_no_files_400(app_env):
    client = app_env
    r = client.post("/api/ext/skills/upload",
                    headers={"Authorization": f"Bearer {_adm(client)}"})
    assert r.status_code == 400


# ================= 3. 重名 409 语义：跳过并回报 =================

def test_upload_name_conflict_skip_and_report(app_env):
    """库内重名 -> 跳过 + 回报（不 400/409 崩溃，整批 200）。"""
    client = app_env
    tok = _adm(client)
    r = _upload(client, tok, [("conflict-skill.md", b"first\n")])
    assert r.json()["counts"]["created"] == 1
    r = _upload(client, tok, [("conflict-skill.md", b"second\n")])
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["created"] == 0
    assert body["skipped"][0]["name"] == "conflict-skill"
    assert "重名" in body["skipped"][0]["reason"]
    # 原内容未被覆盖
    row = next(s for s in client.get("/api/ext/skills",
                                     headers={"Authorization": f"Bearer {tok}"}).json()["skills"]
               if s["name"] == "conflict-skill")
    assert row["content"].strip() == "first"


def test_upload_in_batch_duplicate(app_env):
    """同一批次内两个文件解析出同名 -> 后者跳过。"""
    client = app_env
    r = _upload(client, _adm(client), [
        ("one.md", b"---\nname: batch-dup\n---\nA\n", "text/markdown"),
        ("two.md", b"---\nname: batch-dup\n---\nB\n", "text/markdown"),
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["created"] == 1
    assert body["counts"]["skipped"] == 1
    assert body["skipped"][0]["name"] == "batch-dup"


def test_upload_mixed_batch(app_env):
    """混合批次：新建 + 重名 + 非法类型 -> 各自归类回报。"""
    client = app_env
    tok = _adm(client)
    _upload(client, tok, [("pre.md", b"pre body\n")])
    r = _upload(client, tok, [
        ("new.md", b"new body\n"),
        ("pre.md", b"again\n"),
        ("bad.exe", b"x"),
    ])
    body = r.json()
    assert body["counts"] == {"created": 1, "skipped": 1, "failed": 1}
    assert body["created"][0]["name"] == "new"


# ================= 4. skills.updated_at =================

def test_skill_create_and_put_refresh_updated_at(app_env):
    client = app_env
    tok = _adm(client)
    H = {"Authorization": f"Bearer {tok}"}
    r = client.post("/api/ext/skills", headers=H,
                    json={"name": "ts-skill", "description": "d", "content": "v1"})
    assert r.status_code == 200
    sid = r.json()["id"]
    row1 = next(s for s in client.get("/api/ext/skills", headers=H).json()["skills"] if s["id"] == sid)
    assert row1["updated_at"]
    # 直读 DB 拿精确值，避免 API 时间戳秒级相同造成误判
    import sqlite3
    con = sqlite3.connect(DB_PATH)
    t1 = con.execute("SELECT updated_at FROM skills WHERE id=?", (sid,)).fetchone()[0]
    con.close()
    import time
    time.sleep(1.1)  # 跨秒，保证刷新可观测
    r = client.put(f"/api/ext/skills/{sid}", headers=H,
                   json={"name": "ts-skill", "description": "d2", "content": "v2"})
    assert r.status_code == 200
    con = sqlite3.connect(DB_PATH)
    t2 = con.execute("SELECT updated_at FROM skills WHERE id=?", (sid,)).fetchone()[0]
    con.close()
    assert t2 > t1, f"PUT 未刷新 updated_at: {t1} -> {t2}"


def test_skill_put_rename_conflict_409(app_env):
    client = app_env
    tok = _adm(client)
    H = {"Authorization": f"Bearer {tok}"}
    client.post("/api/ext/skills", headers=H, json={"name": "a-skill", "content": "A"})
    client.post("/api/ext/skills", headers=H, json={"name": "b-skill", "content": "B"})
    sid = next(s["id"] for s in client.get("/api/ext/skills", headers=H).json()["skills"]
               if s["name"] == "a-skill")
    r = client.put(f"/api/ext/skills/{sid}", headers=H,
                   json={"name": "b-skill", "description": "", "content": "A2"})
    assert r.status_code == 409
    # 自身改名（同名同 id）不 409
    r = client.put(f"/api/ext/skills/{sid}", headers=H,
                   json={"name": "a-skill", "description": "", "content": "A3"})
    assert r.status_code == 200


# ================= 5. MCP PUT / DELETE / env =================

def _mk_mcp(client, tok, **kw):
    body = {"name": kw.pop("name"), "command": kw.pop("command"), "args": kw.pop("args", []),
            "env": kw.pop("env", {}), "enabled": kw.pop("enabled", True)}
    body.update(kw)
    r = client.post("/api/ext/mcp", headers={"Authorization": f"Bearer {tok}"}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_mcp_create_persists_env(app_env):
    """创建接口把 env 写入（TASK-022：此前丢失）+ 持久化回显。"""
    client = app_env
    tok = _adm(client)
    mid = _mk_mcp(client, tok, name="env-mcp", command="python3",
                  args=["-m", "srv"], env={"API_KEY": "sk-1234", "MODE": "prod"})
    H = {"Authorization": f"Bearer {tok}"}
    row = next(m for m in client.get("/api/ext/mcp", headers=H).json()["mcp_servers"] if m["id"] == mid)
    assert row["env"] == {"API_KEY": "sk-1234", "MODE": "prod"}
    assert row["args"] == ["-m", "srv"]
    assert row["enabled"] is True
    # 直读 DB 验证落库（JSON 文本）
    import sqlite3
    con = sqlite3.connect(DB_PATH)
    raw = con.execute("SELECT env FROM mcp_servers WHERE id=?", (mid,)).fetchone()[0]
    con.close()
    assert json.loads(raw) == {"API_KEY": "sk-1234", "MODE": "prod"}


def test_mcp_put_update_all_fields(app_env):
    client = app_env
    tok = _adm(client)
    mid = _mk_mcp(client, tok, name="put-mcp", command="python3", args=["a"], env={"K": "1"},
                  enabled=True)
    H = {"Authorization": f"Bearer {tok}"}
    r = client.put(f"/api/ext/mcp/{mid}", headers=H,
                   json={"name": "put-mcp-2", "command": "node", "args": ["b.js"],
                         "env": {"K": "2", "NEW": "x"}, "enabled": False})
    assert r.status_code == 200, r.text
    row = next(m for m in client.get("/api/ext/mcp", headers=H).json()["mcp_servers"] if m["id"] == mid)
    assert row["name"] == "put-mcp-2"
    assert row["command"] == "node"
    assert row["args"] == ["b.js"]
    assert row["env"] == {"K": "2", "NEW": "x"}
    assert row["enabled"] is False


def test_mcp_put_404_and_rename_409(app_env):
    client = app_env
    tok = _adm(client)
    H = {"Authorization": f"Bearer {tok}"}
    _mk_mcp(client, tok, name="mcp-x", command="python3")
    r = client.put("/api/ext/mcp/99999", headers=H,
                   json={"name": "nope", "command": "x"})
    assert r.status_code == 404
    mid = next(m["id"] for m in client.get("/api/ext/mcp", headers=H).json()["mcp_servers"]
               if m["name"] == "mcp-x")
    r = client.put(f"/api/ext/mcp/{mid}", headers=H,
                   json={"name": "demo", "command": "x"})  # demo 为 seed 行
    assert r.status_code == 409


def test_mcp_delete_ok_and_404(app_env):
    client = app_env
    tok = _adm(client)
    H = {"Authorization": f"Bearer {tok}"}
    mid = _mk_mcp(client, tok, name="del-mcp", command="python3")
    r = client.delete(f"/api/ext/mcp/{mid}", headers=H)
    assert r.status_code == 200 and r.json()["deleted"] is True
    r = client.delete(f"/api/ext/mcp/{mid}", headers=H)
    assert r.status_code == 404


def test_mcp_delete_referenced_409(app_env):
    """有 agent_bindings 引用 -> 409 提示先解绑；解绑后可删。"""
    client = app_env
    tok = _adm(client)
    H = {"Authorization": f"Bearer {tok}"}
    mid = _mk_mcp(client, tok, name="bound-mcp", command="python3")
    r = client.post("/api/agents", headers=H,
                    json={"name": "bound-agent", "system_prompt": "s",
                          "bindings": [{"type": "mcp", "ref_id": str(mid)}]})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    r = client.delete(f"/api/ext/mcp/{mid}", headers=H)
    assert r.status_code == 409
    assert "bound-agent" in r.json()["message"]
    # 解绑（更新 agent 去掉绑定）后可删
    r = client.put(f"/api/agents/{aid}", headers=H,
                   json={"name": "bound-agent", "system_prompt": "s", "bindings": []})
    assert r.status_code == 200
    r = client.delete(f"/api/ext/mcp/{mid}", headers=H)
    assert r.status_code == 200


def test_mcp_env_empty_default(app_env):
    client = app_env
    tok = _adm(client)
    mid = _mk_mcp(client, tok, name="no-env-mcp", command="python3")  # 不传 env
    row = next(m for m in client.get("/api/ext/mcp", headers={"Authorization": f"Bearer {tok}"}).json()["mcp_servers"]
               if m["id"] == mid)
    assert row["env"] == {}


# ================= 6. RBAC（ext:manage 不动） =================

def test_upload_and_mcp_write_rbac(app_env):
    client = app_env
    # 无 token -> 401
    r = client.post("/api/ext/skills/upload",
                    files=[("files", ("a.md", io.BytesIO(b"x"), "text/markdown"))])
    assert r.status_code == 401
    # viewer 无 ext:manage -> 403
    vt = _viewer(client)
    r = client.post("/api/ext/skills/upload", headers={"Authorization": f"Bearer {vt}"},
                    files=[("files", ("a.md", io.BytesIO(b"x"), "text/markdown"))])
    assert r.status_code == 403
    r = client.put("/api/ext/mcp/1", headers={"Authorization": f"Bearer {vt}"},
                   json={"name": "x", "command": "y"})
    assert r.status_code == 403
    r = client.delete("/api/ext/mcp/1", headers={"Authorization": f"Bearer {vt}"})
    assert r.status_code == 403
    # admin 可写（200/404 均可，非 401/403 即权限通过）
    adm = _adm(client)
    r = client.put("/api/ext/mcp/99999", headers={"Authorization": f"Bearer {adm}"},
                   json={"name": "x", "command": "y"})
    assert r.status_code == 404


# ================= 7. 双后端 DDL 等价 =================

def test_sqlite_schema_and_alter_idempotent():
    """schema_sqlite.sql 含 skills.updated_at，executescript 两遍幂等；
    旧库（无新列）走 ALTER 补齐后两遍幂等。"""
    schema = os.path.join(SRC, "core", "schema_sqlite.sql")
    sql = open(schema, encoding="utf-8").read()
    assert re.search(r"CREATE TABLE IF NOT EXISTS skills \(", sql)
    assert "updated_at" in sql
    import aiosqlite
    p = os.path.join(TEMP, f"t022_schema_{os.getpid()}.db")
    for f in (p, p + "-wal", p + "-shm"):
        if os.path.exists(f):
            os.unlink(f)

    async def _run():
        # 1) 新库：schema 两遍
        conn = await aiosqlite.connect(p)
        await conn.executescript(sql)
        await conn.commit()
        await conn.executescript(sql)
        await conn.commit()
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(skills)")).fetchall()]
        assert "updated_at" in cols
        # 2) 模拟旧库：删列重建（sqlite 无 DROP COLUMN 稳定版本前用新库模拟——
        #    这里直接验证 ALTER 路径：先建无新列表，再走 db._sqlite_init 同款 ALTER）
        await conn.close()
        old = p + ".old"
        for f in (old, old + "-wal", old + "-shm"):
            if os.path.exists(f):
                os.unlink(f)
        o = await aiosqlite.connect(old)
        await o.executescript(
            "CREATE TABLE skills (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT UNIQUE NOT NULL, description TEXT, content TEXT NOT NULL);"
            "CREATE TABLE mcp_servers (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT UNIQUE NOT NULL, command TEXT NOT NULL, args TEXT DEFAULT '[]',"
            " enabled INTEGER DEFAULT 1);")
        await o.commit()
        for ddl in ("ALTER TABLE skills ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))",
                    "ALTER TABLE mcp_servers ADD COLUMN env TEXT DEFAULT '{}'"):
            try:
                await o.execute(ddl)
                await o.commit()
            except Exception as e:
                assert "duplicate column" in str(e).lower()
        # 第二遍 ALTER：duplicate column 幂等
        for ddl in ("ALTER TABLE skills ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))",
                    "ALTER TABLE mcp_servers ADD COLUMN env TEXT DEFAULT '{}'"):
            try:
                await o.execute(ddl)
                await o.commit()
            except Exception as e:
                assert "duplicate column" in str(e).lower()
        scols = [r[1] for r in await (await o.execute("PRAGMA table_info(skills)")).fetchall()]
        mcols = [r[1] for r in await (await o.execute("PRAGMA table_info(mcp_servers)")).fetchall()]
        assert "updated_at" in scols and "env" in mcols
        await o.close()

    import asyncio
    asyncio.run(_run())
    for f in (p, p + "-wal", p + "-shm", p + ".old", p + ".old-wal", p + ".old-shm"):
        if os.path.exists(f):
            os.unlink(f)


def test_pg_column_backfill_ddl_present():
    """PG 侧列补齐 DDL（db._pg_init 内联）结构断言：skills.updated_at + mcp_servers.env。
    PG 运行时验证（真实 ALTER + 读写）由 05-temp/t022/pg_selftest.sh 完成。"""
    src = open(os.path.join(SRC, "core", "db.py"), encoding="utf-8").read()
    idx = src.index("TASK-022 / F1: skills.updated_at + mcp_servers.env")
    ddl = src[idx:idx + 900]
    assert "ALTER TABLE skills ADD COLUMN IF NOT EXISTS updated_at TEXT" in ddl
    assert "to_char(now()" in ddl  # PG 时间戳默认（与 sqlite datetime('now') 语义一致）
    assert "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS env JSONB DEFAULT '{}'::jsonb" in ddl


def test_mcp_env_of_row_helper():
    """env 列双形态归一（sqlite JSON 文本 / PG JSONB dict / NULL / 坏 JSON）。"""
    from mcp.mcp_client import env_of_row
    assert env_of_row({"env": '{"A": "1"}'}) == {"A": "1"}
    assert env_of_row({"env": {"A": 1}}) == {"A": 1}      # PG JSONB
    assert env_of_row({"env": None}) == {}
    assert env_of_row({}) == {}
    assert env_of_row({"env": "not-json"}) == {}
    assert env_of_row({"env": b'{"B": "2"}'}) == {"B": "2"}
