#!/usr/bin/env bash
# AI Agent Platform 一键启动（端口 8099 + 健康检查）
# 用法:
#   ./run.sh          起服务（venv/.env 缺失时自动创建），前台阻塞，Ctrl-C 停止
#   ./run.sh --check  只对已运行服务做健康检查
# 布局（DECISION-004: uv venv + env 密钥，不硬编码）:
#   02-development/
#   ├── .venv/            # uv 建 venv（本机）
#   ├── run.sh            # 本脚本
#   └── src/
#       ├── core/ ...     # 后端代码（import core.app）
#       ├── requirements.txt
#       ├── .env / .env.example
#       └── data/agp.db   # SQLite（首次启动自动建 + 种子）
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"     # 02-development
CODE="$SRC/src"                          # 代码目录（core.app 在此 import）
PORT="${PORT:-8099}"
UV="${UV:-$HOME/.hermes/bin/uv}"
PY="$SRC/.venv/bin/python"
UVICORN="$SRC/.venv/bin/uvicorn"

echo "[run.sh] AI Agent Platform — 端口 $PORT"
echo "[run.sh] 代码目录: $CODE"

# 0. --check：纯只读健康检查，不做任何 venv/清进程动作
if [ "${1:-}" = "--check" ]; then
  echo "[run.sh] 健康检查 ..."
  curl -fsS "http://localhost:$PORT/healthz" && echo && exit 0
fi

# 1. venv（uv 建，DECISION-004）
if [ ! -x "$UVICORN" ]; then
  echo "[run.sh] 创建 venv + 安装依赖 ..."
  "$UV" venv "$SRC/.venv" --python 3.13
  "$UV" pip install --python "$PY" -r "$CODE/requirements.txt"
fi

# 2. .env（缺失则从 .env.example 生成；key 从成员 env 注入，不硬编码）
if [ ! -f "$CODE/.env" ]; then
  echo "[run.sh] 生成 .env ..."
  cp "$CODE/.env.example" "$CODE/.env"
  MEM_ENV="$HOME/.hermes/profiles/zhangbeihai/.env"
  if [ -f "$MEM_ENV" ] && grep -q "AI_MODEL_API_KEY=" "$MEM_ENV"; then
    KEY=$(grep ^AI_MODEL_API_KEY= "$MEM_ENV" | head -1 | cut -d= -f2-)
    if [ -n "$KEY" ]; then
      sed -i.bak "s|^AI_MODEL_API_KEY=.*|AI_MODEL_API_KEY=$KEY" "$CODE/.env"
      rm -f "$CODE/.env.bak"
      echo "[run.sh] 已从成员 env 注入 LLM API key（仅本地 .env，不入文档/日志）"
    fi
  fi
fi

# 2b. BUG-002: 确保 JWT_SECRET >= 32 字节（短/空则生成 64 hex 随机值并持久化到 .env，
#     避免每次重启换密钥、避免 InsecureKeyLengthWarning）。只改 .env，不硬编码、不落日志。
JWT_LEN=$(awk -F= '/^JWT_SECRET=/{print length($2); exit}' "$CODE/.env")
if [ -z "$JWT_LEN" ] || [ "$JWT_LEN" -lt 32 ]; then
  NEWJWT=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  python3 - "$CODE/.env" "$NEWJWT" <<'PYEOF'
import sys
path, new = sys.argv[1], sys.argv[2]
lines = []
for line in open(path, encoding="utf-8"):
    if line.startswith("JWT_SECRET="):
        lines.append("JWT_SECRET=" + new + "\n")
    else:
        lines.append(line)
open(path, "w", encoding="utf-8").writelines(lines)
PYEOF
  echo "[run.sh] JWT_SECRET 过短/缺失，已生成并写入 .env（64 hex，仅本地）"
fi

# 3. 清旧进程（只杀本服务占用的 8099）
OLD_PID="$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [ -n "${OLD_PID:-}" ]; then
  echo "[run.sh] 清理旧进程 pid=$OLD_PID"
  kill "$OLD_PID" 2>/dev/null || true
  sleep 1
fi

# 4. 起服务（uvicorn 需在 src/ 下，import core.app）
echo "[run.sh] 启动 uvicorn ..."
cd "$CODE"
"$UVICORN" core.app:app --host 0.0.0.0 --port "$PORT" --log-level info &
APP_PID=$!
trap 'kill $APP_PID 2>/dev/null || true' EXIT

# 5. 健康检查（最多 30s）
for i in $(seq 1 30); do
  if curl -fsS "http://localhost:$PORT/healthz" > /dev/null 2>&1; then
    echo "[run.sh] ✓ 服务就绪: http://localhost:$PORT"
    echo "[run.sh]   UI:    http://localhost:$PORT/"
    echo "[run.sh]   健康:  http://localhost:$PORT/healthz"
    wait $APP_PID
    exit 0
  fi
  if ! kill -0 $APP_PID 2>/dev/null; then
    echo "[run.sh] ✗ 服务进程退出，请检查日志"
    exit 1
  fi
  sleep 1
done
echo "[run.sh] ✗ 健康检查超时（30s）"
kill $APP_PID 2>/dev/null || true
exit 1
