#!/usr/bin/env bash
# =============================================================================
# dev_services.sh —— 开发机上的「四服务」管理器
# -----------------------------------------------------------------------------
# 生产用 launchd（deploy/launchd/com.ekbs.*.plist，见 01 §8）；
# 开发机没有 root、也不用 /opt，因此这里用**同一套入口命令、同一套日志布局**
# 起停四个进程，让「开发机上怎么起 = 生产上怎么起」。
#
# 用法：
#   bash scripts/dev_services.sh start [api|dingtalk|worker|scheduler|all]
#   bash scripts/dev_services.sh stop  [api|dingtalk|worker|scheduler|all]
#   bash scripts/dev_services.sh status
#   bash scripts/dev_services.sh logs <service>
#   bash scripts/dev_services.sh restart all
#
# 需先准备好环境（README §一）：
#   .venv/bin/python -m ekbs.tools.init_db
#   export EKBS_CONFIG=config/application.dev.yaml
#   export EKBS_INTERNAL_TOKEN=dev-internal-token        # 仅联调用
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
UVICORN="$ROOT/.venv/bin/uvicorn"
RUN_DIR="$ROOT/var/run"
LOG_DIR="$ROOT/var/log"
export EKBS_CONFIG="${EKBS_CONFIG:-config/application.dev.yaml}"
export PYTHONUNBUFFERED=1

# 与生产 launchd 的口径对齐：launchd 用 `zsh -lc 'set -a; source secrets.env; …'` 注入密钥，
# 而本脚本原先只靠调用者自己 export —— 结果是"生产能起、dev 起不来"（或反过来靠手工 export
# 才能跑云端配置）。这里补上同一件事，dev 与正式路径的密钥来源就一致了。
if [ -f "$HOME/.ekbs/secrets.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$HOME/.ekbs/secrets.env"
  set +a
fi

SERVICES=(api dingtalk worker scheduler)
mkdir -p "$RUN_DIR" "$LOG_DIR"

pid_file() { echo "$RUN_DIR/$1.pid"; }

is_running() {
  local pid_file_path; pid_file_path="$(pid_file "$1")"
  [ -f "$pid_file_path" ] || return 1
  local pid; pid="$(cat "$pid_file_path" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

start_one() {
  local svc="$1"
  if is_running "$svc"; then
    echo "[SKIP] $svc 已在运行（pid $(cat "$(pid_file "$svc")")）"
    return 0
  fi
  local cmd
  case "$svc" in
    # 与 plist 的入口逐字对应：uvicorn ekbs.main:app / python -m ekbs.<x>
    api)       cmd=("$UVICORN" ekbs.main:app --host 127.0.0.1 --port 8000 --workers 1) ;;
    dingtalk)  cmd=("$PY" -m ekbs.stream) ;;
    worker)    cmd=("$PY" -m ekbs.worker) ;;
    scheduler) cmd=("$PY" -m ekbs.scheduler) ;;
    *) echo "[FAIL] 未知服务：$svc"; return 1 ;;
  esac
  nohup "${cmd[@]}" >>"$LOG_DIR/$svc.out.log" 2>>"$LOG_DIR/$svc.err.log" &
  echo $! >"$(pid_file "$svc")"
  sleep 1
  if is_running "$svc"; then
    echo "[ OK ] $svc 已启动（pid $(cat "$(pid_file "$svc")")，日志 $LOG_DIR/$svc.out.log）"
  else
    echo "[FAIL] $svc 启动失败，见 $LOG_DIR/$svc.err.log"
    return 1
  fi
}

stop_one() {
  local svc="$1"
  if ! is_running "$svc"; then
    echo "[SKIP] $svc 未运行"
    rm -f "$(pid_file "$svc")"
    return 0
  fi
  local pid; pid="$(cat "$(pid_file "$svc")")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "[WARN] $svc 未在 10s 内退出，强制结束"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$(pid_file "$svc")"
  echo "[ OK ] $svc 已停止"
}

status_all() {
  printf '%-12s %-8s %s\n' SERVICE STATE PID
  for svc in "${SERVICES[@]}"; do
    if is_running "$svc"; then
      printf '%-12s %-8s %s\n' "$svc" running "$(cat "$(pid_file "$svc")")"
    else
      printf '%-12s %-8s %s\n' "$svc" stopped "-"
    fi
  done
  echo
  echo "探针（api 侧）："
  echo "  curl -s localhost:8000/livez   # 进程活着"
  echo "  curl -s localhost:8000/healthz # 依赖自查（不出网）"
  echo "  curl -s localhost:8000/readyz  # 唯一 readiness 探针"
  echo "  curl -s localhost:8000/health/deps  # ⚠️ 会出网，不要挂探针"
}

# 支持一次给多个服务名（早期版本只取第二个参数，"start api worker" 会静默漏掉 worker）
targets() {
  if [ "$#" -eq 0 ] || [ "$1" = "all" ]; then
    printf '%s\n' "${SERVICES[@]}"
  else
    printf '%s\n' "$@"
  fi
}

ACTION="${1:-status}"
shift 2>/dev/null || true

case "$ACTION" in
  start)   for svc in $(targets "$@"); do start_one "$svc"; done ;;
  stop)    for svc in $(targets "$@"); do stop_one "$svc"; done ;;
  restart) for svc in $(targets "$@"); do stop_one "$svc"; start_one "$svc"; done ;;
  status)  status_all ;;
  logs)
    TARGET="${1:-api}"
    tail -n 50 "$LOG_DIR/${TARGET}.out.log" "$LOG_DIR/${TARGET}.err.log" 2>/dev/null \
      || { echo "无日志：$LOG_DIR/${TARGET}.{out,err}.log  （worker/scheduler 的 structlog 输出在 ${TARGET}.json.log）"; exit 1; }
    ;;
  *) echo "用法：$0 {start|stop|restart|status|logs} [服务名...|all]"; exit 2 ;;
esac
