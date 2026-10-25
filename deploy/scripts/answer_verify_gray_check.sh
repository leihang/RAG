#!/usr/bin/env bash
# 回答核验（answer_verify）灰度巡检：只读，不改配置/不重启服务。
# 用法：bash deploy/scripts/answer_verify_gray_check.sh
set -uo pipefail

CFG="${EKBS_CONFIG:-/opt/ekbs/etc/application.yaml}"
DB="${EKBS_DB:-/data/ekbs/index/ekbs.db}"
LOG_DIR="${EKBS_LOG_DIR:-/opt/ekbs/log}"
SECRETS="${EKBS_SECRETS:-$HOME/.ekbs/secrets.env}"

echo "== 1) 配置开关 =="
if grep -q '^answer_verify:' "$CFG" 2>/dev/null; then
  grep -A14 '^answer_verify:' "$CFG" | grep -E 'enabled|model|reasoning_effort|min_score|max_score|fail_open'
else
  echo "WARN: $CFG 中没有 answer_verify 段"
fi

echo
echo "== 2) 密钥 =="
if [ -f "$SECRETS" ] && grep -q '^DEEPSEEK_API_KEY=' "$SECRETS"; then
  echo "OK: DEEPSEEK_API_KEY 已配置（权限 $(stat -f '%Lp' "$SECRETS" 2>/dev/null || echo '?')）"
else
  echo "WARN: $SECRETS 缺 DEEPSEEK_API_KEY"
fi

echo
echo "== 3) 近 24h 核验事件（审计表，只读） =="
if command -v sqlite3 >/dev/null && [ -f "$DB" ]; then
  sqlite3 "$DB" "
    SELECT event_type, COUNT(*) AS n FROM ekbs_security_event
    WHERE created_at > datetime('now','-1 day')
      AND event_type IN ('answer_verify_refused','faithfulness_refused','faithfulness_degraded','below_threshold')
    GROUP BY event_type ORDER BY n DESC;"
else
  echo "WARN: sqlite3 或 DB 不存在：$DB"
fi

echo
echo "== 4) 判官失败/超时（日志） =="
if compgen -G "$LOG_DIR/*.log" >/dev/null 2>&1; then
  echo "answer_verify_failed: $(grep -h 'answer_verify_failed' "$LOG_DIR"/*.log 2>/dev/null | wc -l | tr -d ' ')"
  echo "answer_verify_error : $(grep -h 'answer_verify_error' "$LOG_DIR"/*.log 2>/dev/null | wc -l | tr -d ' ')"
else
  echo "WARN: 日志目录不存在：$LOG_DIR"
fi

echo
echo "== 5) 服务状态（macOS launchd） =="
if command -v launchctl >/dev/null; then
  launchctl print system/com.ekbs.api 2>/dev/null | grep -E 'state|pid' | head -2 || echo "WARN: 未找到 com.ekbs.api"
fi
