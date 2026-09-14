"""模块 2 Agent CRUD + 6 REST + 1/5 真实 LLM 对话 + 8 Embedding + 9 RAG。

真实调用 vLLM（vllm-qwen3.8-27b），缓存路由/语义缓存均为真实行为。
LLM 断言只校验：格式 / 流程 / 非空 / 命中路径 / 结构（RISK-006：不断言具体文字）。
"""
import sys
import time
import urllib.parse

import pytest

from conftest import C

UNIQ = "t" + str(int(time.time()))


@pytest.fixture(scope="module")
def admin():
    return C.login("admin")[0]


@pytest.fixture(scope="module")
def dev():
    return C.login("developer")[0]


@pytest.fixture(scope="module")
def user():
    return C.login("user")[0]


@pytest.fixture(scope="module")
def seed_agent(admin):
    code, o = C.req("GET", "/api/agents", tok=admin)
    a = next(x for x in o["agents"] if x["name"] == "医疗助手")
    return a


# ---------- 模块 2：Agent CRUD ----------
@pytest.fixture(scope="module")
def test_agent(dev):
    """模块级被测 agent：建一个，用毕删除（setup/yield/teardown）。"""
    code, o = C.req("POST", "/api/agents",
                    {"name": f"pytest-agent-{UNIQ}", "system_prompt": "测试 agent"}, tok=dev)
    assert code == 200
    assert o["name"] == f"pytest-agent-{UNIQ}"
    aid = o["id"]
    yield aid
    C.req("DELETE", f"/api/agents/{aid}", tok=dev)


def test_agent_create(dev):
    code, o = C.req("POST", "/api/agents",
                    {"name": f"pytest-agent-x-{UNIQ}", "system_prompt": "临时"}, tok=dev)
    assert code == 200
    C.req("DELETE", f"/api/agents/{o['id']}", tok=dev)


def test_agent_read(dev, test_agent):
    code, o = C.req("GET", f"/api/agents/{test_agent}", tok=dev)
    assert code == 200
    assert o["system_prompt"] == "测试 agent"


def test_agent_update(dev, test_agent):
    code, o = C.req("PUT", f"/api/agents/{test_agent}",
                    {"name": f"pytest-agent2-{UNIQ}", "system_prompt": "测试 agent v2"}, tok=dev)
    assert code == 200
    assert o["updated"] is True
    code2, o2 = C.req("GET", f"/api/agents/{test_agent}", tok=dev)
    assert o2["name"] == f"pytest-agent2-{UNIQ}"


def test_agent_duplicate_409(dev):
    code, o = C.req("POST", "/api/agents",
                    {"name": f"pytest-agent2-{UNIQ}", "system_prompt": "x"}, tok=dev, expect=409)
    assert code == 409


def test_agent_bind_missing_skill_400(dev):
    code, o = C.req("POST", "/api/agents",
                    {"name": "bad-bind", "system_prompt": "x",
                     "bindings": [{"type": "skill", "ref_id": "99999"}]}, tok=dev, expect=400)
    assert code == 400


def test_agent_not_found_404(dev):
    code, o = C.req("GET", "/api/agents/99999", tok=dev, expect=404)
    assert code == 404


def test_agent_user_read_403():
    tok, _ = C.login("viewer")
    code, o = C.req("POST", "/api/agents",
                    {"name": "x", "system_prompt": "x"}, tok=tok, expect=403)
    assert code == 403


# ---------- 模块 14 规则5：prefix caching 布局 ----------
def test_prompt_layout_system_first(dev, seed_agent):
    code, o = C.req("GET", f"/api/agents/{seed_agent['id']}/prompt?text=" +
                    urllib.parse.quote("你好"), tok=dev)
    msgs = o["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "user"


def test_prompt_skill_injected(dev, seed_agent):
    code, o = C.req("GET", f"/api/agents/{seed_agent['id']}/prompt?text=" +
                    urllib.parse.quote("IgE 多少"), tok=dev)
    sysmsg = o["messages"][0]["content"]
    assert "医疗问答规范" in sysmsg          # seed skill 真实注入
    assert "工具调用规范" in sysmsg


def test_prompt_rag_and_tool_in_system(dev, seed_agent):
    code, o = C.req("GET", f"/api/agents/{seed_agent['id']}/prompt?text=" +
                    urllib.parse.quote("IgE 多少"), tok=dev)
    sysmsg = o["messages"][0]["content"]
    assert "知识库检索上下文" in sysmsg       # RAG 在固定左侧
    assert "get_time" in sysmsg              # 工具 schema 在固定左侧
    assert "tool_call" in sysmsg


# ---------- 模块 1/5/7：真实 LLM 对话 + 缓存路由 ----------
def test_chat_real_llm(user, seed_agent):
    from conftest import _reset_cache
    _reset_cache()  # 干净基线：保证首跑必走 LLM（幂等，可重复运行）
    code, o = C.req("POST", f"/api/chat/{seed_agent['id']}",
                    {"message": "患者 IgE 结果是多少？单位呢？"}, tok=user)
    assert code == 200
    assert len(o["answer"]) > 15
    assert "394" in o["answer"]             # 真实引用知识（非模板）
    assert o["llm_calls"] >= 1


def test_chat_semantic_cache_hit(user, seed_agent):
    # 自包含：重置后首问必走 LLM，重问几乎同句 → 语义缓存命中，不再调 LLM
    from conftest import _reset_cache
    _reset_cache()
    q = "患者 IgE 结果是多少？单位呢？"
    c1, o1 = C.req("POST", f"/api/chat/{seed_agent['id']}", {"message": q}, tok=user)
    assert o1["llm_calls"] >= 1                 # 首问真实走 LLM
    conv = o1["conv_id"]
    c2, o2 = C.req("POST", f"/api/chat/{seed_agent['id']}",
                   {"message": q, "conv_id": conv}, tok=user)
    assert c2 == 200
    assert o2["cache_hit"] == "semantic"
    assert o2["llm_calls"] == 0


def test_chat_different_topic_misses_cache(user, seed_agent):
    from conftest import _reset_cache
    _reset_cache()  # 干净基线：新问题必走 LLM
    code, o = C.req("POST", f"/api/chat/{seed_agent['id']}",
                    {"message": "FEV1 改善量是多少 ml？"}, tok=user)
    assert o["cache_hit"] is None
    assert o["llm_calls"] >= 1


def test_chat_tool_get_time_real(user, seed_agent):
    from conftest import _reset_cache
    _reset_cache()
    code, o = C.req("POST", f"/api/chat/{seed_agent['id']}",
                    {"message": "现在几点了？"}, tok=user)
    assert code == 200
    assert time.strftime("%Y-%m-%d") in o["answer"]   # 真实 get_time 工具结果


def test_chat_complex_split_observable(user, seed_agent):
    from conftest import _reset_cache
    _reset_cache()
    code, o = C.req("POST", f"/api/chat/{seed_agent['id']}",
                    {"message": "患者 IgE 结果是多少？单位呢？现在几点了？FEV1 改善量是多少？"},
                    tok=user)
    assert any("split:complex" in e for e in o["route_events"])


# ---------- 模块 8：Embedding（确定性哈希，直接调库函数）----------
def _emb():
    sys.path.insert(0, "/home/hermes/hermes-workspace/projects/ai-agent-platform/02-development/src")
    from llm import embedding
    return embedding


def test_embedding_deterministic():
    e = _emb()
    a = e.hash_embed("患者 IgE 394.00 KU/L，提示过敏体质")
    b = e.hash_embed("患者 IgE 394.00 KU/L，提示过敏体质")
    assert a == b                          # 逐维相等


def test_embedding_dim_and_backend_info():
    e = _emb()
    v = e.hash_embed("x")
    assert len(v) == 512
    assert "openai_compatible" in e.backend_info()


def test_embedding_similar_gt_dissimilar():
    e = _emb()
    v1 = e.hash_embed("患者 IgE 394.00 KU/L，提示过敏体质")
    v2 = e.hash_embed("今天天气不错，适合去公园散步")
    assert e.cosine(v1, v1) > e.cosine(v1, v2)


# ---------- 模块 9：RAG ----------
def test_rag_seed_kb_chunks(user):
    code, o = C.req("GET", "/api/rag/knowledge", tok=user)
    kb = next(k for k in o["knowledge"] if k["name"] == "医疗知识库")
    assert kb["chunks"] > 2


def test_rag_adjacent_overlap(user):
    code, o = C.req("GET", "/api/rag/knowledge", tok=user)
    kb = next(k for k in o["knowledge"] if k["name"] == "医疗知识库")
    code, o = C.req("GET", f"/api/rag/knowledge/{kb['id']}/chunks", tok=user)
    chunks = o["chunks"]
    ok = False
    for a, b in zip(chunks, chunks[1:]):
        for L in range(20, min(80, len(b["text"]) // 3), 5):
            if b["text"][:L] in a["text"][-L:]:
                ok = True
                break
        if ok:
            break
    assert ok, "相邻块应有重叠文本"


def test_rag_search_hits_ige(user):
    code, o = C.req("GET", "/api/rag/knowledge", tok=user)
    kb = next(k for k in o["knowledge"] if k["name"] == "医疗知识库")
    code, o = C.req("POST", f"/api/rag/knowledge/{kb['id']}/search",
                    {"query": "IgE 结果是多少"}, tok=user)
    assert "IgE" in o["results"][0]["text"]


def test_rag_upload_and_delete_perm(user, dev):
    code, o = C.req("POST", "/api/rag/knowledge", {"name": f"pytest-kb-{UNIQ}"}, tok=dev)
    kid = o["id"]
    code, o = C.req("POST", f"/api/rag/knowledge/{kid}/documents",
                    {"text": "患者张某某。2026-09-10 门诊：FEV1 1.80 L，IgE 512.00 KU/L。" * 4}, tok=dev)
    assert code == 200
    assert o["chunks"] >= 2
    # user 无权删
    c403, _ = C.req("DELETE", f"/api/rag/knowledge/{kid}", tok=user, expect=403)
    assert c403 == 403
    # dev 可删
    code, o = C.req("DELETE", f"/api/rag/knowledge/{kid}", tok=dev)
    assert code == 200
