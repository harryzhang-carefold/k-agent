# AI Agent Platform (k-agent)

多模型 AI Agent 平台：OpenAI 兼容 LLM 底座 + 工具调用引擎 + 三层记忆 + RAG 知识库 + MCP 插件体系，REST/WS API 与纯原生 JS 单页前端，Docker Compose 部署，数据层 PostgreSQL。

## 1. 功能特性

| 模块 | 能力 |
|---|---|
| **LLM 底座** | OpenAI 兼容接口（`/v1/chat/completions`），流式 token 输出，指数退避重试，失败可降级 |
| **Agent 执行引擎** | prefix-caching prompt 组装（固定内容最左、请求最右），工具调用循环（JSON `tool_call` 协议，最多 N 轮） |
| **缓存路由（5 策略）** | ① 图片 MD5 命中直返 ② 文本语义相似度 > 阈值命中直返 ③ 混合输入拆分 ④ 复杂任务拆分 ⑤ prefix caching — 全部真实实现，命中时 LLM 调用 = 0 |
| **RAG 知识库** | 滑动窗口分块（2000 token、20% 重叠）→ 嵌入 → 余弦 top-k 检索 → 上下文拼入 prompt 左侧 |
| **三层记忆** | L0 原始会话 / L1 缓存 / L2 立体图（双向属性图 + 每节点 B+ 树按日期/实体/过程/主题分桶），与 Agent 解耦的可插拔后端 |
| **MCP** | stdio JSON-RPC 客户端（initialize → tools/list → tools/call），外部 MCP 进程配置表接入；内置插件注册表（get_time/mcp_call/echo）作为 tool 暴露给 LLM |
| **长文本 4 策略** | Map-Reduce 提取 / 增量图构建 / 批判-精炼 / 确定性预处理（同名指标单位冲突强制"需人工复核"） |
| **RBAC** | JWT HS256 + 13 权限 × 4 角色（admin/developer/user/viewer），路由级守卫 |
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
                │  mcp/  stdio JSON-RPC 客户端 + plugins 注册表  │
                │  services/longtext.py  长文本 4 策略           │
                ├──────────────────────────────────────────────┤
                │  core/db.py  asyncpg 连接池                    │
                │  core/security.py  JWT + RBAC                 │
                └──────────────────┬───────────────────────────┘
                                   │ search_path=agp
                        ┌──────────▼──────────┐
                        │ PostgreSQL (pg-unified) │
                        │ schema: agp             │
                        └───────────────────────┘
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

### 3.6 数据库（PostgreSQL，schema 隔离）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_HOST` | `pg-unified`（compose 服务名） | 本地调试改 `127.0.0.1` |
| `DB_PORT` | `5432` | |
| `DB_NAME` | `postgres` | |
| `DB_USER` | `agp_user` | 仅 `agp` schema 权限的业务用户 |
| `AGP_DB_PASSWORD` | 空（须填） | 来源：postgres-unified 项目 `.env`，勿外泄 |
| `DB_SCHEMA` | `agp` | 连接池 init 自动 `SET search_path=agp,public` |
| `DB_DSN` | 由上述拼装 | 也可直接给完整 `postgresql://...` DSN 覆盖 |

历史：系统曾基于 SQLite（`src/data/agp.db`），2026-09 迁移至 PostgreSQL 统一容器，方言翻译集中在 `core/db.py::_to_pg()`（业务 SQL 仍写 `?` 占位符）。

### 3.7 服务

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8099` | |
| `TZ`（compose） | `Asia/Shanghai` | |

### 3.8 Docker Compose 资源限制（docker-compose.yml）

| 项 | 值 |
|---|---|
| 镜像 | `agp-platform:1.1.0-pg`（build: `./src/Dockerfile`, python:3.12-slim） |
| 容器名 | `agp-app`，`restart: unless-stopped` |
| 端口 | `8099:8099` |
| 网络 | 外部网络 `agp_default`（用于直连 pg-unified） |
| 资源 | `mem_limit: 512m`，`cpus: 1.0` |
| healthcheck | 每 15s 探 `/healthz`（start_period 20s） |

## 4. 部署与启动

前置：一个可达的 OpenAI 兼容 LLM 端点 + 一个 PostgreSQL（独立 `pg-unified` 容器，需已建 `agp` schema 与 `agp_user` 用户；参考 postgres-unified 项目的 docker-compose）。

### 方式 A：Docker Compose（推荐）

```bash
cd 02-development

# 1. 配置密钥
cp src/.env.example src/.env
#    填写 AI_MODEL_API_KEY / JWT_SECRET(>=32字节) / AGP_DB_PASSWORD / SEED_PASSWORD

# 2. 确保外部网络存在（首次）
sg docker -c 'docker network inspect agp_default >/dev/null 2>&1 || docker network create agp_default'
#    （若 pg-unified 未建，先拉起: cd ../postgres-unified && docker compose up -d）

# 3. 构建并启动
docker compose up -d --build

# 4. 验证
curl http://localhost:8099/healthz      # 应返回 status:up 及组件状态
open  http://localhost:8099/            # 前端 SPA，admin / <SEED_PASSWORD> 登录
```

常用运维：`docker compose logs -f`、`docker compose ps`、`docker compose down`（保留 PG 数据）。

### 方式 B：原生进程（本地开发）

```bash
cd 02-development
./run.sh            # 自动: uv 建 .venv → 装依赖 → 生成 .env(缺失时) → 启动 uvicorn → 健康检查(30s)
./run.sh --check    # 只读健康检查
```

本地直连宿主 PG 时 `.env` 设 `DB_HOST=127.0.0.1`。

### 种子数据

首次连接 PG 的 `agp` schema 为空时，`seed.py` 幂等写入：4 用户 / 4 角色 / 13 权限 / 医疗演示 agent / skills / demo MCP server / plugins / RAG 知识库 / L2 立体图。已存在则跳过。

### 测试

```bash
cd 02-development
.venv/bin/python -m pytest tests/ -v     # 全量（依赖运行中的服务 + PG）
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
| `GET /healthz` | 健康检查 |

详细设计与验收报告：[DESIGN.md](DESIGN.md)、[DEV_REPORT.md](DEV_REPORT.md)。
