#!/usr/bin/env bash
# AGP 一键部署（T-AGP-COMPOSE-REPLACE）
#
# 一条命令完成部署：自动停止并删除已存在的 agp-app（兼容 docker run 手工创建、
# 无 compose 标签的旧容器，以及 compose 项目遗留容器，含 Stopped 状态），
# 再 docker compose up -d --build 创建启动新容器。
# 数据在卷（./src/data/agp.db）/ pg-unified 的 agp schema 中，重建不丢失。
#
# 用法（调用方自带 docker 组权限，如 sg docker -c '...'）:
#   ./deploy.sh           # auto：.env 为 DB_BACKEND=postgres 时自动加 PG 叠加，否则纯 sqlite
#   ./deploy.sh pg        # PostgreSQL 模式（叠加 docker-compose.pg.yml，经 agp_default 连 pg-unified）
#   ./deploy.sh sqlite    # 纯 SQLite 模式（零外部依赖，数据在 ./src/data/agp.db）
#
# 铁律（RISK-015）：只操作 container_name=agp-app 与 compose project=agp 的容器，
# 绝不触碰 pg-unified / gw-nginx 等别的项目容器。
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"     # 02-development
ENV_FILE="$SRC/src/.env"
CONTAINER="agp-app"
HEALTH_URL="http://localhost:8099/healthz"
HEALTH_TRIES=30
MODE="${1:-auto}"

log() { echo "[deploy] $*"; }

usage() {
  echo "用法: $0 [auto|sqlite|pg]"
  echo "  auto    默认：.env 为 DB_BACKEND=postgres 时自动启用 PG 叠加，否则纯 sqlite"
  echo "  pg      PostgreSQL 模式（叠加 docker-compose.pg.yml，需宿主 pg-unified 在 agp_default 网络）"
  echo "  sqlite  纯 SQLite 模式（零外部依赖，数据落 ./src/data/agp.db）"
  exit 2
}
case "$MODE" in
  auto | sqlite | pg) ;;
  *) usage ;;
esac

COMPOSE_FILES=("$SRC/docker-compose.yml")
PG_MODE=0
if [ "$MODE" = "pg" ]; then
  COMPOSE_FILES+=("$SRC/docker-compose.pg.yml")
  PG_MODE=1
  log "使用 PG 模式（叠加 docker-compose.pg.yml）"
elif [ "$MODE" = "auto" ]; then
  if [ -f "$ENV_FILE" ] && grep -q '^DB_BACKEND=postgres' "$ENV_FILE"; then
    COMPOSE_FILES+=("$SRC/docker-compose.pg.yml")
    PG_MODE=1
    log "auto 模式：检测到 .env DB_BACKEND=postgres，自动启用 PG 叠加（docker-compose.pg.yml）"
  else
    log "auto 模式：.env 非 postgres（或不存在），使用纯 sqlite 模式"
  fi
else
  log "使用纯 sqlite 模式"
fi

command -v docker >/dev/null 2>&1 || { log "✗ 未找到 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { log "✗ docker compose 插件不可用"; exit 1; }
if [ ! -f "$ENV_FILE" ]; then
  log "✗ 缺少 $ENV_FILE"
  log "  → 先执行: cp src/.env.example src/.env 并填写 AI_MODEL_API_KEY / JWT_SECRET(≥32字节) / SEED_PASSWORD"
  exit 1
fi

DC=(docker compose)
for f in "${COMPOSE_FILES[@]}"; do
  DC+=(-f "$f")
done

# ---- 1. PG 模式预检（只读检查 + 建网络，绝不改动别的项目容器，RISK-015） ----
if [ "$PG_MODE" = "1" ]; then
  log "PG 模式预检 ..."
  if ! docker inspect pg-unified >/dev/null 2>&1; then
    log "✗ 宿主无 pg-unified 容器，PG 模式不可用"
    log "  → 先启动共享 PostgreSQL（postgres-unified 项目），或改用: $0 sqlite"
    exit 1
  fi
  PG_NETS="$(docker inspect pg-unified --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}')"
  if docker network inspect agp_default >/dev/null 2>&1; then
    if echo "$PG_NETS" | grep -qw agp_default; then
      log "  ✓ agp_default 网络存在，pg-unified 已挂载"
    else
      log "✗ agp_default 网络存在，但 pg-unified 不在该网络上（pg-unified 实际在: ${PG_NETS:-无})"
      log "  → 手工执行（脚本不代改别的项目容器，RISK-015）:"
      log "      sg docker -c 'docker network connect agp_default pg-unified'"
      exit 1
    fi
  else
    docker network create agp_default >/dev/null
    log "  已创建 agp_default 网络（此前不存在）"
    log "✗ 但 pg-unified 不在 agp_default 上（它在: ${PG_NETS:-无}）——无法自动修复"
    log "  → 手工执行后重试（脚本不代改别的项目容器，RISK-015）:"
    log "      sg docker -c 'docker network connect agp_default pg-unified'"
    exit 1
  fi
fi

# ---- 2. 停止并删除已存在容器（同名 agp-app + compose 项目 agp 遗留，含 Stopped） ----
# 注意：不用 `docker compose ps -q`（其默认表头 "CONTAINER" 会被误当容器 ID）。
# 直接按 label + name 过滤 docker 层，天然覆盖两种来源（手工 docker run / compose 创建）。
# 两种 filter 可能对同一容器各出一条（name 过滤给名字、label 过滤给 ID），
# 统一解析为完整 ID 后 sort -u 去重，避免同一容器被 stop/rm 两遍。
TARGETS="$(
  {
    docker ps -aq --filter "label=com.docker.compose.project=agp"
    docker ps -aq --filter "name=^${CONTAINER}$"
  } | sort -u | while IFS= read -r ref; do
    [ -z "$ref" ] && continue
    docker inspect "$ref" --format '{{.Id}}' 2>/dev/null || true
  done | sort -u
)"
if [ -n "$TARGETS" ]; then
  while IFS= read -r id; do
    [ -z "$id" ] && continue
    name="$(docker inspect "$id" --format '{{.Name}}' 2>/dev/null | sed 's|^/||' || echo "$id")"
    state="$(docker inspect "$id" --format '{{.State.Status}}' 2>/dev/null || echo unknown)"
    labels="$(docker inspect "$id" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)"
    log "发现已存在容器: $name (状态=$state, compose项目=${labels:-无标签})"
    docker stop -t 10 "$id" >/dev/null 2>&1 || true
    docker rm -f "$id" >/dev/null 2>&1 || true
    log "  已停止并删除: $name"
  done <<< "$TARGETS"
else
  log "未发现已存在容器（首启路径）"
fi

# ---- 3. 构建并启动新容器 ----
log "docker compose up -d --build ..."
"${DC[@]}" up -d --build

# ---- 4. 健康检查（≤30s） ----
ok=0
for _ in $(seq 1 "$HEALTH_TRIES"); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 1
done
if [ "$ok" != "1" ]; then
  log "✗ 健康检查超时（${HEALTH_TRIES}s）：$HEALTH_URL 未就绪"
  log "  → 查看日志: sg docker -c 'docker logs --tail 80 $CONTAINER'"
  log "  → 回滚（旧镜像仍在本地）: sg docker -c 'docker run -d --name $CONTAINER --network agp_default -p 8099:8099 --env-file $ENV_FILE agp-platform:1.2.0'"
  exit 1
fi
log "✓ 健康检查通过: $HEALTH_URL"
curl -fsS "$HEALTH_URL" | head -c 400
echo

# ---- 5. 最终状态 ----
log "最终状态（docker compose ps）:"
"${DC[@]}" ps
cid="$(docker ps -q --filter "name=^${CONTAINER}$")"
log "agp-app 容器: $cid"
log "  compose 项目标签: $(docker inspect "$cid" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || echo 无)"
log "  健康状态:         $(docker inspect "$cid" --format '{{.State.Health.Status}}' 2>/dev/null || echo unknown)"
log "  数据源:           $(curl -fsS "$HEALTH_URL" | python3 -c 'import json,sys;d=json.load(sys.stdin)["db"];print(d["backend"], d.get("host","-"), d.get("schema",""))' 2>/dev/null || echo unknown)"
log "✓ 部署完成（数据在卷/PG 中，重建不丢失）"
