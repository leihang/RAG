#!/usr/bin/env bash
# 双击打开：EKBS 离线运维页（主 API 挂了也能开机/关机/重启）
# 本窗口只跑运维 UI（:8010），不替代四服务本身。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export EKBS_CONFIG="${EKBS_CONFIG:-config/application.cloud.yaml}"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "找不到 $PY，请先创建虚拟环境"
  read -r _
  exit 1
fi

# 若 8010 已有本运维页，只打开浏览器
if curl -sf -o /dev/null --connect-timeout 1 "http://127.0.0.1:8010/" 2>/dev/null; then
  echo "[OK] 运维页已在跑：http://127.0.0.1:8010/"
  open "http://127.0.0.1:8010/" 2>/dev/null || true
  echo "本窗口可关闭；要停运维页请在对应终端 Ctrl+C。"
  exec bash
fi

echo "== EKBS 离线运维 UI =="
echo "目录：$ROOT"
echo "页面：http://127.0.0.1:8010/"
echo "四服务状态仍用：bash scripts/dev_services.sh status"
echo
exec "$PY" -m ekbs.tools.ops_ui --port 8010
