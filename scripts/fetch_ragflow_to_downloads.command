#!/bin/zsh
# 用本机 Clash Verge（mixed-port 7897）把 RAGFlow 拉到「下载」目录。
# 用法：双击本文件，或在终端执行：zsh scripts/fetch_ragflow_to_downloads.command
set -euo pipefail

PROXY="${PROXY:-http://127.0.0.1:7897}"
DEST="${DEST:-$HOME/Downloads/ragflow}"
REPO="https://github.com/infiniflow/ragflow.git"

export http_proxy="$PROXY" https_proxy="$PROXY"
export HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" ALL_PROXY="$PROXY"

echo "代理: $PROXY"
echo "目标: $DEST"
echo

if ! curl -fsS --max-time 5 -x "$PROXY" https://github.com/ >/dev/null; then
  echo "[失败] 连不上代理或 GitHub。请确认 Clash Verge 已开启，系统代理/TUN 正常，mixed-port=7897。"
  read -r "?按回车关闭 "
  exit 1
fi

rm -rf "$DEST"
echo "[1/2] git clone --depth 1 …"
git -c http.proxy="$PROXY" -c https.proxy="$PROXY" \
  clone --depth 1 --branch main "$REPO" "$DEST"

echo
echo "[2/2] 完成"
du -sh "$DEST"
git -C "$DEST" log -1 --oneline
echo
echo "源码位置: $DEST"
read -r "?按回车关闭 "
