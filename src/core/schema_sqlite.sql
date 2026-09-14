-- AI Agent Platform — SQLite schema（固化自 src/data/agp.db 的 sqlite_master）
-- 导出脚本: 05-temp/gen_schema_sqlite.py（TASK-015, D4）
-- 幂等：全部 CREATE 带 IF NOT EXISTS；executescript 可重复执行（同一库导入两次不报错）。
-- 包含: 20 张业务表 + 全部索引。不含 sqlite_sequence 等内部对象。

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

CREATE TABLE IF NOT EXISTS skills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT,
  content TEXT NOT NULL
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