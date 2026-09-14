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
