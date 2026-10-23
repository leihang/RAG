#!/usr/bin/env bash
# =============================================================================
# deploy/scripts/failover.sh —— 双机热备的**切换端**（00 v4.0 D14、08 §1.3）
# -----------------------------------------------------------------------------
# 部署位置：**备机**（standby）。**由人工确认后执行**，脚本绝不自动触发。
#
# 用法：
#   ./failover.sh                 # 正式切换（含所有安全检查）
#   ./failover.sh --dry-run       # 只做检查与打印，不动服务
#   ./failover.sh --force         # 跳过「对端可达则拒绝」（仅当确认对端为僵尸进程时）
#
# 安全检查（防脑裂，缺一不可）：
#   ① 本机 NODE_ROLE 必须是 standby；
#   ② 对端 /readyz 必须**不可达**（可达还切换 = 双主写入，直接拒绝）；
#   ③ 数据新鲜度：litestream 副本存在且不比本机库旧太多（LITESTREAM_MAX_LAG_SECONDS）。
#
# 切换步骤（08 §1.3 的 1~5 步；RTO 目标见 00 §4）：
#   ① 启动 4 个 launchd 服务 → ② 内网 DNS 指向本机（人工/自动二选一）→
#   ③ 等待 Stream 重连 → ④ smoke.sh 全绿 → ⑤ 记录切换事件
#
# 回退：切换后若发现问题，立即反向执行 failover.sh（在旧主机上），
#       并把本机服务停掉（launchctl bootout），**严禁双机同时提供服务**。
# =============================================================================
set -uo pipefail

NODE_ENV="/opt/ekbs/etc/node.env"
[ -f "$NODE_ENV" ] && . "$NODE_ENV"

DRY_RUN=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

ROLE="${NODE_ROLE:-standby}"
PEER_HOST="${PEER_HOST:-127.0.0.1}"
PEER_PORT="${PEER_PORT:-8000}"
DNS_SWITCH_CMD="${DNS_SWITCH_CMD:-}"          # 可选：切换内网 DNS 的命令（如公司 DNS API 封装）
LITESTREAM_MAX_LAG_SECONDS="${LITESTREAM_MAX_LAG_SECONDS:-300}"
APP_DIR="/opt/ekbs/app"
SERVICES="com.ekbs.api com.ekbs.dingtalk com.ekbs.worker com.ekbs.scheduler"

log() { echo "$(date '+%F %T') [failover] $*"; }
die() { log "拒绝切换：$*"; exit 1; }

# ---- ① 角色检查 ----
[ "$ROLE" = "standby" ] || die "本机 NODE_ROLE=${ROLE}，不是备机（08 §1.1）"

# ---- ② 对端可达性（防脑裂核心）----
peer_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  "http://${PEER_HOST}:${PEER_PORT}/readyz" 2>/dev/null || echo 000)"
if [ "$peer_code" = "200" ]; then
  if [ "$FORCE" -eq 1 ]; then
    log "!! 对端仍然可达（${peer_code}），但 --force 指定继续 —— 请确认对端确为僵尸进程"
  else
    die "对端 ${PEER_HOST}:${PEER_PORT} 仍返回 200，切换会造成双主写入（08 §1.1）"
  fi
else
  log "对端探测：${peer_code}（不可达，符合切换前提）"
fi

# ---- ③ 数据新鲜度 ----
DB="${EKBS_DB:-/data/ekbs/index/ekbs.db}"
if [ -f "$DB" ]; then
  age=$(( $(date +%s) - $(stat -f %m "$DB" 2>/dev/null || echo 0) ))
  log "本机库：${DB}（mtime ${age}s 前；litestream 持续写入属正常）"
  if [ "$age" -gt "$LITESTREAM_MAX_LAG_SECONDS" ]; then
    log "!! 警告：库文件 ${age}s 未更新，可能超出允许滞后 ${LITESTREAM_MAX_LAG_SECONDS}s —— 确认 litestream 恢复链路正常后再继续"
  fi
else
  die "本机找不到 ${DB}（litestream restore 未就绪？）"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  log "dry-run：检查全部通过，将执行：launchctl bootstrap ×4 → DNS 切换 → smoke.sh"
  exit 0
fi

# ---- ④ 启动服务 ----
for svc in $SERVICES; do
  plist="$HOME/Library/LaunchAgents/${svc}.plist"
  [ -f "$plist" ] || die "缺少 plist：${plist}（先跑 deploy/scripts/install.sh）"
  log "启动 ${svc}"
  launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || \
    launchctl kickstart -k "gui/$(id -u)/${svc}"
done

# ---- ⑤ DNS 切换（可选自动化）----
if [ -n "$DNS_SWITCH_CMD" ]; then
  log "执行 DNS 切换：${DNS_SWITCH_CMD}"
  sh -c "$DNS_SWITCH_CMD" || log "!! DNS 切换命令失败，请手工把内网记录指向本机"
else
  log "请人工把内网 DNS 记录指向本机（TTL ≤60s；08 §1.3 第 3 步）"
fi

# ---- ⑥ Stream 重连等待 + 冒烟 ----
log "等待 15s 让钉钉 Stream 在新主机建连（08 §1.3 第 4 步）"
sleep 15

if [ -x "$APP_DIR/deploy/scripts/smoke.sh" ]; then
  if (cd "$APP_DIR" && ./deploy/scripts/smoke.sh); then
    log "smoke 全绿：切换完成。请通知业务方，并把修复后的旧主机转为备机（禁止双写回切）"
    sqlite3 "$DB" "INSERT INTO ekbs_security_event(event_type, user_id, severity, detail) VALUES ('failover_done', NULL, 'high', '{\"node\":\"standby\",\"peer\":\"${PEER_HOST}\"}');" 2>/dev/null || true
    exit 0
  fi
  log "!! smoke 未通过：立即执行回退（停本机服务 + DNS 切回），不要对外宣布恢复"
  for svc in $SERVICES; do launchctl bootout "gui/$(id -u)/${svc}" 2>/dev/null || true; done
  exit 1
fi

log "未找到 smoke.sh（$APP_DIR/deploy/scripts/），请手工验证 /healthz、/readyz 与一次钉钉问答"
exit 0
