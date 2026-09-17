#!/usr/bin/env bash
# ============================================================================
# gcp_deploy.sh — AGP GCP 宿主部署脚本（TASK-046 / 1.5.0）
#
# 在 GCP VM 宿主上执行（由 deploy.yml 经 SSH 调用）：
#   1. 代码同步（git reset --hard origin/main）
#   2. 写入 ENV_FILE（GitHub secrets，内容不可见——只按 key 读取）
#   3. DB 决策（用户拍板策略，不许改策略）：
#        - 无 DB_BACKEND 或 =sqlite → sqlite（零外部依赖），跳过 DB 探测
#        - =postgres → 探测 → 复用 → 自建（幂等）
#            a. 探测：agp-pg（此前自建）/ 其他 postgres 容器 / 127.0.0.1:5432
#            b. 可复用（连通 + 凭据正确 + 库存在）→ 复用，DSN 写 .env
#            c. 复用不了/不存在 → 自建 agp-pg（postgres:16-alpine，数据卷持久化，
#               固定用户/密码，密码来源：ENV_FILE 有则用、无则生成并持久化到
#               $DEPLOY_DIR/.pg_credentials chmod 600；等 pg_isready 就绪）
#   4. 数据卷属主防御（候选根因 R2）：mkdir -p src/data && chown 1000:1000
#      （无 chown 权限则 chmod 777 兜底 + warning）
#   5. docker compose down/up -d --build（PG 模式叠加临时网络文件）
#   6. 部署后健康检查（≤120s 轮询 /healthz）+ 失败诊断输出
#      （docker logs --tail 100 + docker ps -a + restart 次数 → exit 1）
#      —— 让 CI 日志成为第一诊断现场，杜绝"部署成功但应用挂死"（GCP 盲点）
#
# 用法:
#   ENV_FILE_CONTENT='<多行>' DEPLOY_DIR=/path gcp_deploy.sh
# 环境变量（全部可覆盖，本地测试用）:
#   ENV_FILE_CONTENT   必填：.env 内容（来自 GitHub secret ENV_FILE）
#   DEPLOY_DIR         默认 /home/partners/app/k-agent
#   REPO               默认 https://github.com/harryzhang-carefold/k-agent.git
#   APP_CONTAINER      默认 agp-app
#   HEALTH_URL         默认 http://127.0.0.1:8099/healthz
#
# 铁律（RISK-015）：只操作 container_name=$APP_CONTAINER 与 compose project=agp
# 的容器，绝不触碰 pg-unified / gw-nginx 等别的项目容器。
# 安全：密码/密钥只进 .env（宿主文件）与 docker -e（不落日志），任何回显都脱敏。
# ============================================================================
set -uo pipefail
export TZ=Asia/Shanghai

DEPLOY_DIR="${DEPLOY_DIR:-/home/partners/app/k-agent}"
REPO="${REPO:-https://github.com/harryzhang-carefold/k-agent.git}"
ENV_FILE_PATH="$DEPLOY_DIR/src/.env"
APP_CONTAINER="${APP_CONTAINER:-agp-app}"
PG_CONTAINER="agp-pg"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8099/healthz}"
PROBE="$DEPLOY_DIR/scripts/pg_probe.py"
CREDS_FILE="$DEPLOY_DIR/.pg_credentials"

log() { echo "[deploy] $*"; }

# ---------------------------------------------------------------------------
# 1. 代码同步
# ---------------------------------------------------------------------------
if [ ! -d "$DEPLOY_DIR/.git" ]; then
  log "首次部署：clone $REPO -> $DEPLOY_DIR"
  git clone "$REPO" "$DEPLOY_DIR"
fi
cd "$DEPLOY_DIR"
log "同步 origin/main ..."
git fetch origin main
git reset --hard origin/main

# ---------------------------------------------------------------------------
# 2. 写入 ENV_FILE（内容不可见，只按 key 读取；兼容 CRLF/注释/大小写/空值）
# ---------------------------------------------------------------------------
printf '%s' "${ENV_FILE_CONTENT:-}" > "$ENV_FILE_PATH"
if head -c 100 "$ENV_FILE_PATH" | grep -q $'\r'; then
  tr -d '\r' < "$ENV_FILE_PATH" > "$ENV_FILE_PATH.tmp" && mv "$ENV_FILE_PATH.tmp" "$ENV_FILE_PATH"
fi
[ -s "$ENV_FILE_PATH" ] || { log "✗ ENV_FILE 为空"; exit 1; }
log "ENV_FILE 已写入 $ENV_FILE_PATH（内容不回显）"

# env_get KEY：取首个"非注释"的 K=V 行的值（raw，不 trim，密码可含空格）
env_get() {
  awk -v k="$1" '
    { line=$0; sub(/\r$/,"",line) }
    line ~ /^[ \t]*#/ { next }
    {
      eq=index(line,"=")
      if (eq>0) {
        key=substr(line,1,eq-1); gsub(/^[ \t]+|[ \t]+$/,"",key)
        if (key==k) { print substr(line,eq+1); exit }
      }
    }' "$ENV_FILE_PATH"
}
# env_set KEY VALUE：替换首个非注释行；不存在则追加
env_set() {
  awk -v k="$1" -v v="$2" '
    !done && !/^[ \t]*#/ {
      eq=index($0,"=")
      if (eq>0) {
        key=substr($0,1,eq-1); gsub(/^[ \t]+|[ \t]+$/,"",key)
        if (key==k) { printf "%s=%s\n",k,v; done=1; next }
      }
    }
    { print }
    END { if (!done) printf "%s=%s\n",k,v }
  ' "$ENV_FILE_PATH" > "$ENV_FILE_PATH.tmp" && mv "$ENV_FILE_PATH.tmp" "$ENV_FILE_PATH"
}

# ---------------------------------------------------------------------------
# 3. DB 决策
# ---------------------------------------------------------------------------
DB_BACKEND_RAW="$(env_get DB_BACKEND)"
DB_BACKEND="$(printf '%s' "$DB_BACKEND_RAW" | tr '[:upper:]' '[:lower:]' | sed 's/^[ \t]*//;s/[ \t]*$//')"
log "ENV_FILE DB_BACKEND=[$DB_BACKEND]"

DB_PORT_VAL="$(env_get DB_PORT)";  [ -z "$DB_PORT_VAL" ] && DB_PORT_VAL=5432
DB_NAME_VAL="$(env_get DB_NAME)";  [ -z "$DB_NAME_VAL" ] && DB_NAME_VAL=postgres
DB_USER_VAL="$(env_get DB_USER)";  [ -z "$DB_USER_VAL" ] && DB_USER_VAL=agp_user
DB_PASS_VAL="$(env_get AGP_DB_PASSWORD)"
DB_SCHEMA_VAL="$(env_get DB_SCHEMA)"; [ -z "$DB_SCHEMA_VAL" ] && DB_SCHEMA_VAL=agp

PG_CRED_HOST=""   # 非空 = PG 模式（agp-pg / container:X / localhost）
if [ -z "$DB_BACKEND" ] || [ "$DB_BACKEND" = "sqlite" ]; then
  env_set DB_BACKEND sqlite
  log "→ sqlite 模式（零外部依赖，clone 即跑），跳过 DB 探测"
else
  if [ "$DB_BACKEND" != "postgres" ]; then
    log "✗ ENV_FILE DB_BACKEND=[$DB_BACKEND] 非法（仅 sqlite|postgres），部署中止"
    exit 1
  fi
  log "→ postgres 模式：探测 → 复用 → 自建（幂等）"

  # probe HOST PORT USER PASS DB —— 直连 IP/host 探测
  probe() { python3 "$PROBE" "$@"; }
  # probe_container NAME PORT USER PASS DB —— 把容器名解析为其网络 IP 再探测
  #   （Docker 宿主无法按容器名 DNS 解析，必须用容器 IP；宿主可路由到容器 IP）
  probe_container() {
    local name=$1; shift
    local ip
    ip="$(container_ip "$name")"
    if [ -z "$ip" ]; then
      echo "{\"ok\": false, \"reachable\": false, \"db_exists\": false, \"login_ok\": false, \"agp_schema_exists\": false, \"error\": \"no ip for container $name\"}"
      return
    fi
    probe "$ip" "$@"
  }
  container_ip() {
    docker inspect "$1" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' 2>/dev/null | awk '{print $1}'
  }
  # reuse_decision RES —— 按 pg_probe 输出判定复用档位：
  #   ok      = 连通+认证+库存在（直接复用）
  #   login   = 连通+认证通过但目标库缺失（可登录凭据 → 策略 b：建库+schema 后复用）
  #   ""      = 不可复用（不可达/认证失败）
  reuse_decision() {
    local res=$1
    if echo "$res" | grep -q '"ok": true'; then echo "ok"; return; fi
    if echo "$res" | grep -q '"login_ok": true'; then echo "login"; return; fi
    echo ""
  }
  # ensure_db HOST PORT USER PASS DB SCHEMA —— 目标库缺失时建库+建 agp schema。
  # 返回 0=库已存在或建库成功；1=建库失败（无权限）→ 该 PG 不可复用。
  # 密码经 -e 注入探测容器，不落 argv/日志。
  ensure_db() {
    local host=$1 port=$2 user=$3 pass=$4 db=$5 schema=$6
    docker run --rm --network host \
      -e "PGPASSWORD=$pass" -e "PGCONNECTTIMEOUT=6" \
      postgres:16-alpine psql -h "$host" -p "$port" -U "$user" -d "$db" \
      -tAc "SELECT 1" >/dev/null 2>&1 && return 0
    # 库不存在（或无权限）→ 尝试建库
    docker run --rm --network host \
      -e "PGPASSWORD=$pass" -e "PGCONNECTTIMEOUT=6" \
      postgres:16-alpine psql -h "$host" -p "$port" -U "$user" -d postgres \
      -tAc "CREATE DATABASE \"$db\"" >/dev/null 2>&1 || return 1
    docker run --rm --network host \
      -e "PGPASSWORD=$pass" -e "PGCONNECTTIMEOUT=6" \
      postgres:16-alpine psql -h "$host" -p "$port" -U "$user" -d "$db" \
      -c "CREATE SCHEMA IF NOT EXISTS \"$schema\"" >/dev/null 2>&1
  }

  decide_ds=""   # 最终 DSN host（容器名 / 宿主内网 IP）

  # ---- a(1). agp-pg（此前自建，凭据在 CREDS_FILE）----
  AGP_PG_STATE="absent"
  if docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    AGP_PG_STATE="$(docker inspect -f '{{.State.Status}}' "$PG_CONTAINER")"
  fi
  AGP_PG_USER="" AGP_PG_PASS=""
  if [ -f "$CREDS_FILE" ]; then
    AGP_PG_USER="$(sed -n '1p' "$CREDS_FILE" | cut -d= -f2-)"
    AGP_PG_PASS="$(sed -n '2p' "$CREDS_FILE" | cut -d= -f2-)"
  fi
  case "$AGP_PG_STATE" in
    exited|created)
      log "agp-pg 存在但状态=$AGP_PG_STATE，尝试 docker start ..."
      docker start "$PG_CONTAINER" >/dev/null 2>&1 || true
      AGP_PG_STATE="$(docker inspect -f '{{.State.Status}}' "$PG_CONTAINER" 2>/dev/null || echo unknown)"
      ;;
  esac
  if [ "$AGP_PG_STATE" = "running" ] && [ -n "$AGP_PG_PASS" ]; then
    RES="$(probe_container "$PG_CONTAINER" "$DB_PORT_VAL" "$AGP_PG_USER" "$AGP_PG_PASS" "$DB_NAME_VAL")"
    log "探测 agp-pg: $RES"
    case "$(reuse_decision "$RES")" in
      ok)
        decide_ds="$PG_CONTAINER"; PG_CRED_HOST="agp-pg"
        log "✓ 复用已自建容器 agp-pg（幂等，不重建）"
        ;;
      login)
        if ensure_db "$(container_ip "$PG_CONTAINER")" "$DB_PORT_VAL" "$AGP_PG_USER" "$AGP_PG_PASS" "$DB_NAME_VAL" "$DB_SCHEMA_VAL"; then
          decide_ds="$PG_CONTAINER"; PG_CRED_HOST="agp-pg"
          log "✓ 复用 agp-pg（目标库缺失 → 已建库+$DB_SCHEMA_VAL schema）"
        else
          log "⚠ agp-pg 凭据可登录但无建库权限，转探测其他 PG"
        fi
        ;;
    esac
  fi

  # ---- a(2). 其他 postgres 容器（凭据取 ENV_FILE）----
  if [ -z "$decide_ds" ]; then
    for c in $(docker ps --format '{{.Names}}' | grep -iE 'postgres|-pg|pg-|^pg$'); do
      [ "$c" = "$PG_CONTAINER" ] && continue
      log "探测容器 $c ..."
      RES="$(probe_container "$c" "$DB_PORT_VAL" "$DB_USER_VAL" "$DB_PASS_VAL" "$DB_NAME_VAL")"
      log "  $c probe: $RES"
      case "$(reuse_decision "$RES")" in
        ok)
          decide_ds="$c"; PG_CRED_HOST="container:$c"
          log "✓ 复用容器 $c（凭据来自 ENV_FILE）"
          break
          ;;
        login)
          if ensure_db "$(container_ip "$c")" "$DB_PORT_VAL" "$DB_USER_VAL" "$DB_PASS_VAL" "$DB_NAME_VAL" "$DB_SCHEMA_VAL"; then
            decide_ds="$c"; PG_CRED_HOST="container:$c"
            log "✓ 复用容器 $c（目标库缺失 → 已建库+$DB_SCHEMA_VAL schema）"
            break
          else
            log "⚠ 容器 $c 凭据可登录但无建库权限"
          fi
          ;;
      esac
    done
  fi

  # ---- a(3). 127.0.0.1:5432（宿主本机 PG，凭据取 ENV_FILE）----
  if [ -z "$decide_ds" ]; then
    log "探测 127.0.0.1:$DB_PORT_VAL ..."
    RES="$(probe "127.0.0.1" "$DB_PORT_VAL" "$DB_USER_VAL" "$DB_PASS_VAL" "$DB_NAME_VAL")"
    log "  127.0.0.1 probe: $RES"
    case "$(reuse_decision "$RES")" in
      ok)
        decide_ds="127.0.0.1"; PG_CRED_HOST="localhost"
        log "✓ 复用 127.0.0.1:$DB_PORT_VAL（凭据来自 ENV_FILE）"
        ;;
      login)
        if ensure_db "127.0.0.1" "$DB_PORT_VAL" "$DB_USER_VAL" "$DB_PASS_VAL" "$DB_NAME_VAL" "$DB_SCHEMA_VAL"; then
          decide_ds="127.0.0.1"; PG_CRED_HOST="localhost"
          log "✓ 复用 127.0.0.1:$DB_PORT_VAL（目标库缺失 → 已建库+$DB_SCHEMA_VAL schema）"
        else
          log "⚠ 127.0.0.1 PG 凭据可登录但无建库权限"
        fi
        ;;
    esac
  fi

  # ---- b/c. 复用不了 → 自建 agp-pg（幂等：已存在则上面已尝试复用）----
  if [ -z "$decide_ds" ]; then
    log "无可用 PG → 自建 $PG_CONTAINER（postgres:16-alpine，数据卷持久化）"
    if [ -z "$DB_PASS_VAL" ]; then
      DB_PASS_VAL="$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
      log "ENV_FILE 无密码 → 已生成随机密码"
    fi
    { echo "agp_user=$DB_USER_VAL"; echo "password=$DB_PASS_VAL"; } > "$CREDS_FILE"
    chmod 600 "$CREDS_FILE"
    log "凭据已持久化到 $CREDS_FILE (chmod 600)"
    docker run -d --name "$PG_CONTAINER" \
      --restart unless-stopped \
      -e POSTGRES_USER="$DB_USER_VAL" \
      -e POSTGRES_PASSWORD="$DB_PASS_VAL" \
      -e POSTGRES_DB="$DB_NAME_VAL" \
      -v "$DEPLOY_DIR/pgdata:/var/lib/postgresql/data" \
      postgres:16-alpine >/dev/null
    log "等待 $PG_CONTAINER 就绪（pg_isready ≤120s）..."
    ready=0
    for _ in $(seq 1 60); do
      if docker exec "$PG_CONTAINER" pg_isready -h 127.0.0.1 -p 5432 -U "$DB_USER_VAL" >/dev/null 2>&1; then
        ready=1; break
      fi
      sleep 2
    done
    if [ "$ready" != "1" ]; then
      log "✗ $PG_CONTAINER 未就绪，诊断："
      docker logs --tail 50 "$PG_CONTAINER" 2>&1 || true
      docker ps -a --filter "name=$PG_CONTAINER" || true
      exit 1
    fi
    # 首启确保 agp schema 存在（app 的 CREATE TABLE 不会自动建 schema）
    docker exec "$PG_CONTAINER" psql -h 127.0.0.1 -U "$DB_USER_VAL" -d "$DB_NAME_VAL" \
      -c "CREATE SCHEMA IF NOT EXISTS $DB_SCHEMA_VAL" >/dev/null 2>&1 \
      && log "✓ agp schema 已就绪" \
      || log "⚠ agp schema 创建失败（若 DB_USER 无权限将导致建表失败）"
    decide_ds="$PG_CONTAINER"; PG_CRED_HOST="agp-pg(created)"
    log "✓ $PG_CONTAINER 就绪"
  fi

  # ---- 有效 DSN 写回 .env（覆盖 ENV_FILE 旧值；DB_DSN 置空避免旧 DSN 生效）----
  # 容器方案（BUG-009 修复，TASK-049）：DB_HOST 写"容器名"而非容器 IP。
  # 旧版写 container_ip()（Go map 乱序取首项网络 IP）有两个致命问题：
  #   1) 抓 IP 发生在 docker network connect agp_default 之前，自建场景容器
  #      只有 bridge 网 → DSN 写 bridge IP（172.17.0.x），app 在 agp_default
  #      连 bridge IP 不可达 → fail-fast 重启循环（GCP 8099 部署后依旧不通）；
  #   2) 容器多网络时 map 乱序取首项，取到哪个 IP 随机（flaky）。
  # 现改为：先把 PG 容器加入 agp_default（下方 case 块），app 经 compose 叠加
  # 也在 agp_default → Docker 内置 DNS 按容器名解析，跨重连/重启稳定，无乱序。
  # 127.0.0.1 复用分支保持宿主内网 IP 路径（app 不在宿主 netns，不能用 127.0.0.1）。
  if ! docker network inspect agp_default >/dev/null 2>&1; then
    docker network create agp_default >/dev/null
  fi
  case "$PG_CRED_HOST" in
    agp-pg|agp-pg\(created\)|container:*)
      docker network connect agp_default "$decide_ds" 2>/dev/null || true
      log "PG 容器 $decide_ds 已加入 agp_default（app 按容器名解析）"
      ;;
    localhost)
      log "127.0.0.1 复用：app 经 bridge 直连宿主内网 IP（需该 PG 在宿主 5432 监听）"
      ;;
  esac

  EFF_HOST="$decide_ds"
  if [ "$EFF_HOST" = "127.0.0.1" ]; then
    HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [ -n "$HOST_IP" ]; then
      EFF_HOST="$HOST_IP"
      log "127.0.0.1 复用 → app 容器改用宿主内网 IP $EFF_HOST（app 不在宿主 netns）"
    fi
  fi
  env_set DB_HOST "$EFF_HOST"
  env_set DB_PORT "$DB_PORT_VAL"
  env_set DB_NAME "$DB_NAME_VAL"
  env_set DB_USER "$DB_USER_VAL"
  env_set AGP_DB_PASSWORD "$DB_PASS_VAL"
  env_set DB_SCHEMA "$DB_SCHEMA_VAL"
  env_set DB_DSN ""
  log "DSN 已写入 .env: host=$EFF_HOST port=$DB_PORT_VAL user=$DB_USER_VAL db=$DB_NAME_VAL schema=$DB_SCHEMA_VAL（密码不回显）"
fi

# ---------------------------------------------------------------------------
# 4. 数据卷属主防御（候选根因 R2：容器 uid=1000 vs GCP 宿主用户 uid 未知）
# ---------------------------------------------------------------------------
mkdir -p "$DEPLOY_DIR/src/data"
if chown 1000:1000 "$DEPLOY_DIR/src/data" 2>/dev/null; then
  log "✓ src/data 属主 = 1000:1000"
else
  log "⚠ WARNING: 无 chown 权限（非 root），chmod 777 兜底（src/data）"
  chmod 777 "$DEPLOY_DIR/src/data" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 5. compose 启动（PG 模式叠加临时网络文件，不改动仓库 docker-compose.yml）
# ---------------------------------------------------------------------------
COMPOSE_FILES=(-f docker-compose.yml)
if [ -n "$PG_CRED_HOST" ]; then
  cat > "$DEPLOY_DIR/.compose.agp-net.yml" <<'NET_EOF'
services:
  app:
    networks:
      - agp_default
networks:
  agp_default:
    external: true
NET_EOF
  COMPOSE_FILES+=(-f .compose.agp-net.yml)
fi

# 端口占用预检（避免与本机既有服务冲突；GCP 上 8099 即本服务）
PORT="${HEALTH_URL##*:}"; PORT="${PORT%%/*}"
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  log "⚠ 端口 $PORT 已被占用（将被 compose down 释放的本服务容器或外部进程）"
fi

log "docker compose down --remove-orphans && up -d --build ..."
docker compose "${COMPOSE_FILES[@]}" down --remove-orphans >/dev/null 2>&1 || true
docker compose "${COMPOSE_FILES[@]}" up -d --build

# ---------------------------------------------------------------------------
# 6. 部署后健康检查 + 失败诊断（GCP 盲点修复：CI 日志=第一诊断现场）
# ---------------------------------------------------------------------------
ok=0
for _ in $(seq 1 120); do
  if curl -fsS "$HEALTH_URL" 2>/dev/null | grep -q '"status": *"ok"'; then
    ok=1; break
  fi
  sleep 1
done
if [ "$ok" = "1" ]; then
  log "✓ 健康检查通过: $HEALTH_URL"
  curl -fsS "$HEALTH_URL"; echo
  log "容器状态:"
  docker ps --filter "name=$APP_CONTAINER" --filter "name=$PG_CONTAINER" \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
  log "数据库后端:"
  curl -fsS "$HEALTH_URL" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("db"))' 2>/dev/null || true
  log "✓ 部署完成"
else
  log "✗ 健康检查失败（120s 内 $HEALTH_URL 未就绪）——诊断现场如下："
  echo "===================== docker logs $APP_CONTAINER --tail 100 ====================="
  docker logs --tail 100 "$APP_CONTAINER" 2>&1 || echo "(无日志——容器可能未创建)"
  echo "===================== docker ps -a（agp 相关）====================="
  docker ps -a --filter "name=$APP_CONTAINER" --filter "name=$PG_CONTAINER"
  echo "===================== 容器状态 / restart 次数 ====================="
  for c in "$APP_CONTAINER" "$PG_CONTAINER"; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
      docker inspect -f "$c: status={{.State.Status}} restarts={{.RestartCount}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}" "$c"
    fi
  done
  echo "===================== /healthz 最后一次响应 ====================="
  curl -sS --max-time 5 "$HEALTH_URL" || echo "(无响应)"
  echo
  log "✗ 部署失败：应用未就绪（上方含真实错误，供排查）"
  exit 1
fi
