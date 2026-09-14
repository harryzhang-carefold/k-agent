# DESIGN — AI Agent 平台（ai-agent-platform）

> 技术设计文档。章北海维护，写完即动工。对应 PRD 15 模块 + BRIEF 第五节核心逻辑。

## 1. 技术栈选型

| 组件 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.13.5（系统） | BRIEF 第六节 |
| 框架 | FastAPI + uvicorn | 异步、WS 原生支持 |
| HTTP 客户端 | httpx | 异步调用 OpenAI 兼容端点 |
| 模型 | pydantic v2 | schema / critique-refine 校验 |
| 持久化 | aiosqlite（`data/agp.db` 单文件） | DECISION-002，VM 无外部 DB |
| 数值 | numpy（余弦/向量）+ pandas（长文本预处理策略4） | BRIEF 第六节 |
| JWT | PyJWT（HS256，密钥 `.env`） | 模块 10 |
| 前端 | 纯静态 SPA（原生 JS+CSS，`static/`） | DECISION-001，无构建步骤 |
| 环境 | `uv venv .venv` | DECISION-004 |

## 2. 架构

```
static/ (SPA: Dashboard/Agent构建器/对话/RAG/用户权限/记忆/MCP-Skills)
   │ REST /api/*  +  WS /ws/chat/{agent_id}/{conv_id}
   ▼
FastAPI app（core/app.py）
   ├── auth 依赖（JWT + RBAC 路由守卫 require_perm）
   ├── routers: auth/agents/chat/rag/memory/users/ext/longtext/healthz
   ▼
┌──────────────┬───────────────┬──────────────┬───────────────┐
│ llm provider │ cache_router  │   rag        │  memory 插件   │
│ (httpx,重试, │ (MD5/语义>0.95 │ (滑窗分块+   │ (L0/L1/L2,    │
│  流式,降级)  │  /混合/复杂拆分 │  嵌入/余弦/  │  适配器接口)   │
└──────┬───────┴───────┬───────┴──────┬───────┴───────┬───────┘
       ▼               ▼              ▼               ▼
 agent 引擎（engine.py：prefix-caching prompt 组装 + 工具调用循环）
       │
   plugins(get_time/mcp_call/echo)  mcp_client(stdio JSON-RPC + demo server)
   skills(指令注入)                longtext(Map-Reduce/增量图/critique-refine/pandas)
```

**记忆与 agent 解耦**（AC-50）：`memory/backend.py` 提供 `MemoryBackend` 抽象接口（抽象基类），
`engine/agent_engine.py` / `routers/` 只 import 接口 + `get_memory_backend()` 工厂，**不 import**
`LocalBackend`/`StubBackend` 等具体实现；Hermes 适配器（`memory/backend.py` 的 `HermesBackend`）
按 Hermes 记忆策略（JSONL 事实行写入 `HERMES_MEMORY_DIR`）实现同一接口位。

**存储可插拔**（AC-49）：`MEMORY_BACKEND=local|redis|milvus|neo4j`（`.env`，默认 local）。
redis/milvus/neo4j 为存根适配器（接口一致，无真实服务时抛受控错误并在 `/api/memory/backend`
报告 `stub=true`），文档说明部署期接入方式（DECISION-002 / PD-006）。

## 3. 数据模型（SQLite，schema.sql）

| 表 | 关键字段 |
|---|---|
| users | id, username, password_hash, created_at |
| roles | id, name, description（4 内置） |
| user_roles | user_id, role_id（多对多） |
| permissions | code, description（13 条） |
| role_permissions | role_id, permission_code |
| agents | id, name, description, system_prompt, model, temperature, max_tokens, top_p, created_at, updated_at |
| agent_bindings | agent_id, type(skill/mcp/plugin/rag/memory), ref_id |
| skills | id, name, description, content |
| mcp_servers | id, name, command, args(json), enabled |
| rag_knowledge | id, name, created_at |
| rag_chunks | id, knowledge_id, seq, text, embedding(json), token_est |
| memory_l0_raw | id, agent_id, ts, input, output（不降噪原文） |
| memory_l1_cache | id, agent_key, text, answer, embedding(json), ts |
| memory_l2_nodes | id, label, props(json) |
| memory_l2_edges | id, src, dst, type, props(json)（UNIQUE(src,dst,type) 支撑 MERGE 幂等） |
| hitl_queue | id, task, reason, status(pending), created_at |
| conversations | id, agent_id, created_at |
| messages | id, conv_id, role, content, ts |

> B+ 树为**内存结构**（进程内，按实体节点×维度分桶；种子数据启动时从 L2 图重建，
> 会话期间 L0→L2 归纳持续写入），不单独落盘——持久事实全在 L2 图（AC-44 的有序性/检索
> 对内存树断言即可复现）。

## 4. 接口定义（REST 全清单）

| 方法/路径 | 权限 | 说明 |
|---|---|---|
| GET /healthz | 无 | 200 + 状态（服务/LLM/记忆后端/计数） |
| POST /api/auth/login | 无 | {username,password} → {token, user{roles,permissions}} |
| GET /api/auth/me | 登录 | 当前用户 + 角色 + 权限 |
| GET/POST /api/agents | agent:read / agent:create | CRUD（重名 409，绑不存在资源 400） |
| GET/PUT/DELETE /api/agents/{id} | agent:read / agent:update / agent:delete | |
| GET /api/agents/{id}/prompt | agent:read | **调试接口**：返回组装后的 messages（prefix 布局断言用，AC-18/24/33） |
| GET/POST /api/chat/{agent_id} | 基线(登录) | 同步对话；POST body {message, conv_id?, images?} → 含 cache_hit/llm_calls/tools 观测 |
| WS /ws/chat/{agent_id}/{conv_id}?token=*** | 基线 | 流式 token 回传 {type:token/done/error, content, cache_hit?} |
| GET/POST /api/rag/knowledge | rag:read / rag:write | 知识库 CRUD |
| DELETE /api/rag/knowledge/{id} | rag:delete | |
| POST /api/rag/knowledge/{id}/documents | rag:write | 上传文本 → 滑窗分块+嵌入 |
| GET /api/rag/knowledge/{id}/chunks | rag:read | 分块列表（重叠断言用） |
| POST /api/rag/knowledge/{id}/search | rag:read | {query, top_k} → 带分数 top-k |
| GET /api/memory/l0 | memory:read | 原始记录（不降噪） |
| GET /api/memory/l1 | memory:read | 语义缓存条目 |
| POST /api/memory/l1/search | memory:read | 语义检索（余弦） |
| GET /api/memory/l2/nodes / l2/edges | memory:read | 图节点/边（四要素+属性） |
| POST /api/memory/l2/edge | memory:write | 加边（UPPER_SNAKE 校验 + 拒绝万能边 → 400） |
| GET /api/memory/l2/subgraph?start=&hops= | memory:read | 无向多跳子图：`nodes`(中心+N跳内全部节点) / `edges`(对应边) / `bfs` / `path` / `summary`；start 不存在→空且不报错（BUG-002） |
| GET /api/memory/l2/events | memory:read | Visit 事件节点及其 1:N 关联 |
| GET /api/memory/l2/bucket?node=&dim=&key= | memory:read | B+ 树分桶检索（日期/主题/实体/过程） |
| GET /api/memory/backend | memory:read | 当前后端 + 适配器列表（local/redis/milvus/neo4j，存根标记） |
| GET/POST /api/users | user:manage | 用户 CRUD |
| GET /api/roles, PUT /api/roles/{id}/permissions | role:manage | 角色矩阵读写 |
| GET /api/ext/plugins | 登录 | 内置插件注册表 |
| POST /api/ext/plugins/{name}/call | 登录 | 直接调用插件（get_time/mcp_call/echo） |
| GET /api/ext/mcp, POST /api/ext/mcp | ext:manage | MCP server 注册 |
| GET /api/ext/mcp/{id}/tools, POST .../tools/{t}/call | ext:manage | 走 stdio JSON-RPC（initialize→tools/list→tools/call） |
| GET/POST /api/ext/skills | ext:manage | skill CRUD（空内容 400） |
| POST /api/longtext/map-reduce | 登录 | 策略1：分块→Map 提取→Reduce 去重/时间线/冲突标记 |
| POST /api/longtext/incremental-graph | 登录 | 策略2：逐段三元组→MERGE 入 L2 图→子图摘要 |
| POST /api/longtext/critique-refine | 登录 | 策略3：pydantic 校验→错误回喂重试≤5→HITL 队列 |
| GET /api/longtext/hitl | memory:read | HITL 待办队列 |
| POST /api/longtext/preprocess | 登录 | 策略4：pandas 按 patient_id+date 聚合→结构化 JSON |

统一错误格式：`{"code": <int>, "message": str, "detail": ...}`；401/403/404/400 契约。

## 5. 关键设计

### 5.1 L2 立体图（BRIEF 五-1 / 模块 12+13）
- `graph.py`：内存双向属性图，**免索引邻接**——节点持 `out_edges[(type,dir)] → [Edge]`
  直接指针，多跳遍历 O(跳数)，与图规模无关。
- 边写入校验：`^[A-Z][A-Z0-9_]*$`（UPPER_SNAKE）+ 拒绝 `RELATED_TO/LINKED_TO`（万能边），
  违规 → 写入失败（API 400）。日期/时间/置信度/数值一律存**关系属性**，不建 Date 节点。
- 事件节点：`Visit`（label=Visit，props.id=V20260909），把患者-诊断/检验多对多降维成
  ATTENDED / PRIMARY_DIAGNOSIS / ORDERED_TEST / HAS_TEST_RESULT 的 1:N 边。
- **每个实体节点是一棵 B+ 树根**（`btree.py`，fanout=4，key 排序，支持插入/范围扫描/
  中序遍历有序）：实体节点（如 患者王建国）按 4 维度分桶——`date`（2-1）、`entity`
  （2-2-1）、`process`（2-2-2）、`topic`。写记忆时同时入图 + 入对应 B+ 树桶。

### 5.2 缓存路由 5 策略（模块 14，engine 前置）
1. 图片：MD5(bytes) → 查 L1（image 行）命中 → 直返历史解析，`cache_hit=md5`，LLM 调用=0。
2. 纯文本：嵌入 → 与 L1 同 agent 缓存余弦 > 阈值(默认 0.95，`SEMANTIC_CACHE_THRESHOLD`)
   → 直返，`cache_hit=semantic`，LLM 调用=0。
3. 混合（图片+文本）：先拆分 → 图片走 1、文本走 2（`route_events` 记录 `split:mixed`）。
4. 复杂任务（多句/多问，启发式：≥2 个问句或 >150 字）：先拆分为简单请求 → 各走 1/2
   （`route_events` 记录 `split:complex`）。
5. prefix caching：LLM 请求 = 单条 system（system_prompt + skills 指令 + 工具 schema +
   RAG 上下文，**固定最左**）+ 历史 + 用户请求（**最右**）。同一 agent 同一轮次间
   固定前缀逐字节一致 → vLLM KV 命中。`/api/agents/{id}/prompt` 可断言顺序。

### 5.3 Embedding（模块 8）
- local 确定性哈希向量：字符 3-gram（中文按字）→ MD5 哈希 → 512 维计数向量 → L2 归一化。
  同文本两次**逐维相等**（可断言）；相似度文本余弦 > 不相似（3-gram 共享率决定）。
- OpenAI 兼容配置位：`EMBEDDING_BASE_URL/EMBEDDING_MODEL`，配置且可达时走远程
  `/v1/embeddings`，失败自动降级 local（`provider/embedding.py`）。

### 5.4 长文本 4 策略（模块 15）
1. **Map-Reduce**：token 估算（CJK≈1/字，英文≈1/词）按 2000-3000 token 切块、重叠
   15%；Map=逐块 LLM 提取检验指标 JSON；Reduce=聚合 prompt 让 LLM 去重/时间线排序/
   单位冲突标记"需人工复核"；平台侧再做确定性兜底：同名指标单位不一致 → 强制追加
   `需人工复核` 标记（保证可断言）。
2. **增量图构建**：流式逐段 → LLM 仅输出标准三元组（JSON 数组）→ 应用层 MERGE 写入
   L2 图（UNIQUE(src,dst,type) 幂等，重复写不产生重复边）→ 最终问题只查**子图摘要**
   （BFS 相关子图序列化），不回顾原文。
3. **critique-refine**：pydantic QAItem{question, answer, source_field, value?≥0}；
   校验失败 → 具体错误信息 + 原输入重喂 LLM，≤5 次；达上限 → 写 `hitl_queue`
   （status=pending，不硬失败）。
4. **pandas 预处理**：散乱 CSV 检验记录 → 清洗/去空/按 patient_id+date 聚合 → 结构化
   JSON（LLM 只做语义转换，输入 token 显著小于原始）。

### 5.5 MCP（模块 3）
- `mcp_server_demo.py`：真实 Python stdio 进程，JSON-RPC 2.0（LSP 风格 Content-Length
  头解析），`initialize`→capabilities、`tools/list`→2 个工具（`get_time`、
  `get_patient_demo`）、`tools/call`→真实结果。
- `mcp_client.py`：`asyncio.create_subprocess_exec` 拉起进程，逐次 initialize/list/call，
  超时 + 受控错误；外部真实 MCP 进程属**适配位**（mcp_servers 表存 command/args，
  配置即可接入，文档说明）。

### 5.6 Plugins / Skills（模块 5/4）
- 内置插件（`plugins.py`）：`get_time`（当前时间 ISO）、`mcp_call`（{server,tool,arguments}
  转发到 MCP 客户端）、`echo`。插件 schema 注入 prompt 固定左侧；LLM 以
  `{"tool_call": {...}}` JSON 块请求调用（协议写进 system，跨端点可靠、可断言），
  引擎执行后把结果喂回 LLM，循环上限 5 轮。
- Skills（`skills` 表）：只读 markdown 指令，组装 prompt 时追加进 system 段（Hermes
  风格，固定左侧）；seed：医疗问答规范、工具调用规范。

## 6. RBAC 模型（模块 10）
- 4 角色 / 13 权限矩阵（PD-002）：super_admin=13；agent_developer=agent×4+rag×3+
  memory×2+ext:manage=10；user=agent:read+rag:read+memory:read=3；viewer=agent:read+
  rag:read=2。`chat:use` 为登录基线，不占 13 权限（PD-001）。
- 路由守卫：`Depends(require_perm("agent:create"))` → 无 token 401 / 无权限 403。
- JWT HS256，`JWT_SECRET` 从 `.env`，默认 24h 过期；密码 bcrypt 不可用 → PBKDF2-HMAC-SHA256
  （stdlib hashlib，加盐 100k 轮）。
  **BUG-002 后约束**：JWT_SECRET 必须 >= 32 字节（pyjwt 对短 HMAC key 报 InsecureKeyLengthWarning）。
  `core/config.py` 在 .env 未配置/过短时运行时生成 64 hex 随机值（进程内存，开发环境可接受；
  `run.sh` 会把它持久化进 `.env` 避免重启换密钥）。**生产环境必须显式配置随机 JWT_SECRET**
  （如 `openssl rand -hex 32`），并保证部署间稳定，否则重启后旧 token 全部失效。

## 7. 部署
- `run.sh`：建/复用 `.venv`（uv）→ 写 `.env`（若缺失，从 .env.example + 成员 env 取 key）
  → 清端口 → 起 uvicorn 8099 → 轮询 /healthz 200。
- 无 Docker（VM 无 Docker，BRIEF 第三节）→ 原生 uvicorn 部署；原因记录于 DECISIONS.md
  （DECISION-007）。单进程、SQLite 单文件、纯静态前端，离线可跑（LLM 走远程 vLLM，
  embedding 本地降级）。
- 台账：端口 8099 登记 `~/hermes-workspace/shared/infrastructure/SERVER_REGISTRY.md`。

## 8. 已知问题 / 风险
- RISK-001（LLM 无 embedding）→ 本地哈希降级（已实现，DECISION-003）。
- RISK-006（LLM 非确定）→ 测试只断言格式/流程/非空/命中路径（PD-007）。
- 哈希 embedding 相似度天花板：完全同句≈1.0，同义改写通常 <0.95（n-gram 共享有限）→
  语义命中测试用"近同文"（同句微调）保证 >0.95 可复现；阈值可配置。
- B+ 树内存化：进程重启后从 L2 图重建（种子数据启动重建），不单独持久化（AC-44 断言
  在进程内可复现）。
