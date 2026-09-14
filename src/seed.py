"""种子数据（医疗场景最小闭环，PD-003）：4 用户 / 4 角色 / 13 权限 / 医疗 agent /
2 skills / demo MCP server / 3 plugins / 医疗 RAG 知识库 / L2 立体图（Visit 事件节点）。
幂等：已存在则跳过。
"""
import json
from core import db
from core.security import hash_password, ROLE_MATRIX, PERMISSIONS
from core.config import S

SEED_RAG_DOC = """\
患者：王建国（P001），男，52岁，因"反复喘息、气促2年，加重1周"就诊。
诊断：支气管哮喘（中重度，控制不佳）。
2026-09-09 门诊：支气管舒张试验阳性，FEV1 由 2.31 L 改善至 2.55 L，
FEV1 改善量 240 ml，改善率 10.39%，达阳性标准（≥12% 或绝对值 ≥200 ml）。
血清总 IgE 394.00 KU/L（显著升高，提示过敏体质）。
尘螨皮试阳性；花粉（蒿属）阳性。
治疗：吸入布地奈德/福莫特罗 160/4.5 μg，每12小时一次；
口服氯雷他定 10 mg qn。
2026-07-14 复测 FEV1 2.10 L，IgE 410.20 KU/L，控制不佳，已加用孟鲁司特 10 mg qn。
2026-09-09 医嘱：避免接触尘螨，卧室防螨处理；2周后复测肺功能。
患者自述对阿司匹林不耐受，用药需避开 NSAIDs。
"""

SEED_SKILLS = [
    ("医疗问答规范", "回答医疗问题时遵循以下规范：先给出结论，再列依据；"
     "涉及检验指标必须标注数值与单位；不确定时明确说明需进一步检查。"),
    ("工具调用规范", "当用户询问当前时间、需要查询患者档案时，优先使用工具获取真实数据，"
     "不要凭空编造。"),
]


async def seed(conn):
    from core import db
    # ---- 角色 / 权限 ----
    for code, desc in PERMISSIONS:
        await db.execute(conn, "INSERT OR IGNORE INTO permissions (code, description) VALUES (?,?)",
                         (code, desc))
    role_rows = {}
    for name in ROLE_MATRIX:
        r = await db.fetchone(conn, "SELECT id FROM roles WHERE name=?", (name,))
        if not r:
            r = {"id": await db.execute(conn, "INSERT INTO roles (name, description) VALUES (?,?)",
                                        (name, f"{name} 内置角色"))}
        role_rows[name] = r["id"]
        for code in ROLE_MATRIX[name]:
            await db.execute(conn, "INSERT OR IGNORE INTO role_permissions (role_id, permission_code) VALUES (?,?)",
                             (r["id"], code))

    # ---- 用户（4 seed，密码从 .env SEED_PASSWORD）----
    users = [("admin", "super_admin"), ("developer", "agent_developer"),
             ("user", "user"), ("viewer", "viewer")]
    for uname, role in users:
        r = await db.fetchone(conn, "SELECT id FROM users WHERE username=?", (uname,))
        if not r:
            uid = await db.execute(
                conn, "INSERT INTO users (username, password_hash) VALUES (?,?)",
                (uname, hash_password(S.SEED_PASSWORD)))
            await db.execute(conn, "INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)",
                             (uid, role_rows[role]))

    # ---- Skills ----
    skill_ids = {}
    for name, content in SEED_SKILLS:
        r = await db.fetchone(conn, "SELECT id FROM skills WHERE name=?", (name,))
        if not r:
            r = {"id": await db.execute(
                conn, "INSERT INTO skills (name, description, content) VALUES (?,?,?)",
                (name, f"seed skill: {name}", content))}
        skill_ids[name] = r["id"]

    # ---- MCP demo server（真实 stdio 进程）----
    import os
    py = os.environ.get("PY", "python3")
    # seed.py 在 src/ 下 → demo 脚本 = <src>/mcp/mcp_server_demo.py
    demo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp", "mcp_server_demo.py")
    r = await db.fetchone(conn, "SELECT id FROM mcp_servers WHERE name='demo'")
    if not r:
        r = {"id": await db.execute(
            conn, "INSERT INTO mcp_servers (name, command, args, enabled) VALUES (?,?,?,true)",
            ("demo", py, json.dumps([demo])))}
    # 自愈：历史行若指向不存在的脚本路径（如旧版 seed 少算一层 /src），纠正之
    row = await db.fetchone(conn, "SELECT command, args FROM mcp_servers WHERE id=?", (r["id"],))
    cur_args = json.loads(row["args"] or "[]")
    if not cur_args or not os.path.exists(cur_args[0]):
        await db.execute(conn, "UPDATE mcp_servers SET command=?, args=? WHERE id=?",
                         (py, json.dumps([demo]), r["id"]))
    demo_mcp_id = r["id"]

    # ---- 医疗 agent ----
    r = await db.fetchone(conn, "SELECT id FROM agents WHERE name='医疗助手'")
    if not r:
        agent_id = await db.execute(
            conn,
            """INSERT INTO agents (name, description, system_prompt, model, temperature, max_tokens, top_p)
               VALUES (?,?,?,?,?,?,?)""",
            ("医疗助手", "医疗场景示例 agent（患者-哮喘-FEV1-IgE）",
             "你是一名医疗助手，擅长解读肺功能与过敏指标（FEV1、IgE 等）。"
             "回答需引用知识库上下文；不确定时提示就医。",
             S.LLM_MODEL, 0.2, 1024, 0.9))
        for btype, ref in [("skill", skill_ids["医疗问答规范"]),
                           ("skill", skill_ids["工具调用规范"]),
                           ("mcp", demo_mcp_id),
                           ("plugin", "get_time"),
                           ("plugin", "mcp_call"),
                           ("rag", None)]:  # rag 绑定在知识库创建后
            if btype != "rag":
                await db.execute(conn,
                                 "INSERT OR IGNORE INTO agent_bindings (agent_id, type, ref_id) VALUES (?,?,?)",
                                 (agent_id, btype, str(ref)))
        r = {"id": agent_id}
    agent_id = r["id"]

    # ---- RAG 医疗知识库 ----
    r = await db.fetchone(conn, "SELECT id FROM rag_knowledge WHERE name='医疗知识库'")
    if not r:
        r = {"id": await db.execute(conn, "INSERT INTO rag_knowledge (name) VALUES (?)", ("医疗知识库",))}
    kb_id = r["id"]
    n_chunks = (await db.fetchone(conn, "SELECT COUNT(*) c FROM rag_chunks WHERE knowledge_id=?", (kb_id,)))["c"]
    if n_chunks == 0:
        from rag import rag as ragmod
        await ragmod.add_document(conn, kb_id, SEED_RAG_DOC)  # 短块 150 字符 + 20% 重叠
    await db.execute(conn, "INSERT OR IGNORE INTO agent_bindings (agent_id, type, ref_id) VALUES (?,?,?)",
                     (agent_id, "rag", str(kb_id)))

    # ---- L2 立体图种子（Visit 事件节点 + 医疗关系，模块 13 示例）----
    from memory import backend as memory
    mb = memory.get_memory_backend()
    if (await mb.l2_query({"nodes": 1}))["counts"]["nodes"] == 0:
        await mb.l2_node_or_create("P001", "Patient", {"name": "王建国", "age": 52})
        await mb.l2_node_or_create("ASTHMA", "Disease", {"name": "支气管哮喘"})
        await mb.l2_node_or_create("ASPIRIN", "Drug", {"name": "阿司匹林"})
        await mb.l2_node_or_create("V20260909", "Visit", {"id": "V20260909",
                                                           "visit_date": "2026-09-09"})
        await mb.l2_merge_edge("P001", "V20260909", "ATTENDED",
                               {"visit_date": "2026-09-09"})
        await mb.l2_merge_edge("P001", "ASTHMA", "DIAGNOSED_WITH",
                               {"diagnosis_date": "2026-09-09", "confidence": 0.95})
        await mb.l2_merge_edge("V20260909", "ASTHMA", "PRIMARY_DIAGNOSIS",
                               {"diagnosis_date": "2026-09-09"})
        await mb.l2_node_or_create("ENT:支气管舒张试验", "Test", {"name": "支气管舒张试验"})
        await mb.l2_merge_edge("V20260909", "ENT:支气管舒张试验", "ORDERED_TEST",
                               {"test_date": "2026-09-09"})
        await mb.l2_node_or_create("ENT:FEV1改善量", "TestRecord", {"name": "FEV1改善量"})
        await mb.l2_merge_edge("P001", "ENT:FEV1改善量", "HAS_TEST_RESULT",
                               {"test_date": "2026-09-09", "test_name": "FEV1改善量",
                                "value": 240, "unit": "ml"})
        await mb.l2_node_or_create("ENT:总IgE", "TestRecord", {"name": "总IgE"})
        await mb.l2_merge_edge("P001", "ENT:总IgE", "HAS_TEST_RESULT",
                               {"test_date": "2026-09-09", "test_name": "总IgE",
                                "value": 394.0, "unit": "KU/L"})
        await mb.l2_merge_edge("ENT:总IgE", "ASTHMA", "INDICATES_POSITIVE",
                               {"confidence": 0.9, "note": "过敏体质"})
        await mb.l2_merge_edge("ASTHMA", "ASPIRIN", "CONTRAINDICATES",
                               {"note": "NSAIDs 不耐受"})
        # B+ 树分桶（立体图：实体节点=B+ 树根，PD-008 四维度）
        await mb.l2_bucket_write("P001", "date", "2026-09-09",
                                 {"event": "门诊", "summary": "支气管舒张试验阳性，FEV1 改善 240ml，IgE 394 KU/L"})
        await mb.l2_bucket_write("P001", "date", "2026-07-14",
                                 {"event": "复测", "summary": "FEV1 2.10L，IgE 410.20 KU/L，控制不佳"})
        await mb.l2_bucket_write("P001", "entity", "王建国", {"ref": "P001"})
        await mb.l2_bucket_write("P001", "topic", "哮喘", {"ref": "ASTHMA"})
        await mb.l2_bucket_write("P001", "process", "肺功能检查", {"ref": "ENT:FEV1改善量"})
    return {"agent_id": agent_id, "kb_id": kb_id, "demo_mcp_id": demo_mcp_id,
            "skill_ids": skill_ids}
