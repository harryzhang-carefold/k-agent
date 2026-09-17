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
import asyncio
import os
import re
import asyncpg
from core.config import S

# 有自增主键、INSERT 需要返回 id 的表（15 张；postgres 走 BIGINT identity，
# sqlite 走 INTEGER AUTOINCREMENT，语义一致，D3）
IDENTITY_ID_TABLES = {
    "users", "roles", "agents", "skills", "mcp_servers", "rag_knowledge",
    "rag_chunks", "memory_l0_raw", "memory_l1_cache", "memory_l1_image",
    "memory_l2_edges", "memory_l2_buckets", "hitl_queue", "messages",
    "llm_endpoints",  # TASK-029 / 需求1：LLM endpoint 多维护
}

_TS_EXPR = "to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')"
_POOL = None
_SQLITE_CONN = None

# TASK-046: 启动阶段连接 PG 的超时（秒）。用户拍板策略第 4 条：
# 启动连 DB 必须 fail-fast（≤15s，明确日志后退出，禁止无限 hang）。
# 取 10s（<15s 要求内），为容器启动开销留出余量，保证容器总生命周期 ≤15s。
# 只约束"首次建池"；运行期断连由 asyncpg 池自愈（reconnect），不受此超时影响。
_PG_STARTUP_TIMEOUT = 10.0


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


async def _pg_bootstrap_schema(conn):
    """TASK-046：确保 PG 侧 search_path 跨连接稳定指向 agp schema（幂等）。

    在【启动 probe 连接】上执行一次，建池之前调用。做两件事：
      1) CREATE SCHEMA IF NOT EXISTS <agp> —— 自建/复用外部 PG 时 schema 可能不存在。
      2) ALTER ROLE current_user SET search_path = <agp>, public —— 角色级默认。

    为什么用角色级默认而不是池 init 回调（已实测的根因）：
      asyncpg 池在一条语句失败后会 reset 物理连接（DISCARD ALL），而 init 回调
      只在物理连接【创建时】执行一次、reset 后不再重放。fresh PG 下 _pg_init 的
      4 条 ALTER（skills/mcp_servers/agents 列补齐）会因"表尚不存在"而失败 → 触发
      reset → 后续池连接回到默认 search_path（"$user", public）→ seed 的无 schema
      前缀查询落到 public → UndefinedTableError → 启动失败。
      角色级默认由 PostgreSQL 在每个新 backend 连接启动时自动套用，跨 reset / 重启 /
      新连接都稳定，彻底消除该竞态。agp_user 对自身角色有 ALTER 权限（实测 OK）；
      若权限不足则仅告警不阻断（退化为旧行为，不引入新故障）。
    """
    import logging
    log = logging.getLogger("core.db")
    schema = S.DB_SCHEMA
    # 1) 补建 schema（外部复用的 PG 可能没有 agp schema）
    try:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    except Exception as e:
        log.warning("AGP PG schema 创建失败（若 DB_USER 无 CREATE SCHEMA 权限，"
                    "建表将失败）: %s: %s", type(e).__name__, e)
    # 2) 角色级默认 search_path（幂等：重复 SET 同值无副作用）
    try:
        await conn.execute(f"ALTER ROLE current_user SET search_path = {schema}, public")
        log.info("AGP PG search_path 角色级默认已设为 %s, public（跨连接稳定）", schema)
    except Exception as e:
        log.warning("AGP PG 角色级 search_path 设置失败（DB_USER 可能非角色属主，"
                    "退回池 init 回调方案；fresh PG 下 seed 可能受影响）: %s: %s",
                    type(e).__name__, e)


async def _pg_connect():
    """返回单例 asyncpg pool（首次创建；init 回调设 search_path）。

    TASK-046 fail-fast：首次建池前先做一次 ≤15s 的直连探测。
    - host 不可达/认证失败/超时 → 立即抛 RuntimeError（启动事件失败 →
      uvicorn 退出 → 容器按 restart 策略重启，CI 诊断日志可读），绝不无限 hang。
    - 运行期断连不受影响：探测成功后池照常惰性连接，断连由 asyncpg 自愈。
    """
    global _POOL
    if _POOL is None:
        import logging
        log = logging.getLogger("core.db")
        dsn = S.require_dsn()
        # 只打 host:port/db，不打 user/password（DECISION-004 密钥纪律）
        from urllib.parse import urlparse
        u = urlparse(dsn)
        host, port, dbname = u.hostname, u.port or 5432, (u.path or "/").lstrip("/") or "postgres"
        log.info("AGP DB connecting: postgres %s:%s/%s (startup timeout %.0fs)",
                 host, port, dbname, _PG_STARTUP_TIMEOUT)
        try:
            probe = await asyncio.wait_for(
                asyncpg.connect(dsn=dsn, command_timeout=_PG_STARTUP_TIMEOUT),
                timeout=_PG_STARTUP_TIMEOUT)
            try:
                await asyncio.wait_for(probe.execute("SELECT 1"),
                                       timeout=_PG_STARTUP_TIMEOUT)
                # TASK-046 根因修复（R-新：fresh PG 下池连接 search_path 丢失）：
                # asyncpg 池在一条语句失败后会 reset 连接（DISCARD ALL），池 init
                # 回调只在"物理连接创建时"执行一次、reset 后不再重放 → 新连接回到
                # 默认 search_path（public 在前），seed 的无 schema 前缀查询
                # （SELECT ... FROM llm_endpoints）落到 public → UndefinedTableError。
                # 只在旧 pg-unified（表已预置、ALTER 全成功、无 reset）时不暴露，
                # 但本次新引入的"自建 agp-pg / 复用外部 PG"路径天然是 fresh PG → 必炸。
                # 修复：在 probe 连接上把 search_path 设为【角色级默认】
                # （ALTER ROLE current_user SET search_path），PG 对每个新 backend
                # 连接都自动套用，跨池 reset / 重启 / 新连接都稳定生效，幂等且
                # agp_user 对自身有 ALTER 权限（已实测）。同时补建 schema（自建/复用
                # 外部 PG 时 schema 可能不存在），确保后续 DDL 有落点。
                await _pg_bootstrap_schema(probe)
            finally:
                await probe.close()
            _POOL = await asyncio.wait_for(
                asyncpg.create_pool(
                    dsn=dsn,
                    min_size=1,
                    max_size=8,
                    command_timeout=30,
                    init=lambda c: c.execute(f"SET search_path TO {S.DB_SCHEMA}, public"),
                ),
                timeout=_PG_STARTUP_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"AGP DB fail-fast: 连接 postgres 超时（>{_PG_STARTUP_TIMEOUT:.0f}s）"
                f" host={host}:{port} db={dbname} —— "
                "检查 DB_HOST/DB_PORT 指向的 PG 是否可达、防火墙/安全组、凭据是否正确")
        except Exception as e:
            raise RuntimeError(
                f"AGP DB fail-fast: 连接 postgres 失败 host={host}:{port} db={dbname}: "
                f"{type(e).__name__}: {e} —— 检查 DB_HOST/DB_PORT/DB_USER/AGP_DB_PASSWORD")
        log.info("AGP DB connected: postgres %s:%s/%s", host, port, dbname)
    return _POOL


async def _pg_init():
    """连通性自检 + 全量 schema 幂等自举（agp schema，CREATE IF NOT EXISTS）。

    TASK-046：1.4.0 只自举 settings / llm_endpoints 两张表，core 表（users/
    permissions/roles/skills/agents/... 共 20 张）依赖外部预置 schema。本次拍板
    策略引入"自建 agp-pg / 复用外部 PG"路径——fresh PG 上 core 表不存在，seed 直接
    报错启动失败。故改为 executescript 全量 schema_pg.sql（与 schema_sqlite.sql
    一一对应），使 PG 模式与 sqlite 模式能力对齐：fresh PG 也能 clone/自建即跑。

    执行在【启动 probe 连接已设角色级 search_path=agp,public】之后（见
    _pg_bootstrap_schema），表自动落 agp schema。逐语句执行（asyncpg 单连接），
    单条失败仅告警不阻断（与 1.4.0 settings/列补齐的降级语义一致）。
    """
    import logging
    log = logging.getLogger("core.db")
    pool = await connect()
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_pg.sql")
    with open(schema_path, encoding="utf-8") as f:
        script = f.read()
    # 去掉整行注释/空行；本 schema 默认值不含分号，按顶层 ';' 切分成独立语句，
    # 逐条执行（asyncpg 单连接；避免多语句协议边界问题）。
    lines = [l.strip() for l in script.splitlines()]
    lines = [l for l in lines if l and not l.startswith("--")]
    clean = "\n".join(lines)
    parts = [p.strip() for p in clean.split(";") if p.strip()]
    async with pool.acquire() as c:
        await c.execute("SELECT 1")
        failed = []
        for ddl in parts:
            try:
                await c.execute(ddl)
            except Exception as e:
                failed.append((ddl[:60], type(e).__name__, str(e)[:120]))
        if failed:
            for ddl_head, tname, msg in failed:
                log.warning("schema_pg.sql 建表/补列失败（降级）: %s: %s %s", tname, ddl_head, msg)
        else:
            log.info("AGP PG schema 自举完成（schema_pg.sql, %d 条语句，fresh PG 可用）", len(parts))


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
    """单例 aiosqlite 连接（D7: 单连接 + check_same_thread=False）。

    TASK-046 fail-fast：数据目录不存在则创建；创建失败 / 目录不可写（如
    bind 卷宿主属主非容器用户）→ 明确报错退出（日志指明目录与权限），
    不让 sqlite 以含糊的 "unable to open database file" 卡死或静默降级。
    """
    global _SQLITE_CONN
    if _SQLITE_CONN is None:
        import aiosqlite
        path = S.effective_sqlite_path
        d = os.path.dirname(path) or "."
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            raise RuntimeError(
                f"AGP DB fail-fast: sqlite 数据目录无法创建 {d!r}: {e} —— "
                "检查该目录所在卷的挂载与权限")
        if not os.access(d, os.W_OK):
            raise RuntimeError(
                f"AGP DB fail-fast: sqlite 数据目录不可写 {d!r}（当前用户 "
                f"uid={os.getuid() if hasattr(os, 'getuid') else 'n/a'}）—— "
                f"目标文件 {path!r}。若目录来自 bind 卷且宿主属主非 uid 1000，"
                "请在宿主执行 chown 1000:1000 <目录>（deploy 脚本已含此防御）")
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        # D7: 单连接被 FastAPI 多协程共享，aiosqlite 内部线程池执行需放开线程限制
        await conn.execute("PRAGMA journal_mode=WAL")
        # 落盘写自检（WAL 已建；写一条回滚的探测事务，验证可写性）
        try:
            await conn.execute("BEGIN")
            await conn.execute("CREATE TABLE IF NOT EXISTS _agp_write_probe (x INTEGER)")
            await conn.execute("INSERT INTO _agp_write_probe VALUES (1)")
            await conn.execute("DELETE FROM _agp_write_probe")
            await conn.commit()
        except Exception:
            await conn.close()
            raise RuntimeError(
                f"AGP DB fail-fast: sqlite 数据文件不可写 {path!r}（目录 {d!r}）—— "
                "检查卷挂载权限与磁盘空间")
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
    # TASK-022 / F1: 旧库在线补齐列（schema_sqlite.sql 的 CREATE IF NOT EXISTS
    # 不会给已存在的表加列）：skills.updated_at + mcp_servers.env（JSON 文本，
    # 与 PG 侧 JSONB 在 API 层统一反序列化，双后端行为一致）。
    for ddl in (
        "ALTER TABLE skills ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))",
        "ALTER TABLE mcp_servers ADD COLUMN env TEXT DEFAULT '{}'",
        # TASK-037: agents 后端字段（custom|hermes）+ hermes profile 名。
        # backend NOT NULL DEFAULT 'custom'：旧行自动落 custom（内置引擎），零感知。
        "ALTER TABLE agents ADD COLUMN backend TEXT NOT NULL DEFAULT 'custom'",
        "ALTER TABLE agents ADD COLUMN hermes_profile TEXT",
    ):
        try:
            await conn.execute(ddl)
            await conn.commit()
        except Exception as e:
            if "duplicate column" in str(e).lower():
                pass  # 列已存在（幂等）
            else:
                import logging
                logging.getLogger("core.db").warning("sqlite 列补齐失败（降级）: %s: %s", ddl, e)
                await conn.rollback()


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
