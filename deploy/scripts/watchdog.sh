#!/usr/bin/env bash
# =============================================================================
# deploy/scripts/watchdog.sh —— 双机热备的**检测端**（00 v4.0 D14、08 §1.1）
# -----------------------------------------------------------------------------
# 部署位置：**备机**（standby）。launchd 每 60s 跑一次（com.ekbs.watchdog.plist）。
#
# 职责边界（08 §1.1 的硬规则）：
#   本脚本**只检测 + 告警，绝不自动切换** —— 切换必须人工确认（防脑裂）。
#   主机的进程/网络抖动与真实宕机在 60s 尺度上无法可靠区分，自动切换会把
#   「一次网络抖动」放大成「双主写入」。
#
# 判定：连续 3 次（间隔 5s）探测主机 /readyz 全失败 → 写告警事件 + 经带外通道通知。
#
# 配置：/opt/ekbs/etc/node.env（install.sh 生成模板）——
#   NODE_ROLE=standby
#   PEER_HOST=10.0.0.11          # 主机的内网地址（另一台 Mac mini）
#   PEER_PORT=8000
#   EKBS_ALERT_DINGTALK_WEBHOOK=...   # 可选；邮件通道在 secrets.env 里
# 退出码：0 = 主机正常或已告警（launchd 不应重试）；非 0 仅用于人工调用时区分。
# =============================================================================
set -uo pipefail

NODE_ENV="/opt/ekbs/etc/node.env"
[ -f "$NODE_ENV" ] && . "$NODE_ENV"

ROLE="${NODE_ROLE:-standby}"
PEER_HOST="${PEER_HOST:-127.0.0.1}"
PEER_PORT="${PEER_PORT:-8000}"
PROBE_ATTEMPTS="${PROBE_ATTEMPTS:-3}"
PROBE_GAP_SECONDS="${PROBE_GAP_SECONDS:-5}"
STATE_FILE="/opt/ekbs/var/watchdog.state"
LOG_TAG="ekbs-watchdog"

log() { echo "$(date '+%F %T') [$LOG_TAG] $*"; }

# 主机角色不跑看门狗（08 §1.1：看门狗只在备机）
if [ "$ROLE" != "standby" ]; then
  log "node role=${ROLE}，非备机，退出"
  exit 0
fi

fails=0
for i in $(seq 1 "$PROBE_ATTEMPTS"); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    "http://${PEER_HOST}:${PEER_PORT}/readyz" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then
    fails=0
    break
  fi
  fails=$((fails + 1))
  log "第 ${i}/${PROBE_ATTEMPTS} 次探测 http://${PEER_HOST}:${PEER_PORT}/readyz → ${code}"
  [ "$i" -lt "$PROBE_ATTEMPTS" ] && sleep "$PROBE_GAP_SECONDS"
done

if [ "$fails" -lt "$PROBE_ATTEMPTS" ]; then
  echo "ok $(date +%s)" > "$STATE_FILE"
  exit 0
fi

# ---- 连续失败：告警（不切换）----
if [ -f "$STATE_FILE" ] && grep -q "^alerted" "$STATE_FILE" 2>/dev/null; then
  log "主机仍不可达（已告警过，不重复）"
  exit 0
fi

log "!! 主机连续 ${PROBE_ATTEMPTS} 次不可达：${PEER_HOST}:${PEER_PORT} —— 请人工确认后执行 failover.sh（禁止自动切换）"
echo "alerted $(date +%s) peer=${PEER_HOST}" > "$STATE_FILE"

# 带外通知：优先邮件（与主机出网无共享依赖），可选钉钉 webhook（08 §5.2）
if [ -n "${EKBS_ALERT_DINGTALK_WEBHOOK:-}" ]; then
  curl -s --max-time 5 -X POST "$EKBS_ALERT_DINGTALK_WEBHOOK" \
    -H 'Content-Type: application/json' \
    -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"[EKBS P0] 备机看门狗：主机 ${PEER_HOST}:${PEER_PORT} 连续 ${PROBE_ATTEMPTS} 次不可达，等待人工确认切换（08 §1.1）\"}}" \
    >/dev/null || log "钉钉告警发送失败（继续）"
fi

# 落审计：本机 SQLite 写 security_event（备机常驻 litestream restore，库可写）
if command -v sqlite3 >/dev/null 2>&1 && [ -f "${EKBS_DB:-/data/ekbs/index/ekbs.db}" ]; then
  sqlite3 "${EKBS_DB:-/data/ekbs/index/ekbs.db}" \
    "INSERT INTO ekbs_security_event(event_type, user_id, severity, detail) VALUES ('peer_unreachable', NULL, 'high', '{\"peer\":\"${PEER_HOST}:${PEER_PORT}\",\"attempts\":${PROBE_ATTEMPTS}}');" \
    2>/dev/null || log "security_event 写入失败（继续）"
fi
exit 0
