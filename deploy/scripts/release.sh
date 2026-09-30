#!/usr/bin/env bash
# =============================================================================
# deploy/scripts/release.sh —— 切换版本并逐个重启（04-钉钉接入方案.md §7.6）
# -----------------------------------------------------------------------------
# 部署目录用「版本目录 + 符号链接」，回滚即改链接，不做原地覆盖：
#     /opt/ekbs/releases/20260926-1430/     # 每个版本一个目录
#     /opt/ekbs/app -> /opt/ekbs/releases/20260926-1430
#
# 重启顺序很重要：先 API（适配器依赖它），再调度/worker，最后才让适配器接管消息。
# 首次切版本前请确认 04 §7.6 的阶段 1–2 已完成：开发机 pytest + smoke 全绿、备份已生成。
#
# 用法：bash release.sh /opt/ekbs/releases/<时间戳>
# 回滚：ln -sfn /opt/ekbs/releases/<上一个时间戳> /opt/ekbs/app && 重跑本脚本
#
# 相对 04 §7.6 原文的增量（均为「验证点」的机器化，不改变流程与顺序）：
#   · pip 之后补 `pip check`（阶段 3 的验证点「pip check 无冲突」）；
#   · 重启循环内对每个服务做有界就绪等待，而不是固定 sleep 2；
#   · 末尾两个 /readyz 失败时打印回滚命令。
# =============================================================================
set -euo pipefail
NEW="${1:?用法: release.sh /opt/ekbs/releases/<时间戳>}"

test -d "$NEW" || { echo "目标目录不存在: $NEW"; exit 1; }

APP=/opt/ekbs/app
# app 必须最终是**符号链接**（04 §7.6 的布局）。
# [实测修订 2026-09-27] install.sh §1 会把 $APP 建成**真实目录**，此时
# `ln -sfn "$NEW" "$APP"` 不会替换它，而是把链接建到该目录**里面**
# （/opt/ekbs/app/<时间戳> -> .../releases/<时间戳>）—— 布局静默失效，
# 且每次发布/回滚都会再套一层。故先把真实目录收掉（保留其中的 sql 引用文件）。
if [ -d "$APP" ] && [ ! -L "$APP" ]; then
  echo "  [注意] $APP 是真实目录（install.sh 首次创建），收掉后改为符号链接"
  mkdir -p "$NEW/sql"
  cp -n "$APP/sql/001_init.sql" "$NEW/sql/" 2>/dev/null || true
  rm -rf "$APP"
fi
ln -sfn "$NEW" "$APP"
echo "  $APP -> $(readlink "$APP")"

/opt/ekbs/venv/bin/pip install -q -e "$NEW"

# 阶段 3 验证点：依赖无冲突（不通过就不该重启服务）
/opt/ekbs/venv/bin/pip check

# 有界等待：等服务回到 launchctl 管理且进程起来（最多 30s），避免固定 sleep 掩盖启动失败
wait_up() {
  local svc="$1" i
  for i in $(seq 1 15); do
    if launchctl list 2>/dev/null | awk -v l="com.ekbs.$svc" '$3 == l && $1 != "-" {found=1} END {exit !found}'; then
      echo "  $svc 已启动"
      return 0
    fi
    sleep 2
  done
  echo "  [WARN] $svc 30s 内未进入运行态，请查 /opt/ekbs/log/$svc.err.log" >&2
  return 0
}

# 顺序很重要：先 API（适配器依赖它），再调度/worker，最后才让适配器接管消息
for s in api scheduler worker dingtalk; do
  launchctl kickstart -k "gui/$(id -u)/com.ekbs.$s"
  wait_up "$s"
done

launchctl list | grep ekbs                      # 四行，第二列（上次退出码）应为 0

# 两个 /readyz：api 8000 与 adapter 8100（04 §7.6 阶段 4 的验证点）
ready=0
for url in http://127.0.0.1:8000/readyz http://127.0.0.1:8100/readyz; do
  body=""
  for i in $(seq 1 15); do
    if body="$(curl -sf --max-time 5 "$url" 2>/dev/null)" && printf '%s' "$body" | grep -qE '"status"[[:space:]]*:[[:space:]]*"ready"'; then
      break
    fi
    body=""; sleep 2
  done
  if [ -n "$body" ]; then
    echo "$url -> $body"
  else
    echo "[FAIL] $url 未返回 ready（04 §7.3：冷启动头几秒 not_ready 属正常；持续不 ready 需回滚）" >&2
    ready=1
  fi
done

if [ "$ready" -ne 0 ]; then
  echo "发布中断：链接已切到 ${NEW}，但就绪校验未通过。" >&2
  echo "回滚：ln -sfn /opt/ekbs/releases/<上一个时间戳> /opt/ekbs/app && bash $0 /opt/ekbs/releases/<上一个时间戳>" >&2
  exit 1
fi

echo "发布完成：$NEW"
