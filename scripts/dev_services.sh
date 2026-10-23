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

# 扫描本机全部 ``python -m ekbs.worker``（含 pidfile 丢失的孤儿）。
# 绝不能靠删 worker.lock「清锁」——删了会 flock inode 分裂，双 worker 并存。
# ps 失败必须非 0 退出：把「扫不了」当成「没有 worker」会直接制造双开。
list_worker_pids() {
  "$PY" - <<'PY'
import re, subprocess, sys
pat = re.compile(r"(?:^|[\s/])-m\s+ekbs\.worker(?:\s|$)")
try:
    out = subprocess.check_output(
        ["ps", "-ax", "-o", "pid=", "-o", "command="],
        text=True, stderr=subprocess.DEVNULL,
    )
except (OSError, subprocess.CalledProcessError) as exc:
    print(f"ps 扫描失败: {exc}", file=sys.stderr)
    sys.exit(2)
pids = []
for line in out.splitlines():
    line = line.strip()
    if not line:
        continue
    parts = line.split(None, 1)
    if len(parts) < 2:
        continue
    try:
        pid = int(parts[0])
    except ValueError:
        continue
    if pat.search(parts[1]):
        pids.append(str(pid))
print(" ".join(pids))
PY
}

kill_pid_wait() {
  # 返回 0=已死；1=仍存活（含无权限 kill）
  local pid="$1" label="${2:-proc}"
  kill "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  echo "[WARN] $label (pid $pid) 未在 10s 内退出，强制 kill -9"
  kill -9 "$pid" 2>/dev/null || true
  sleep 0.5
  if kill -0 "$pid" 2>/dev/null; then
    echo "[FAIL] 无法停止 ${label}（pid $pid 仍存活，可能无权限）"
    return 1
  fi
  return 0
}

start_one() {
  local svc="$1"
  if is_running "$svc"; then
    echo "[SKIP] $svc 已在运行（pid $(cat "$(pid_file "$svc")")）"
    return 0
  fi
  # worker：pidfile 可能过期/被误删，但进程还在——此时绝不能再起第二个。
  if [ "$svc" = worker ]; then
    local orphans
    if ! orphans="$(list_worker_pids)"; then
      echo "[FAIL] 无法扫描 worker 进程（ps 失败），拒绝启动以免双开"
      return 1
    fi
    if [ -n "$orphans" ]; then
      echo "[FAIL] worker 已有存活进程（pid ${orphans}），拒绝再起。"
      echo "       请先：bash scripts/dev_services.sh stop worker"
      echo "       禁止删除 var/worker.lock（会导致双 worker）"
      return 1
    fi
    # 清掉陈旧 pidfile，避免 is_running 误判
    rm -f "$(pid_file worker)"
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
  # 必须新会话启动：Cursor Agent / 部分 IDE 会在 shell 结束时向整个
  # process group 发 SIGTERM；普通 `nohup ... &` 仍在同组，会被一并杀掉
  # （open_admin.command 注释里已踩过）。Popen(start_new_session=True) = setsid。
  local pid_path out_path err_path
  pid_path="$(pid_file "$svc")"
  out_path="$LOG_DIR/$svc.out.log"
  err_path="$LOG_DIR/$svc.err.log"
  "$PY" - "$pid_path" "$out_path" "$err_path" "${cmd[@]}" <<'PY'
import os, subprocess, sys
pid_path, out_path, err_path, *cmd = sys.argv[1:]
os.makedirs(os.path.dirname(pid_path), exist_ok=True)
out = open(out_path, "a", encoding="utf-8")
err = open(err_path, "a", encoding="utf-8")
proc = subprocess.Popen(
    cmd,
    stdout=out,
    stderr=err,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)
open(pid_path, "w", encoding="utf-8").write(str(proc.pid))
PY
  sleep 1
  if is_running "$svc"; then
    # worker 再确认：只允许恰好一个进程，且与 pidfile 一致
    if [ "$svc" = worker ]; then
      local live
      if ! live="$(list_worker_pids)"; then
        echo "[FAIL] worker 已写入 pidfile 但无法复核进程列表，见 $err_path"
        return 1
      fi
      local expect; expect="$(cat "$(pid_file worker)")"
      if [ "$live" != "$expect" ]; then
        echo "[FAIL] worker 启动后进程集合异常（期望 ${expect}，实际 ${live:-无}），见 $err_path"
        return 1
      fi
    fi
    echo "[ OK ] $svc 已启动（pid $(cat "$(pid_file "$svc")")，日志 $LOG_DIR/$svc.out.log）"
  else
    echo "[FAIL] $svc 启动失败，见 $LOG_DIR/$svc.err.log"
    return 1
  fi
}

stop_one() {
  local svc="$1"
  local failed=0
  local pid=""

  if is_running "$svc"; then
    pid="$(cat "$(pid_file "$svc")")"
    kill_pid_wait "$pid" "$svc" || failed=1
  fi

  # worker：无论 pidfile 是否记录，都清掉全部孤儿 -m ekbs.worker
  if [ "$svc" = worker ]; then
    local orphans
    if ! orphans="$(list_worker_pids)"; then
      echo "[FAIL] 无法扫描 worker 进程（ps 失败），拒绝假装已停"
      return 1
    fi
    local op
    for op in $orphans; do
      [ -n "$pid" ] && [ "$op" = "$pid" ] && continue
      echo "[INFO] 清理孤儿 worker pid $op"
      kill_pid_wait "$op" "orphan-worker" || failed=1
    done
    # 二次确认：仍有存活则失败，且保留 pidfile 以免误 start
    if ! orphans="$(list_worker_pids)"; then
      echo "[FAIL] 无法复核 worker 进程列表，拒绝假装已停"
      return 1
    fi
    if [ -n "$orphans" ]; then
      echo "[FAIL] worker 仍有存活进程：${orphans}（未删 pidfile，拒绝假装已停）"
      return 1
    fi
  elif [ "$failed" -ne 0 ]; then
    echo "[FAIL] $svc 未能停止，保留 pidfile"
    return 1
  fi

  rm -f "$(pid_file "$svc")"
  if [ -n "$pid" ] || [ "$svc" = worker ]; then
    echo "[ OK ] $svc 已停止"
  else
    echo "[SKIP] $svc 未运行"
  fi
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
  # worker 孤儿显式报警：pidfile 说 stopped 但进程还在，就是双开温床
  local orphans
  if orphans="$(list_worker_pids)"; then
    if [ -n "$orphans" ]; then
      local file_pid=""; [ -f "$(pid_file worker)" ] && file_pid="$(cat "$(pid_file worker)")"
      if ! is_running worker || [ "$orphans" != "$file_pid" ]; then
        echo
        echo "[WARN] 检测到 worker 进程与 pidfile 不一致：live=[$orphans] pidfile=[${file_pid:-}]"
        echo "       请：bash scripts/dev_services.sh stop worker && bash scripts/dev_services.sh start worker"
        echo "       禁止删除 var/worker.lock"
      fi
    fi
  else
    echo
    echo "[WARN] 无法扫描 worker 进程（ps 失败），status 可能不完整"
  fi
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

RC=0
case "$ACTION" in
  start)
    for svc in $(targets "$@"); do
      start_one "$svc" || RC=1
    done
    ;;
  stop)
    for svc in $(targets "$@"); do
      stop_one "$svc" || RC=1
    done
    ;;
  restart)
    # 停不干净绝不能起新的——否则就是双 worker 根因
    for svc in $(targets "$@"); do
      if stop_one "$svc"; then
        start_one "$svc" || RC=1
      else
        echo "[FAIL] $svc 停止失败，跳过启动以免双开"
        RC=1
      fi
    done
    ;;
  status)  status_all ;;
  logs)
    TARGET="${1:-api}"
    tail -n 50 "$LOG_DIR/${TARGET}.out.log" "$LOG_DIR/${TARGET}.err.log" 2>/dev/null \
      || { echo "无日志：$LOG_DIR/${TARGET}.{out,err}.log  （worker/scheduler 的 structlog 输出在 ${TARGET}.json.log）"; exit 1; }
    ;;
  *) echo "用法：$0 {start|stop|restart|status|logs} [服务名...|all]"; exit 2 ;;
esac
exit "$RC"
