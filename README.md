# AI Agent Platform (k-agent)

多模型 AI Agent 平台：OpenAI 兼容 LLM 底座 + 工具调用引擎 + 三层记忆 + RAG 知识库 + MCP 插件体系，REST/WS API 与纯原生 JS 单页前端，Docker Compose 部署，数据层双后端（默认 SQLite 零依赖，PostgreSQL 可配置）。

## 1. 功能特性

| 模块 | 能力 |
|---|---|
| **LLM 底座** | OpenAI 兼容接口（`/v1/chat/completions`），流式 token 输出，指数退避重试，失败可降级 |
| **Agent 执行引擎** | prefix-caching prompt 组装（固定内容最左、请求最右），工具调用循环（JSON `tool_call` 协议，最多 N 轮） |
| **缓存路由（5 策略）** | ① 图片 MD5 命中直返 ② 文本语义相似度 > 阈值命中直返 ③ 混合输入拆分 ④ 复杂任务拆分 ⑤ prefix caching — 全部真实实现，命中时 LLM 调用 = 0 |
| **RAG 知识库** | 滑动窗口分块（2000 token、20% 重叠）→ 嵌入 → 余弦 top-k 检索 → 上下文拼入 prompt 左侧 |
| **三层记忆** | L0 原始会话 / L1 缓存 / L2 立体图（双向属性图 + 每节点 B+ 树按日期/实体/过程/主题分桶），与 Agent 解耦的可插拔后端 |
| **MCP** | 双传输客户端（**stdio** JSON-RPC 子进程 + **http** Streamable HTTP 远程端点，`initialize → tools/list → tools/call` 同语义），外部 MCP server 配置表接入（`transport`/`url`/`headers` 字段）；内置插件注册表（get_time/mcp_call/echo）作为 tool 暴露给 LLM |
| **长文本 4 策略** | Map-Reduce 提取 / 增量图构建 / 批判-精炼 / 确定性预处理（同名指标单位冲突强制"需人工复核"） |
| **RBAC** | JWT HS256 + 13 权限 × 4 角色（admin/developer/user/viewer），路由级守卫 |
| **工具管理（阶段四）** | Skills 管理（增删改/搜索/**上传导入** 单文件 .md/.txt/zip，**导入结果面板**显示「✓ 完成：新增 N·跳过 N·失败 N」+ 每项明细；BUG-006 已修复）+ MCP 管理（表单/env 键值对/启停/删除） |
| **记忆可视化（阶段四）** | L0/L1/L2 列表 + 3D 力导向立体图（纯 Canvas-2D 零框架，点击节点→2 跳子图）+ B+ 树分桶可视化 + 多跳 |
| **模型节点配置（阶段四）** | LLM/Embedding 节点卡片：全字段（脱敏回显 api_key 只回 key_set+末4位）+ 测试连接（真实连通 ok / 失败受控 200+ok:false）+ 持久化（DB>env 优先级，重启不丢） |
| **LLM endpoint 多维护（阶段五）** | 新表 `llm_endpoints`（双后端幂等）+ `/api/llm-endpoints` CRUD/连通测试/设默认；api_key 只回 `key_set`+末4位（0 明文）；Agent 模型字段改**下拉选择 endpoint**；对话按 endpoint 真实路由 base_url/api_key，endpoint 删除/停用后回退系统默认 `S.LLM_MODEL`（BUG-007 已修复，不崩） |
| **MCP / 长文本 tooltip（阶段五）** | MCP 配置 + 长文本 4 策略信息 tooltip（纯 CSS hover 原生实现），文案与 `mcp_server_demo.py` 实际参数 / `longtext.py` docstring 逐条核对，窄屏不遮挡 |
| **Agent 绑定端到端（阶段五）** | Skill/MCP/RAG/**Plugins（动态下拉 GET /api/ext/plugins）** 四选绑定，保存落库 + Prompt 预览体现生效（工具 schema 注入） |
| **Hermes Agent 双后端（阶段六）** | Agent 可选 `custom`（内置引擎，零回归）/ `hermes`（hermes CLI profile 后端）：`/api/hermes/profiles` CRUD + `/status` 探测（CLI 缺失优雅 503）；hermes 对话=同步 `hermes -p <profile> -z` + WS 流式（整段 token）+ L0 记忆照写 + 受控降级（不裸 500）；profile 创建即零工具面（仅对话）；api_key 全程脱敏（AC-H9）；双后端（sqlite/PG）幂等迁移 |
| **Hermes 前端联动（阶段六）** | Agent 表单顶部类型单选 `[自定义 Agent \| Hermes Agent]`（hermes 不可用时选项隐藏+提示，AC-H7）；选 hermes → profile 下拉（`GET /api/hermes/profiles`，含「+ 新建 profile」内嵌创建并自动选中）+ 提示文案（默认仅对话/工具由 profile skills 决定），隐藏 system_prompt/模型/绑定区；选 custom → 原表单零回归；列表 hermes agent 加紫色 `Hermes` badge（模型列显示 profile 名）；删除 hermes agent 二次确认提示 profile 保留 |
| **全链路 Trace 记录（迭代5，1.7.0）** | 每次对话记录完整链路：会话级聚合 `trace_conversations`（token 总量/模型/工具/skill/RAG/文件/状态/耗时）+ 步骤级明细 `trace_spans`（llm_call 真实 token+模型+耗时 / tool_call / mcp_call / rag_search 含 chunk 粒度 / skill_inject / file_op / cache_hit / error）。**非阻塞铁律**：写入失败只 log 不阻塞主链路；span input/output 截断 2000 字符；保留 N 天（默认 30，`settings`/`TRACE_RETENTION_DAYS` 可配，启动幂等清理）；hermes 后端 token 无 usage → NULL + `hermes_no_usage` 标注（不编造数字）。查询 `GET /api/trace/conversations[/{conv_id}]`（`system:admin`，不跨 agent 泄露） |
| **API 与前端** | REST `/api/*` + WebSocket `/ws/chat/{agent}/{conv}`（流式）；纯原生 JS SPA（无框架） |
| **可观测** | `/healthz` 健康检查（含 LLM/embedding/DB/记忆后端状态） |

## 2. 架构

```
                ┌─────────────────────────────────────────────┐
 浏览器 ──────► │  static/ SPA（index.html + app.js 原生 JS）  │
 (HTTP/WS)      └──────────────────┬──────────────────────────┘
                                   │ /api/*  /ws/chat/...
                ┌──────────────────▼──────────────────────────┐
                │  FastAPI  (core/app.py)                      │
                │  routers/api1.py  auth·agents·ext            │
                │  routers/api2.py  chat·ws·rag·memory·longtext│
                ├──────────────────────────────────────────────┤
                │  engine/agent_engine.py  prompt 组装 + 工具循环 │
                │  engine/cache_router.py  5 策略缓存路由        │
                ├──────────────┬───────────────┬───────────────┤
                │  llm/        │  rag/         │  memory/      │
                │  provider    │  分块+嵌入+检索 │  backend 抽象 │
                │  embedding   │  上下文拼装    │  graph 属性图 │
                │  (远程/哈希)  │               │  btree  B+树  │
                ├──────────────┴───────────────┴───────────────┤
                │  mcp/  stdio+HTTP 双传输客户端 + plugins 注册表  │
                │  services/longtext.py  长文本 4 策略           │
                ├──────────────────────────────────────────────┤
                │  core/db.py  双后端：aiosqlite(默认) | asyncpg │
                │  core/security.py  JWT + RBAC                 │
                └──────────────────┬───────────────────────────┘
                      DB_BACKEND=  │  DB_BACKEND=postgres
                      sqlite       │
                ┌──────────────────▼──────────┐
                │ SQLite src/data/agp.db       │  默认（零依赖）
                │ 或 PostgreSQL (pg-unified)   │  可选（agp schema）
                └─────────────────────────────┘
```

代码目录：

```
src/
├── core/        app.py 入口 | config.py 配置 | db.py 数据层 | security.py JWT+RBAC
├── engine/      agent_engine.py 执行引擎 | cache_router.py 5 策略缓存
├── llm/         provider.py LLM 抽象 | embedding.py 双模向量
├── memory/      backend.py 可插拔抽象 | graph.py 属性图 | btree.py B+树
├── rag/         rag.py 分块/嵌入/检索/拼装
├── mcp/         mcp_client.py stdio 客户端 | mcp_server_demo.py | plugins.py
├── services/    longtext.py 长文本 4 策略
├── routers/     api1.py (auth/agents/ext) | api2.py (chat/ws/rag/memory/longtext)
├── seed.py      种子数据（4 用户/4 角色/13 权限/医疗 agent/RAG 文档/L2 图）
├── static/      前端 SPA（index.html / app.js / style.css）
├── Dockerfile   python:3.12-slim
└── requirements.txt
tests/           pytest 套件（auth/agents/chat/rag/memory/mcp/longtext/ws）
```

## 3. 服务组件配置

所有配置经 `src/.env` 注入（复制 `.env.example` → `.env` 填写）。**密钥严禁入库/入日志**（DECISION-004），`.env` 已被 `.gitignore` 排除。

### 3.1 LLM 底座

| 变量 | 默认值 | 可替换为 |
|---|---|---|
| `LLM_BASE_URL` | `http://34.121.9.233:4000/v1`（vLLM） | 任意 OpenAI 兼容端点：OpenAI、Azure、DeepSeek、Moonshot、Ollama(`/v1`)、其它 vLLM 实例 |
| `LLM_MODEL` | `vllm-qwen3.8-27b` | 端点上任意模型名（qwen/gpt-4o/deepseek-…） |
| `AI_MODEL_API_KEY` | 空（须填） | 对应端点 API key |
| `LLM_TIMEOUT` | `90`（秒） | 按模型响应速度调 |
| `LLM_RETRIES` | `3` | 0 关闭重试 |

### 3.2 Embedding（双模，自动回退）

| 变量 | 默认值 | 可替换为 |
|---|---|---|
| `EMBEDDING_BASE_URL` | **空 → 本地确定性哈希向量** | 任意 OpenAI 兼容 `/v1/embeddings` 端点 |
| `EMBEDDING_MODEL` | 空 | 远程模型名（text-embedding-3 等） |
| `EMBEDDING_API_KEY` | 空 | 远程端点 key |
| `EMBEDDING_DIM` | `512` | 需与本地哈希维度一致；远程向量会被 resize 到该维度 |

留空即零外部依赖：字符 3-gram → MD5 → 512 维 → L2 归一化，同文本恒同向量（可断言）。远程不可达时运行时自动回退本地。

### 3.3 记忆后端（可插拔）

| 变量 | 默认值 | 可替换为 |
|---|---|---|
| `MEMORY_BACKEND` | `local`（SQLite/PG 内表） | `redis` / `milvus` / `neo4j`（适配位存根，接口一致，部署期接入）/ `hermes`（JSONL 事实流对接 Hermes Agent 记忆） |
| `HERMES_MEMORY_DIR` | `../../05-temp/hermes_memory.jsonl` | hermes 后端的事实流输出路径 |

引擎只依赖 `MemoryBackend` 抽象 + 工厂，切换后端不改业务代码。

### 3.4 缓存路由

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SEMANTIC_CACHE_THRESHOLD` | `0.95` | 文本语义命中阈值（余弦），提高更严格 |
| `MAX_TOOL_ROUNDS` | `5` | 单轮对话最大工具调用循环次数 |

### 3.5 RBAC

| 变量 | 默认值 | 说明 |
|---|---|---|
| `JWT_SECRET` | 空 → 运行时生成 64 hex（进程内，重启失效） | **生产必须显式配置** ≥32 字节随机串（`openssl rand -hex 32`），否则触发 InsecureKeyLengthWarning（BUG-002） |
| `JWT_TTL_HOURS` | `24` | token 有效期 |
| `SEED_PASSWORD` | `admin123` | 4 个种子用户初始密码（admin/developer/user/viewer），生产必改 |

### 3.6 数据库（双后端：默认 SQLite，PostgreSQL 可配置）

由 `DB_BACKEND` 切换（`sqlite` | `postgres`），业务代码零感知（全部走 `core/db.py` 抽象层）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_BACKEND` | `sqlite` | 后端选择：`sqlite`（零依赖）或 `postgres` |
| `SQLITE_PATH` | `<src>/data/agp.db` | 仅 sqlite 模式；数据文件路径（相对按进程 cwd 解析）。compose 内固定为 `/app/data/agp.db` |

**SQLite（默认，零外部依赖，clone 即跑）**
- 首次启动 `init_db()` 自动 `executescript(src/core/schema_sqlite.sql)` 幂等建表（20 表，全部带 `IF NOT EXISTS`）+ 种子数据，无需任何外部服务。
- 方言原生支持：`?` 占位符、`datetime('now')`、`INSERT OR IGNORE`（不走 `_to_pg` 翻译）。
- 数据落 named volume `agpdata`（compose 声明 `agpdata:/app/data`，持久化，重启/重建不丢；宿主物理位置 `docker volume inspect agp_agpdata`）。

**PostgreSQL（可选，`DB_BACKEND=postgres`）**
| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_HOST` | `pg-unified`（compose 服务名） | 本地调试改 `127.0.0.1` |
| `DB_PORT` | `5432` | |
| `DB_NAME` | `postgres` | |
| `DB_USER` | `agp_user` | 仅 `agp` schema 权限的业务用户 |
| `AGP_DB_PASSWORD` | 空（须填） | 来源：postgres-unified 项目 `.env`，勿外泄 |
| `DB_SCHEMA` | `agp` | 连接池 init 自动 `SET search_path=agp,public` |
| `DB_DSN` | 由上述拼装 | 也可直接给完整 `postgresql://...` DSN 覆盖 |

- 现有 asyncpg 连接池路径完全保留（已验收，未重写）。表由迁移脚本创建，运行时不建/改表。
- 方言翻译集中在 `core/db.py::_to_pg()`：`?`→`$N`、`datetime('now')`→`to_char(...)`、`INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`、identity 表 INSERT 自动 `RETURNING id`。

**切换方法**：只改 `.env` 的 `DB_BACKEND`（sqlite ↔ postgres），重启即生效，业务代码零改动。

历史：系统早期基于 SQLite，2026-09 统一迁移至 PostgreSQL（阶段 C），2026-09 又改回**默认 SQLite + PG 可配置**（TASK-015 双后端，兼顾零依赖易部署与既有 PG 生产数据）。

### 3.7 服务

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8099` | |
| `TZ`（compose） | `Asia/Shanghai` | |

### 3.8 Docker Compose 资源限制（docker-compose.yml）

| 项 | 值 |
|---|---|
| 镜像 | `agp-platform:1.7.0`（build: `./src/Dockerfile`, python:3.12-slim；1.7.0 含 TASK-057 全链路 trace 记录（trace_conversations/trace_spans + /api/trace 查询 + hermes 后端埋点）。回滚锚点 `1.6.1`/`1.6.0`/`1.5.0`/`1.4.0` 保留） |
| 容器名 | `agp-app`，`restart: unless-stopped` |
| 端口 | `8099:8099` |
| 卷 | `agpdata:/app/data`（named volume，持久化 `agp.db`，sqlite 模式重启/重建不丢；**无需 chown 宿主目录**，属主由 Docker 管理，容器内 uid 1000 可直接写）；**Hermes（可选）**: `/home/hermes/.hermes:/home/hermes/.hermes`（宿主 hermes 运行时：venv + profiles + .env）+ `/home/hermes/.local/share/uv:/home/hermes/.local/share/uv`（venv python 符号链接目标）。未挂载的环境 hermes 功能优雅 503，custom 不受影响。显式 bind 卷用户见下方「从 bind 卷迁移到 named volume」 |
| 网络 | 默认（sqlite）不需要外部网络；PG 模式叠加 `docker-compose.pg.yml` 接外部网络 `agp_default`（直连 pg-unified） |
| 资源 | `mem_limit: 512m`，`cpus: 1.0` |
| healthcheck | 每 15s 探 `/healthz`（start_period 20s） |

### 3.9 MCP 服务器注册（stdio / HTTP 双传输，1.6.0）

MCP server 支持**两种传输**，可并存（`mcp_servers.transport` 字段，`'stdio'` / `'http'`）：

- **stdio**：本地可执行命令。平台 spawn 子进程，用 LSP `Content-Length` 帧做 JSON-RPC。需 `command`（可执行程序）+ `args` + 可选 `env`。
- **http（Streamable HTTP）**：远程/容器化 MCP server 的 HTTP 端点。JSON-RPC over POST 单端点，响应可为 JSON 或 SSE 流，自动处理 `Mcp-Session-Id` 会话头。**无需本地可执行程序**，只需 `url`（http(s) 端点地址）+ 可选 `headers`（自定义请求头，如 `Authorization`）。超时默认 10s（connect 5s）。通知（`notifications/*`）的 2xx 响应一律视为成功、不解析 body（兼容 server 在 202 里放占位 JSON，如 `{"received":true}`——BUG-011，1.6.1 修复，MCP Streamable HTTP 2025-03-26 规范）。

校验矩阵（`POST`/`PUT /api/ext/mcp`）：`transport=http ⇒ url 非空且为合法 http(s) URL`（否则 400）；`transport=stdio ⇒ command 非空`（否则 400）；http 行 `command` 存空串（command 保持 stdio 专属语义）。

**注册示例（两种传输）**：

```bash
# ① stdio 本地 demo（容器内置，保存后点 tools/list 可见 get_time / get_patient_demo）
curl -s -X POST http://<host>:8099/api/ext/mcp \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"name":"demo","command":"python3","args":["/app/mcp/mcp_server_demo.py"],
       "env":{},"transport":"stdio","url":"","headers":{},"enabled":true}'

# ② http 远程 server（Streamable HTTP 端点，如容器/云端起的远程 MCP server）
#    headers 用于鉴权：Authorization 值为 "Bearer <token>"
curl -s -X POST http://<host>:8099/api/ext/mcp \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"name":"remote-weather","command":"","args":[],"env":{},
       "transport":"http","url":"http://10.0.0.5:8900/mcp",
       "headers":{"Authorization":"Bearer <remote-token>"},
       "enabled":true}'
```

前端（MCP-Skills 页面）新建/编辑时选**传输类型**：`stdio`（本地命令）显示 command/args/env；`http`（Streamable HTTP 端点）显示 URL + headers 键值对。列表行带 `stdio` / `http` 传输标记；`tools/list` 测试按钮对两种传输通用（后端按行 `transport` 分流）。

### 3.10 全链路 Trace 记录（迭代5，1.7.0）

每次对话记录完整执行链路，用于"这次对话到底发生了什么"的可观测性。**双后端**（SQLite / PostgreSQL）幂等迁移：`trace_conversations`（会话级聚合，一个 conv 一行）+ `trace_spans`（步骤级明细，按 `seq` 时序），启动时自动建表（`CREATE IF NOT EXISTS`），旧库零回归。

**Span 类型**（`span_type`）：
- `llm_call`：每次 LLM 调用 → 真实 `tokens_in`/`tokens_out`（来自 provider usage，流式聚合末尾 usage chunk）+ 真实 `model` 名 + `duration_ms`
- `tool_call` / `mcp_call`：插件 / MCP 工具调用（`mcp:server/tool` 命名）+ 入参/结果
- `rag_search`：RAG 检索 → `rag_chunks` 字段记录 chunk 粒度（`knowledge_id`/`chunk_seq`/`score`/`preview`，前 5 个 chunk）
- `skill_inject`：注入的 skill 摘要
- `file_op`：中间文件路径（工具结果中的 `files`/`file_path` 字段 + hermes 后端 CLI 工作目录文件）
- `cache_hit`：缓存命中直返
- `error`：降级 / 异常（附错误信息）

**非阻塞铁律**：所有 trace 写入包 `try/except`，失败只 `log` 不 `raise`——主聊天链路绝不被埋点阻塞（即使 trace 表损坏，对话仍正常返回）。span 的 `input`/`output` 截断 **2000 字符**，防 DB 膨胀。

**保留策略**：trace 数据保留 N 天（默认 30，`settings` 表 `trace_retention_days` 或 `.env` `TRACE_RETENTION_DAYS` 可配，DB > env 优先级），启动时幂等清理过期行（失败只 log 不阻断启动）。

**hermes 后端**：hermes CLI 无 token usage → `llm_call` span 的 `tokens_in`/`tokens_out` 记 **NULL**（不编造数字）+ `error` 字段注明 `hermes_no_usage`；CLI 工作目录文件变化记 `file_op`。

**查询 API**（`system:admin`，安全优先，不跨 agent 泄露）：
```bash
# 会话聚合列表（按 started_at 倒序）
curl -s -H "Authorization: Bearer <admin-token>" \
  "http://localhost:8099/api/trace/conversations?limit=50" | jq

# 会话详情（聚合 + 全部 spans，按 seq 升序）
curl -s -H "Authorization: Bearer <admin-token>" \
  "http://localhost:8099/api/trace/conversations/<conv_id>" | jq
```
非 admin（developer/user/viewer）访问 `/api/trace/*` 一律 **403**（不返回任何 trace 数据）。

**前端「链路追踪」tab（迭代5，1.7.0）**：登录 **admin** 后进入 **MCP-Skills** 页面，页面底部为「链路追踪」面板：
- **会话列表**：时间 / agent（含后端 badge）/ 模型 / token 总量（in/out · LLM 次数）/ 工具数 / 状态；可按 agent 下拉过滤；点行进入详情。
- **会话详情（时间线视图）**：每个 span 一行——序号 / 类型图标（🧠LLM · 🔧工具 · 🔌MCP · 📚RAG · 🧩Skill · 📄文件 · ⚡缓存 · ⚠️错误）/ 名称 / 时间 / 耗时 / token / 状态；点「展开」看该步 `input`/`output`（JSON 自动美化），**`rag_search` 展开可见命中 chunk 列表**（`knowledge_id` / `chunk_seq` / `score` / `preview`，具体到 chunk）。
- **三态**：加载态（"加载 trace 会话列表…"）、空态（"暂无 trace 数据"）、错误态（显示后端错误信息 + 重试按钮，如 403 无权限）；非 admin 账号直接显示无权限提示（不发请求）。
- 面板为独立局部刷新（`#trc-panel`），刷新/展开不影响页面上方 Skills / MCP / Plugins / 长文本 4 策略区域（零回归）。

## 4. 部署与启动

前置：一个可达的 OpenAI 兼容 LLM 端点。**默认 SQLite 模式无需任何数据库外部依赖**；仅 PG 模式需要一个 PostgreSQL（独立 `pg-unified` 容器，需已建 `agp` schema 与 `agp_user` 用户；参考 postgres-unified 项目的 docker-compose）。

### 方式 A：一键部署脚本 `./deploy.sh`（推荐）

> **1.5.0 起 GCP 云端部署（CI 自动）走 `scripts/gcp_deploy.sh`**，数据库策略见下方"数据库自动决策（1.5.0）"。本地/手动部署仍用本节 `./deploy.sh`。

### 数据库自动决策（1.5.0，GCP CI 部署）

`push main` → GitHub Actions（`.github/workflows/deploy.yml`）→ SSH 到 GCP VM 执行 `scripts/gcp_deploy.sh`，DB 策略（用户拍板，TASK-046）：

1. **默认 sqlite**：`ENV_FILE` 未配置 `DB_BACKEND` 或 `=sqlite` → 一律 sqlite（零外部依赖，clone 即跑），跳过 DB 探测。
2. **postgres 模式**（`DB_BACKEND=postgres`）：在 GCP 宿主按序 **探测 → 复用 → 自建（幂等）**：
   - 探测 `agp-pg`（此前自建，凭据在 `.pg_credentials`）→ 其他 postgres 容器 → `127.0.0.1:5432`；
   - 可复用（连通 + 认证 + 库存在；库缺失但可登录则自动建库 + `agp` schema）→ 复用，有效 DSN 写 `.env`；
   - 复用不了/不存在 → `docker run -d postgres:16-alpine` 自建 `agp-pg`（固定名、数据卷持久化、固定用户、密码 ENV_FILE 有则用/无则生成并持久化到 `.pg_credentials` chmod 600，等 `pg_isready` 就绪）；
   - 重跑 deploy 幂等（`agp-pg` 已存在 → 复用不重建）。
3. **部署后健康检查**：compose up 后轮询 `/healthz`（≤120s）；失败 → CI 日志输出 `docker logs --tail 100` + `docker ps -a` + restart 次数后 exit 1（CI 日志 = 第一诊断现场）。
4. **app 侧 fail-fast**：启动连 DB 超时（10s）→ 明确错误日志后立即退出（禁止无限 hang）；sqlite 卷不可写同样 fail-fast（日志指明目录与权限）。
5. **数据卷属主防御**：up 前 `chown 1000:1000 src/data`（无权限则 `chmod 777` 兜底 + warning）。

```bash
cd 02-development

# 1. 配置密钥（只需 LLM key + JWT，无需数据库密码）
cp src/.env.example src/.env
#    填写 AI_MODEL_API_KEY / JWT_SECRET(>=32字节) / SEED_PASSWORD
#    （PG 模式另填 DB_BACKEND=postgres + AGP_DB_PASSWORD 等 DB_*）

# 2. 一键部署（一条命令完成）
sg docker -c 'cd 02-development && ./deploy.sh'
```

`deploy.sh` 的行为（全程 `[deploy]` 日志前缀）：

1. **自动替换已存在容器**：先停止并删除已存在的 `agp-app`（兼容两种来源——手工 `docker run` 创建、无 compose 标签的旧容器，以及 compose 项目 `agp` 的遗留容器，含 Stopped 状态），再 `docker compose up -d --build` 创建启动新容器。**数据在 named volume `agpdata`（sqlite）/ pg-unified 的 `agp` schema（PG）中，重建不会丢失。**
2. 模式选择：`./deploy.sh`（默认 auto：`.env` 为 `DB_BACKEND=postgres` 时自动叠加 `docker-compose.pg.yml`，否则纯 sqlite）/ `./deploy.sh pg`（显式 PG 模式，先做 pg-unified + `agp_default` 网络预检，缺失时打印明确指引而非静默失败）/ `./deploy.sh sqlite`（纯 SQLite）。
3. 健康检查（`/healthz`，重试 ≤30s）+ 打印最终状态（`docker compose ps`、compose 标签、健康状态、数据源）。

铁律（RISK-015）：脚本只操作 `container_name=agp-app` 与 compose 项目 `agp` 的容器，绝不触碰 pg-unified / gw-nginx 等别的项目容器。

**裸 `docker compose up -d` 用法（仍可用，但不含自动替换）**：

```bash
docker compose up -d --build                        # sqlite 模式
docker compose -f docker-compose.yml -f docker-compose.pg.yml up -d --build   # PG 模式
```

注意：若线上已有同名 `agp-app` 容器（尤其是手工 `docker run` 创建、无 compose 标签的旧容器），裸 `up -d` 会报 `Name "agp-app" is already in use`——此时请改用 `./deploy.sh`，或先手工 `docker stop agp-app && docker rm -f agp-app` 再 `up -d`。

**验证**（两种方式通用）：
```bash
curl http://localhost:8099/healthz      # db.backend=sqlite 或 postgres
open  http://localhost:8099/            # 前端 SPA，admin / <SEED_PASSWORD> 登录
```

常用运维：`docker compose logs -f`、`docker compose ps`、`docker compose down`（保留卷数据）。

### 从 bind 卷迁移到 named volume（旧部署升级，T-AGP-NAMEDVOL）

**背景**：1.5.0 默认把 sqlite 数据从 bind 卷 `./src/data:/app/data` 改为 named volume
`agpdata`（全名 `agp_agpdata`），消除「新机器宿主目录属主非 1000 → 容器 uid 1000
不可写 → sqlite fail-fast」的坑（此前每台机器都要记得 `sudo chown 1000:1000 <宿主目录>`）。
named volume 由 Docker 管理属主（首次挂载把镜像内已 chown 1000 的 `/app/data` 复制进卷），
**无需 chown**。

- **全新部署（无旧 `./src/data`）**：`docker compose up -d --build` 即自动建 named volume
  并初始化（空库，首次启动建表 + 种子）。无需任何额外步骤。
- **已有 bind 卷数据的机器（`./src/data/agp.db` 存在）**：先 `docker compose down`（保留
  旧数据），再把旧库拷进 named volume，一条命令（在本目录 `02-development` 下执行）：

  ```bash
  docker compose down
  # 关键：alpine cp 以 root 运行，必须 chown 1000，否则 agp.db 属 root →
  # 容器 uid 1000 不可写 → sqlite fail-fast（实测）。
  docker run --rm -v agp_agpdata:/data -v $(pwd)/src/data:/old:ro \
    alpine sh -c 'cp /old/agp.db /data/ && chown -R 1000:1000 /data'
  docker compose up -d --build
  ```

  之后启动的 `agp-app` 读到的就是旧数据（旧 agent 仍在）。

- **显式 bind 卷用户（保留 `./src/data`）**：如需继续用 bind 卷，用外部 `-f` override
  覆盖回 `./src/data:/app/data`（例如自备 `docker-compose.override.yml` 或 `-f` 文件）——
  此时行为不变，**bind 卷的宿主目录属主由用户自理**（`chown 1000:1000 src/data`，
  无权限则 `chmod 777` 兜底，见 `scripts/gcp_deploy.sh` 的属主防御）。named volume 默认
  不影响这条路径。

- **PG 模式不受影响**：`DB_BACKEND=postgres` 时不读 sqlite 文件，`agpdata` 卷闲置。


### 数据库自动决策（1.5.0，GCP 云端部署）

`scripts/gcp_deploy.sh`（由 CI push main 后经 SSH 在 GCP 宿主执行）按 `.env` 的
`DB_BACKEND` 自动决策数据库，**默认 sqlite，零外部依赖，clone 即跑**：

| `.env` 取值 | 行为 |
|---|---|
| 无 `DB_BACKEND` 或 `=sqlite`（大小写不敏感） | 一律写回 `DB_BACKEND=sqlite`，跳过 DB 探测 |
| `=postgres` | 按序：① 探测已有 PG（`agp-pg` 自建容器 → 其他 postgres 容器 → `127.0.0.1:5432`）；② 可复用（连通 + 凭据正确 + 库可查）→ 直接复用；③ 复用不了 → **自建 `agp-pg`**（`postgres:16-alpine`，数据卷持久化 `$DEPLOY_DIR/pgdata/`，密码来自 `.env` 或生成后持久化到 `$DEPLOY_DIR/.pg_credentials` chmod 600，`pg_isready` 等就绪），并把有效 DSN 写回 `.env` |

幂等性：重跑 deploy 时 `agp-pg` 已存在且健康 → 复用不重建；已退出 → `docker start` 后复用。

**应用侧兜底（fail-fast，1.5.0 新增）**：无论 DB 策略如何，启动连 DB 超时 ≤15s 即
打明确错误日志后退出（`AGP DB fail-fast: ...`），绝不无限 hang——配置错误也表现为
"容器重启 + 日志可读"。启动成功后 stdout 打 `AGP STARTUP OK backend=...` 就绪标记。
deploy 脚本在 `compose up` 后轮询 `/healthz`（≤120s），失败时自动打印
`docker logs --tail 100` + `docker ps -a` + 容器 restart 次数再 exit 1——
CI 日志即第一诊断现场。数据卷属主防御：up 前 `mkdir -p src/data && chown 1000:1000`
（无权限则 chmod 777 兜底并告警）。

### 方式 B：原生进程（本地开发）

```bash
cd 02-development
./run.sh            # 自动: uv 建 .venv → 装依赖 → 生成 .env(缺失时) → 启动 uvicorn → 健康检查(30s)
./run.sh --check    # 只读健康检查
```

默认 sqlite 模式零依赖，`run.sh` 直接可跑；PG 模式 `.env` 设 `DB_BACKEND=postgres` + `DB_HOST=127.0.0.1`（宿主 PG）。

### 种子数据

首次启动（库为空）时，`seed.py` 幂等写入：4 用户 / 4 角色 / 13 权限 / 医疗演示 agent / skills / demo MCP server / plugins / RAG 知识库 / L2 立体图。已存在则跳过。sqlite 模式 `init_db()` 先自动建表（`schema_sqlite.sql`），PG 模式表由迁移脚本预先创建。

### 测试

```bash
cd 02-development
.venv/bin/python -m pytest tests/ -v     # 全量（依赖运行中的服务；sqlite 模式无 PG 依赖）
```

## 5. API 速览

| 方法 路径 | 说明 |
|---|---|
| `POST /api/auth/login` | 登录 → JWT |
| `GET/POST/PATCH/DELETE /api/agents...` | Agent CRUD + 绑定（skill/mcp/plugin/rag/memory） |
| `POST /api/chat` | 同步对话 |
| `WS /ws/chat/{agent_id}/{conv_id}?token=*** | 流式对话（token 逐字推送 + 工具调用可观测） |
| `POST /api/rag/knowledge` + `/documents` + `/search` | 知识库 / 文档 / 检索 |
| `GET /api/memory/l2` 等 | 三层记忆查询 |
| `POST /api/longtext/...` | 长文本 4 策略 |
| `GET /api/trace/conversations[?agent_id=&limit=&offset=]` | 链路追踪：会话聚合列表（`system:admin`） |
| `GET /api/trace/conversations/{conv_id}` | 链路追踪：会话聚合 + 全部 spans（`system:admin`） |
| `GET /healthz` | 健康检查 |

详细设计与验收报告：[DESIGN.md](DESIGN.md)、[DEV_REPORT.md](DEV_REPORT.md)。
