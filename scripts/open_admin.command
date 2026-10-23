#!/usr/bin/env bash
# 双击或在终端运行：在独立会话里常驻拉起 EKBS（云端配置）
# 不要在 Cursor Agent 里起服务——Agent shell 退出会带走子进程。
#
# 幂等：若四服务已在跑且 /livez 正常，只打印状态，不再 stop/start，
# 避免误开第二个终端时「好像起了两套」。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== EKBS 启动（云端配置）=="
echo "目录：$ROOT"
echo

already_ok=0
if bash scripts/dev_services.sh status 2>/dev/null | grep -q 'api[[:space:]]\+running' \
  && curl -sf -o /dev/null --connect-timeout 1 http://127.0.0.1:8000/livez; then
  # 再确认没有第二套 uvicorn 占着别的方式
  n_api="$(pgrep -f 'uvicorn ekbs.main:app' 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${n_api:-0}" -eq 1 ]; then
    already_ok=1
  fi
fi

if [ "$already_ok" -eq 1 ]; then
  echo "[OK] 服务已在运行，无需重复拉起（本窗口只作状态/日志占位）。"
  echo "     若桌面上还有另一个「EKBS 启动」终端，关掉多余的即可，只留一个。"
  echo
  bash scripts/dev_services.sh status
else
  bash scripts/start_cloud.sh
fi

echo
echo "管理台：http://127.0.0.1:8000/admin/"
echo "状态：bash scripts/dev_services.sh status"
echo "停止：bash scripts/dev_services.sh stop all"
echo
echo "⚠️ 请只保留一个本脚本打开的终端窗口；关掉它不会自动停服务（服务已 nohup），"
echo "   要停请执行：bash scripts/dev_services.sh stop all"
echo
# 保持终端窗口，方便看状态
exec bash
