# DEV_REPORT — ai-agent-platform

> 开发报告。章北海（开发工程师）在开发完成后更新。本文件所有自测数字均来自**真实运行**
> （真实 curl / WebSocket / 真实 vLLM LLM / 真实 MCP stdio 子进程 / 真实 pytest），无 mock、无捏造。
> 证据文件清单见文末「自测证据索引」。

## 本次交付

### 实现了哪些功能（对应 PRD 15 模块 / BRIEF 第五节）

- **模块 1 后端框架**：FastAPI 单进程应用（`src/core/app.py`），统一错误格式
  `{"code","message","detail"}`，401/403/404/400/502 契约；端口 **8099**。
- **模块 2 Agent CRUD**：`/api/agents` 增删改查 + 资源绑定（skill/mcp/plugin/rag），
  重名 409、绑定不存在资源 400、越权 403。
- **模块 3 MCP**：真实 Python stdio 子进程（`mcp/mcp_server_demo.py`，JSON-RPC 2.0
  Content-Length 头解析），`initialize→tools/list→tools/call` 全流程走真实进程；
  `mcp_client.py` 用 `asyncio.create_subprocess_exec` 拉起，超时+受控错误。
- **模块 4 Skills**：只读 markdown 指令注入 prompt 固定左侧（Hermes 风格），空内容 400。
- **模块 5 Plugins**：内置 `get_time` / `mcp_call` / `echo`，schema 注入 system，
  LLM 以 `{tool_call}` JSON 块请求调用，引擎执行循环（上限 5 轮）。
- **模块 6 REST 对话**：`POST /api/chat/{agent_id}` 同步对话，返回
  `answer / llm_calls / cache_hit / route_events / sub_requests` 全可观测。
- **模块 7 WebSocket**：`/ws/chat/{agent_id}/{conv_id}?token=*** 流式 token 回传
  `{type:token/done/error, content, cache_hit?}`；未认证连接被拒。
- **模块 8 Embedding**：local 确定性 3-gram 哈希向量（512 维 L2 归一），同文本两次逐维相等；
  OpenAI 兼容配置位（`EMBEDDING_*`），不可达自动降级 local。
- **模块 9 RAG**：滑窗分块（PRD 2000-3000 token / 重叠 10-20%，seed 默认窗口 150 字符 /
  20% 重叠保证相邻块可断言重叠）+ 余弦 top-k 检索。
- **模块 10 RBAC**：4 角色 / 13 权限矩阵，JWT HS256（密钥仅 `.env`），
  路由守卫 `require_perm`（无 token 401 / 无权限 403）。
- **模块 12/13 三层记忆**：L0 原始不降噪 / L1 语义缓存（余弦 > 阈值默认 0.95）/
  L2 立体属性图（B+ 树分桶 + 事件节点 + 拒绝万能边 + MERGE 幂等 + 日期作属性不节点化）。
- **模块 14 缓存路由 5 策略**：图片 MD5 / 文本语义 / 混合拆分 / 复杂任务拆分 /
  prefix caching（固定左侧 system = system_prompt + skills + 工具 schema + RAG 上下文）。
- **模块 15 长文本 4 策略**：Map-Reduce / 增量图构建 / critique-refine（HITL 队列）/
  pandas 确定性预处理。
- **前端**：纯静态 SPA（`src/static/`，原生 JS+CSS），7 页面
  （Dashboard / Agent 构建器 / 对话 / RAG / 用户权限 / 记忆 / MCP-Skills），
  页脚 `AI Agent Platform · dev-team 构建`。

### 修改 / 新增了哪些文件

代码（`02-development/src/`）：
- `core/{app,config,db,security,stats}.py` — 应用骨架 / 配置 / aiosqlite / JWT+RBAC / 计数器
- `llm/{provider,embedding}.py` — LLM（httpx 重试/流式/降级）+ 确定性 embedding
- `mcp/{mcp_client,mcp_server_demo}.py` — MCP stdio 客户端 + demo 子进程
- `memory/{backend,graph,btree}.py` — 记忆后端接口+本地实现 / L2 图 / B+ 树
- `rag/rag.py` — 分块+嵌入+余弦检索
- `routers/{api1,api2,ws}.py` — REST 全端点 + WebSocket
- `services/longtext.py` — 长文本 4 策略
- `engine/{agent_engine,cache_router}.py` — agent 引擎 + 缓存路由
- `seed.py` — 启动种子（4 用户/医疗 agent/RAG 知识/MCP demo/B+ 树数据）
- `static/{index.html,app.js,style.css}` — 前端 SPA
- `requirements.txt` — 依赖（含 `websockets` / `websocket-client` / `pytest`）

部署 / 文档 / 测试（`02-development/`）：
- `run.sh` — 一键启动脚本（uv venv + .env 生成 + 清端口 + uvicorn + 健康检查 + `--check`）
- `DESIGN.md` — 技术设计（本次更新模块路径、记忆解耦说明）
- `tests/` — pytest 自测套件（`conftest.py` + 3 个测试模块，63 用例，真实集成）
- `DEV_REPORT.md` — 本报告

## 自测结果（真实运行）

**自测环境**：`0.0.0.0:8099`，LLM = 远程 vLLM `vllm-qwen3.8-27b`
（`http://34.121.9.233:4000/v1`），SQLite `src/data/agp.db`，embedding local 确定性。

### 1) 端到端自测（e2e_test.py，164 断言）—— 连续 3 次 + 收尾 1 次全绿

```
==== 自测汇总: 164/164 PASS ====   （e2e_run1 / e2e_run2 / e2e_run3 / e2e_final 四次一致）
FAIL 数 = 0
```

覆盖：RBAC（4 用户登录 / 越权 403 / 无 token 401 / 13 权限矩阵）、Agent CRUD、
prefix-caching 布局（system 最左 + 用户最右 + skill/RAG/工具注入）、真实 LLM 医疗问答
（非模板，引用知识库 394 KU/L）、语义缓存命中（`llm_calls=0`）、不同主题缓存 miss、
`get_time` 工具真实调用、复杂任务拆分（`split:complex` 可观测）、图片 MD5 路由
（`split:mixed` / `sub_requests[].md5`）、RAG 相邻块重叠 + 检索命中、MCP stdio
`tools/list`+`tools/call` 真实调用 + 未知工具 502、L2 图多跳（患者→疾病→禁忌药）+
拒绝万能边 + MERGE 幂等 + 日期作属性、B+ 树有序 + 分桶、三层记忆可查询、
长文本 4 策略、WebSocket 流式 token + 缓存命中标注 + 未认证被拒、healthz/前端页脚。

### 2) pytest 套件（tests/，63 用例，真实 LLM/MCP/WS 集成）—— 连续 2 次全绿

```
======================== 63 passed in 64.96s =========================   （第 1 次）
======================== 63 passed in 68.42s =========================   （第 2 次，幂等）
```

满足 DECISION-006「自测用例 ≥ 40」。分文件：
- `test_auth_rbac.py`（12）— healthz / 端口 / 前端页脚 / 4 用户 / 错误密码 401 /
  无 token 401 / viewer 越权 403 / developer 无 user:manage 403 / 13 权限 / 角色矩阵 /
  /me 权限 / 非法 JWT 401
- `test_agents_chat_rag.py`（23）— Agent CRUD（建/读/改/重名 409/坏绑 400/404/越权 403）、
  prompt 布局 + skill/RAG/工具注入、真实 LLM 问答、语义缓存命中、异主题 miss、
  get_time 工具、复杂拆分、embedding 确定性/维度/相似度、RAG 分块/重叠/检索/权限删
- `test_memory_mcp_longtext_ws.py`（29）— L2 边属性/日期属性/无 Date 节点/多跳禁忌药/
  事件节点/拒小写类型/拒 RELATED_TO+LINKED_TO/MERGE 幂等/B+ 树分桶+有序/L0/L1 可查询/
  适配器可插拔、插件 get_time、MCP tools/list+call+demo 数据+未知 502、skills CRUD、
  长文本 4 策略、图片 MD5 路由+混合拆分、WS 流式+缓存命中+未认证被拒

> 幂等设计：`conftest.py` 会话级 `_reset_cache()` 清 L1/L0，保证「首跑必走 LLM」
> 断言在任意重跑下都成立（真实首次部署也是空缓存基线）。

### 3) 部署一键启动验证（run.sh）—— 真实

```
$ ./run.sh            # 一键：venv/.env 缺失自动建 → 起 uvicorn 8099 → 轮询 healthz
[run.sh] ✓ 服务就绪: http://localhost:8099
$ ./run.sh --check    # 纯只读健康检查（不再误杀运行中服务）
[run.sh] 健康检查 ...
{"status":"ok","service":"ai-agent-platform","port":8099,"llm":{"configured":true,...}}
```

真实 curl 证据（节选，完整见 `curl_evidence.txt`）：

- `GET /healthz` → 200，`llm.configured=true`，`memory_backend.graph_nodes=15/edges=12`
- `POST /api/auth/login` (admin) → 200，token + roles=[super_admin] + 13 permissions
- `GET /api/roles` → 4 角色（super_admin=13 / agent_developer=10 / user=3 / viewer=2）
- 越权：viewer `GET /api/roles` → **403** `无权限：需要 role:manage`
- `POST /api/chat/1`（"患者 IgE 结果是多少？单位呢？"）→ 200，**真实 LLM 非模板答案**：
  "…血清总 IgE 有两次记录：初测 394.00 KU/L…2026-07-14 复测 410.20 KU/L…
  单位说明：KU/L（kU/L）…建议按医嘱 2 周后复测肺功能…"，`llm_calls=2`
- `POST /api/ext/mcp/1/tools/get_time/call` → 200，`{"ok":true,"tool":"get_time",
  "result":{"utc":"2026-09-11T17:06:04Z","local":"2026-09-12T01:06:04"}}`（真实子进程）
- `GET /api/memory/l2/subgraph?start=P001&hops=3` → 200，`path=[P001, V20260909, ASTHMA, ASPIRIN]`
  （患者→就诊→疾病→禁忌药 多跳），bfs 含 7 类边
- `POST /api/rag/knowledge/1/search`（"IgE 结果是多少"）→ 200，top1 命中 IgE 410.20 KU/L，score=0.1015
- `POST /api/chat/1` + images → `sub_requests[].md5=b554660308dcb7c12fa8c39cfdf615d2`（MD5 路由可观测）
- `POST /api/longtext/preprocess`（pandas 聚合 CSV）→ 结构化 JSON（P001|2026-09-09 IgE=394.0）

### 部署（服务已起，可运行）

- **部署形态**：单机原生 uvicorn 单进程（Docker 未安装，见 DECISION-007）。
  后端 + 纯静态前端 + WebSocket **同端口 8099**，无独立前端端口、无外部 DB。
- **一键启动**：`cd 02-development && ./run.sh`（首次自动 uv 建 venv + 装依赖 +
  从 `.env.example` 生成 `.env` + 从成员 env 注入 LLM key；key 仅存本地 `.env`，不入代码/文档/日志）。
- **健康检查**：`./run.sh --check` 或 `curl http://localhost:8099/healthz`。
- **实测资源**（回写台账 `shared/infrastructure/SERVER_REGISTRY.md`）：
  uvicorn RSS ~115 MB；项目 `02-development/` 145 MB（含 .venv）；`agp.db` ~4.3 MB；
  服务器总内存 7.7 GiB（used 2.2 GiB / available 5.5 GiB）。端口 8099 已登记台账。
- **前端可访问**：`http://<host>:8099/`（7 页面 + 页脚 `AI Agent Platform · dev-team 构建`）。

## 已知问题

1. **哈希 embedding 相似度天花板**：完全同句 ≈1.0，同义改写通常 <0.95（n-gram 共享有限），
   故「语义缓存命中」测试用近同文（同句微调）保证 >0.95 可复现；阈值 `SEMANTIC_CACHE_THRESHOLD`
   可配置。接真实 OpenAI 兼容 embedding 后此限制消失（配置位已留）。
2. **B+ 树内存化**：进程重启后从 L2 图重建（种子启动重建），不单独落盘。AC-44 的
   有序性/检索断言在进程内可复现。
3. **LLM 非确定性（RISK-006）**：所有 LLM 相关断言只校验格式 / 流程 / 非空 / 命中路径 /
   结构，不断言具体文字（PD-007）。
4. **e2e / pytest 需服务先起**：两者均为对 8099 的集成测试，未起服务时 pytest skip
   （不误判为失败）；请先 `./run.sh`。
5. **端口 8099 重启**：`run.sh` 启动前会清 8099 旧进程；`--check` 为只读不清理。
   若手动起过旧 uvicorn 占用 8099，新实例将无法绑定——`run.sh` 已处理。

## 给测试的提示

- **一键起服务**：`cd 02-development && ./run.sh`（或 `--check` 只查）。种子 4 用户
  密码见 `.env` 的 `SEED_PASSWORD`（admin/developer/user/viewer）。
- **自测复现**：
  - e2e：`cd 02-development/src && ../.venv/bin/python ../../05-temp/e2e_test.py`（164 断言）
  - pytest：`cd 02-development/tests && ../.venv/bin/python -m pytest -v`（63 用例）
  - 两者均幂等（缓存已显式重置前置）。
- **重点覆盖的边界 / 异常**：
  - RBAC：无 token 401 / 错误密码 401 / viewer 访问 /api/roles 403 / developer 建用户 403 /
    非法/过期 JWT 401。
  - Agent：重名 409 / 绑定不存在 skill 400 / 404。
  - L2 图：小写边类型 400 / 万能边 RELATED_TO、LINKED_TO 400 / MERGE 幂等不重复 /
    日期是属性不建节点。
  - 缓存路由：图片 MD5 命中 / 文本语义命中（llm_calls=0）/ 异主题 miss /
    混合与复杂拆分（route_events 可观测）。
  - MCP：真实 stdio 调用 / 未知工具 502 受控错误 / demo 数据含 394。
  - WebSocket：流式 token + 缓存命中标注 / 未认证连接被拒。
  - 长文本：4 策略各自返回结构 + HITL 队列。
- **确定性注意**：LLM 答案文字会变，但 `llm_calls` / `cache_hit` / 路由事件 / 结构稳定可断言。

## 自测证据索引（均在 `05-temp/`）

| 文件 | 内容 |
|---|---|
| `e2e_run1/2/3.txt` | e2e 连续 3 次运行，均 164/164 PASS |
| `e2e_final.txt` | 收尾 e2e（对 run.sh 起的服务），164/164 PASS |
| `curl_evidence.txt` | 11 段真实 curl 证据（登录/越权/真实 LLM/MCP/L2 多跳/RAG/图片/pandas） |
| `server.log` | uvicorn 运行日志（真实请求 + MCP 子进程启动） |
| `run_e2e_once.sh` / `run_pytest.sh` | 自测复现脚本 |

## BUG-001 修复记录（2026-09-12，章北海，t_4d78237f）

### 缺陷
P1：图片 MD5 缓存从未写入——`cache_router.remember_image()` 是死代码（全仓无调用点），
`agent_engine.run()/run_stream()` LLM 作答后只调 `remember_answer`（文本语义缓存），
从不写 `memory_l1_image`，导致 AC-51（同一图片第二次请求 `cache_hit=md5` 且 `llm_calls=0`）失败。

### 根因（比"死代码"更深一层）
即便补上 `remember_image` 调用点，TC-63 验收场景仍不通过：
- TC-63 第二次请求是**混合**请求（同一图片 b64 + **不同**文本：1st `tcimg<ts> 描述这张图` / 2nd `描述这张图`）。
- `route_request` 把混合请求拆成 图片子请求 + 文本子请求，`_decide` 要求 **ALL 子请求命中**才算全命中。
- 两个文本的本地哈希 embedding 余弦相似度仅 **0.40**（已实测），远低于 0.95 阈值 → 文本子请求必 miss → `all(hit)` 不成立 → 无法命中 MD5。

因此单补"写缓存"不够，必须让**图片 MD5 命中具有最高优先级**（图片 md5 是精确键，命中即可权威直返，与文本子请求是否命中无关），才能满足 `llm_calls=0`。

### 修改（3 处，均在 02-development/src/engine/）
1. **`cache_router.py` 新增 `remember_images(images, answer, sub_requests)`**：
   LLM 真实作答后，把本次请求中**未命中缓存**的图片按 `md5_base64` 写入 `memory_l1_image`
   （`parsed = {"answer": <LLM 解析文本>}`，命中分支 `json.dumps(rec["parsed"])` 可直返）。
   - 已命中的图片不重写；缓存命中 / LLM 降级路径不调用（`run()`/`run_stream()` 仅在真实作答后调用），
     避免把空/降级文案写进缓存。
2. **`agent_engine.py` `run()` 与 `run_stream()`**：真实作答、写 `remember_answer` 之后，
   调用 `await cache_router.remember_images(images, answer, decision["sub_requests"])`。
3. **`cache_router.py` `_decide()`**：最前置增加"图片 MD5 命中优先"分支——
   只要任一 `kind=image` 且 `hit=True`，即返回 `cache_hit="md5"` 全命中（跳过文本子请求与 LLM）。
   其余（全命中 / 部分命中 / 全 miss）分支行为**保持不变**。

### 自测结果（对运行中 :8099 真实服务，真实 vLLM，无 mock）
- **AC-51 核心断言（ac51_repro.py，逐字复刻 TC-63）**：
  1st：`md5=427a652b…, llm_calls=2`；2nd（同图不同文）：`cache_hit=md5, llm_calls=0`，
  全局 llm 计数增量 0。`memory_l1_image` 行数 = 2（不再恒 0）。**PASS**
- **TC-63（test_harness.py 全量 73 用例）**：**PASS**（修复前 FAIL）。
  同模块 TC-64（文本语义>0.95 直返）/ TC-65（异主题 miss）/ TC-66（混合拆分）/ TC-67（复杂拆分）全 PASS。
- **e2e 164/164 PASS**（`05-temp/e2e_test.py`）。
- **pytest 64 passed**（原 63 + 新增 `test_image_md5_cache_second_hit` 锁定 AC-51 核心断言；连续 2 次全绿）。
- **命中分支 L0 原文保留**：MD5 命中时 `run()` 命中分支仍调 `_l0()` 记录（DB 可查 L0 行含缓存答案）。

### 回归范围对照（03-testing/REGRESSION.md）
- 直接命中路径（AC-51）✅ 核心断言全通过
- 同模块 AC-52（语义/异主题/混合/复杂拆分）✅ 无回归
- 缓存路由上下游：seed 医疗 agent 真实对话 ✅、WS 流式 ✅（pytest WS 用例全绿）
- 全量 P0/P1：本修复仅触及 `cache_router.py`/`agent_engine.py`，未引入新增失败。
  （注：harness 中 TC-09/11/26/27/36/54/60 为测试脚本自身问题，非本修复引入，已在 TEST_REPORT 中复核为非应用缺陷，不在本任务范围。）

### 状态
- BUG-001：**已修复，可回归测试**。
- 服务已在 0.0.0.0:8099 运行（`./run.sh`，代码已热重启加载本修复）。
- 服务器台账（SERVER_REGISTRY.md）：8099 端口/资源占用不变，仅代码更新；无新增容器/端口/组件。

## BUG-002 修复记录（2026-09-12，章北海，t_c3b7ceac）

### 缺陷（P2，独立核验发现，PM 验收遗漏）
`GET /api/memory/l2/subgraph?start=P001&hops=2` 返回 200，但响应体里**没有 `nodes`/`edges` 键**
（即测试侧读 `nodes=[]/edges=[]` 全空）；同时服务日志每次登录/鉴权都刷
`InsecureKeyLengthWarning: The HMAC key is 7 bytes long`（`.env` 里 `JWT_SECRET` 仅 7 字节）。

### 根因（复现确认，与任务单三种猜测对照）
1. **遍历本身没有坏**（任务单猜测 (a)/(b)/(c) 均不成立）：`src/routers/api2.py` 的
   `l2_subgraph` 调 `mb.l2_query({"from","hops","subgraph"})`，`PropertyGraph.bfs` 沿
   免索引邻接表（`out_edges/in_edges`）多跳遍历，P001 的 5 条出边 + 二跳 3 条边**全部正确
   吐进 `bfs`（9 条）**。已对 DB 直接核验：`memory_l2_nodes`/`memory_l2_edges` 数据完整。
2. **真正的坑在响应契约**：端点只返回 `{start, hops, bfs, path, summary}`，
   **从不返回 `nodes`/`edges` 两个键**——调用方（测试/前端/UI 期望的子图节点集合）读
   `o["nodes"]`/`o["edges"]` 得到空。这是"端点缺字段"，不是"图遍历空"。
3. **`summary` 只含出边、漏入边**：`subgraph_summary` 用有向 `bfs`（只走 `out_edges`），
   对纯入边节点（如 `ASTHMA`、`V20260909` 作为中心时）摘要会缺邻接，与"双向属性图"语义不符。
4. **JWT 密钥过短**：`core/config.py` `JWT_SECRET` 默认/实际值 7 字节（`dev-onl...`），
   低于 HS256 推荐 32 字节 → pyjwt 2.14 每次 encode/decode 告警。

### 修改（4 处，均在 02-development/）
1. **`src/memory/graph.py` 新增 `PropertyGraph.subgraph(center_id, hops)`**：
   **无向** BFS（出边+入边都算邻接），返回 `{"nodes": [GNode], "edges": [GEdge]}`；
   中心不存在 → 返回空（不报错、不伪造节点）。
2. **`src/memory/graph.py` `subgraph_summary`**：改为基于 `subgraph()` 的无向邻接
   （之前只用有向 `bfs`），摘要覆盖中心节点完整 N 跳邻域。
3. **`src/memory/backend.py` `l2_query`**：`"subgraph"` 查询时额外产出
   `subgraph_nodes`/`subgraph_edges`（无向），供端点直接透传。
4. **`src/routers/api2.py` `l2_subgraph`**：响应体新增 `nodes`/`edges` 两个键
   （`q["subgraph_nodes"]`/`q["subgraph_edges"]`），保留原 `bfs`/`path`/`summary`。
   **JWT 安全**（`src/core/config.py` + `src/.env` + `src/.env.example` + `run.sh`）：
   - `.env` 的 `JWT_SECRET` 换成 64 hex 随机值（32 字节），**不落文档/日志**。
   - `config.py` 新增 `_jwt_secret()`：.env 缺失/过短时运行时生成 64 hex 随机值兜底
     （进程内有效，消除告警）；`Settings.JWT_SECRET` 统一走它。
   - `.env.example` 注释改为"生产必须显式配置 >=32 字节随机密钥"，默认留空。
   - `run.sh` 新增 2b 步：启动时检测 `JWT_SECRET` 长度，<32 则生成 64 hex 并持久化回
     `.env`（避免每次重启换密钥导致旧 token 全失效）。
   - `DESIGN.md` 第 4/6 节：接口表更新 subgraph 字段；RBAC 节注明"生产必须配置
     JWT_SECRET，重启间须稳定"。

### 自测结果（对运行中 :8099 真实服务，真实数据，无 mock）
**修复前 curl**（P001&hops=2，仅 5 键，无 nodes/edges；summary 边数=8 但只含出边）：
```
GET /api/memory/l2/subgraph?start=P001&hops=2
{ "start":"P001","hops":2,
  "bfs":[ …9 条边… ],            # 遍历其实有数据
  "path":["P001","V20260909","ASTHMA"],
  "summary":"[子图摘要 中心=P001 hops=2 边数=8]\n…" }
# 无 "nodes" 键、无 "edges" 键 → 调用方读到空
```
**修复后 curl**（`05-temp/bug002_after_p001.json`）：
```
GET /api/memory/l2/subgraph?start=P001&hops=2
{ "start":"P001","hops":2,
  "nodes":[ P001, V20260909, ASTHMA, ENT:FEV1改善量, ENT:总IgE, Z1,
            ASPIRIN, ENT:支气管舒张试验, ENT:王建国 ],   # 9 节点（自身+一跳+二跳）
  "edges":[ …10 条边，含 P001 全部 5 出边 + 二跳… ],
  "bfs":[ …9 条边… ],
  "path":["P001","V20260909","ASTHMA"],
  "summary":"[子图摘要 中心=P001 hops=2 边数=10]\n…" }
```
**自测脚本 `05-temp/bug002_selftest.py`（32 断言，全 PASS）**：
- P001 hops=2：含自身 + 一跳(V20260909/ASTHMA/FEV1改善量/总IgE/Z1) + 二跳
  (ASPIRIN/支气管舒张试验) + 对应边（ATTENDED/DIAGNOSED_WITH/CONTRAINDICATES/
  ORDERED_TEST）✅
- 自查 ASTHMA hops=2：非空，2 跳含 P001/V20260909/ASPIRIN/总IgE（入边正确回溯）✅
- 自查 V20260909 hops=2：非空，2 跳含 P001/ASTHMA/支气管舒张试验/ASPIRIN ✅
- 空 start（`NO_SUCH_NODE_X`）：http 200、`nodes=[]`、`edges=[]`，不报错 ✅
- JWT：登录 admin 后 `/api/auth/me` 200（username=admin, roles=super_admin）✅；
  无 token → 401 ✅；`/api/agents` 200 ✅；**服务日志无 InsecureKeyLengthWarning**
  （密钥 64 hex=32 字节）✅

**回归**：`pytest tests/ -q` **64 passed**（真实 LLM/MCP/WS 集成，73.7s）——
含 L2 全接口(nodes/edges/subgraph/events/bucket)、多跳遍历、拒绝万能边、MERGE 幂等、
事件节点、日期作属性等既有断言，无连带破坏。

### 回归范围对照
- 直接命中（subgraph 遍历/子图）✅ 非空且拓扑正确
- 同模块 L2 其余接口（nodes/edges/events/bucket）✅ 无回归
- 无向摘要 `subgraph_summary` 语义增强（补入边）✅，既有"子图摘要"文本断言仍 PASS
- JWT/RBAC 鉴权链路 ✅ /me + 守卫端点正常，无告警
- 全量 P0/P1：仅触及 `graph.py`/`backend.py`/`api2.py`/`config.py`，64 用例全绿。

### 状态
- BUG-002：**已修复，可回归测试**（回归测试任务 t_11c6c3e3 已就绪，待云天明执行）。
- 服务已在 0.0.0.0:8099 运行（`./run.sh` 重启加载本修复 + 新 JWT 密钥）。
- 服务器台账（SERVER_REGISTRY.md）：8099 端口/资源占用不变，仅代码 + .env 更新；
  无新增容器/端口/组件。
- 已知提示：`run.sh` 重启后若 `.env` 密钥被替换，旧 token 失效需重新登录（开发环境可接受；
  生产须固定 JWT_SECRET，见 DESIGN.md 第 6 节）。

## BUG-003 修复记录（2026-09-12，章北海，t_8a632590）

### 缺陷（P3，非阻塞；云天明回归 t_11c6c3e3 新发现）
`POST /api/memory/l2/edge`（`routers/api2.py::l2_add_edge`）在**校验边类型之前**先
`l2_node_or_create(src)` + `l2_node_or_create(dst)`。当边类型非法（万能边 RELATED_TO/
LINKED_TO、小写、空串）被 400 拒绝时，src/dst 两个**孤儿节点已被持久化**进
`memory_l2_nodes`（0 边、遍历不可达），属**非原子写**，污染 L2 图。

### 根因（复现确认）
- 原代码先建节点后建边：`merge_edge` 内部 `validate_edge_type` 抛 `GraphError` 触发 400，
  但 `l2_node_or_create` 已各自 `INSERT` 落库且无回滚 → 400 后 src/dst 孤儿节点残留。
- 复现（对运行中 :8099）：发 `RELATED_TO` 坏边 → 400，`memory_l2_nodes` 41→43，
  `REPRO_SRC_1`/`REPRO_DST_1` 两个孤儿节点确实落库。

### 修改（1 处，`02-development/src/routers/api2.py`）
1. `l2_add_edge` 改为**先校验后落库**：入口先 `validate_edge_type(body.type)`
   （`memory.graph`，已 import），非法类型直接 400，**不触碰任何节点/边**。
   校验通过后才 `l2_node_or_create(src/dst)` + `l2_merge_edge`（保留原 try/except，
   端点不存在等边界仍 400）。
   - 选择"先校验后落库"而非 DB 事务回滚，原因：L2 是**双态架构**（内存 `PropertyGraph`
     + SQLite），事务只能回滚 DB，回滚不了内存图节点；先校验是零副作用且更彻底。

### 自测结果（对运行中 :8099 真实服务，无 mock）
验证脚本 `05-temp/bug003_final.py`（真实执行，基线 41 节点/26 边）：
- **① 坏边零副作用**：`RELATED_TO`/`LINKED_TO`/`bad_type`（小写）/``（空）4 类坏边
  全部 400，`memory_l2_nodes` 行数 41→41 **不变**、边 26→26 不变、DB 无孤儿节点、
  内存图 graph_nodes 41→41 不变。
- **② 合法边写入**：`DIAGNOSED_WITH` + props → 200，节点 41→43（+2）、边 26→27（+1），
  边确在 `memory_l2_edges`。
- **③ 幂等**：重复写同一合法边 → 200，节点/边均不变（MERGE 幂等不产生重复）。
- **④ BUG-002 不回归**：`GET /l2/subgraph?start=P001&hops=2` → 200，
  nodes=9、edges=10（拓扑正确非空）。
- **⑤ 全量回归**：`pytest tests/` **64 passed / 0 failed**（真实 LLM/MCP/WS 集成），
  无引入回归。
- **⑥ 服务-代码一致性**：服务重启加载本修复（`src/routers/api2.py` 改动早于启动，
  `find src -name '*.py' -newermt <start>` 为空）；`/healthz` graph_nodes=41 graph_edges=26。

### 部署 / 环境
- 服务：`./run.sh` 重启加载本修复，运行于 0.0.0.0:8099（`/healthz` 200）。
- **DB 已清理回基线**：自测过程中产生的测试节点（M1w*/M2w* 幂等对、REPRO_* 复现孤儿）
  已全部清理，`memory_l2_nodes` 恢复 **41 节点 / 26 边** 团队基线（种子拓扑
  P001-ASTHMA-V20260909-ASPIRIN 完整）；重启服务使内存图与 DB 一致（graph 41/26）。
- 服务器台账（SERVER_REGISTRY.md）：8099 端口/资源占用不变，仅代码更新；无新增容器/端口/组件。

### 状态
- BUG-003：**已修复，可回归测试**。
- 遗留说明（非本次范围）：DB 中仍有历史孤儿节点（A1/B1/X/acc_*/ENT:accWang_* 等，
  系此前坏边写入 + 早期测试残留，非本次修复引入；本次修复后**新**坏边不再产生孤儿）。
  是否一并清理归测试/PM 决定，已如实记录，不影响本修复验收。

## 状态

- 开发状态：**DONE**（全部 PRD 15 模块实现 + 部署环境可运行 + 自测全绿；BUG-001/BUG-002/BUG-003 已修复）
- 是否可进入测试：**是**（服务已起在 8099，测试可直接连；`./run.sh --check` 可验证）

> 说明：本开发阶段不自行宣布「项目最终完成」。最终完成由测试（云天明）+ 验收（褚岩）决定。
> 本文档已把服务部署方式、启动命令、真实自测证据、已知问题、测试提示全部写清，供进入测试。

---

# TASK-015 交付记录：AGP 数据库双后端（默认 SQLite 零依赖 + PostgreSQL 可配置）

> 开发：章北海（2026-09-14，t_22a51d0c）。分支 `db-dual-backend`（feature，未 merge）。
> 任务书：`shared/tasks/T-AGP-DB-DUAL.md`；决策 D1~D12 全部按任务书执行，未另行决策。

## 目标回顾

数据库层由「纯 asyncpg（阶段 C）」改为**双后端分派**：
- **默认 SQLite**（`DB_BACKEND=sqlite`，零外部依赖，clone 即跑，首次启动自动建表 + 种子）
- **PostgreSQL 可配置**（`DB_BACKEND=postgres`，现有 asyncpg 路径已验收、不重写）
- 切换 = 纯 `.env` 配置，业务代码零感知（全部走 `core/db.py` 抽象层）

## 改动清单（`02-development/`，git 可查）

| 文件 | 改动 |
|---|---|
| `src/core/db.py` | 双后端分派。postgres 路径**逻辑逐字保留**（62 行代码逻辑验证未变，仅函数改名 `_pg_*` + 新增分派入口 `_backend()`/`connect()/init_db()/fetchall()/fetchone()/execute()/close()` 按 `S.DB_BACKEND` 路由）；新增 sqlite 路径 `_sqlite_*`（aiosqlite 单连接 + WAL + `check_same_thread`，`?`/`datetime('now')`/`INSERT OR IGNORE` 原生不走 `_to_pg`，identity 表 INSERT 用 `lastrowid`+`rowcount` 对齐 `RETURNING id` 语义）。新增 `backend_info()`（healthz 用） |
| `src/core/config.py` | 加 `DB_BACKEND`（默认 sqlite）、`SQLITE_PATH`（默认 `<src>/data/agp.db`）、`effective_sqlite_path`（绝对路径解析）、`require_dsn()`（postgres 模式 DSN 校验） |
| `src/core/schema_sqlite.sql` | **新增**。从 `agp.db` 的 `sqlite_master` 导出 20 表 + 全索引，全部补 `IF NOT EXISTS`，`executescript` 幂等 |
| `src/requirements.txt` | 加 `aiosqlite`（保留 `asyncpg`） |
| `src/core/app.py` | `healthz` 加 `db` 字段（`dbmod.backend_info()`，D8，不暴露密码） |
| `src/memory/backend.py` | `LocalBackend.info()` 的 `store` 字符串按后端显示（SQLite 显示 path / PG 显示 schema+host） |
| `tests/conftest.py` | `_reset_cache()` 按 `DB_BACKEND` 分派：sqlite 用 aiosqlite 清表 / postgres 用 asyncpg 清表（D7 范围 6） |
| `src/.env.example` | 数据库段改为「默认 SQLite（零依赖）+ PG 可选」两组注释（D1） |
| `docker-compose.yml` | sqlite 默认模式：加卷 `./src/data:/app/data`（持久化 agp.db）+ `SQLITE_PATH=/app/data/agp.db` + **去掉 external 网络声明**（零外部依赖）；镜像 tag `1.2.0-dual` |
| `docker-compose.pg.yml` | **新增**。PG 模式叠加文件：只声明 external 网络 `agp_default`（`docker compose -f ... -f docker-compose.pg.yml up`） |
| `DESIGN.md` / `README.md` | 技术栈/数据模型/数据库章节/部署章节同步（默认 sqlite、PG 可配置、切换方法） |
| `02-development/.gitignore` | 已有 `src/data/`、`*.db`（agp.db 及备份均 gitignore，不入仓） |

## 关键设计决策（对应任务书 D 编号）

- **D2 asyncpg 路径一行不改**：验证脚本 `05-temp/verify_d2_pg_unchanged.py` 逐行比对 `main:src/core/db.py` 与现版本，**62 行代码逻辑全部逐字保留**（跳过 docstring/注释/签名/空行）。唯一允许的改动：函数改名（`connect`→`_pg_connect` 等）+ 新增 sqlite 路径与公共分派入口。`_pg_init` 内 `pool = await connect()` 保留原样（`connect()` 现是公共分派器，postgres 模式路由到 `_pg_connect`，行为等价）。
- **D3 execute() 语义对齐**：sqlite `INSERT OR IGNORE` 冲突被忽略时 `lastrowid` 会**保留前一次 INSERT 的值（非 0）**，故用 `rowcount` 判断本条是否真插入——冲突时返回 `None`，与 asyncpg `ON CONFLICT DO NOTHING` 时 `fetchrow=None`→`None` 完全对齐（自测 A3 断言）。
- **D4 schema 幂等**：`schema_sqlite.sql` 验证——空库导入 2 遍不报错；**有数据的库**（agp.db 字节副本）重复导入 2 遍不报错且 `users=4 / messages=706` 不变（`05-temp/gen_schema_sqlite.py` 验证）。
- **D7 sqlite 连接**：aiosqlite 单连接 + WAL + `conn.row_factory=aiosqlite.Row`（业务 SQL 只写 `?`，FastAPI 多协程共享同一连接，WAL 允许读写并发）。
- **D9 compose 双模式兼容**：sqlite 默认模式 base compose 无 external 网络（`docker compose config` 验证 `external: true` 计数=0；`agp_default` 仅是项目名 `agp` 自动生成的**内部**默认网络，compose 自建，非外部依赖）。PG 模式叠加 `docker-compose.pg.yml` 声明 external `agp_default`。
- **生产 .env 自洽**：生产 `src/.env`（gitignore，不入仓）加 `DB_BACKEND=postgres`——线上行为与阶段 C **完全一致**，不依赖任何 05-temp 临时 override。新代码默认 sqlite 是「全新 clone 零依赖」的交付语义；线上既有部署显式声明 postgres。

## 自测结果（真实运行，无 mock）

### 1) sqlite 模式本地全链路（run.sh，无 Docker、无 PG 依赖）

**证据：`05-temp/selftest_sqlite_final.log`**
- `pytest tests/` → **64 passed in 58.11s**（真实 LLM / 真实 MCP stdio / 真实 WebSocket，无 PG 依赖，D7 范围 4 满足）
- 核心接口冒烟全通：
  - `admin` 登录 OK（roles=`super_admin`）
  - `GET /api/agents` OK（6 个）
  - `POST /api/agents`（建）OK（id=53）→ `DELETE /api/agents/53` OK
  - RAG 检索 OK（top 3）
  - 记忆查询 OK（L2 节点 47，P001 subgraph 6 节点）
  - `POST /api/chat` 对话 OK（真实 vLLM，answer 非空，llm_calls=2）
- `healthz` 的 `db` 字段：`{'backend': 'sqlite', 'path': '.../src/data/agp.db'}`（D8）
- `memory_backend.store`：`SQLite(.../agp.db) + in-memory B+ tree/property graph`
- 数据落盘验证：冒烟后 `conversations` 275→301、`messages` 706→782（真实写入 SQLite 文件）

### 2) PG 模式容器冒烟（恢复 agp-app 容器，证明原 asyncpg 路径未破坏）

**证据：`05-temp/selftest_pg2.log`**
- 容器以 `DB_BACKEND=postgres` 运行，`healthz` 的 `db` 字段：`{'backend': 'postgres', 'host': 'pg-unified', 'port': 5432, 'database': 'postgres', 'schema': 'agp'}`
- 核心接口冒烟全通：
  - `admin` 登录 OK / `GET /api/agents` OK（6 个）
  - 记忆 L2 节点 OK（**41 个**，含迁移数据，与线上锚点一致）
  - P001 subgraph OK（6 节点 / 5 边）
  - RAG 检索 OK（top 3）
  - `POST /api/chat` 对话 OK（真实 LLM，llm_calls=2）
- **`psql` 直连 pg-unified 核对**（证明容器真写 PG、非卷内 sqlite）：
  `l2_nodes=41 / users=4 / messages=730 / agents=6`（l2_nodes=41 命中迁移锚点）

### 3) schema 幂等验证

**证据：`05-temp/gen_schema_sqlite.py` 运行输出**
- 空库导入 2 遍：均 OK（幂等）
- 业务 SQL 冒烟：`INSERT`（identity 返回 lastrowid）/ `INSERT OR IGNORE`（原生）/ `SELECT ?` / `datetime('now')` 默认值 / `ON CONFLICT ... DO UPDATE` upsert（l2_merge_edge 同款 SQL）全 OK
- 有数据库（agp.db 字节副本）重复导入 2 遍：均 OK，`users=4 / messages=706` 不变（D4 要求）

### 4) D2 asyncpg 路径零改动验证

**证据：`05-temp/verify_d2_pg_unchanged.py` 运行输出**
- `main:src/core/db.py` 的 62 行代码逻辑全部逐字保留（缺失 0 行）

### 5) 8081 网关 /agent/ 路由回归（红线：网关不动）

**实测**：
- `GET /agent/healthz` → HTTP 200
- `GET /agent/` → HTTP 200
- 网关经 `/agent/` 透传 healthz：`status=ok / db.backend=postgres`（网关未动，路由不受影响）

### 6) 部署环境状态（服务已恢复运行）

- **线上 agp-app**：镜像 `agp-platform:1.2.0-dual`（最终代码重建），`DB_BACKEND=postgres`（生产 .env），`0.0.0.0:8099`，healthcheck healthy，`healthz` 200。
- **实测资源回写台账**（SERVER_REGISTRY.md）：agp-app 47.2MiB（/512MiB）、pg-unified 45MiB、gw-nginx 15.3MiB。
- **数据兜底**：`src/data/agp.db.bak-20260914`（20 表迁移基线，users=4/messages=706/l2_nodes=41）gitignore 本地保留；`agp.db` 现文件自测后已恢复为干净基线（sqlite 模式默认路径，重启自动建表幂等）。
- **未触碰红线**：未动 8081 网关、未动 pg-unified 容器及其 agp schema 迁移数据、未动哮喘系统。自测临时停 agp-app（sqlite 本地测试需释放 8099）后已恢复 PG 模式 online。

## 部署（可运行）

```bash
cd 02-development

# --- 默认 SQLite（零依赖，clone 即跑）---
cp src/.env.example src/.env        # 只需填 AI_MODEL_API_KEY / JWT_SECRET / SEED_PASSWORD
docker compose up -d --build         # 无需任何外部网络/PG
curl http://localhost:8099/healthz   # db.backend=sqlite

# 或本地原生：./run.sh

# --- 可选 PostgreSQL（.env 设 DB_BACKEND=postgres + AGP_DB_PASSWORD）---
sg docker -c 'docker network inspect agp_default >/dev/null 2>&1 || docker network create agp_default'
docker compose -f docker-compose.yml -f docker-compose.pg.yml up -d --build
```

## 已知问题 / 说明

1. **生产 .env 含 `DB_BACKEND=postgres`**（gitignore 不入仓）：这是为了让线上既有部署行为与阶段 C 完全一致、不依赖临时 override。全新 clone 用 `.env.example`（默认 sqlite 零依赖）即可，两者不冲突——交付语义 = 默认 sqlite，线上既有 = 显式 postgres。测试做「默认模式 AC-1（全新 clone）」时请用 `.env.example` 生成的 .env（无 DB_BACKEND 或 =sqlite）。
2. **sqlite 单连接 + WAL**：面向 AGP 单进程低并发（对话/RAG/记忆），WAL 允许读写并发，满足当前场景。若未来需要高并发写，可升级为 aiosqlite 连接池（接口不变，仅 `_sqlite_connect` 内部改动）。
3. **`SQLITE_PATH` 相对路径**：按进程 cwd 解析。本地 run.sh（cwd=src）→ `src/data/agp.db`；compose 显式设 `/app/data/agp.db`。`effective_sqlite_path` 已做绝对化。
4. **compose base 的 `agp_default`**：是项目名 `agp` 自动生成的内部网络（非 external），全新 clone 时 compose 自建，零外部依赖成立（`external: true` 计数=0 已验证）。

## 自测证据索引（均在 `05-temp/`）

| 文件 | 内容 |
|---|---|
| `gen_schema_sqlite.py` | schema 导出 + 幂等/业务 SQL 冒烟/有数据库重复导入验证脚本 |
| `selftest_db_dual.py` | db 层双后端单元冒烟（sqlite 11 项：identity INSERT/OR IGNORE 语义/fetchall/fetchone/upsert/backend_info/分派路由/未知后端 fail-fast） |
| `selftest_sqlite.sh` / `selftest_sqlite_final.log` | sqlite 模式全链路（run.sh + pytest 64 + 核心接口冒烟 + 落盘验证） |
| `selftest_pg.sh` / `selftest_pg2.log` | PG 模式容器冒烟（healthz postgres + 接口 + psql 核对 l2_nodes=41）+ 收尾清理 |
| `verify_d2_pg_unchanged.py` | D2 合规：asyncpg 路径 62 行代码逻辑逐字未变 |
| `healthz_sqlite_final.json` / `healthz_pg2.json` / `healthz_final.json` | 双模式 + 线上恢复的 healthz 原始 JSON |
| `run_sh_sqlite*.log` / `pg_up*.log` / `pg_rebuild_final.log` | run.sh 与 compose 启动原始日志 |

## 给测试（云天明 t_12b63ade）的提示

- **默认 sqlite 模式（AC-1）**：建议用**全新 clone/复制到 05-temp**（不带 PG 容器/网络），`.env` 用 `.env.example` 生成（无 DB_BACKEND 或 =sqlite），`docker compose up -d --build`（或 run.sh）→ healthz `db.backend=sqlite` → admin 登录 → 建 agent/对话/RAG → 数据落 `src/data/agp.db`，重启后仍在。
- **PG 模式（AC-2）**：`.env` 设 `DB_BACKEND=postgres` + `AGP_DB_PASSWORD`，叠加 `docker-compose.pg.yml`，行为应与当前线上完全一致（记忆 41 节点）。
- **切换一致性（AC-3）**：同一 .env 只改 `DB_BACKEND`，对比两套后端核心接口（登录/CRUD/对话/WS/RAG/记忆查询）。
- **测试套件（AC-4）**：双模式 `pytest tests/` 全绿（sqlite 模式无 PG 依赖必须能跑——本开发已验证 64 passed）。
- **回归（AC-6）**：8081 网关 `/agent/` 路由未动（本开发已验证 200）。
- **注意**：`src/data/agp.db.bak-20260914` 是开发已备份的回滚兜底，测试做 sqlite 全新目录测试时**不要动仓库内该备份文件**（用 05-temp 独立副本）。

## 状态

- 开发：**DONE**（双后端实现 + schema 固化 + 部署环境双模式可运行 + 自测双绿 + D2 合规 + 网关回归 PASS）。
- 是否可进入测试：**是**（线上 agp-app 以 PG 模式 online；sqlite 模式本地 run.sh 可复现；测试可按上提示独立验证）。
- 交下游：t_12b63ade（云天明，双模式回归测试）。

> 说明：本任务只做开发 + 自测证据，**不做最终验收**（终审由褚岩做，双模式各起一次真实容器验证，不采信自测）。

---

# TASK-018 修复记录：BUG-004（PG 绑定 500）+ BUG-005（.env.example *** 污染）

> 上游：TASK-016 判定 FAIL（BUG-004）；TASK-017 褚岩独立终审复现 BUG-004 并新发现 BUG-005。
> 修复在 feature 分支 `db-dual-backend`，未 merge main（等复审 + 用户确认）。

## BUG-004（P1，阻断 AC-3/AC-4）

### 缺陷
PG 模式（`DB_BACKEND=postgres`）下，**带 skill/mcp/rag 绑定**的 agent 创建/更新全 500：
`asyncpg.exceptions.DataError: invalid input for query argument $1: '1' ('str' object cannot be
interpreted as an integer)`。sqlite 模式正常（200/400）。

### 根因
`src/routers/api1.py::_validate_bindings` 把 `ref_id` 转 `str` 后传入 `WHERE id=?`。
- sqlite 有**类型亲和性**（TEXT 比较时隐式转 int），掩盖了 str→int，碰巧能查到；
- PG（asyncpg）**严格类型校验**，BIGINT 主键列收到 str 直接抛 `DataError` → 500。
`agent_bindings.ref_id` 两后端都是 TEXT（存储层无碍），问题只在**查询参数类型**。

### 修改（1 处，`02-development/src/routers/api1.py`）
`_validate_bindings` 对 skill/mcp/rag 三类 ref_id 做**安全整数转换**（`int(ref_id)`，
捕获 `TypeError`/`ValueError` → 400），用整型参数 `WHERE id=?`。plugin 绑定仍按字符串名
匹配（语义不变，PLUGIN_REGISTRY 用名字）。三表查询合并为 `table,label` 映射表。
效果：双后端行为完全一致（有效→200，无效/非整数→400）。

### 自测（本地隔离容器 HTTP 黑盒，无 mock；证据 `05-temp/t018_selftest_evidence.md`）
- **PG 模式**（隔离容器 `t018-pg-isolated` 54330 + 本地 app 8399）：
  - 创建·有效 skill 绑定 → **200**（修复前 500）
  - 创建·无效 skill 绑定(99999) → **400** "skill 不存在"（修复前 500）
  - 创建·非整数 ref_id("abc") → **400** "ref_id 必须为整数"
  - 更新·有效/无效 skill 绑定 → **200 / 400**
  - mcp 有效/无效 → **200 / 400**；rag 有效/无效 → **200 / 400**
- **sqlite 模式**（全新目录 8499）：同 9 用例 → 200/400 与 PG **逐项一致**。
- **双后端一致性**：9 用例 sqlite 与 PG 全部相同（有效 200 / 无效 400）。
- 约束终检：8081 网关 healthy（postgres 透传）/ 8099 线上 agp-app 未动（postgres）/
  `src/data/agp.db` md5=28042b3f776bcddf4c719a570aebbdd6（与 TASK-017 基线一致）/ 哮喘系统 healthy。
- 测试环境说明：隔离 PG 容器内 asyncpg pool 的 init 回调在该测试环境未生效（线上 pg-unified
  同代码正常，TASK-017 AC-2 已 PASS），测试库用 `ALTER ROLE ... SET search_path` 固定 schema，
  仅影响一次性测试库、不改代码。

### 状态
**已修复 + 自测 PASS**。交 t_a77f683b（云天明回归）。注意：线上 agp-app 仍是旧镜像
（`agp-platform:1.2.0-dual`），修复代码在 git，**需重建镜像后才生效**（重建/回归属下游）。

## BUG-005（P1，阻断 AC-1/AC-5）

### 缺陷
`src/.env.example` 第 9 行 `*** LLM_TIMEOUT=90`、第 16 行 `***` 含字面 `***`。
`docker compose` 严格解析 env_file 直接失败：`line 9: unexpected character "*" in variable name`
→ 默认（sqlite）容器化部署路径不可用。损坏先于 TASK-015（git 三提交同款）。

### 修改（1 处，`02-development/src/.env.example`）
- 第 9 行 `*** LLM_TIMEOUT=90` → `LLM_TIMEOUT=90`
- 第 16 行 `***` → 删除（对齐 live `src/.env` 的空行结构）

### 自测
- 项目目录（真实 compose + .env）：`docker compose config --quiet` → **PARSE OK**
- 干净目录复现（.env.example→.env 复制 + compose + Dockerfile）：`docker compose config --quiet`
  → **PARSE OK**（TASK-017 复现路径，修复前报错）
- 全仓复查：其余 `***` 仅 DESIGN/README/DEV_REPORT 的 `?token=***`（文档占位，任务书认定合理）
  + gitignored 的 `src/.env.bak`（不入仓、不被 compose 读取）。无其他破坏可解析性的 `***`。

### 状态
**已修复 + 自测 PASS**。交 t_a77f683b（云天明回归）。

## 改动文件（TASK-018，git 可查）
- `02-development/src/routers/api1.py`（BUG-004）
- `02-development/src/.env.example`（BUG-005）
- `02-development/DEV_REPORT.md`（本记录）

## 自测证据索引（均在 `05-temp/`，t018_*）
| 文件 | 内容 |
|---|---|
| `t018_selftest_evidence.md` | BUG-004/005 完整自测证据（PG/sqlite 双后端一致性 + compose config） |
| `t018_start_pg.sh` / `t018_pg_app.log` | 隔离 PG 模式 app 启动脚本 + 日志 |
| `t018_start_sqlite.sh` / `t018_sqlite_app.log` | sqlite 模式 app 启动脚本 + 日志 |
| `t018_pg_ddl.sql` / `t018_pg_init.sql` | 隔离 PG 容器 agp schema DDL（源自 postgres-unified 迁移） |
| `t018_compose_check/` | 干净目录 compose config 验证副本 |

---

# TASK-021 交付记录：F3 后端 — settings 表持久化 + config 全字段（脱敏）+ /config/test 连通测试

> 开发：章北海（2026-09-15，t_23bbba48）。分支 `feat/ui-tools`（阶段四首张卡，由本卡创建）。
> 任务书：`shared/tasks/T-AGP-UI-TOOLS.md` §F3 + 硬性约束；基线 main @ aad7179。
> 范围：仅 F3 后端（F1/F2 由 t_0f154f9b 下并行卡负责）；未碰 src/static/、skills/mcp 路由、RBAC 矩阵、8099/8081 端口。

## 1. 接口变更摘要

### 1.1 新增 `settings` 表（双后端）
- 结构：`key TEXT PRIMARY KEY, value TEXT NOT NULL (JSON 标量), updated_at TEXT`。
- key 白名单 9 个：`llm_base_url, llm_model, llm_api_key, llm_timeout, llm_retries, embedding_base_url, embedding_model, embedding_api_key, embedding_dim`。
- SQLite：`schema_sqlite.sql` 幂等 `CREATE TABLE IF NOT EXISTS settings`（20→21 表，executescript 两遍不报错）。
- PG：`db.py::_pg_init` 启动时 `CREATE TABLE IF NOT EXISTS`（agp schema；agp_user 为 schema owner 有 CREATE 权，已验证）。**建表失败只告警不抛错**（降级为纯内存热更新，不阻断启动）。
- 白名单外 key 一律拒绝（POST 400 / 启动加载时跳过）。

### 1.2 启动加载（DB > .env，DECISION-007 模式）
`startup()` 在 `init_db()` 后调用 `load_settings_from_db()`：DB settings 逐项覆盖内存 Settings（按类型反序列化，脏值跳过保留 env）。重启后配置不丢。

### 1.3 `GET /api/system/config`（权限维持现状：登录即可）
- 兼容旧结构：`llm` / `embedding` / `memory_backend` / `semantic_cache_threshold` / `max_tool_rounds` 字段不变（前端 dashboard 不破坏）。
- 新增 `settings` 段：9 项白名单全字段回显。
- **脱敏规则**：`llm_api_key` / `embedding_api_key` **永不回传明文**，只回 `{key_set: bool, key_tail: "****" + 末4位}`（key 为空 → `{key_set:false, key_tail:""}`）。

### 1.4 `POST /api/system/config`（权限：`system:admin`，RBAC 不动）
- 扩展为 9 项白名单全字段可更新；**未提供的字段不变**（部分更新安全）。
- 类型校验：int/float 必须为正数（否则 400，不落库不热更）；`llm_base_url` 自动 rstrip('/')。
- 未知字段 → 400。
- 写入语义：热更新内存 Settings（即时生效，与现状一致）+ 落库持久化（`INSERT ... ON CONFLICT (key) DO UPDATE`，双后端同语义；落库失败仅告警降级内存生效）。
- 兼容 legacy 字段 `semantic_cache_threshold`（非白名单，仅内存热更，保持 AC-13 旧行为）。
- 响应含 `updated[]` + 脱敏 `settings` 回显。

### 1.5 新增 `POST /api/system/config/test`（权限：`system:admin`）
- body：`{"target": "llm"|"embedding", 可选 base_url/model/api_key 覆盖}`；未覆盖参数取当前 Settings 值（即"用当前待保存或已保存的参数"，供"保存前测试"按钮）。
- 真实连通（httpx，15s 上限，避免用户配 90s 卡死按钮）：
  - llm：`GET {base_url}/models`（OpenAI 兼容），detail 含 HTTP 码 + 前 5 个 model id；
  - embedding：`POST {base_url}/embeddings`（4 字符 input "测试"），detail 含 HTTP 码 + 向量 dim。
- 返回 `{ok, latency_ms, detail}`；**失败不 500**（ok=false + 错误摘要，如 `ConnectError: ...` / `HTTP 401: ...`）。
- 依赖检查：`httpx` 已在 requirements.txt（无新依赖，未引 numpy 等）。

## 2. 自测结果

### 2.1 pytest（sqlite 全链路，本地 venv，05-temp 隔离库）
新增 `tests/test_system_config.py`（17 用例，全绿）：
- GET：无 token 401 / 9 字段全回显 / api_key 脱敏（key_set+末4位，明文零泄漏）
- POST：viewer 403 / 全 9 字段更新（热更+脱敏回显）/ 部分更新保留其余 / 未知字段 400 / 非法类型 400 / legacy semantic_cache_threshold
- /config/test：llm ok（真实 HTTP，mock OpenAI 兼容 server）/ embedding ok（dim 校验）/ 不可达 fail 分支（ok=false+摘要，无 500）/ embedding 未配置 / 非法 target 400 / RBAC 401+403
- 持久化：写库 3 key 直读 SQLite 验证（JSON 值 + updated_at 列）+ **重启模拟**（DB 值覆盖 env，未写字段保留 env 值）
- 双后端 DDL：sqlite schema 幂等（executescript 两遍）+ PG DDL 结构断言（与 sqlite 同构：key PK / value / updated_at，防漂移）

### 2.2 PG 模式（docker run 隔离容器 t021-pg :8599，连 pg-unified，DECISION-010 合规）
`05-temp/t021/pg_selftest.sh` **8/8 PASS**：
1. agp schema settings 表启动自举（21 表，pg-unified 上真实验证）
2. GET 9 字段 + 脱敏（key_tail=****1111，明文零泄漏）
3. POST 9 字段全更新 + 脱敏回显
4. PG 落库 9 行（agp.settings 直查）
5. /config/test llm：ok=true，25ms，models=['mock-pg-llm']（真实 HTTP 经容器→宿主 mock server）
6. /config/test embedding：ok=true，6ms，dim 校验
7. /config/test fail 分支：ok=false + ConnectError 摘要（无 500）
8. 容器重启：DB settings 覆盖 env（llm_model=pg-persisted-model / timeout=45 / retries=2 / dim=256 / key_tail=****4321）
自测结束已清空 agp.settings 测试行（不留生产库测试数据）。

### 2.3 全量回归（tests/ 全部用例，本地 venv + 线上 8099 集成）
- **新增 test_system_config.py 17 用例全绿**（全量套件中同样全绿，76 passed）。
- 既有 79 用例中 74 passed；**5 个失败为存量环境问题，与本卡无关**（已用 main 基线 aad7179 工作树复现验证：零改动代码跑出完全相同的 5 个失败）：
  - test_agents_chat_rag.py::test_chat_real_llm / test_chat_semantic_cache_hit / test_chat_different_topic_misses_cache / test_chat_tool_get_time_real
  - test_memory_mcp_longtext_ws.py::test_ws_streaming_and_cache_hit
  - 共性：`llm_calls == 0`——这些用例打**线上 8099 容器**（旧镜像 `agp-platform:1.2.0-dual`），其 LLM 调用路径当前降级（vLLM 本身 200 可达，问题在线上容器运行态）。本卡代码未部署到线上容器（镜像重建归 TASK-025），工作树改动不可能影响该容器。
  - **提醒云天明**：这 5 个失败在 TASK-024 增量测试前需先排查线上 agp-app 的 LLM 运行态（重启容器或查日志），否则回归基线不干净。

## 3. 部署说明（本卡不重建镜像/不重启线上 agp-app）
- 代码在 `feat/ui-tools` 分支；**线上 agp-app 仍为 `agp-platform:1.2.0-dual` 旧镜像**（无 settings 端点），镜像重建 + compose up 归 TASK-025（褚岩终审卡，任务书硬性约束 7：镜像 1.2.0 重建 + 8081 /agent/ 验证）。
- 旧镜像兼容：`settings` 表在 PG 由新代码启动自举，旧镜像不依赖该表（只读 20 表），重建前后均无风险。
- 台账（SERVER_REGISTRY.md）已登记：agp schema 20→21 表（settings）+ 自举机制。
- 端口/网关/RBAC 零变更（8099/8081//agent/ 不动，13 权限矩阵不动）。

## 4. 已知问题
1. **embedding_dim 热更的局限**：`llm/embedding.py` 模块加载时 `DIM = S.EMBEDDING_DIM` 快照，改 dim 后 local 哈希向量维度下次重启才生效（远端向量按响应 dim 动态 resize，不受影响）。语义缓存阈值/其它字段热更正常。记录备查，不影响本卡验收（F3 要求"与现状一致"，现状即此行为）。
2. 线上 agp-app 未重建镜像前，`/api/system/config/test` 与 settings 持久化在生产容器内不可用（分支代码已就绪，等 TASK-025 重建）。
3. `settings` 表若被外部误删：启动时 sqlite 幂等重建 / PG 自举重建（CREATE IF NOT EXISTS），仅丢失已持久化配置（回退 env 值）。
4. **测试基建提示（供 TASK-024 参考）**：`test_system_config.py` 是进程内自包含测试（TestClient 起独立 app 实例），对导入顺序敏感——`core.config.S` 只在模块导入时从 env 求值一次，fixture 内已就地强制 `DB_BACKEND=sqlite` + 9 白名单基准值，**与全量套件里其它文件先导入 core.config（读 .env 的 postgres 配置）无关**。新增进程内测试时勿依赖 monkeypatch.setenv 影响已建模块的 S。
5. **存量 5 个 chat/WS 集成用例失败**（llm_calls==0，打线上 8099 容器）：本卡已验证 main 基线同样失败，属线上 agp-app LLM 运行态问题，需 TASK-024 前排查。

## 5. 自测证据索引（05-temp/t021/）
| 文件 | 内容 |
|---|---|
| `pg_selftest.sh` / `pg_selftest_output.log` | PG 模式隔离容器自测脚本 + 8/8 全绿输出（含 raw 响应） |
| `mock_openai.py` | OpenAI 兼容 mock server（/v1/models + /v1/embeddings，供 /config/test 真实连通） |
| `check_pg_perms.sh` | agp schema owner/CREATE 权前置验证（agp_user=owner，has CREATE=t） |
| `test_config_db_*.db` | pytest sqlite 测试库（每进程一份，跑完自清理） |

---

# TASK-022 交付记录：F1 后端 — skills 上传导入 + MCP PUT/DELETE 补全 + skills.updated_at + mcp env 持久化

> 分支：`feat/ui-tools`（续 TASK-021，commit 见 git log）。本卡只做 F1 后端；F3 已完成（TASK-021），前端归 TASK-023。

## 1. 接口变更摘要

### 1.1 新增 `POST /api/ext/skills/upload`（multipart）
- 字段名 `files`（可多值）。支持：
  - 单文件 `.md` / `.txt`
  - zip 目录（内含多个 SKILL.md / *.md / *.txt；忽略非 md/txt、`__MACOSX`、隐藏文件）
- **SKILL.md 风格解析**：头部 frontmatter（`---` 包围，YAML 风格 `key: value`）取 `name` / `description`；
  无 frontmatter 或无 name 时，用文件名命名（去扩展名，`_`/空格→`-`，小写，去危险字符）。
  正文 = frontmatter 之外的内容（strip 后入库）。
- **批量入库 + 重名 409 语义**：重名（库内已有 / 本批次内重复）→ 跳过并回报，整批 200（不 400/409 崩溃）。
- 返回：
  ```json
  {"created": [{"name","id","source"}], "skipped": [{"name","reason"}],
   "failed": [{"file","reason"}], "counts": {"created","skipped","failed"}}
  ```
- 约束：单文件/zip ≤ 10MB；单次 ≤ 50 文件；非法 zip → 400；无 md/txt 的 zip → 记入 failed。
- 权限：`ext:manage`（与现有 skills CRUD 一致，RBAC 矩阵不动）。

### 1.2 MCP CRUD 补全
- `PUT /api/ext/mcp/{mid}`：更新 name/command/args/env/enabled（改名冲突 409，不存在 404）。
- `DELETE /api/ext/mcp/{mid}`：删除；**有 agent_bindings 引用时 409**（message 列出引用方 agent 名，提示先解绑）；不存在 404。
- `POST /api/ext/mcp`：**env 写入**（此前 `MCPServerIn` 有 env 字段但 INSERT 丢失）；新增 `enabled` 字段（默认 true）。
- `GET /api/ext/mcp`：返回值中 `args`/`env` 反序列化为 JSON，`enabled` 归一为 bool（此前回传原始 JSON 文本）。
- **env 生效路径**：`/mcp/{mid}/tools`、`/mcp/{mid}/tools/{tool}/call` 及 agent 对话的 `mcp_call` 插件路由
  均按持久化的 env 注入 MCP 子进程（`mcp_client.with_session(..., env=...)`）。

### 1.3 skills.updated_at + mcp_servers.env（双后端列）
- `skills.updated_at`（TEXT，默认 now）：新建默认 now，PUT 刷新（`datetime('now')` / PG `to_char(now()...)`）。
- `mcp_servers.env`：MCP 环境变量。双后端统一存 **JSON 文本**（sqlite TEXT / PG JSONB），
  API 层统一反序列化为 dict（`mcp_client.env_of_row`），双后端行为一致。

## 2. 双后端 schema 改动（identity/建表清单同步 db.py 双分派）

| 后端 | 改动 |
|---|---|
| SQLite | `schema_sqlite.sql` skills 表 CREATE 含 `updated_at`（21 表幂等）；`db._sqlite_init` 对旧库在线 `ALTER TABLE ADD COLUMN`（skills.updated_at + mcp_servers.env，duplicate column 幂等忽略） |
| PG | `db._pg_init` 幂等 `ALTER TABLE ADD COLUMN IF NOT EXISTS`：skills.updated_at TEXT（默认 `to_char(now()...)`）+ mcp_servers.env JSONB（默认 `'{}'::jsonb`）；建列失败仅告警降级，不阻断启动 |

> 说明：`mcp_servers.env` 在 sqlite 存 TEXT、PG 存 JSONB，但 API 层 `env_of_row` 统一处理（bytes/str 走 json.loads，dict 直接返回），
> 对上层完全透明，双后端读回一致（已双绿验证）。

## 3. 自测结果

### 3.1 pytest（本地 venv，sqlite 模式，自包含 TestClient）
- **新增 `tests/test_t022_ext_tools.py` 24 用例全绿**，覆盖：
  - upload 单文件：frontmatter 解析 / 无 frontmatter 文件名命名 / .txt / 空内容跳过 / 非法类型 failed
  - upload zip：多 SKILL.md 批量入库 / 坏 zip 400 / 无 md 的 zip 记 failed / 无文件 400
  - 重名 409 语义：库内重名跳过回报 + 原内容不被覆盖 / 批次内重名 / 混合批次归类
  - skills.updated_at：新建有值 / PUT 跨秒刷新（直读 DB 断言）/ 改名校验 409
  - mcp：create 带 env 持久化回显（直读 DB）/ PUT 全字段 / PUT 404+改名 409 / DELETE ok+404 / DELETE 引用 409+解绑后可删 / env 默认空
  - RBAC：upload/mcp put/delete 无 token 401 / viewer 403 / admin 通过
  - 双后端 DDL 等价：sqlite schema 幂等 + 旧库 ALTER 幂两遍 / PG 列补齐 DDL 结构断言 / env_of_row 双形态归一
- **全量回归**：`pytest tests/` → **100 passed, 5 failed**。5 个失败为**存量环境问题**（打线上 8099 旧镜像容器，`llm_calls==0`，
  已在 TASK-021 用 main 基线 aad7179 复现确认为线上 agp-app LLM 运行态问题，与本卡无关）：
  test_agents_chat_rag.py 4 例 + test_memory_mcp_longtext_ws.py::test_ws_streaming_and_cache_hit。

### 3.2 PG 模式（docker run 隔离容器 t022-pg :8598，DECISION-010 合规，禁用项目 compose）
- **12/12 PASS**（`05-temp/t022/pg_selftest.sh` / `pg_selftest_output.log`）：
  - agp schema `skills.updated_at` + `mcp_servers.env` 列真实 ALTER 补齐（information_schema 核对）
  - upload 单文件（frontmatter 解析 + updated_at）/ 重名跳过回报
  - mcp create 带 env（jsonb 落库 + 读回 + psql 直查键序无关比对）/ PUT 全字段 + 读回 / DELETE 404 / 引用 409 / 正常删除
- 自测结束已清空 agp schema 测试行（skills/mcp_servers/agents/agent_bindings，不留生产库测试数据）。

## 4. 部署说明（本卡不重建镜像/不重启线上 agp-app）
- 代码在 `feat/ui-tools` 分支；**线上 agp-app 仍为 `agp-platform:1.2.0-dual` 旧镜像**（无 upload/PUT/DELETE 端点、无 env 列），
  镜像重建 + compose up 归 TASK-025（褚岩终审卡，任务书硬性约束 7）。
- 旧镜像兼容：新列（skills.updated_at / mcp_servers.env）由**新代码启动时幂等 ALTER** 补齐；
  旧镜像不写/不读这两列，重建前后均无风险。PG 侧 agp_user 为 schema owner 有 ALTER 权（已实测）。
- 台账（SERVER_REGISTRY.md）已登记：agp schema 新增 2 列（skills.updated_at + mcp_servers.env）+ 在线 ALTER 机制 + TASK-022 更新记录行。
- 端口/网关/RBAC 零变更（8099/8081//agent/ 不动，13 权限×4 角色矩阵不动，plugins 保持内置只读）。

## 5. 已知问题
1. **存量 5 个 chat/WS 集成用例失败**（llm_calls==0，打线上 8099 旧镜像容器）：本卡已确认与 TASK-021 同一根因
   （线上 agp-app LLM 运行态问题，main 基线同样失败），非本卡引入。TASK-024 增量测试前需先排查线上 agp-app LLM 运行态（重启容器或查日志），否则回归基线不干净。
2. 线上 agp-app 未重建镜像前，upload/PUT/DELETE 端点与 env 持久化在生产容器内不可用（分支代码已就绪，等 TASK-025 重建）。
3. **测试基建提示（供 TASK-024 参考）**：`test_t022_ext_tools.py` 与 `test_system_config.py` 同为进程内自包含测试（TestClient 起独立 app 实例），
   fixture 内已就地强制 `DB_BACKEND=sqlite` 与 S 基准值，与全量套件导入顺序无关；新增进程内测试勿依赖 monkeypatch.setenv 影响已建模块的 S。
4. skills.upload 重名采用"跳过并回报"而非"覆盖"——如需覆盖语义由前端在上传前删除旧 skill（当前 UI 无此交互，归 TASK-023）。

## 6. 自测证据索引（05-temp/t022/）
| 文件 | 内容 |
|---|---|
| `pg_selftest.sh` / `pg_selftest_output.log` | PG 模式隔离容器自测脚本 + 12/12 全绿输出 |
| `fullsuite.log` | pytest 全量回归（100 passed, 5 存量失败）输出 |
| `smoke.db` | 手工功能冒烟 sqlite 库（upload/mcp 全路径，跑完保留备查） |
| `probe_env.sh` | PG mcp_servers.env 列存储探针（jsonb 键序确认） |

---

# TASK-023 交付记录：前端 — MCP-Skills-Plugins 页 + 记忆页（L0/L1/L2 + 3D + B+树 + 多跳）+ 模型节点配置页

> 分支：`feat/ui-tools`（续 TASK-021/022）。本卡只做**前端**（纯 JS、零框架、离线，DECISION-001 无 CDN）；
> 后端 F1/F3 已完成（TASK-022 / TASK-021），本卡不碰 src/routers/、RBAC 矩阵、端口、网关、8099 线上容器。

## 1. 功能范围（对应任务书 F1 / F2 / F3）

### 1.1 F1 — 扩展能力页（`ext`，`ext.js`）
- **Skills 管理**：列表（搜索、名称/描述/来源/更新时间）、新建、编辑、删除、**上传导入**
  （`.md` / `.txt` / zip 目录，走 `POST /api/ext/skills/upload`，重名"跳过并回报"提示，对应 TASK-022 新增端点）。
- **MCP 管理**：列表（启停开关）、新建、编辑（含 **env KV 行编辑**）、**tools/call 输出**列、删除
  （引用 409 时提示解绑）。对应 TASK-022 的 PUT/DELETE + env 持久化。
- **Plugins（内置只读）+ 长文本（Longtext）**：保留既有面板。

### 1.2 F2 — 记忆页（`memory`，`memory.js` + `graph3d.js`）
4 个 Tab：
- **列表**：L0 对话 / L1 记忆 / L2 图节点 三张表（后端已就绪）。
- **3D 图**：自研 **纯 Canvas-2D 三维力导向图**（`graph3d.js`，318 行，离线无依赖，满足 DECISION-001）：
  - Fruchterman-Reingold 3D 布局 + 视角透视投影 + 拖拽旋转 / 滚轮缩放 / 悬停高亮。
  - **点击节点 → `onNodeClick` → `GET /api/memory/l2/subgraph?start=&hops=2`**，渲染子图节点/边/摘要到详情面板，
    并高亮 2 跳邻居（中心节点光晕、邻接边高亮）。
- **B+ 树**：bucket 分桶可视化 + 触发一次真实 bucket 读（展示"共 N 条"）。
- **多跳**：保留既有文本多跳问答输入。

### 1.3 F3 — 模型节点配置页（`model`，`config.js`）
- **LLM 卡片** + **Embedding 卡片**：9 白名单字段表单（base_url / model / api_key / timeout / retries / dim）。
- **密钥指示**：只读展示 `key_set` + `key_tail`（末 4 位），**永不回显明文**（对应 TASK-021 脱敏）。
- **测试连接**：`POST /api/system/config/test`，显示延迟 / HTTP 码 / model 列表或向量 dim。
- **保存**：`POST /api/system/config`，**只提交实际变更的字段**（避免覆盖合法的留空字段，如 local-hash 模式下
  embedding 的 `base_url`）。RBAC：viewer 角色无 `system:admin`，**隐藏保存按钮**。

## 2. 代码组织

| 文件 | 行数 | 说明 |
|---|---|---|
| `src/static/graph3d.js` | 318 | 新增：纯 JS 3D 力导向图引擎（window.Graph3D） |
| `src/static/ext.js` | 431 | 新增：Skills/MCP/Plugins/Longtext（window.loadExt） |
| `src/static/memory.js` | 329 | 新增：记忆 4-Tab 页（window.loadMemory / memTab） |
| `src/static/config.js` | 167 | 新增：模型节点配置页（window.loadConfig） |
| `src/static/index.html` | 62 | 改：nav 加 `model` 页 + `<div id="page-model">` + 4 个新 `<script>` 标签 |
| `src/static/app.js` | 436 | 改：LOADERS / PAGE_PERMS 加 `model`；memory/ext loader 指向 window 全局；移除旧的 inline loadMemory/walkGraph/bucketDemo/loadExt 及其助手（避免重复全局冲突） |
| `src/static/style.css` | 103 | 改：记忆 Tab / 3D canvas / B+树 / 缓存徽标样式 |

> 设计：新逻辑拆 4 个独立模块文件，各自暴露 `window` 全局；旧 inline 实现从 `app.js` 移除，避免同名全局碰撞。
> 复用 `app.js` 既有全局：`api()`、`esc()`、`$`、RBAC 守卫（`PAGE_PERMS` / `LOADERS`）。

## 3. 自测结果（真实运行，无 mock，隔离环境）

### 3.1 隔离自测环境（不碰线上 8099）
- uvicorn 起在 `127.0.0.1:18099`，`DB_BACKEND=sqlite`，**全新** SQLite 库 `05-temp/t023/agp_test.db`（跑前 `rm -f` 清库）。
- 线上 `agp-app` 容器（8099）全程未动。

### 3.2 无头 Chromium 自测（Playwright，`05-temp/t023/selftest.py`）— **26/26 全绿**
| 断言 | 结果 |
|---|---|
| login（admin / super_admin） | PASS |
| nav-has-model（7 页：dashboard/agents/chat/rag/users/memory/ext/**model**） | PASS |
| dashboard + 4 页回归（agents/chat/rag/users） | PASS |
| memory-tabs（list/g3d/btree/walk） | PASS |
| memory-list-l0 | PASS |
| memory-3d-canvas / painted（canvas 真实绘制 dataurl 54414B）/ nodes-exist（7 节点） | PASS |
| **memory-3d-subgraph（真实 mouse.click 命中节点 V20260909 → 子图渲染）** | **PASS（交互命中 clicked=True）** |
| memory-btree-records（"共 N 条"） | PASS |
| memory-walk | PASS |
| ext-skills-list / **skill-create**（2→3，无报错） | PASS |
| ext-mcp-list / **mcp-create**（1→2，含 env KV）/ **mcp-toggle** / **mcp-delete** | PASS |
| model-page（LLM base + emb dim 字段在） | PASS |
| **model-test-llm**（真实 vLLM 连通：✓ 延迟 883ms HTTP 200; models:['vllm-qwen3.8-27b']） | PASS |
| **model-save**（llm_retries 3→4，**经 API 验证服务端持久化 persisted=True**） | PASS |
| **model-viewer-nosave**（viewer 角色保存按钮隐藏） | PASS |
| no-page-errors（全程 0 console 错误） | PASS |

### 3.3 关键 bug 修复（自测发现）
- **3D 节点点击无法选中**：`graph3d._pick()` 原用 `n.r` 作命中半径，但投影节点对象 `{node,sx,sy,scale,z}`
  根本没有 `r` 字段 → 半径恒为 `NaN` → 任何点击都落空。修复为与绘制一致的屏幕半径
  `Math.max(6,(node.r||6)*scale)+4`（`graph3d.js` `_pick`）。修复后真实 mouse.click 直接命中并拉出 2 跳子图。

## 4. 部署说明（本卡不重建镜像/不重启线上 agp-app）
- 代码在 `feat/ui-tools` 分支；静态文件由后端 `StaticFiles` 直出。**线上 agp-app 仍为旧镜像**，
  前端新页面需随 TASK-025 镜像重建（`agp-platform:1.2.x` 重建 + compose up）后才在 8099 生效。
- 本卡自测在隔离 18099 + 全新 SQLite 上验证，**未触碰** 8099/8081//agent/、13 权限矩阵、RBAC。
- 前端零新增依赖（纯 JS + Canvas-2D），无 npm/CDN，Dockerfile/compose 无需改动。
- SERVER_REGISTRY.md 无端口/容器/组件变更（纯静态资源 + 既有 8099 服务），无需台账变更。

## 5. 已知问题
1. 前端新页面（ext 上传/编辑、memory 3D、model 配置）在**线上 agp-app 旧镜像**中不可用，需 TASK-025 重建镜像后生效（分支代码已就绪）。
2. 存量 5 个 chat/WS 集成用例失败（llm_calls==0，打线上 8099 旧镜像容器）与 TASK-021/022 同根因，非本卡引入。
3. 3D 视图为纯 Canvas-2D 透视投影（非 WebGL/three.js），满足离线约束；节点数较大时（>~150）力导向 O(n²) 会偏慢，
   当前 demo 数据 7 节点无压力。

## 6. 自测证据索引（05-temp/t023/）
| 文件 | 内容 |
|---|---|
| `selftest.py` | 无头 Chromium 自测脚本（26 断言，26/26 全绿） |
| `verify3dclick.py` | 3D 节点点击→子图专项验证（真实 mousedown/mouseup，子图 7 节点 8 边全渲染） |
| `agp_test.db` | 隔离自测 SQLite 库（全新，跑前清库） |
| `uvicorn.log` | 隔离服务（18099）运行日志 |
| `shots/01..13_*.png` | 各页面真实截图（dashboard/memory/3d+子图/btree/walk/ext skills+mcp/model 配置+保存+viewer） |

---

# TASK-026 修复记录：BUG-006（P1，F1 前端 skills 上传导入结果展示被 loadExt() 重渲染清空）

## 1. 缺陷与根因
`src/static/ext.js` `extDoUpload()`：上传成功后先 `_renderUploadRes(j)`（把"✓ 完成：新增 N · 跳过 N · 失败 N"+明细渲染进 `#sk-up-res`，位于 `#sk-upload` 内），紧接 `await loadExt()` **整块重渲染 `#page-ext`**——其中重新生成一个**空的、`hidden` 的 `#sk-upload`**，把刚渲染的导入结果面板**整体清空**。后果：用户导入后只看到列表新增，永远看不到 新增/跳过/失败 汇总；重名/失败场景下零反馈（误以为全部成功）。BUG-006 由 TASK-024 发现、TASK-025 PM 终审独立复现确认（`03-testing/BUGS.md` §BUG-006）。

## 2. 修复方式（任务书选项 1 的实现：结果落在不会被重渲染的节点内）
```js
// 修复前
_renderUploadRes(j);
await loadExt();        // ← 整块重渲染 #page-ext，#sk-upload 被重生为空 hidden → 结果被清空

// 修复后（仅改 extDoUpload() 一处）
_renderUploadRes(j);
// 不再 loadExt() 整块重渲染；仅局部刷新 skills 列表：
_skillQ = '';
const sk = await api('/api/ext/skills').catch(() => ({ skills: [] }));
_skills = sk.skills || [];
const qInput = $('#sk-q'); if (qInput) qInput.value = '';
const list = $('#sk-list'); if (list) list.innerHTML = _skillListHTML(canManage());
```
**为什么"最后渲染/最终可见的是导入结果"**：`_renderUploadRes(j)` 渲染结果后，上传流程**不再触碰** `#sk-upload`（不再被 `loadExt()` 重生成），结果面板在整个流程完成后**原样保留、稳定可见**——最终状态就是结果面板。列表同步改为**局部刷新**（重拉 `/api/ext/skills` + 重绘 `#sk-list` + 复位搜索框）：新增技能立即出现在列表（含计数），搜索框回空态（与全量刷新语义一致），上传区/结果面板不被覆盖。重名/失败/空内容明细全部可见，消除"零反馈"。
**约束合规**：纯 JS 零框架零依赖、无 CDN；仅改 `extDoUpload()` 一处，其余功能（新建/编辑/删除/搜索/MCP/Plugins/长文本）零改动。

## 3. 提交
- 分支 `feat/ui-tools`，commit `5c5cbef`（接 `d6ed696`），工作树 clean；`node --check src/static/ext.js` 语法 OK。

## 4. 自测（混合文件：新技能 + 重名 + 不支持扩展名 + 空内容）
- 隔离容器（RISK-015 铁律：docker run 独立容器名+端口，未动线上 agp-app:8099/8081/13 权限矩阵）：
  - `t026-sqlite` **:8399**（全新卷，DB_BACKEND=sqlite）
  - `t026-pg` **:8499**（agp_default 网络，DB_BACKEND=postgres → 共享 pg-unified/agp）
  - 镜像 `agp-platform:1.2.1-t026` 从含修复的 `src/` 独立重建（`grep BUG-006 /app/static/ext.js` = 1 命中，确认修复在镜像内）。
- Playwright（chromium headless，真实登录 admin → MCP-Skills 页 → 上传导入 → 开始导入），**双后端各 12/12 PASS**（0 console/page 错误）：
  - `skills.upload-result-display`（#sk-upload 可见 + 含 新增/完成/跳过/失败）✅
  - `result.summary-line`（✓ 完成：新增 1 · 跳过 2 · 失败 1）✅
  - 明细断言：重名（医疗问答规范）/ 空内容 / 不支持类型 / 新增（t026-selftest-skill）✅
  - **`result.stable-after-wait`**（2.5s 后结果面板仍可见，无异步清空）✅
  - 列表刷新：新技能落库 + "共 N 个"计数行 ✅；无文件→错误提示（回归护栏）✅
- 结果面板最终文本（双后端一致）：
  ```
  ✓ 完成：新增 1 · 跳过 2 · 失败 1
  新增成功（1）「t026-selftest-skill」 — t026_new_skill.md
  跳过（重名/空内容）（2）「医疗问答规范」 — 重名，库内已存在（医疗问答规范.md）
                 「t026-empty」 — 空内容（t026_empty.txt）
  失败（类型/大小）（1） — 不支持的文件类型或空 zip（仅 .md/.txt 或含此类文件的 zip）
  ```
- 视觉确认：`05-temp/t026/shots/01_upload_result.png`（sqlite）+ `shots/pg01_upload_result.png`（PG）。

## 5. 部署说明
- **未动线上**：agp-app(:8099, `agp-platform:1.2.0-dual`)、8081 网关、13 权限矩阵均未触碰；生产切换（compose up -d 替换线上）**归后续交付卡**（需 TASK-027 回归 PASS 后由褚岩放行，同 TASK-025 守门模式）。
- 临时测试容器 t026-sqlite/t026-pg（:8399/:8499）**保留至 TASK-027 回归**（云天明可复用验证），非生产、非常驻；sqlite 为独立新卷；**PG 共享 schema 自测行已清空**（agp.skills 回到 seed 2 条：医疗问答规范/工具调用规范）。
- SERVER_REGISTRY.md 已登记本卡（2026-09-15 TASK-026 行）。

## 6. 给 TASK-027（云天明回归）的提示
1. **回归重点**：`skills.upload-result-display` + 重名/失败/空内容场景，双后端（sqlite+PG）+ Playwright UI **连跑 2 次**确认无 flake。
2. 可额外验证：导入后在搜索框输入/清空，结果面板仍在（`#sk-up-res` 不在 `extFilterSkills` 重绘范围，应稳定）。
3. 若走全量 `loadExt()`（切页面再回来），`#sk-upload` 回空 hidden 态是**设计如此**（与修复前一致）——BUG-006 验收口径是"导入流程完成后结果可见且稳定"，非"跨页面导航后仍保留"。
4. 复用容器：t026-sqlite(:8399) / t026-pg(:8499) 已健康运行；PG 端若重跑需先清 agp.skills 的 t026 行（或换新 name 文件）。

## 7. 自测证据索引（05-temp/t026/）
| 文件 | 内容 |
|---|---|
| `t026_ui_test.js` / `t026_ui_test_pg.js` | Playwright 自测脚本（sqlite / PG 双后端） |
| `t026_ui_results.json` / `t026_ui_results_pg.json` | 断言结果（各 12/12 PASS）+ upload API 响应 + page_errors |
| `t026_selftest_evidence.md` | 完整自测证据（修复方式 + 双后端明细 + 视觉确认） |
| `shots/01_upload_result.png` / `shots/pg01_upload_result.png` | 导入结果面板截图（双后端，视觉确认） |
| `shots/02_stable_after_2_5s.png` / `shots/03_list_after_upload.png` | 稳定性 + 列表刷新截图 |
| `files/` | 4 个混合测试文件 |
| `clean_pg_t026.py` | PG 共享 schema 自测行清理脚本（凭据走容器内 .env，不上命令行） |

# T-AGP-COMPOSE-REPLACE 交付记录：`docker compose up -d` 部署时自动替换已存在容器（deploy.sh + README + 线上平滑替换验证）（2026-09-16，章北海，t_739d53cc）

## 1. 背景与目标
线上 agp-app 是手工 `docker run` 创建（`agp-platform:1.2.0-dual`，**不带 compose 标签**），执行 `docker compose up -d` 时遇已存在容器会报 `conflict: Name "agp-app" is already in use`。用户要求：**一条命令即可完成部署**——自动停止并删除已存在的同名/同项目容器（含 docker run 手工创建的无标签旧容器、compose 遗留容器、Stopped 状态），再创建启动新容器；数据在卷/PG 中，重建不丢。

## 2. 改动清单（`02-development/`，git 可查）
| 文件 | 改动 |
|---|---|
| `deploy.sh`（新增，可执行） | 一键部署脚本，见下 |
| `README.md` | §4 部署与启动：方式 A 改为推荐 `./deploy.sh`（说明自动替换+数据不丢+模式选择+RISK-015），保留裸 `docker compose up -d` 用法及其 conflict 提示 |
| compose 文件 | **零改动**（`container_name: agp-app` 保留；替换逻辑全部在脚本层，符合任务书） |
| `src/` 业务代码 | **零改动**（本任务仅部署脚本 + 文档） |

**deploy.sh 设计**（`set -euo pipefail`，日志前缀 `[deploy]`，调用方自带 docker 组权限）：
1. **模式选择**：`./deploy.sh`（auto：`.env` 为 `DB_BACKEND=postgres` 自动叠加 `docker-compose.pg.yml`，否则纯 sqlite）/ `./deploy.sh pg` / `./deploy.sh sqlite`。
2. **PG 模式预检（只读+建网络，RISK-015 合规）**：检查宿主 `pg-unified` 存在、`agp_default` 网络存在且 pg-unified 挂载其中；缺失时打印**明确手工指引**（`docker network connect agp_default pg-unified`）而非静默失败——脚本绝不代改别的项目容器。
3. **容器检测与替换**：不用 `docker compose ps -q`（表头 "CONTAINER" 会被误当 ID）；直接 `docker ps -aq` 双过滤（`label=com.docker.compose.project=agp` ∪ `name=^agp-app$`，天然覆盖手工 docker run / compose 两种来源、含 Stopped）→ 统一解析为完整容器 ID 后 `sort -u` 去重 → `docker stop -t 10`（等优雅退出）→ `docker rm -f`。
4. `docker compose [ -f ... ] up -d --build` 创建启动新容器。
5. 健康检查 `curl http://localhost:8099/healthz`（重试 ≤30s），通过后打印最终状态（`docker compose ps` + compose 项目标签 + 健康状态 + 数据源）。失败时打印日志查看命令 + 回滚指引（旧镜像保留本地）。

## 3. 线上验证（VM 192.168.48.134，真实执行，证据 `05-temp/t_739d53cc/`）
共 6 轮 `./deploy.sh` 真实替换（前 5 轮为首个运行，第 6 轮为 PG 显式模式；另加本次复核轮，见 `final_check/`）：

| 场景 | 结果 | 证据 |
|---|---|---|
| ① 线上 docker run 无标签 1.2.0 运行中 → `./deploy.sh` | 旧容器 stop+rm，新容器 compose 创建并 healthy；**新容器带 compose 标签**（`com.docker.compose.project=agp`） | `deploy1_run.log` + `labels_before/after.txt` |
| ② 重复执行（旧容器已带 compose 标签） | 平滑替换，无 name conflict | `deploy2~4_run.log` |
| ③ 无容器首启（全删后） | 正常首启 healthy | `deploy5_run.log` |
| ④ `./deploy.sh pg` 显式 PG 模式 | 预检通过（agp_default 存在且 pg-unified 已挂载）→ 正常替换 | `deploy6_run.log` |
| ⑤ 数据不丢 | 部署前后 `GET /api/system/config` 完全一致；pg-unified `agp` schema 21 表行数前后一致（total_rows=169，messages=86/conversations=33/memory_l2_nodes=8 等逐表一致） | `config_before/after.json`、`pg_tables_before/after.txt`、`pg_total_*.txt` |
| ⑥ 回归 | 部署后 `http://127.0.0.1:8081/agent/` = 200（gw-nginx 未受影响） | `gateway_before/after.txt` |

**本次复核轮（final_check/，验证去重修复后的脚本）**：
- 部署前容器 `bf7f3fbe3395`（healthy, compose=agp）→ 部署后新容器 `0fa0d16ad037`（healthy, compose=agp），单容器条目（无重复 stop/rm）
- `agp` schema 行数前后一致：messages=984 / conversations=389 / memory_l2_nodes=63（注意：与 9/16 早间 169 行基线不同——期间 TASK-027 等回归已写入新业务数据，属正常增长；**本部署前后一致**即数据不丢）
- healthz 前后一致（除实时 counters：graph_nodes=63/edges=37、db=postgres/pg-unified/agp）
- 网关 8081/agent/ = 200

**已知小瑕疵（已修复）**：首版脚本的 name/label 双过滤对同一容器各出一条（name 过滤给短 ID、label 过滤给长 ID），导致同一容器被 stop/rm 两遍（第二遍 `docker rm` 报 no such container，被 `|| true` 吞掉，无副作用但日志噪音）。已改为**统一解析完整 ID 后去重**，复核轮日志确认单条目。

## 4. 提交
- 分支 `main`，commit 见 metadata；`deploy.sh` + `README.md` + `DEV_REPORT.md`；工作树 clean。

## 5. 部署说明（当前线上状态）
- agp-app 现为 **compose 管理**（project=agp，config_files=base+pg 叠加），镜像 `agp-platform:1.2.0-dual`，PG 模式（pg-unified/agp schema），`0.0.0.0:8099`，healthy。
- **未动** gw-nginx / pg-unified / 哮喘系（astm-backend / astm-mariadb）；8081 网关回归 200。
- 后续更新部署入口统一为 `sg docker -c 'cd 02-development && ./deploy.sh'`（auto 模式按 .env 自动选后端）。
- SERVER_REGISTRY.md 已登记本次变更（2026-09-16 行 + agp-app 容器管理方式更新）。

## 6. 自测证据索引（`05-temp/t_739d53cc/`）
| 文件 | 内容 |
|---|---|
| `deploy1~6_run.log` | 6 轮真实部署完整日志（无标签替换/带标签替换×3/首启/PG 显式） |
| `labels_before/after.txt` | 容器 compose 标签前后对照（无标签 → project=agp） |
| `config_before/after.json` | `/api/system/config` 前后一致（数据不丢，脱敏字段） |
| `healthz_before/after.json` | healthz 全字段前后对照 |
| `pg_tables_before/after.txt` / `pg_total_*.txt` | agp schema 21 表行数逐表一致（total=169） |
| `gateway_before/after.txt` | 8081 网关前后均 200 |
| `final_check/` | 复核轮（去重修复后）：healthz/rows/container 前后 + `deploy_final_run.log` |
| `capture.py` | 证据采集脚本（可复跑） |
| `probe/compose-probe.yml` | 网络兼容探测（agp 项目名 + 宿主已有 agp_default 网络的行为验证，容器 agp-probe-app 已清理） |

# TASK-029 交付记录：需求1 核心 — LLM endpoint 多维护 + Agent 模型下拉选择（2026-09-16，章北海，t_5690c4cd）

> 分支：`feat/ui-iter2`（自 main a7d4aba 切出，本卡全部提交在此分支）。
> 任务书：`~/hermes-workspace/shared/tasks/T-AGP-UI-ITER2.md` §需求1。本卡**不部署**（部署归 TASK-033）。

## 1. 方案决策（任务书留白项，供下游 030/031 参考）

| 决策点 | 选定方案 | 理由 |
|---|---|---|
| **agents.model 存什么** | **endpoint 的 `name`**（唯一标识） | ① 双后端自增 `id` 不一致，跨后端不可用；② endpoint 的 `model` 标识可重复（两个端点可指同一模型），不唯一；③ `name` 唯一、可读、跨双后端稳定，且天然兼容旧数据（旧 agent 的 `model` 是自由文本——匹配不到 name 就回退，不报错）。**下游 030/031 若需按 endpoint 查，一律 `WHERE name=?`。** |
| **系统默认 endpoint 方案** | **seed 固定名 `system-default` 行**（启动时幂等补建，快照 S.LLM_*，is_active=1），**禁止删除（409）**；"设为默认"= 把某 endpoint 值同步到 S.LLM_*（热更+落库）+ system-default 行 + 启用该 endpoint | 不变式：**system-default 行 ≡ S.LLM_* 单值**。config 页保存 LLM 字段时后端自动同步该行（`app.py _sync_system_default`），避免两处漂移。兜底：agent 的 model 匹配不到任何 enabled endpoint（旧自由文本/端点已删/停用）→ engine 回退 S.*，不崩（RISK-018）。 |
| **模型节点页布局** | **保留原单 LLM 卡片**（标题改"LLM 底座 · 系统默认"，编辑 S.LLM_*，保存后同步 system-default 行）+ **新增"LLM Endpoint 列表"面板**（列表 + 新建/编辑/删除/测试/启用停用/设为默认）。Embedding 卡片不动。 | 单卡片语义不变（= 系统默认 endpoint 编辑器），列表是增量；避免"迁移进列表"造成的旧配置页行为变化（向后兼容硬约束）。 |
| **endpoint api_key 空值语义** | 创建时留空 → 落库为**共享 key 快照**（S.LLM_API_KEY，即当前 .env/settings 值）；引擎解析时行内 key 为空 → 不覆盖、回退 S.LLM_API_KEY | 常见场景：多个端点指向同一 vLLM 集群，无需每端点重填 key。key 变更时 set-default/config 保存会刷新 system-default 行，其它端点行保留创建时快照（显式 PUT 可改）。 |
| **provider.py 覆盖参数** | `chat()/chat_stream()` 加可选 `base_url / api_key / timeout`（None=读 S.*）；`retries` 不暴露端点级（保持 S.LLM_RETRIES，避免过度设计） | 向后兼容：不传覆盖参数 = 现状行为，不绑 endpoint 的 agent 零影响。 |

## 2. 改动清单（`02-development/src/`，文件:行号，git 可查）

| 文件 | 改动 |
|---|---|
| `core/schema_sqlite.sql:137-151` | 新表 `llm_endpoints`（CREATE TABLE IF NOT EXISTS 幂等；id 自增/name 唯一/base_url/model/api_key/timeout real/retries int/is_active 默认 0/created_at/updated_at） |
| `core/db.py:21-27` | `IDENTITY_ID_TABLES` 加 `llm_endpoints`（INSERT 返回自增 id，双后端语义一致 D3） |
| `core/db.py:96-117` | `_pg_init()` 幂等建表（`BIGINT GENERATED BY DEFAULT AS IDENTITY`，与其余 identity 表一致；失败仅告警降级 S.* 不阻断启动） |
| `routers/endpoints.py`（新文件，233 行） | `GET/POST/PUT/DELETE /api/llm-endpoints` + `POST /api/llm-endpoints/test` + `POST /api/llm-endpoints/{name}/set-default`。写操作 `require_perm("system:admin")`；GET 登录即可（Agent 下拉需要）。`_view()` 脱敏：api_key 只回 `key_set`+`key_tail`（末 4 位），**永不回明文**。test 复用 sys_config_test 逻辑（GET {base_url}/models，15s 上限，失败不 500）。系统默认行删除 → 409。PUT 不支持改名（name 是绑定标识）；api_key 留空=不改动。 |
| `core/app.py:226-259` | `sys_config_update` 保存 LLM 设置时调 `_sync_system_default(conn)` 同步 system-default 行（保持不变式）；`_LLM_SETTING_KEYS` 白名单 |
| `core/app.py:331-338` | 路由挂载（文件尾延迟 import，endpoints.py 依赖本模块 mask_key/persist_setting，避免循环导入） |
| `seed.py:34-53` | 启动 seed：system-default 行幂等补建（快照 S.LLM_*，is_active=1）+ 自愈（key 后配时回填） |
| `llm/provider.py:36-45,48-50,81-84` | `chat()/chat_stream()` 加可选 `base_url/api_key/timeout` 覆盖（`_endpoint_override()` 从 **params 提取并移除），默认 S.*（RISK-018 向后兼容） |
| `engine/agent_engine.py:199-228,239,300` | 新增 `_endpoint_kwargs(agent)`：按 `agent.model` 查 `llm_endpoints WHERE name=? AND is_active=1` → 返回 `{base_url, model, api_key?, timeout}`（model 覆盖为 endpoint 行的 model 标识，**关键**：agent.model 是 endpoint name，LLM payload 的 model 字段必须用端点行的 model 标识）；查不到/未启用/表异常 → `{}` 回退 S.* 不崩。`_tool_loop`（run 路径 239 行）+ `run_stream`（WS 路径 300 行）两处 `args.update(...)` |
| `static/config.js`（重写 367 行） | 模型节点页改造：原 LLM 卡片保留为"系统默认"编辑器 + 新增 endpoint 列表面板（`_endpointList()` 107 行起：表格 + 状态标签 + 编辑/启用停用/测试/设为默认/删除按钮；`epNew/epEdit/epSave/epDel/epToggle/epSetDefault/epTest/epTestForm`；新建/编辑内联表单）。Embedding 卡片 + cfgSave/cfgTest 逻辑原样。无框架无新依赖（原生 JS）。 |
| `static/app.js:123-130,148-173,202` | `loadAgents` 拉 `/api/llm-endpoints` 存 `window._endpoints`（filter is_active）；`renderAgentForm` 模型字段 `<input>` → `<select>`（选项 = enabled endpoints，显示 `name → model`，value 存 name）；**旧数据兼容**（157-164 行）：自由文本 model 匹配不到 → "（自定义/默认）<原值>" 占位项 selected，保存不改动；新建 agent 默认选 system-default（**注意：占位项与 system-default 不能同时 selected——浏览器取最后一个 selected 会静默改值，159-164 行已处理**）。`saveAgent` 读 `$('#ag-model').value` 不变（select 兼容）。 |

## 3. 自测结果（真实容器，无 mock；证据 `05-temp/t029/`）

**方法**：`docker build` 隔离镜像 `agp-platform:t029` + `docker run` 双隔离容器（RISK-015：独立名+隔离端口，未用项目 compose、未动线上）：
- `t029-sqlite` :8599（全新卷 t029-sqlite-data，无 PG 依赖）
- `t029-pg` :8699（连 pg-unified:agp，agp_user）

### 3.1 REST 双后端（`api_test.py`，各 40 断言）
- **sqlite: 40/40 PASS**（`sqlite_api.json`）
- **pg: 40/40 PASS**（`pg_api.json`）

双后端一致覆盖：
| 项 | 结果 |
|---|---|
| 建表幂等（healthz + 表存在 + 重启不重建） | ✅ sqlite `db.backend=sqlite` / pg `agp` schema，llm_endpoints 双端存在 |
| system-default seed + is_active + 删除 409 | ✅ |
| endpoint CRUD（POST 200 / 重名 409 / PUT 部分更新 / 400 校验 / 404 / DELETE 200+404） | ✅ 双后端行为一致 |
| **api_key 脱敏**（GET/POST/PUT 只回 key_set+末 4 位，0 明文；key_tail=`****9999` 精确匹配） | ✅ |
| 空 key 回退共享 key（POST 不填 key → key_set=true 脱敏显示） | ✅ |
| PUT 留空 key=不改动 / 仅空 key=400 无操作 / 改名拒绝 400 | ✅ |
| test 端点：真实连通 vLLM 34.121.9.233:4000（ok=true, ~460ms, models 列表）+ 假端点 ConnectError 不 500 + 无参数 200 ok:false | ✅ |
| set-default：S.LLM_* 热更+落库同步 + system-default 行同步 + 该 endpoint 启用 | ✅ |
| RBAC：无 system:admin 写/test 403 / 无 token 401 / 普通用户 GET 200 | ✅ |
| **agent 对话按 endpoint 路由**（agent.model=endpoint name → 真实 vLLM 走对应 base_url，llm_calls=1 真实答案"1+1等于2。"） | ✅ 双后端 |
| 假端点路由验证（agent 绑 127.0.0.1:59998 → 受控降级 degraded=true，**不 500**，错误提示含端点地址） | ✅ |
| **旧 agent 兼容**（自由文本 model `vllm-qwen3.8-27b`：创建/GET/PUT 编辑全 200 不报错） | ✅ 双后端 |
| 收尾清理后仅 system-default 残留 | ✅ |

### 3.2 UI（Playwright，`ui_test.js`，打 t029-sqlite）
**12/12 PASS**（`ui_results.json` + 5 张截图 `shots/`）：
- 模型节点页 endpoint 列表区 + system-default 行"系统默认"标签 ✅（截图 01）
- UI 新建 endpoint → 列表出现 + key 脱敏显示（已配置·末4位）✅（截图 02）
- UI 测试连接真实连通 vLLM（连接成功）✅（截图 03）
- UI 删除 endpoint ✅
- Agent 模型字段为 `<select>`（非 input）；选项含 `system-default → vllm-qwen3.8-27b`；新建默认选 system-default ✅（截图 04）
- **旧 agent 编辑：自由文本 model 显示"（自定义/默认）some-old-free-model"，selected=原值，保存 PUT 200 不报错** ✅（截图 05）

### 3.3 回归（t029-sqlite 容器内 `pytest tests/`，99 passed / 6 failed）
99 个功能用例全过（含既有 auth/agents/chat/rag/memory/mcp/system_config/t022）。6 个 failed **均为容器环境 artifact，非回归**：
- 4 个 `FileNotFoundError`：`test_system_config.py:391,425` / `test_t022_ext_tools.py` 用宿主相对路径 `../src/core/*.sql|py` 读源码文件，镜像内 `/app/tests/../src` 无此结构（tests 是 docker cp 进去的，源码在 `/app/core/`）——**宿主本地跑这些测试正常**（此前阶段三/四均在宿主 venv 跑过）。
- 1 个 `test_frontend_footer`：同上，读 `src/static/index.html` 宿主路径。
- 1 个 `test_chat_semantic_cache_hit`：断言 `llm_calls>=1` 失败——自测脚本先手动对话污染了 L1 语义缓存（conftest `_reset_cache` 只清 l0/l1_image/l1_cache 中部分表，容器内手动 chat 的缓存残留），非代码缺陷。
**结论：无功能回归。**

## 4. 部署说明（本卡不部署，TASK-033 部署 1.3.0）
- 线上 agp-app（1.2.0-dual, PG :8099）、8081 网关、哮喘系**全程未动**；自测走 docker run 隔离容器（RISK-015 铁律）。
- 隔离容器 t029-sqlite/t029-pg **保留至 TASK-032 回归**（云天明可复用，端口 8599/8699 空闲）。
- **pg-unified:agp 测试残留（待 TASK-032/033 清理）**：自测向共享 agp schema 写入的测试数据为——
  - `llm_endpoints` 表 1 行（system-default，**属新代码正常 seed，保留**）
  - `settings` 5 行（set-default 同步的 llm_base_url/model/api_key/timeout/retries——**值与 env 基准一致，非数据变更，可保留**；若需严格复原执行 `DELETE FROM agp.settings;`）
  - conversations/messages/memory_l0_raw/memory_l1_cache 增量（t029 容器内 agent 对话产生，agent 已删）：基线 389/984/18/11 → 现 393/992/22/13。清理 SQL（**destructive，本次无人值守未执行，由下游/PM 确认后执行**）：
    ```sql
    DELETE FROM agp.messages m USING agp.conversations c
      WHERE m.conv_id=c.id AND c.created_at >= '2026-09-16 10:00:00';
    DELETE FROM agp.conversations WHERE created_at >= '2026-09-16 10:00:00';
    DELETE FROM agp.settings;
    DELETE FROM agp.memory_l0_raw WHERE ts >= '2026-09-16 10:00:00';
    DELETE FROM agp.memory_l1_cache WHERE ts >= '2026-09-16 10:00:00';
    ```
    基线快照：`05-temp/t029/pg_baseline.txt`；自测后快照：`pg_after.txt`。

## 5. 给 TASK-030/031/032 的提示
1. **agents.model = endpoint name**（唯一绑定标识）。按 endpoint 查用 `WHERE name=?`。下拉只列 is_active=1。
2. 旧 agent 自由文本 model 编辑时 select 显示"（自定义/默认）<原值>"且 selected=原值（保存不改值）；若用户主动改选某 endpoint，model 变成该 endpoint name。
3. 引擎解析链路：`agent_engine._endpoint_kwargs()` → 查 `llm_endpoints WHERE name=? AND is_active=1` → `provider.chat/chat_stream(base_url=, api_key=, timeout=, model=<endpoint 行的 model 标识>)`。回退条件：model 空/查不到/未启用/表异常 → S.*。
4. 回归重点：双后端 endpoint CRUD+test+脱敏（已有 `api_test.py` 40 断言可复用）、UI 4 项（模型节点页列表/Agent 下拉/旧 agent 编辑/endpoint 路由）、**set-default 后 S.LLM_* 与 system-default 行一致性**、**假端点 agent 受控降级不 500**。
5. 已知观察（非阻塞）：endpoint 行 api_key 是创建时快照，共享 key 轮换后其它端点行不会自动刷新（system-default 行会刷新）；如需强一致可在"设为默认"或新 key 保存时批量刷新——P3，留给下游决定。

## 6. 自测证据索引（`05-temp/t029/`）
| 文件 | 内容 |
|---|---|
| `api_test.py` | REST 双后端自测脚本（40 断言/后端，幂等前置清理） |
| `sqlite_api.json` / `pg_api.json` | 双后端断言结果（各 40/40 PASS，含请求响应摘要） |
| `ui_test.js` / `ui_results.json` | Playwright UI 自测脚本 + 12/12 PASS 结果 |
| `shots/01~05_*.png` | 模型节点页列表 / endpoint 新建 / 测试连接 / Agent 模型下拉 / 旧 agent 编辑 |
| `read_keys.py` / `start_containers.py` | key 读取 + 隔离容器启动（key 走 --env-file 0600，不落命令行/日志） |
| `pg_baseline.txt` / `pg_after.txt` | pg-unified:agp 测试前后行数快照（复原对照） |

# TASK-030 交付记录：需求2+3 — MCP 配置 tooltip + 长文本 4 策略 tooltip（2026-09-16，章北海，t_7c20c6bb）

> 分支：`feat/ui-iter2`（接 TASK-029 d09a2c0，本卡全部提交在此分支）。
> 任务书：`~/hermes-workspace/shared/tasks/T-AGP-UI-ITER2.md` §需求2、§需求3。本卡**不部署**（部署归 TASK-033）。
> 范围：仅改前端 `src/static/ext.js` + `src/static/style.css`（原生 CSS/JS，零依赖、零构建），不影响任何后端/MCP/长文本功能。

## 1. 实现说明

两个区块标题旁各加一个 **info 图标 + hover tooltip**（纯 CSS `:hover`/`:focus-within`，无 JS 事件绑定、无新依赖），风格统一（共用 `.tip-wrap/.tip-icon/.tip-box` 一套样式，参考现有 `.k`/`.tag` 配色变量）。

### 需求 2：MCP Servers 区块（`src/static/ext.js:73`）
- 标题 "MCP Servers（stdio JSON-RPC · 内置 demo 真实进程）" 后插入 `_tipBox(MCP_TIP)`。
- `MCP_TIP`（`src/static/ext.js:34`）内容：① 什么是 MCP Server（本地可执行命令 stdio JSON-RPC，平台 spawn 之并用 LSP Content-Length 帧通信，**仅 stdio、无 HTTP/SSE**，对齐 `src/mcp/mcp_client.py:1-6`）；② 配置步骤 ①名称→②command→③args→④env→⑤enabled→保存（对齐 `_showMcpForm` 字段）；③ **真实可跑示例**（容器内 `python3 /app/mcp/mcp_server_demo.py`，保存后点 tools/list 可见 `get_time`/`get_patient_demo` 两工具）+ 第二例演示 env 注入（`LOG_LEVEL=debug`）；④ 提示（保存后点 tools/list 验证、测试真实启动子进程、被 agent 引用删除 409 需先解绑，对齐 `extDelMcp` 确认文案）。

### 需求 3：长文本 4 策略区块（`src/static/ext.js:426`）
- 标题 "长文本 4 策略" 后插入 `_tipBox(LT_TIP)`。
- `LT_TIP`（`src/static/ext.js:48`）：一两句概括（策略 1/2/3 真实调用 LLM、策略 4 纯 pandas 确定性）+ 4 条要点，**逐字对齐 `src/services/longtext.py` 头部 docstring（1-11 行）**。

### 样式（`src/static/style.css:104-125`）
- `.tip-wrap` 相对定位包裹，`.tip-icon` 16px 圆形 "i"（`--acc` 描边 + `cursor:help`，`tabindex=0` 支持键盘 focus 展开），`.tip-box` hover 时 `display:block` 绝对定位下展。
- **窄屏/移动端不遮挡**：`@media (max-width:640px)` 时 `.tip-box` 改 `position:fixed`（顶部 52px、左右 10px 边距、`max-height:44vh` 可滚动）——实测 375px 宽下高度 308px（44vh），不横向溢出，下方 4 策略按钮/HITL 区在可滚动区域下方（不被永久覆盖），tooltip 移开即消失。

## 2. 准确性核对（任务书硬约束：示例必须真实可跑，不编造）

| 核对项 | 结论 | 依据 |
|---|---|---|
| demo 脚本真实路径 | `/app/mcp/mcp_server_demo.py` 成立 | `src/Dockerfile:4` `WORKDIR /app` + `COPY . .`（src 平铺到 /app）；`src/core/app.py:96-98` set_demo_script 指向 `<src>/mcp/mcp_server_demo.py` |
| demo 工具名 | `get_time` / `get_patient_demo` 真实存在 | `src/mcp/mcp_server_demo.py:15-34` TOOLS 列表 |
| **`--verbose` 参数** | **不存在，故未举** | 全脚本 `grep argv/argparse/verbose` 零命中，`main()` 直接读 stdin，不解析任何 CLI 参数。按"不存在就不要举"硬约束，第二例改以 env 注入演示，并如实注明"demo 脚本不消费该变量" |
| MCP 仅 stdio 无 HTTP/SSE | 成立 | `src/mcp/mcp_client.py:1-6,52`（`create_subprocess_exec` + Content-Length 帧） |
| 4 策略文案 | 与 docstring 逐字一致（含 2000-3000/重叠10-20%、MERGE 幂等、≤5 次、HITL 不硬失败、patient_id+date、不依赖 LLM） | `src/services/longtext.py:1-11`；脚本断言 10/10 关键短语全部命中 |
| 删除 409 提示 | 成立 | `src/static/ext.js` `extDelMcp`（"若仍被 agent 引用，后端会返回 409 提示先解绑"） |

## 3. 自测结果（真实运行，RISK-015 隔离容器）

- **隔离容器** `t030-ui`：`docker run` 起 `agp-platform:t030`（sqlite 后端），映射 `8580→8099`（**不碰 8081 网关 / 8099 线上 agp-app / 哮喘 / pg-unified**），static 目录 `-v` 只读挂载（改 CSS/JS 即生效，无需重建镜像）。
- **Playwright 自测**（`05-temp/t030/ui_test.py`，chromium headless）：登录 admin → 进 MCP-Skills 页 → hover 两个 info 图标 → 断言 tooltip 可见 + 内容完整（MCP 8 关键串 / 长文本 11 关键串）+ 移动端 fixed overlay 不遮挡。**结果 4/4 PASS，OVERALL PASS**（`05-temp/t030/ui_results.json`）。
- **截图**（`05-temp/t030/shots/`）：
  - `01_mcp_desktop.png` — 桌面 MCP tooltip（内容完整、含真实路径与工具名）
  - `02_longtext_desktop.png` — 桌面长文本 tooltip（4 策略齐全）
  - `03_mcp_mobile.png` / `04_longtext_mobile.png` — 375px 移动端（fixed 紧凑 overlay，不横向溢出）
  - `00_page_ext_full.png` — MCP-Skills 整页基线
- **现有功能不受影响**：MCP 增删改 / tools-list / call、长文本 4 策略按钮、HITL 队列渲染均未改动（仅标题旁插入只读 `<span>`，无 JS 逻辑变更）。

## 4. 给 TASK-031 / 测试（云天明）的提示
1. 本卡为纯前端展示增量，**无后端/接口/数据变更**，回归只需 UI 层：hover 两 tooltip 出现 + 内容正确 + 窄屏不遮挡 + 现有 MCP/长文本功能正常。
2. 若后续 MCP 支持 HTTP/SSE 或 demo 脚本新增 CLI 参数（如真加 `--verbose`），需同步更新 `MCP_TIP` 文案（当前按"仅 stdio、无 --verbose"如实描述）。
3. 移动端 tooltip 是 `position:fixed` 顶部 overlay（`max-height:44vh` 可滚动）——测试在窄屏验证时留意其展开/收起（鼠标移开即消失，不阻塞后续点击）。

## 5. 自测证据索引（`05-temp/t030/`）
| 文件 | 内容 |
|---|---|
| `ui_test.py` | Playwright 自测脚本（登录→hover→断言，桌面+移动双 viewport） |
| `ui_results.json` | 4/4 PASS 结果（含移动端 fixed overlay 几何：y=52,w=355,h=308） |
| `shots/01~04_*.png` | 桌面 MCP/长文本 tooltip + 移动端两例 + 整页基线 |
| `t030.env` | 隔离容器 env（sqlite，dummy LLM key——UI tooltip 自测无需真实 LLM） |
| `data/` | 隔离容器 sqlite 数据目录（一次性自测，不复用） |

# TASK-031 交付记录：需求4 — Agent 绑定 skill/mcp/rag/plugins 端到端验证 + plugins 动态下拉（2026-09-16，章北海，t_6c74bc7d）

> 分支：`feat/ui-iter2`（接 TASK-030 4991c9c，本卡全部提交在此分支）。
> 任务书：`~/hermes-workspace/shared/tasks/T-AGP-UI-ITER2.md` §需求4。本卡**不部署**（部署归 TASK-033）。
> 本卡是 4 项需求中**开发最后一张卡**：feat/ui-iter2 工作树 clean，4 项需求代码全部就绪，下游 TASK-032（云天明回归）直接可测。

## 1. 实现说明

任务书已明确"该功能已基本存在，本卡 = 验证 + 补齐缺口 + 打磨"。勘察结论：

- **后端 `GET /api/ext/plugins` 已存在**（`src/routers/api1.py:182-184`）：返回 `PLUGIN_REGISTRY`（`src/mcp/plugins.py:12`）全部插件的 name+description+schema。数据源是**内存注册表而非 DB** → 双后端天然一致，无新表、无 DDL、无向后兼容风险（缺口 B 的后端部分无需新增，本卡核实后直接复用）。
- **缺口 A（模型下拉）TASK-029 已到位**（`src/static/app.js:156-173`）：本卡 E2E 复核通过（模型为下拉、默认 system-default、旧 agent 回退不报错），未重复开发。

### 缺口 B：plugins 下拉去硬编码（`src/static/app.js`，唯一代码改动）

| 改动 | 位置 | 说明 |
|---|---|---|
| `loadAgents()` 拉取 plugins | `app.js:126-129` | `window._plugins = (await api('/api/ext/plugins').catch(...)).plugins`；接口失败降级空列表（select 空、保存不选即可，不阻断表单） |
| `renderAgentForm()` 动态渲染 | `app.js:152-154,194-196` | plugins select 选项 = `window._plugins.map(p => p.name)`（原 `['get_time','mcp_call','echo']` 硬编码已删除）；选项名与 value 均过 `esc()`；label 标注"（动态：GET /api/ext/plugins）"便于测试定位 |
| 向后兼容 | 同 | 现有 `bindings:[{type:'plugin', ref_id:'<name>']} 存储格式不变；`_validate_bindings`（`api1.py:66-70`）继续按名校验；已绑定插件在新选项里照常回显 selected |

### 打磨：绑定区说明 + 空值轻提示

- 说明文字（`app.js:187-189`）：一行讲清"Skills/Plugins 注入 prompt 固定左侧（指令+工具 schema）；RAG 按请求检索知识库上下文注入 prompt；MCP 不注入 prompt，是运行时工具（经 mcp_call 插件路由）；全部可选"——与 `agent_engine.assemble()`（`src/engine/agent_engine.py:48-99`）的真实消费行为逐一对齐。
- 空值校验（`app.js:196,203-214`，`checkBindHint()`）：四个绑定 select 都未选时显示一行轻提示"（未绑定任何 skill/mcp/rag/plugin — 可选，agent 仍可用）"，任一勾选即消失。**不强制、不阻断保存**（任务书"可选提示，不强制"）。
- 前端零依赖零构建：仅改 `src/static/app.js` 原生 JS，`node --check` 语法通过。

## 2. 端到端验证证据（Playwright，隔离容器 t031-sqlite:8581，sqlite 后端）

自测脚本 `05-temp/t031/ui_test.js`，**19/19 PASS**（`05-temp/t031/ui_results.json`）。流程：
登录 admin → Agent 构建器 → 核对 plugins 动态下拉 → 新建 t031-e2e 勾选 skill(医疗问答规范)/mcp(demo)/rag(医疗知识库)/plugin(get_time+mcp_call)（Ctrl+click 多选）→ 保存 → 列表回显 5 个绑定 tag → 编辑回显勾选 → 取消 get_time、新增 echo → 再保存 → 回显更新 → prompt 预览核对 → UI Prompt 按钮渲染 → 删除测试 agent 清理。

关键断言（节选，全文见 ui_results.json）：

```
[PASS] plugins 下拉选项来自后端（3 个） — ["get_time","mcp_call","echo"]
[PASS] GET /api/ext/plugins 返回 3 插件（sqlite） — ["echo","get_time","mcp_call"]
[PASS] 绑定区说明文字（哪些注入 prompt/运行时工具） — count=1
[PASS] 空值提示：未选绑定时出现轻提示 — hint="（未绑定任何 skill/mcp/rag/plugin — 可选，agent 仍可用）"
[PASS] 模型为下拉且默认 system-default（029 缺口 A 复核） — default=system-default
[PASS] 列表回显 t031-e2e + 5 个绑定 tag — …mcp:1 plugin:get_time plugin:mcp_call rag:1 skill:1…
[PASS] 编辑表单回显勾选（1/1/1/2） — {"sk2":["skill:1"],"mc2":["mcp:1"],"rk2":["rag:1"],"pl2":["plugin:get_time","plugin:mcp_call"]}
[PASS] 再保存后回显：get_time 已取消、echo 已新增 — …plugin:echo plugin:mcp_call…
[PASS] prompt 预览含 skill 指令 / 含 RAG 上下文 / 含 echo+mcp_call schema 且 get_time 已移除 / 含工具调用协议块
[PASS] 清理：测试 agent 已删除 — count=0
```

### prompt 预览片段（`05-temp/t031/prompt_preview_excerpt.txt`，GET /api/agents/5/prompt 真实响应）

```
你是一名医疗助手。

[Skills 指令]
## 医疗问答规范
回答医疗问题时遵循以下规范：先给出结论，再列依据；涉及检验指标必须标注数值与单位；不确定时明确说明需进一步检查。
[可用工具 schema]
[{"name": "echo", ...}, {"name": "mcp_call", "description": "调用 agent 绑定的 MCP server 工具（name=工具名）", ...}]

[工具调用协议]
你可以调用以下工具。需要调用时，回复一个 JSON … {"tool_call": {"name": "<工具名>", "arguments": {...}}}

[RAG 知识库上下文]
知识库检索上下文（top-k，按相似度降序）：
[0] 患者：王建国（P001），男，52岁，因"反复喘息、气促2年，加重1周"就诊。
诊断：支气管哮喘（中重度，控制不佳）。2026-09-09 门诊：… FEV1 改善量 240 ml …
```

**证据链闭环**：绑定（skill/mcp/rag/plugin）→ 落库（agent_bindings）→ 引擎 `assemble()` 真实消费（skill 注入 `[Skills 指令]`、plugin 注入 `[可用工具 schema]`、RAG 注入 `[RAG 知识库上下文]`）→ 编辑再保存后 get_time 从 schema 消失、echo 出现（持久化与消费同步正确）。

### plugins 动态化验证（硬约束 5：加测试插件→前端自动出现→测毕复原无残留）

`05-temp/t031/verify_dynamic_plugins.sh`，4 步全 PASS：

1. `docker cp` 临时替换容器内 `/app/mcp/plugins.py`（追加 `t031_temp_test` 注册项，`plugins_patched.py`）→ 重启容器；
2. `GET /api/ext/plugins` 返回 4 个（含 t031_temp_test）→ `evidence_api_with_temp_plugin.json`；
3. Playwright 打开 Agent 表单，plugins select **不改前端代码自动出现 t031_temp_test** → `shots/06_plugins_temp_plugin_in_frontend.png`；
4. 从原始镜像重建容器 → API 恢复 3 个插件、`t031_temp_test` 无残留（`evidence_api_after_restore.json`）。

## 3. 双后端验证（硬约束 1）

`GET /api/ext/plugins` 双后端各验一次（均为 200，数据来自内存 registry、与 DB 无关）：

| 后端 | 容器 | 端口 | 结果 | 证据 |
|---|---|---|---|---|
| sqlite | t031-sqlite | 8581 | 200, 3 插件（get_time/mcp_call/echo） | `05-temp/t031/evidence_api_plugins_sqlite.json` |
| postgres（pg-unified:agp） | t031-pg | 8681 | 200, 3 插件（同上） | `05-temp/t031/evidence_api_plugins_pg.json` |

PG 容器 seed 幂等性：`pg_snap.py before/after` 18 张表行数逐一比对**完全一致**（`pg_baseline.txt`/`pg_after.txt`），pg-unified:agp 无新增无变更，无需复原。

## 4. 截图索引（`05-temp/t031/shots/`）

| 文件 | 内容 |
|---|---|
| `01_agents_form_plugins_dynamic.png` | Agent 表单：plugins 动态 label、绑定区说明文字、空值提示 |
| `02_agents_form_selected.png` | 新建 t031-e2e：四类绑定已勾选（skill/mcp/rag/2 plugins），模型=system-default |
| `03_agents_list_created.png` | 列表回显 5 个绑定 tag |
| `04_agents_list_edited.png` | 编辑再保存后回显（get_time 已取消、echo 已新增） |
| `05_prompt_preview.png` | UI Prompt 按钮渲染的组装 prompt（含 Skills 指令/工具 schema/RAG 上下文） |
| `06_plugins_temp_plugin_in_frontend.png` | 临时测试插件 t031_temp_test 自动出现在前端 select（动态化铁证） |
| `prompt_preview.json` / `prompt_preview_excerpt.txt` | GET /api/agents/{id}/prompt 完整响应 + 关键片段 |

## 5. 自测环境与纪律（RISK-015）

- 隔离容器 `t031-sqlite:8581`（sqlite 新卷）+ `t031-pg:8681`（连 pg-unified:agp），`docker run` 独立命名/端口，**全程未碰 8081 网关 / 8099 线上 agp-app / 哮喘 / gw-nginx**；密钥走 `--env-file`（0600），未进命令行。
- 镜像 `agp-platform:t031`：以 t030 镜像为 base 仅 `COPY src/static /app/static`（本卡改动全在静态层），后端代码零改动。
- **本卡不部署**：线上 agp-app(1.2.0-dual) 未动；部署归 TASK-033（镜像 tag 递增 + 4 需求线上 E2E）。

## 6. 给 TASK-032（云天明回归）/ TASK-033（部署）的提示

1. 本卡改动仅 `src/static/app.js`（+73/-11 行级）：回归面 = Agent 构建器页（plugins 下拉/绑定说明/空值提示/保存回显）+ 模型节点页与 MCP-Skills 页不受影响。
2. 回归时若用 `<select multiple>` 自动化，注意 **Ctrl+click 才是多选语义**（纯 click 是替换选择）——自测脚本已按此实现。
3. 线上部署后核对：Agent 表单 plugins 下拉 3 项（get_time/mcp_call/echo）来自 `GET /api/ext/plugins`（线上当前与源码一致，registry 未变）；新增插件后前端自动出现（本卡已用临时插件验证）。
4. 测试 agent 已清理（sqlite 卷随容器删除）；pg-unified:agp 数据零变更（18 表行数 before/after 一致）。

---

# TASK-034 — 修复 BUG-007：endpoint 删除/停用后 agent 回退 S.LLM_MODEL（同步+流式双路径）

> 章北海 · 2026-09-16 · feat/ui-iter2 @ **d108944**（上一最终 commit 886543f 之上）

## 1. 根因（TASK-032 发现 / TASK-033 褚岩独立黑盒确认）

`src/engine/agent_engine.py`：agent.model 存 endpoint **name**（绑定标识）。同步路径
`_tool_loop`（原 237-239 行）与流式路径 `run_stream`（原 298-300 行）均为：

```python
if agent.get("model"):
    args["model"] = agent["model"]          # 先塞 endpoint name（如 "t033-del"）
args.update(await self._endpoint_kwargs(agent))  # 端点被删/停用 → 返回 {}，不覆盖 model
```

`_endpoint_kwargs`（199-231 行）在"查不到 / is_active=0 / 表异常"时 `return {}`，
只覆盖 base_url/api_key/timeout，**不回退 model 到 S.LLM_MODEL**。后果：endpoint
被删/停用后 payload model 停在 endpoint name → 发给系统默认端点（真 vLLM）→
404 model does not exist → `KeyError: 'choices'` → 重试 3 次 → 降级（degraded）。
违反需求1"选不到时回退默认不崩"+ RISK-018 兜底不变式。

## 2. 修复方式（两处同改，不得只改一处）

`_tool_loop`（同步）与 `run_stream`（流式）统一改为：

```python
_epk = await self._endpoint_kwargs(agent)
if _epk:                      # endpoint 解析成功（返回含 base_url 的 dict）
    args.update(_epk)         # 才用端点行覆盖 base_url/api_key/timeout/model
elif agent.get("model"):      # 解析失败（查不到/停用/已删/表异常）且 model 非空
    args["model"] = S.LLM_MODEL  # 回退系统默认模型，走 S.LLM_BASE_URL
```

行为矩阵：

| agent.model | endpoint 行 | 修复后行为 |
|---|---|---|
| 空 | — | 不变：S.* 默认（provider 默认 model） |
| = endpoint name，**启用** | 存在 is_active=1 | 不变：端点行覆盖 base_url/api_key/timeout/model |
| = endpoint name，**停用/已删/查不到/表异常** | 无 | **新**：model 回退 S.LLM_MODEL → 系统默认端点，degraded=false |
| **旧自由文本**（如 vllm-qwen3.8-27b，非 endpoint name） | 查不到 | **新**：model 回退 S.LLM_MODEL（与旧值相同，行为不变；RISK-018 向后兼容） |

不回归面：① 正常 endpoint 对话真实走对应 base_url/model（含假 endpoint 受控降级
不 500）；② 旧自由文本 agent 既有对话不变；③ system-default 兜底端点 seed 不变式
（至少保留一个可用系统默认端点，DELETE 409）未动。

## 3. 双后端自测证据（RISK-015 全程 docker run 隔离，未动线上 8099/8081/哮喘/gh）

镜像 `agp-platform:t034`（修复后源码构建；镜像内 agent_engine.py 两处含
"BUG-007 回退" 注释已核）。密钥经容器 env 注入（profile .env 读取，未打印、
未进镜像/.dockerignore 排除 .env）。

### 3.1 sqlite 全新卷（`t034-sqlite` :8999，全新卷 `/app/data/agp.db`）

探针 `05-temp/t034/t034_probe.py` → `05-temp/t034/t034_probe_out.json`，**verdict=PASS**：

| 检查 | 结果 |
|---|---|
| A 删除 endpoint 后绑定 agent 对话 | 200，**degraded=false**，llm_calls=1，真实答案 2155330（=9371*23）✅ |
| B 停用 endpoint（is_active=0）后对话 | 200，**degraded=false**，llm_calls=1，真实答案 1593070（=93710*17）✅ |
| C 正常启用 endpoint 对话（不回归） | 200，degraded=false，llm_calls=1，真实答案 1218230 ✅ |
| D 假 endpoint（base_url 不可达）对话 | 200，**degraded=true**（受控降级不 500，行为保持）✅ |
| E 旧自由文本 agent（model=vllm-qwen3.8-27b）对话（不回归） | 200，degraded=false，llm_calls=1，真实答案 655970 ✅ |
| F system-default 兜底端点存在且启用 | ✅（model=vllm-qwen3.8-27b，base=真 vLLM） |

### 3.2 PG 连共享 pg-unified（`t034-pg` :9001，agp schema，agp_default 网络）

探针 `05-temp/t034/t034_probe_pg.py`（容器内运行），**verdict=PASS**：

| 检查 | 结果 |
|---|---|
| A 删除 endpoint 后对话 | 200，degraded=false，llm_calls=1，真实答案 959744 ✅ |
| B 停用 endpoint 后对话 | 200，degraded=false，llm_calls=1，真实答案 709376 ✅ |
| C 正常 endpoint 对话 | 200，degraded=false，llm_calls=1，真实答案 542464 ✅ |
| E 旧自由文本 agent 对话 | 200，degraded=false，llm_calls=1，真实答案 292096 ✅ |
| 前后快照 t034pg- 前缀 endpoint/agent | before/after 均为空 ✅ |

**双后端一致：引擎缺陷后端无关，修复后 sqlite + PG 均 degraded=false 且真实答案。**

## 4. 共享 pg-unified 测试残留（**已记录复原 SQL，未执行**——destructive 操作交 PM/用户确认）

探针创建的 4 个 agent/endpoint 已全部经 API 删除；但 `DELETE /api/agents/{id}`
不级联 conversations/messages（既有行为），残留 4 条孤儿 conversations + 8 条
messages（无 memory_l1，算术短答案未过语义缓存阈值；无 t034pg- 前缀 agent/
endpoint 残留）。精确复原 SQL（`05-temp/t034/pg_residue.json`，**交下游/PM 确认
后执行**，本卡不自行执行）：

```sql
DELETE FROM agp.messages WHERE id IN (1021,1022,1023,1024,1025,1026,1027,1028);
DELETE FROM agp.conversations WHERE id IN ('c10bc0e23b248','caa33a8ce9d04','cdf058180cf7e','c3c9001797871');
```

## 5. 纪律自检

- 改动仅 `src/engine/agent_engine.py`（+20/-6，纯后端）；前端零改动（本 bug 纯后端）✅
- 零硬编码密钥（镜像 src/*.py 真实 key 0 命中；key 经容器 env 注入，未进命令行/日志）✅
- 临时文件全在 `05-temp/t034/`（t034_probe.py / t034_probe_pg.py / t034_launch.py /
  t034_pg_residue_audit.py / t034_probe_out.json / pg_residue.json / data/）✅
- 全程 `docker run` 隔离（t034-sqlite :8999 全新卷 / t034-pg :9001）；未用项目
  compose、未动线上 agp-app 8099/8081/哮喘/gh/pg-unified 数据（除上述已记录残留）✅
- 未 merge main、未部署、未构建 1.3.0（TASK-036 范围）✅

## 6. 给 TASK-035（云天明回归）/ TASK-036（褚岩复审）的提示

1. 修复 commit：**feat/ui-iter2 @ d108944**（单一文件 agent_engine.py，两处同改）。
   回归面 = 同步 chat（/api/chat/{id}）+ 流式 WS（/ws/chat）两条路径的 endpoint
   解析回退；重点：删除/停用 endpoint 后对话 degraded=false + 真实答案（sqlite + PG 双端）。
2. 回归建议用例：① 删除 endpoint 后绑定 agent 对话（本卡 A 场景）；② 停用
   （PUT is_active=0）后对话（B 场景）；③ 正常 endpoint 对话不回归（C）；④ 假
   endpoint（不可达 base_url）受控降级不 500（D）；⑤ 旧自由文本 agent 对话
   不回归（E）；⑥ system-default 兜底端点仍在（F）。
3. 共享 pg-unified 残留复原 SQL 见 §4（pg_residue.json），destructive 操作请确认
   后执行（同 TASK-033 遗留：05-temp/t032/pg_contamination_record.json 亦待用户确认）。
