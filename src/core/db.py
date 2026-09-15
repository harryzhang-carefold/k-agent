"""database layer — 双后端分派（TASK-015，D2~D7）。

对外接口保持不变（D6）: connect() / init_db() / fetchall() / fetchone() / execute() / close()。
conn 参数语义统一为"连接句柄"（postgres: asyncpg 池 / sqlite: aiosqlite 单连接），业务代码零感知。

- sqlite（默认，D1）: aiosqlite 单连接 + check_same_thread=False（D7）。
  `?` 占位符原生可用——不走 _to_pg 的占位符/时间戳/OR IGNORE 翻译；
  execute() 对 identity 表 INSERT 返回 lastrowid（D3，与 asyncpg RETURNING id 语义一致），
  非 identity / UPDATE / DELETE 返回 None。
  init_db() = executescript(schema_sqlite.sql) 幂等建表（D4/D5）。
- postgres（可配置）: asyncpg 池路径**逻辑一行不动**（D2，已验收），
  仅函数改名为私有 _pg_* 并加一层公共分派入口。
"""
import os
import re
import asyncpg
from core.config import S

# 有自增主键、INSERT 需要返回 id 的表（14 张；postgres 走 BIGINT identity，
# sqlite 走 INTEGER AUTOINCREMENT，语义一致，D3）
IDENTITY_ID_TABLES = {
    "users", "roles", "agents", "skills", "mcp_servers", "rag_knowledge",
    "rag_chunks", "memory_l0_raw", "memory_l1_cache", "memory_l1_image",
    "memory_l2_edges", "memory_l2_buckets", "hitl_queue", "messages",
}

_TS_EXPR = "to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')"
_POOL = None
_SQLITE_CONN = None


# ============================ postgres 路径（asyncpg，阶段 C 原样保留，D2） ============================

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


async def _pg_connect():
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


async def _pg_init():
    """连通性自检 + TASK-021 settings 表幂等自举（agp schema，CREATE IF NOT EXISTS）。

    建表失败（如权限被收紧）只告警不抛错：settings 持久化降级为纯内存热更新
    （重启不持久），不阻断应用启动。
    """
    pool = await connect()
    async with pool.acquire() as c:
        await c.execute("SELECT 1")
        try:
            await c.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                " key TEXT PRIMARY KEY,"
                " value TEXT NOT NULL,"
                " updated_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')))"
            )
        except Exception as e:
            import logging
            logging.getLogger("core.db").warning(
                "settings 表创建失败（降级为纯内存配置，重启不持久）: %s", e)


async def _pg_fetchall(conn, sql, params=()):
    pool = conn
    p = _split_params(params)
    async with pool.acquire() as c:
        return [dict(zip(r.keys(), r)) for r in await c.fetch(_to_pg(sql), *p)]


async def _pg_fetchone(conn, sql, params=()):
    pool = conn
    p = _split_params(params)
    async with pool.acquire() as c:
        row = await c.fetchrow(_to_pg(sql), *p)
    return dict(zip(row.keys(), row)) if row is not None else None


async def _pg_execute(conn, sql, params=()):
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


async def _pg_close(conn):
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None


# ============================ sqlite 路径（aiosqlite，TASK-015 新增） ============================

async def _sqlite_connect():
    """单例 aiosqlite 连接（D7: 单连接 + check_same_thread=False）。"""
    global _SQLITE_CONN
    if _SQLITE_CONN is None:
        import aiosqlite
        path = S.effective_sqlite_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        # D7: 单连接被 FastAPI 多协程共享，aiosqlite 内部线程池执行需放开线程限制
        await conn.execute("PRAGMA journal_mode=WAL")
        _SQLITE_CONN = conn
    return _SQLITE_CONN


async def _sqlite_init():
    """幂等建表（D4/D5）：executescript(schema_sqlite.sql)，同一库可重复执行。"""
    conn = await _sqlite_connect()
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_sqlite.sql")
    with open(schema_path, encoding="utf-8") as f:
        script = f.read()
    await conn.executescript(script)
    await conn.commit()


async def _sqlite_fetchall(conn, sql, params=()):
    p = _split_params(params)
    cur = await conn.execute(sql, p)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _sqlite_fetchone(conn, sql, params=()):
    p = _split_params(params)
    cur = await conn.execute(sql, p)
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row is not None else None


async def _sqlite_execute(conn, sql, params=()):
    """D3: identity 表 INSERT 返回自增 id（lastrowid），其余返回 None。

    INSERT OR IGNORE 在 sqlite 原生可用（不走 _to_pg）；冲突被忽略时
    cursor.rowcount=0 而 lastrowid 会**保留前一次 INSERT 的值**（不是 0），
    必须用 rowcount 判断本条语句是否真的插入了行——与 asyncpg 路径
    ON CONFLICT DO NOTHING 时 fetchrow=None -> 返回 None 的语义对齐。
    """
    p = _split_params(params)
    m = re.search(r"INSERT\s+OR\s+IGNORE\s+INTO\s+([a-zA-Z_]+)", sql, re.I) \
        or re.search(r"INSERT\s+INTO\s+([a-zA-Z_]+)", sql, re.I)
    tbl = m.group(1) if m else None
    cur = await conn.execute(sql, p)
    await conn.commit()
    rid, rc = cur.lastrowid, cur.rowcount
    await cur.close()
    if tbl in IDENTITY_ID_TABLES and rc and rid:
        return rid
    return None


async def _sqlite_close(conn):
    global _SQLITE_CONN
    if _SQLITE_CONN is not None:
        await _SQLITE_CONN.close()
        _SQLITE_CONN = None


# ============================ 对外统一接口（D6：接口名不变，按 S.DB_BACKEND 分派） ============================

def _backend():
    """分派用后端名；未知值显式 fail-fast（不静默落入任一实现）。"""
    b = S.DB_BACKEND
    if b not in ("sqlite", "postgres"):
        raise RuntimeError(f"未知 DB_BACKEND: {b!r}（仅支持 sqlite | postgres）")
    return b


async def connect():
    """返回单例连接句柄（sqlite: aiosqlite 连接 / postgres: asyncpg 池）。"""
    if _backend() == "sqlite":
        return await _sqlite_connect()
    return await _pg_connect()


async def init_db():
    if _backend() == "sqlite":
        await _sqlite_init()
    else:
        await _pg_init()


async def fetchall(conn, sql, params=()):
    if _backend() == "sqlite":
        return await _sqlite_fetchall(conn, sql, params)
    return await _pg_fetchall(conn, sql, params)


async def fetchone(conn, sql, params=()):
    if _backend() == "sqlite":
        return await _sqlite_fetchone(conn, sql, params)
    return await _pg_fetchone(conn, sql, params)


async def execute(conn, sql, params=()):
    if _backend() == "sqlite":
        return await _sqlite_execute(conn, sql, params)
    return await _pg_execute(conn, sql, params)


async def close(conn):
    if _backend() == "sqlite":
        await _sqlite_close(conn)
    else:
        await _pg_close(conn)


# ---------------- healthz 展示（D8：不暴露密码） ----------------

def backend_info() -> dict:
    if _backend() == "postgres":
        return {"backend": "postgres", "host": S.DB_HOST, "port": S.DB_PORT,
                "database": S.DB_NAME, "schema": S.DB_SCHEMA}
    return {"backend": "sqlite", "path": S.effective_sqlite_path}
