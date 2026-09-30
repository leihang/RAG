#!/usr/bin/env bash
# deploy/scripts/check-secrets.sh —— 凭据纪律自动断言（CI 与日常巡检都会跑）
#
# 来源：04-钉钉接入方案.md §2.6（逐字抽取，未改动任何断言逻辑）。
# 三条断言：① secrets.env 权限恰为 600；② 未被 git 跟踪；③ plist 中无密钥明文。
# 安装位置建议：/opt/ekbs/etc/check-secrets.sh（install.sh 会顺带拷贝）。
set -euo pipefail

S="${HOME}/.ekbs/secrets.env"
[ -f "$S" ] || { echo "缺少 $S"; exit 1; }

# ① 权限必须恰为 600
perm="$(stat -f '%Lp' "$S")"
[ "$perm" = "600" ] || { echo "$S 权限必须为 600，当前 $perm"; exit 1; }

# ② 不得被 git 跟踪
if git -C "$(dirname "$0")/.." ls-files --error-unmatch "${S}" >/dev/null 2>&1; then
  echo "$S 已被 git 跟踪，立即 git rm --cached 并轮换其中全部密钥"; exit 1
fi

# ③ plist 中不得出现具体密钥名（注意：不能笼统 grep 'secret'，
#    否则会误命中包裹脚本里的 "source …/secrets.env"）
for p in "$HOME"/Library/LaunchAgents/com.ekbs.*.plist; do
  [ -e "$p" ] || continue
  if grep -qE 'DINGTALK_CLIENT_SECRET|DASHSCOPE_API_KEY|DEEPSEEK_API_KEY|JWT_SECRET|INTERNAL_TOKEN|PASSWORD' "$p"; then
    echo "$p 中出现密钥明文，请改为 source secrets.env"; exit 1
  fi
done

echo "凭据检查通过"
