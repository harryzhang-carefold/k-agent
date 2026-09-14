"""asyncpg database layer — PostgreSQL (schema=agp) 替代 aiosqlite (PG 统一迁移 阶段 C)。

设计要点（RISK-003 逐文件改造收敛到本文件）：
- 连接：asyncpg.create_pool，init 回调 SET search_path=agp,public（DECISION-002）。
  connect() 返回单例 pool（app 与 memory backend 共用一个池）。
- SQLite 方言差异集中翻译到 _to_pg()，业务 SQL 只写 ? 占位符：
    1) ?            -> $1, $2, ...（顺序位置替换）
    2) datetime('now') -> to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')
       （与迁移的 text 时间戳格式 / DDL 默认值一致）
    3) INSERT OR IGNORE INTO x (...) VALUES (...)
       -> INSERT INTO x (...) VALUES (...) ON CONFLICT DO NOTHING
- execute() 对 identity 表 INSERT 自动追加 RETURNING id 并返回（替代 lastrowid）；
  非 identity / UPDATE / DELETE 返回 None（与 SQLite 语义一致：无自增 id 可用）。
- 表结构由迁移脚本（migrate_c_import.py）创建，运行时**不改表**（防 schema 漂移，
  与阶段 B ddl-auto=none 同策略）；init_db() 仅做连通性自检，不建表。
"""
import re
import asyncpg
from core.config import S

# 有 BIGINT identity 主键、INSERT 需要返回自增 id 的表（14 张）
IDENTITY_ID_TABLES = {
    "users", "roles", "agents", "skills", "mcp_servers", "rag_knowledge",
    "rag_chunks", "memory_l0_raw", "memory_l1_cache", "memory_l1_image",
    "memory_l2_edges", "memory_l2_buckets", "hitl_queue", "messages",
}

_TS_EXPR = "to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')"
_POOL = None


def _to_pg(sql: str) -> str:
    """SQLite 方言 -> PostgreSQL：占位符 / 时间戳 / OR IGNORE。见模块 docstring。"""
    s = re.sub(r"datetime\(\s*'now'\s*\)", _TS_EXPR, sql)
    had_ignore = re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", s, re.I) is not None
    s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, flags=re.I)
    counter = {"i": 0}

    def _ph(_):
        counter["i"] += 1
        return f"${counter['i']}"

    s = re.sub(r"\?", _ph, s)
    if had_ignore and not re.search(r"ON\s+CONFLICT", s, re.I):
        s = s + " ON CONFLICT DO NOTHING"
    return s


def _dict(row) -> dict | None:
    if row is None:
        return None
    return dict(zip(row.keys(), row))


def _split_params(params=()):
    return tuple(params) if params else ()


async def connect():
    """返回单例 asyncpg pool（首次创建；init 回调设 search_path）。"""
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(
            dsn=S.DSN,
            min_size=1,
            max_size=8,
            command_timeout=30,
            init=lambda c: c.execute(f"SET search_path TO {S.DB_SCHEMA}, public"),
        )
    return _POOL


async def init_db():
    """连通性自检（表由迁移脚本创建，运行时不建/改表，防 schema 漂移）。"""
    pool = await connect()
    async with pool.acquire() as c:
        await c.execute("SELECT 1")


async def fetchall(conn, sql, params=()):
    pool = conn
    p = _split_params(params)
    async with pool.acquire() as c:
        return [dict(zip(r.keys(), r)) for r in await c.fetch(_to_pg(sql), *p)]


async def fetchone(conn, sql, params=()):
    pool = conn
    p = _split_params(params)
    async with pool.acquire() as c:
        row = await c.fetchrow(_to_pg(sql), *p)
    return dict(zip(row.keys(), row)) if row is not None else None


async def execute(conn, sql, params=()):
    pool = conn
    p = _split_params(params)
    pg_sql = _to_pg(sql)
    m = re.search(r"INSERT\s+INTO\s+([a-zA-Z_]+)", pg_sql, re.I)
    tbl = m.group(1) if m else None
    if tbl in IDENTITY_ID_TABLES:
        async with pool.acquire() as c:
            row = await c.fetchrow(pg_sql + " RETURNING id", *p)
            return row["id"] if row is not None else None
    async with pool.acquire() as c:
        await c.execute(pg_sql, *p)
    return None


async def close(conn):
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None
