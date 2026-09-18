-- AI Agent Platform — SQLite schema（固化自 src/data/agp.db 的 sqlite_master）
-- 导出脚本: 05-temp/gen_schema_sqlite.py（TASK-015, D4）
-- 幂等：全部 CREATE 带 IF NOT EXISTS；executescript 可重复执行（同一库导入两次不报错）。
-- 包含: 21 张业务表 + 全部索引。不含 sqlite_sequence 等内部对象。

-- ==================== TABLES ====================
CREATE TABLE IF NOT EXISTS agent_bindings (
  agent_id INTEGER NOT NULL,
  type TEXT NOT NULL,               -- skill|mcp|plugin|rag|memory
  ref_id TEXT NOT NULL,
  PRIMARY KEY (agent_id, type, ref_id)
);

CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT,
  system_prompt TEXT NOT NULL,
  model TEXT,
  temperature REAL DEFAULT 0.2,
  max_tokens INTEGER DEFAULT 1024,
  top_p REAL DEFAULT 0.9,
  backend TEXT NOT NULL DEFAULT 'custom',       -- TASK-037: custom(内置引擎) | hermes(hermes CLI 后端)
  hermes_profile TEXT,                          -- TASK-037: backend=hermes 时指向的 hermes profile 名
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS hitl_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task TEXT,
  reason TEXT,
  status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mcp_servers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  command TEXT NOT NULL,
  args TEXT DEFAULT '[]',           -- JSON list
  env TEXT DEFAULT '{}',            -- JSON dict（TASK-022 / F1）
  transport TEXT DEFAULT 'stdio',   -- TASK-053 迭代4: 'stdio' | 'http'（Streamable HTTP）
  url TEXT DEFAULT '',              -- TASK-053 迭代4: http 传输的端点（http/https URL）
  headers TEXT DEFAULT '{}',        -- TASK-053 迭代4: JSON dict，可选自定义请求头
  enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS memory_l0_raw (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER,
  ts TEXT DEFAULT (datetime('now')),
  input TEXT,
  output TEXT
);

CREATE TABLE IF NOT EXISTS memory_l1_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_key TEXT NOT NULL,
  text TEXT NOT NULL,
  answer TEXT NOT NULL,
  embedding TEXT,
  ts TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_l1_image (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  md5 TEXT UNIQUE NOT NULL,
  content_type TEXT,
  parsed TEXT,                      -- JSON historical analysis result
  ts TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_l2_buckets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL,
  dim TEXT NOT NULL,                -- date|entity|process|topic
  key TEXT NOT NULL,
  record TEXT NOT NULL              -- JSON
);

CREATE TABLE IF NOT EXISTS memory_l2_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  type TEXT NOT NULL,
  props TEXT DEFAULT '{}',
  UNIQUE (src, dst, type)
);

CREATE TABLE IF NOT EXISTS memory_l2_nodes (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  props TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  ts TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS permissions (
  code TEXT PRIMARY KEY,
  description TEXT
);

CREATE TABLE IF NOT EXISTS rag_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  knowledge_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  text TEXT NOT NULL,
  embedding TEXT,                   -- JSON list[float]
  token_est INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rag_knowledge (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS role_permissions (
  role_id INTEGER NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY (role_id, permission_code)
);

CREATE TABLE IF NOT EXISTS roles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT
);

-- LLM endpoint 多维护（TASK-029 / 需求1）：可配置多个 OpenAI 兼容端点，
-- Agent 按 name 绑定（agents.model 存 endpoint name）。api_key 写库但 API 永不回明文。
-- 启动时 seed 一条固定名 system-default（快照 S.LLM_*，is_active=1）作兜底/默认。
CREATE TABLE IF NOT EXISTS llm_endpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  base_url TEXT NOT NULL,
  model TEXT NOT NULL,
  api_key TEXT DEFAULT '',
  timeout REAL DEFAULT 90,
  retries INTEGER DEFAULT 3,
  is_active INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

-- 系统配置持久化（TASK-021 / F3）：key 限 9 项白名单（见 core.app SETTING_KEYS），
-- value 为 JSON 标量字符串，updated_at 记录最近一次写入。
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS skills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT,
  content TEXT NOT NULL,
  updated_at TEXT DEFAULT (datetime('now'))   -- TASK-022 / F1: 最近修改时间（新建默认 now，PUT 刷新）
);

CREATE TABLE IF NOT EXISTS user_roles (
  user_id INTEGER NOT NULL,
  role_id INTEGER NOT NULL,
  PRIMARY KEY (user_id, role_id)
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

-- ==================== INDEXES ====================