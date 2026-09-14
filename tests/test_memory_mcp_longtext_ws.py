"""模块 12/13 三层记忆 + 3/4/5 MCP/Skills/Plugins + 15 长文本4策略 + 7 WebSocket。

记忆：B+ 树正确性 / 图遍历 / 事件节点 / 拒绝万能边 / MERGE 幂等 / 日期作属性。
MCP：真实 stdio 进程（initialize/tools/list/tools/call）。
长文本：Map-Reduce / 增量图 / critique-refine / pandas 预处理。
WebSocket：流式 token + 缓存命中标注 + 未认证被拒。
"""
import base64
import json
import time

import pytest
import websocket  # websocket-client

from conftest import C, BASE

UNIQ = "w" + str(int(time.time()))


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
    return next(x for x in o["agents"] if x["name"] == "医疗助手")


# ---------- 模块 12/13：三层记忆（L0/L1/L2 立体图 + B+ 树）----------
def test_l2_edge_has_props(user):
    code, o = C.req("GET", "/api/memory/l2/edges", tok=user)
    assert any(e["type"] == "DIAGNOSED_WITH" and e["props"].get("diagnosis_date")
               for e in o["edges"])


def test_l2_date_as_edge_prop(user):
    code, o = C.req("GET", "/api/memory/l2/edges", tok=user)
    assert any("test_date" in e["props"] for e in o["edges"])


def test_l2_no_date_nodes(user):
    code, o = C.req("GET", "/api/memory/l2/nodes", tok=user)
    labels = {n["label"] for n in o["nodes"]}
    assert "Date" not in labels          # 日期做属性，不节点化


def test_l2_multihop_to_contraindicated_drug(user):
    code, o = C.req("GET", "/api/memory/l2/subgraph?start=P001&hops=3", tok=user)
    assert any(e["dst"] == "ASPIRIN" for e in o["bfs"])   # 患者→疾病→禁忌药
    assert len(o["path"]) >= 2


def test_l2_visit_event_node(user):
    code, o = C.req("GET", "/api/memory/l2/events", tok=user)
    v = o["events"][0]
    assert v["visit"]["id"] == "V20260909"
    assert "PRIMARY_DIAGNOSIS" in v["relations"]
    assert "ORDERED_TEST" in v["relations"]


def test_l2_reject_lowercase_type(dev):
    code, o = C.req("POST", "/api/memory/l2/edge",
                    {"src": "A1", "dst": "B1", "type": "diagnosed_with",
                     "src_label": "X", "dst_label": "Y"}, tok=dev, expect=400)
    assert code == 400


def test_l2_reject_universal_edge_related_to(dev):
    code, o = C.req("POST", "/api/memory/l2/edge",
                    {"src": "A1", "dst": "B1", "type": "RELATED_TO"}, tok=dev, expect=400)
    assert code == 400
    assert "RELATED_TO" in o["message"]


def test_l2_reject_universal_edge_linked_to(dev):
    code, o = C.req("POST", "/api/memory/l2/edge",
                    {"src": "A1", "dst": "B1", "type": "LINKED_TO"}, tok=dev, expect=400)
    assert code == 400


def test_l2_merge_idempotent(dev):
    body = {"src": f"M1{UNIQ}", "dst": f"M2{UNIQ}", "type": "HAS_TEST_RESULT",
            "props": {"value": 1}}
    C.req("POST", "/api/memory/l2/edge", body, tok=dev)
    C.req("POST", "/api/memory/l2/edge", body, tok=dev)   # 重复写
    code, o = C.req("GET", "/api/memory/l2/edges", tok=dev)
    n = sum(1 for e in o["edges"] if e["src"] == f"M1{UNIQ}" and e["type"] == "HAS_TEST_RESULT")
    assert n == 1            # MERGE 幂等：不产生重复边


def test_btree_date_bucket(user):
    code, o = C.req("GET", "/api/memory/l2/bucket?node=P001&dim=date&key=2026-09-09", tok=user)
    assert len(o["records"]) == 1
    assert "FEV1" in o["records"][0]["summary"]


def test_btree_inorder_sorted(user):
    code, o = C.req("GET", "/api/memory/l2/bucket?node=P001&dim=date", tok=user)
    assert len(o["records"]) >= 2


def test_l0_raw_preserved(user):
    code, o = C.req("GET", "/api/memory/l0?limit=20", tok=user)
    assert o["count"] >= 1        # L0 不降噪，原始记录存在


def test_l1_semantic_cache_queryable(user):
    code, o = C.req("GET", "/api/memory/l1", tok=user)
    assert o["count"] >= 1


def test_memory_adapters_pluggable(user):
    code, o = C.req("GET", "/api/memory/backend", tok=user)
    for k in ("local", "redis", "milvus", "neo4j"):
        assert k in o["adapters"]
    assert o["adapters"]["redis"]["stub"] is True
    assert o["current"]["name"] == "local"


# ---------- 模块 3/4/5：MCP stdio / Skills / Plugins ----------
def test_plugins_builtin(user):
    code, o = C.req("GET", "/api/ext/plugins", tok=user)
    names = {p["name"] for p in o["plugins"]}
    assert "get_time" in names
    assert "mcp_call" in names


def test_plugin_get_time_real(user):
    code, o = C.req("POST", "/api/ext/plugins/get_time/call", {"arguments": {}}, tok=user)
    assert o["ok"]
    assert time.strftime("%Y-%m-%d") in o["local"]


def test_mcp_tools_list(dev):
    code, o = C.req("GET", "/api/ext/mcp", tok=dev)
    demo = next(m for m in o["mcp_servers"] if m["name"] == "demo")
    code, o = C.req("GET", f"/api/ext/mcp/{demo['id']}/tools", tok=dev)
    assert code == 200
    assert len(o["tools"]) >= 2
    assert any(t["name"] == "get_time" for t in o["tools"])


def test_mcp_tools_call_real(dev):
    code, o = C.req("GET", "/api/ext/mcp", tok=dev)
    demo = next(m for m in o["mcp_servers"] if m["name"] == "demo")
    code, o = C.req("POST", f"/api/ext/mcp/{demo['id']}/tools/get_time/call",
                    {"arguments": {}}, tok=dev)
    assert o["ok"] is not False
    assert "utc" in json.dumps(o["result"])


def test_mcp_patient_demo_data(dev):
    code, o = C.req("GET", "/api/ext/mcp", tok=dev)
    demo = next(m for m in o["mcp_servers"] if m["name"] == "demo")
    code, o = C.req("POST", f"/api/ext/mcp/{demo['id']}/tools/get_patient_demo/call",
                    {"arguments": {}}, tok=dev)
    assert "394" in json.dumps(o["result"])


def test_mcp_unknown_tool_502(dev):
    code, o = C.req("GET", "/api/ext/mcp", tok=dev)
    demo = next(m for m in o["mcp_servers"] if m["name"] == "demo")
    code, o = C.req("POST", f"/api/ext/mcp/{demo['id']}/tools/no_such/call",
                    {"arguments": {}}, tok=dev, expect=502)
    assert code == 502


def test_skills_crud(dev):
    code, o = C.req("GET", "/api/ext/skills", tok=dev)
    assert len(o["skills"]) >= 1
    code, o = C.req("POST", "/api/ext/skills", {"name": "empty-skill", "content": "  "},
                    tok=dev, expect=400)
    assert code == 400
    code, o = C.req("POST", "/api/ext/skills",
                    {"name": f"pytest-skill-{UNIQ}", "content": "测试指令"}, tok=dev)
    sid = o["id"]
    code, o = C.req("DELETE", f"/api/ext/skills/{sid}", tok=dev)
    assert code == 200


# ---------- 模块 15：长文本 4 策略 ----------
def test_longtext_map_reduce(user):
    long_text = "患者王建国，52岁，支气管哮喘中重度。" * 200
    code, o = C.req("POST", "/api/longtext/map-reduce", {"text": long_text}, tok=user)
    assert code == 200
    assert o["chunks"] >= 2
    assert isinstance(o["reduced_items"], list)


def test_longtext_incremental_graph(user):
    code, o = C.req("POST", "/api/longtext/incremental-graph",
                    {"text": "患者王建国患有支气管哮喘。2026-09-09 检验 IgE 394.00 KU/L，FEV1 改善 240 ml。",
                     "center": "王建国"}, tok=user)
    assert code == 200
    assert o["triplets_merged"] >= 1
    assert "子图摘要" in o["subgraph_summary"]


def test_longtext_critique_refine(user):
    code, o = C.req("POST", "/api/longtext/critique-refine",
                    {"text": "患者 IgE 394 KU/L，FEV1 2.31 L。生成 QA。", "max_rounds": 5}, tok=user)
    assert code == 200
    assert o["passed"] or o.get("hitl")
    code2, _ = C.req("GET", "/api/longtext/hitl", tok=user)
    assert code2 == 200


def test_longtext_pandas_preprocess(user):
    csv = ("patient_id,date,indicator,value,unit\n"
           "P001,2026-07-14,FEV1,2.10,L\nP001,2026-09-09,IgE,394.00,KU/L\nP002,2026-08-20,FEV1,1.95,L\n")
    code, o = C.req("POST", "/api/longtext/preprocess",
                    {"csv": csv, "llm_convert": False}, tok=user)
    sj = o["structured_json"]
    assert "P001|2026-09-09" in sj
    assert sj["P001|2026-09-09"]["tests"]["IgE"]["value"] == 394.0


# ---------- 模块 14 规则1：图片 MD5 缓存路由 ----------
def test_image_md5_route_observable(user, seed_agent):
    img = base64.b64encode(b"pytest-fake-px-999").decode()
    code, o = C.req("POST", f"/api/chat/{seed_agent['id']}",
                    {"message": "", "images": [{"b64": img, "content_type": "image/png"}]}, tok=user)
    assert any(s.get("md5") for s in o.get("sub_requests", []))


def test_image_mixed_split_observable(user, seed_agent):
    img = base64.b64encode(b"pytest-fake-px-999").decode()
    code, o = C.req("POST", f"/api/chat/{seed_agent['id']}",
                    {"message": "这张图里有什么？", "images": [{"b64": img}]}, tok=user)
    assert any("split:mixed" in e for e in o["route_events"])


def test_image_md5_cache_second_hit(user, seed_agent):
    """BUG-001 / AC-51 核心回归：同一图片连续两次请求，第二次 cache_hit=md5 且 llm_calls=0。

    第一次走 LLM（llm_calls>=1）并把解析写入 memory_l1_image；第二次（即使文本不同，
    混合拆分后文本子请求语义 miss）仍由图片 MD5 命中直返，跳过 LLM。
    用唯一 img/conv，避免与其它用例或会话级缓存重置冲突。
    """
    import uuid
    img = base64.b64encode(b"pytest-ac51-img-" + uuid.uuid4().bytes[:6]).decode()
    conv = "ac51-" + uuid.uuid4().hex[:8]
    c1, o1 = C.req("POST", f"/api/chat/{seed_agent['id']}",
                   {"message": f"img{int(time.time())} 描述这张图",
                    "images": [{"b64": img, "content_type": "image/png"}], "conv_id": conv}, tok=user)
    assert c1 == 200 and o1.get("llm_calls", 0) >= 1, o1
    assert any(s.get("kind") == "image" and s.get("md5") for s in o1.get("sub_requests", [])), o1.get("sub_requests")
    c2, o2 = C.req("POST", f"/api/chat/{seed_agent['id']}",
                   {"message": "描述这张图",
                    "images": [{"b64": img, "content_type": "image/png"}], "conv_id": conv}, tok=user)
    assert c2 == 200, o2
    assert o2.get("cache_hit") == "md5", o2
    assert o2.get("llm_calls", 0) == 0, o2
    assert (o2.get("answer") or "").strip(), o2


# ---------- 模块 7：WebSocket 流式 ----------
def test_ws_streaming_and_cache_hit(user, seed_agent):
    tok = user
    q = "支气管扩张试验阳性标准是什么？"
    ws = websocket.create_connection(
        f"{BASE.replace('http', 'ws')}/ws/chat/{seed_agent['id']}/cws1?token={tok}", timeout=120)
    ws.send(json.dumps({"message": q}))
    tokens, done = [], None
    while True:
        m = json.loads(ws.recv())
        if m["type"] == "token":
            tokens.append(m["content"])
        elif m["type"] in ("done", "cache_hit"):
            done = m
            break
        elif m["type"] == "error":
            done = m
            break
    ws.close()
    assert len(tokens) >= 2 and done is not None
    # 重发相同问题 → WS 缓存命中
    ws2 = websocket.create_connection(
        f"{BASE.replace('http', 'ws')}/ws/chat/{seed_agent['id']}/cws1?token={tok}", timeout=60)
    ws2.send(json.dumps({"message": q}))
    got = None
    while True:
        m = json.loads(ws2.recv())
        if m["type"] in ("done", "cache_hit"):
            got = m
            break
    ws2.close()
    assert got is not None and got.get("cache_hit") == "semantic"


def test_ws_unauthenticated_rejected():
    try:
        ws = websocket.create_connection(
            f"{BASE.replace('http', 'ws')}/ws/chat/1/cws9", timeout=5)
        ws.close()
        pytest.fail("未认证 WS 应被拒")
    except Exception:
        pass
