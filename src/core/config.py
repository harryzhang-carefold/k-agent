"""Configuration: all secrets/keys read from .env only (DECISION-004). Never hardcode."""
import os
import secrets as _secrets

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """Minimal .env loader (no external dep). Values must NOT be logged."""
    path = os.path.join(_SRC_DIR, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv()


def _get(name, default=""):
    return os.environ.get(name, default)


class Settings:
    # LLM provider (OpenAI-compatible)
    LLM_BASE_URL = _get("LLM_BASE_URL", "http://34.121.9.233:4000/v1")
    LLM_MODEL = _get("LLM_MODEL", "vllm-qwen3.8-27b")
    LLM_API_KEY = _get("AI_MODEL_API_KEY", "")
    LLM_TIMEOUT = float(_get("LLM_TIMEOUT", "90"))
    LLM_RETRIES = int(_get("LLM_RETRIES", "3"))

    # Embedding: OpenAI-compatible slot (may be empty -> local hash fallback, DECISION-003)
    EMBEDDING_BASE_URL = _get("EMBEDDING_BASE_URL", "")
    EMBEDDING_MODEL = _get("EMBEDDING_MODEL", "")
    EMBEDDING_API_KEY = _get("EMBEDDING_API_KEY", "")
    EMBEDDING_DIM = int(_get("EMBEDDING_DIM", "512"))

    # Cache routing (module 14)
    SEMANTIC_CACHE_THRESHOLD = float(_get("SEMANTIC_CACHE_THRESHOLD", "0.95"))

    # Memory plugin (module 12): local | redis | milvus | neo4j | hermes
    MEMORY_BACKEND = _get("MEMORY_BACKEND", "local")
    HERMES_MEMORY_DIR = _get("HERMES_MEMORY_DIR", os.path.join(_SRC_DIR, "..", "..", "05-temp", "hermes_memory.jsonl"))

    # RBAC
    JWT_SECRET = _get("JWT_SECRET", "")
    JWT_TTL_HOURS = int(_get("JWT_TTL_HOURS", "24"))
    SEED_PASSWORD = _get("SEED_PASSWORD", "admin123")

    # Server
    HOST = _get("HOST", "0.0.0.0")
    PORT = int(_get("PORT", "8099"))
    DATA_DIR = os.path.join(_SRC_DIR, "data")
    STATIC_DIR = os.path.join(_SRC_DIR, "static")
    MAX_TOOL_ROUNDS = int(_get("MAX_TOOL_ROUNDS", "5"))

    # 数据库后端（TASK-015 双后端：默认 sqlite 零依赖，postgres 可配置）
    # DB_BACKEND: sqlite | postgres（默认 sqlite；纯 .env 切换，业务代码零感知）
    DB_BACKEND = _get("DB_BACKEND", "sqlite").lower()
    # SQLite 数据文件：默认 <src>/data/agp.db（容器内 compose 注入 /app/data/agp.db，卷持久化）
    SQLITE_PATH = _get("SQLITE_PATH", os.path.join(_SRC_DIR, "data", "agp.db"))

    # PostgreSQL（仅 DB_BACKEND=postgres 时需要；密码走 env，严禁硬编码，DECISION-004）
    DB_HOST = _get("DB_HOST", "pg-unified")
    DB_PORT = int(_get("DB_PORT", "5432"))
    DB_NAME = _get("DB_NAME", "postgres")
    DB_USER = _get("DB_USER", "agp_user")
    DB_PASSWORD = _get("AGP_DB_PASSWORD", "")
    DB_SCHEMA = _get("DB_SCHEMA", "agp")
    # 容器内直连 pg-unified:5432（双网络 agp_default）；本地调试可 DB_HOST=127.0.0.1
    # TASK-046: DB_DSN 为空（未配置或 deploy 脚本置空 DB_DSN= 防旧值生效）时，
    # 回退到 DB_HOST/DB_USER/AGP_DB_PASSWORD 组件构造——os.environ.get 对
    # "DB_DSN="（空值）返回 "" 而非 fallback，必须显式判空。
    DSN = (_get("DB_DSN", "")
           or f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}")

    @property
    def effective_sqlite_path(self) -> str:
        """sqlite 数据文件绝对路径（相对路径按进程 cwd 解析）。"""
        p = self.SQLITE_PATH
        return p if os.path.isabs(p) else os.path.abspath(p)

    def require_dsn(self) -> str:
        """postgres 模式 DSN 校验（D1: 仅 postgres 模式必需）。"""
        if not self.DSN:
            raise RuntimeError(
                "DB_BACKEND=postgres 时必须配置 DSN（DB_DSN 或 DB_HOST/DB_USER/AGP_DB_PASSWORD 等）")
        return self.DSN


def _jwt_secret() -> str:
    """BUG-002: JWT HS256 key 必须 >= 32 字节（pyjwt 对短 HMAC key 报
    InsecureKeyLengthWarning）。.env 未配置或过短时，运行时生成 64 hex 随机值
    （仅进程内存有效，重启换密钥——开发环境可接受，run.sh 会持久化进 .env）；
    生产环境必须显式配置随机 JWT_SECRET（DESIGN.md 第 8 节）。严禁硬编码。"""
    v = Settings.JWT_SECRET
    if len(v.encode("utf-8")) >= 32:
        return v
    return _secrets.token_hex(32)  # 64 hex chars = 32 bytes


S = Settings()
S.JWT_SECRET = _jwt_secret()
