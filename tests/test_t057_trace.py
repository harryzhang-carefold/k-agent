"""TASK-057 / 迭代5 链路追踪 — 单元测试 + 集成自测（章北海）。

自包含：不起线上 8099（RISK-015）；本文件内用 TestClient 起独立 app 实例，
SQLite 库放 05-temp/t057/（铁律：临时文件禁止 /tmp），并用本地 mock LLM server
（OpenAI 兼容，返回真实 usage + 流式末尾 usage chunk）验证：

1. **usage 解析**：chat() 返回 (content, usage_dict)（prompt/completion 与 mock
   一致）；chat_stream() 返回 (content_str, usage_dict)（聚合 chunk + 末尾 usage）。
   端点无 usage → usage={}（不 raise）。
2. **rag chunk 元信息**：rag.search 每项含 knowledge_id/chunk_seq/score/preview。
3. **埋点完整性**：custom agent 绑定 skill + rag + 插件，发一条会触发 LLM 的消息
   → trace 出现 ≥1 llm_call(真实 token>0 + 模型名) + skill_inject + rag_search(含
   chunk 粒度) + 聚合行字段正确。
4. **非阻塞铁律**：把 trace 表 rename 掉模拟故障 → 主聊天仍成功 + 只 log 错误。
5. **2000 截断**：超长 span input/output 截断到 2000 字符。
6. **保留策略**：手工插入 31 天前会话 → clean_expired(30) 删除。
7. **API 权限**：/api/trace 非 admin 403 / admin 200。
8. **hermes 后端 token**：mock hermes CLI 输出 → llm_call span token NULL +
   status 注明 hermes_no_usage（不编造数字）。
"""
import json
import os
import shutil
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
TEMP = os.path.abspath(os.path.join(HERE, "..", "..", "05-temp", "t057"))
os.makedirs(TEMP, exist_ok=True)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

DB_PATH = os.path.join(TEMP, f"test_trace_db_{os.getpid()}.db")


# ---------------- 本地 mock LLM server（OpenAI 兼容 + 真实 usage） ----------------
# 返回固定 usage（prompt=42/completion=13）便于断言"token 真实"；
# 流式返回 3 个 content delta + 末尾 usage chunk（vLLM 形态）。
_MOCK_PROMPT = 42
_MOCK_COMPLETION = 13


class _MockLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(ln) or b"{}")
        stream = body.get("stream", False)
        if not stream:
            resp = {
                "choices": [{"message": {"role": "assistant",
                                         "content": "MOCK-ANSWER"}}],
                "usage": {"prompt_tokens": _MOCK_PROMPT,
                          "completion_tokens": _MOCK_COMPLETION,
                          "total_tokens": _MOCK_PROMPT + _MOCK_COMPLETION},
            }
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return
        # 流式：3 个 content delta + 末尾 usage chunk（choices=[]）+ [DONE]
        def _chunk(delta=None, usage=None):
            o = {"choices": []}
            if delta is not None:
                o["choices"] = [{"delta": delta}]
            if usage is not None:
                o["usage"] = usage
            return ("data: " + json.dumps(o) + "\n").encode()
        parts = [
            _chunk({"role": "assistant", "content": "MOCK"}),
            _chunk({"content": "-ANS"}),
            _chunk({"content": "WER"}),
            _chunk(usage={"prompt_tokens": _MOCK_PROMPT,
                          "completion_tokens": _MOCK_COMPLETION,
                          "total_tokens": _MOCK_PROMPT + _MOCK_COMPLETION}),
            b"data: [DONE]\n",
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for p in parts:
            self.wfile.write(p)

    def log_message(self, *a):
        pass


@pytest.fixture()
def llm_server():
    srv = HTTPServer(("127.0.0.1", 0), _MockLLM)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.fixture()
def app_env(monkeypatch, llm_server):
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", DB_PATH)
    monkeypatch.setenv("LLM_BASE_URL", llm_server)
    monkeypatch.setenv("LLM_MODEL", "mock-llm")
    monkeypatch.setenv("LLM_API_KEY", "mock-key-9999")
    monkeypatch.setenv("LLM_RETRIES", "1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("EMBEDDING_MODEL", "")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_DIM", "512")

    for f in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(f):
            os.unlink(f)

    import core.db as dbmod
    dbmod._SQLITE_CONN = None
    dbmod._POOL = None

    from core.config import S
    S.DB_BACKEND = "sqlite"
    S.SQLITE_PATH = DB_PATH
    S.LLM_BASE_URL = llm_server
    S.LLM_MODEL = "mock-llm"
    S.LLM_API_KEY = "mock-key-9999"
    S.LLM_TIMEOUT = 30.0
    S.LLM_RETRIES = 1
    S.EMBEDDING_BASE_URL = ""
    S.EMBEDDING_MODEL = ""
    S.EMBEDDING_API_KEY = ""
    S.EMBEDDING_DIM = 512
    S.SEMANTIC_CACHE_THRESHOLD = 0.95
    S.TRACE_RETENTION_DAYS = 30

    from fastapi.testclient import TestClient
    from core import app as appmod
    with TestClient(appmod.app) as client:
        yield client, S
    dbmod._SQLITE_CONN = None
    dbmod._POOL = None
    if os.path.exists(DB_PATH):
        os.unlink(DB_PATH)


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _conn(S):
    import core.db as dbmod
    return dbmod._SQLITE_CONN


# ================= 1. usage 解析（provider 级，真实 mock HTTP） =================

def test_chat_returns_usage(app_env):
    client, S = app_env
    from llm import provider as llm
    import asyncio
    content, usage = asyncio.run(llm.chat(
        [{"role": "user", "content": "hi"}], model="mock-llm"))
    assert content == "MOCK-ANSWER"
    assert usage["prompt_tokens"] == _MOCK_PROMPT
    assert usage["completion_tokens"] == _MOCK_COMPLETION
    assert usage["total_tokens"] == _MOCK_PROMPT + _MOCK_COMPLETION


def test_chat_stream_returns_usage(app_env):
    client, S = app_env
    from llm import provider as llm
    import asyncio
    content, usage = asyncio.run(llm.chat_stream(
        [{"role": "user", "content": "hi"}], model="mock-llm"))
    # 聚合 3 个 delta：MOCK + -ANS + WER
    assert content == "MOCK-ANSWER"
    assert usage["prompt_tokens"] == _MOCK_PROMPT
    assert usage["completion_tokens"] == _MOCK_COMPLETION


def test_usage_empty_when_absent(app_env, monkeypatch):
    """端点无 usage → usage={}（不 raise）。用返回无 usage 的 mock。"""
    class _NoUsage(BaseHTTPRequestHandler):
        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            self.rfile.read(ln)
            data = json.dumps({"choices": [{"message": {"role": "assistant",
                                                         "content": "X"}}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _NoUsage)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    S = app_env[1]
    S.LLM_BASE_URL = url
    from llm import provider as llm
    import asyncio
    content, usage = asyncio.run(llm.chat([{"role": "user", "content": "hi"}],
                                           model="mock-llm"))
    assert content == "X"
    assert usage == {}  # 无 usage → 空 dict，不 raise
    srv.shutdown()


# ================= 2. rag.search 返回 chunk 元信息 =================

def test_rag_search_chunk_metadata(app_env):
    client, S = app_env
    import asyncio
    import core.db as dbmod
    from rag import rag
    conn = _conn(S)

    async def _run():
        kid = await dbmod.execute(conn, "INSERT INTO rag_knowledge (name) VALUES (?)",
                                  ("kb-test",))
        await rag.add_document(conn, kid, "哮喘急性发作的处理与用药指南。" * 20,
                               chunk_chars=150)
        res = await rag.search(conn, kid, "哮喘处理", top_k=3)
        return kid, res
    kid, res = asyncio.run(_run())
    assert len(res) >= 1
    for r in res:
        assert r["knowledge_id"] == kid
        assert "chunk_seq" in r
        assert "score" in r
        assert isinstance(r["preview"], str) and len(r["preview"]) <= 120
        # 向后兼容：seq 字段仍在
        assert "seq" in r


# ================= 3. 埋点完整性（skill + rag + 插件 → llm_call/skill_inject/rag_search） =================

def _setup_agent_with_tools(client, tok):
    """建一个绑定 skill + rag + echo 插件的 custom agent，返回 agent_id + 绑定名。"""
    import core.db as dbmod
    import asyncio
    conn = _conn(S) if (S := app_env_global) else None
    # 直接用 client 的 app.state.db
    conn = client.app.state.db

    async def _run():
        # skill
        sid = await dbmod.execute(conn, "INSERT INTO skills (name, content) VALUES (?,?)",
                                  ("trace-skill", "你是追踪测试助手，请简洁回答。"))
        # rag kb + doc
        kid = await dbmod.execute(conn, "INSERT INTO rag_knowledge (name) VALUES (?)",
                                  ("trace-kb",))
        from rag import rag
        await rag.add_document(conn, kid, "RAG 测试文档内容：关于链路追踪的说明。" * 10,
                               chunk_chars=150)
        # 插件绑定（echo）
        # agent
        aid = await dbmod.execute(
            conn, "INSERT INTO agents (name, system_prompt, model) VALUES (?,?,?)",
            ("trace-agent", "测试 agent", None))
        # 绑定
        await dbmod.execute(conn, "INSERT INTO agent_bindings (agent_id, type, ref_id) VALUES (?,?,?)",
                            (aid, "skill", str(sid)))
        await dbmod.execute(conn, "INSERT INTO agent_bindings (agent_id, type, ref_id) VALUES (?,?,?)",
                            (aid, "rag", str(kid)))
        await dbmod.execute(conn, "INSERT INTO agent_bindings (agent_id, type, ref_id) VALUES (?,?,?)",
                            (aid, "plugin", "echo"))
        return aid
    return asyncio.run(_run())


app_env_global = None


def test_trace_spans_completeness(app_env):
    global app_env_global
    app_env_global = app_env[1]
    client, S = app_env
    tok = _login(client, "admin")
    aid = _setup_agent_with_tools(client, tok)

    # 发一条消息（触发 LLM；mock 返回纯文本回答 → 1 次 llm_call）
    r = client.post(f"/api/chat/{aid}",
                    json={"message": "请介绍链路追踪"}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    conv_id = r.json()["conv_id"]

    # 查 trace
    r2 = client.get(f"/api/trace/conversations/{conv_id}",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200, r2.text
    detail = r2.json()
    conv = detail["conversation"]
    spans = detail["spans"]
    types = [s["span_type"] for s in spans]

    # llm_call 带真实 token + 模型名
    llm_spans = [s for s in spans if s["span_type"] == "llm_call"]
    assert len(llm_spans) >= 1
    assert llm_spans[0]["tokens_in"] == _MOCK_PROMPT
    assert llm_spans[0]["tokens_out"] == _MOCK_COMPLETION
    assert llm_spans[0]["model"] == "mock-llm"
    # skill_inject
    assert "skill_inject" in types
    # rag_search 含 chunk 粒度
    rag_spans = [s for s in spans if s["span_type"] == "rag_search"]
    assert len(rag_spans) >= 1
    assert rag_spans[0]["rag_chunks"] is not None and len(rag_spans[0]["rag_chunks"]) >= 1
    c0 = rag_spans[0]["rag_chunks"][0]
    assert "chunk_seq" in c0 and "score" in c0 and "preview" in c0
    # 聚合行字段正确
    assert conv["total_tokens_in"] == _MOCK_PROMPT
    assert conv["total_llm_calls"] == 1
    assert "mock-llm" in conv["models"]
    assert conv["status"] == "ok"
    assert conv["backend"] == "custom"


# ================= 4. 非阻塞铁律（rename 表 → 主聊天仍成功） =================

def test_trace_non_blocking(app_env):
    client, S = app_env
    tok = _login(client, "admin")
    aid = _setup_agent_with_tools(client, tok)
    # 把 trace 表 rename 掉模拟故障
    import core.db as dbmod
    import asyncio

    async def _rename():
        await dbmod.execute(client.app.state.db, "ALTER TABLE trace_spans RENAME TO trace_spans_gone")
        await dbmod.execute(client.app.state.db, "ALTER TABLE trace_conversations RENAME TO trace_conversations_gone")
    asyncio.run(_rename())

    # 主聊天仍成功（埋点失败只 log，不 raise）
    r = client.post(f"/api/chat/{aid}",
                    json={"message": "非阻塞测试消息"}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"]  # 有回答
    # 恢复表名（清理）
    async def _restore():
        await dbmod.execute(client.app.state.db, "ALTER TABLE trace_spans_gone RENAME TO trace_spans")
        await dbmod.execute(client.app.state.db, "ALTER TABLE trace_conversations_gone RENAME TO trace_conversations")
    asyncio.run(_restore())


# ================= 5. 2000 字符截断 =================

def test_span_truncation_2000(app_env):
    client, S = app_env
    from core import trace as t
    import core.db as dbmod
    import asyncio
    conn = client.app.state.db

    async def _run():
        # 造一个 conv + 超长 skill_inject output（5000 字符）
        await dbmod.execute(conn, "INSERT INTO conversations (id, agent_id) VALUES (?,?)",
                            ("ct-1", 1))
        ctx = t.TraceContext(conn, "ct-1", 1, "a", "custom")
        await ctx.init_seq()
        await ctx.record_skill_inject("big", "X" * 5000)
        await ctx.finish(status="ok")
        row = await dbmod.fetchone(conn, "SELECT * FROM trace_spans WHERE conv_id='ct-1'")
        return row
    row = asyncio.run(_run())
    assert len(row["output"]) == 2000  # 截断到 2000


# ================= 6. 保留策略（插 31 天前行 → clean_expired 删除） =================

def test_retention_cleanup(app_env):
    client, S = app_env
    import core.db as dbmod
    from core import trace as t
    import asyncio
    from datetime import datetime, timezone, timedelta
    conn = client.app.state.db

    async def _run():
        old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")
        new_ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        await dbmod.execute(conn,
            "INSERT INTO trace_conversations (id, agent_id, agent_name, backend, started_at, ended_at, status) VALUES (?,?,?,?,?,?,?)",
            ("old-c", 1, "a", "custom", old_ts, old_ts, "ok"))
        await dbmod.execute(conn,
            "INSERT INTO trace_conversations (id, agent_id, agent_name, backend, started_at, ended_at, status) VALUES (?,?,?,?,?,?,?)",
            ("new-c", 1, "a", "custom", new_ts, new_ts, "ok"))
        await dbmod.execute(conn,
            "INSERT INTO trace_spans (conv_id, seq, ts, span_type) VALUES ('old-c',1,?,?)",
            (old_ts, "llm_call"))
        removed = await t.clean_expired(conn, 30)
        remaining = await dbmod.fetchall(conn, "SELECT id FROM trace_conversations ORDER BY id")
        return removed, [r["id"] for r in remaining]
    removed, remaining = asyncio.run(_run())
    assert removed == 1
    assert "old-c" not in remaining
    assert "new-c" in remaining


# ================= 7. API 权限（非 admin 403 / admin 200） =================

def test_trace_api_auth(app_env):
    client, S = app_env
    viewer_tok = _login(client, "viewer")
    # 先造一条 admin 可见的 trace
    admin_tok = _login(client, "admin")
    aid = _setup_agent_with_tools(client, admin_tok)
    r = client.post(f"/api/chat/{aid}", json={"message": "权限测试"},
                    headers={"Authorization": f"Bearer {admin_tok}"})
    conv_id = r.json()["conv_id"]

    # viewer 访问 → 403（system:admin 要求）
    rv = client.get(f"/api/trace/conversations",
                    headers={"Authorization": f"Bearer {viewer_tok}"})
    assert rv.status_code == 403
    # admin 访问 → 200
    ra = client.get(f"/api/trace/conversations?limit=10",
                    headers={"Authorization": f"Bearer {admin_tok}"})
    assert ra.status_code == 200
    assert any(c["id"] == conv_id for c in ra.json()["conversations"])


# ================= 8. hermes 后端 token NULL + hermes_no_usage =================

def test_hermes_token_no_usage(app_env, monkeypatch):
    """mock hermes CLI：零工具面 profile，输出固定文本 → llm_call span token NULL
    + error 注明 hermes_no_usage（不编造数字）。"""
    client, S = app_env
    tok = _login(client, "admin")
    import core.db as dbmod
    import asyncio
    import core.hermes_cli as hc
    conn = client.app.state.db

    # mock hermes_bin → 一个会成功的假命令（用 python -c 打印固定文本）
    import shutil
    # 造一个假 hermes wrapper：打印固定回答
    fake = os.path.join(TEMP, "fake_hermes.py")
    with open(fake, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env python3\nimport sys\nprint('HERMES-ANSWER')\n")
    os.chmod(fake, 0o755)
    # hermes_bin 返回 fake；-p/-z 参数被忽略
    monkeypatch.setattr(hc, "hermes_bin", lambda: fake)

    async def _run():
        # 建 hermes agent + profile 字段
        aid = await dbmod.execute(conn,
            "INSERT INTO agents (name, system_prompt, backend, hermes_profile) VALUES (?,?,?,?)",
            ("hermes-agent", "test", "hermes", "fakeprofile"))
        # 触发 run
        client.app.state.hermes_adapter.set_conn(conn)
        out = await client.app.state.hermes_adapter.run(
            {"id": aid, "name": "hermes-agent", "backend": "hermes",
             "hermes_profile": "fakeprofile"},
            "hi", conv_id="hm-c-1")
        return out, aid
    out, aid = asyncio.run(_run())
    assert out["answer"] == "HERMES-ANSWER"

    # 查 trace span
    async def _span():
        rows = await dbmod.fetchall(conn,
            "SELECT * FROM trace_spans WHERE conv_id='hm-c-1' ORDER BY seq")
        return rows
    spans = asyncio.run(_span())
    llm_spans = [s for s in spans if s["span_type"] == "llm_call"]
    assert len(llm_spans) >= 1
    s0 = llm_spans[0]
    # token NULL（不编造）+ 注明 hermes_no_usage
    assert s0["tokens_in"] is None
    assert s0["tokens_out"] is None
    assert "hermes_no_usage" in (s0["error"] or "")
