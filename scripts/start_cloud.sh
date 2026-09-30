#!/usr/bin/env bash
# 一键按正式路径（真实云端模型）起四服务。
# 用法：bash scripts/start_cloud.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 清掉 Cursor/系统代理，避免 CONNECT 403 / DNS 失败
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY
unset http_proxy https_proxy all_proxy
unset SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy
unset GIT_HTTP_PROXY GIT_HTTPS_PROXY

export EKBS_CONFIG=config/application.cloud.yaml

if [ -f "$HOME/.ekbs/secrets.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$HOME/.ekbs/secrets.env"
  set +a
else
  echo "[FAIL] 缺少 $HOME/.ekbs/secrets.env（权限应为 600）"
  exit 1
fi

bash scripts/dev_services.sh stop all >/dev/null
bash scripts/dev_services.sh start all
bash scripts/dev_services.sh status
echo
echo "探针："
curl -s localhost:8000/readyz | python3 -m json.tool || true
echo
curl -s localhost:8000/health/deps | python3 -m json.tool || true
echo
echo "管理台：http://127.0.0.1:8000/admin/"
