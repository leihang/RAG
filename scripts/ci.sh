#!/usr/bin/env bash
# scripts/ci.sh —— 本地 CI 门禁（与远端 CI 同口径）
#
# 跑什么：
#   ① ruff check .              静态检查（配置在 pyproject.toml）
#   ② pytest tests/             全量离线用例（约 85s）
#   ③ 管理台前端 typecheck / vitest / build（有 node_modules 才跑）
#
# 用法：
#   bash scripts/ci.sh                # 全跑
#   bash scripts/ci.sh --no-frontend  # 跳过前端（无 node_modules 时也会自动跳过）
#
# 退出码：0 = 全绿；1 = 有 FAIL；2 = 参数错误。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
RUFF="$ROOT/.venv/bin/ruff"
WEB="$ROOT/ekbs-admin-web"
RUN_FRONTEND=1

for arg in "$@"; do
  case "$arg" in
    --no-frontend) RUN_FRONTEND=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "未知参数：${arg}（用法：bash scripts/ci.sh [--no-frontend]）"; exit 2 ;;
  esac
done

FAIL=0
PASS=0
stage() { echo; echo "== $* =="; }
ok()   { echo "[PASS] $*"; PASS=$((PASS + 1)); }
bad()  { echo "[FAIL] $*"; FAIL=$((FAIL + 1)); }

stage "0. 环境"
if [ -x "$PY" ]; then ok "python: $("$PY" --version 2>&1)"; else bad "缺少 ${PY}（先建 .venv）"; fi
if [ -x "$RUFF" ]; then ok "ruff: $("$RUFF" --version 2>&1)"; else bad "缺少 ruff（pip install -r requirements-dev.txt）"; fi

if [ "$FAIL" -eq 0 ]; then
  stage "1. 静态检查（ruff）"
  if "$RUFF" check .; then ok "ruff check 全绿"; else bad "ruff check 有违规"; fi

  stage "2. 契约测试（pytest，离线）"
  # 注意：不要再传 -q —— pyproject 的 addopts 已有 -q，双 quiet 会吞掉汇总行
  if "$PY" -m pytest tests/; then ok "pytest 全绿"; else bad "pytest 未全绿"; fi
fi

if [ "$RUN_FRONTEND" -eq 0 ]; then
  stage "3. 管理台前端（--no-frontend 跳过）"
elif [ ! -d "$WEB/node_modules" ]; then
  stage "3. 管理台前端（无 node_modules，跳过；先在 ekbs-admin-web 跑 npm install）"
else
  stage "3. 管理台前端（typecheck / vitest / build）"
  if (cd "$WEB" && npm run typecheck); then ok "前端 typecheck 通过"; else bad "前端 typecheck 失败"; fi
  if (cd "$WEB" && npm test); then ok "前端 vitest 通过"; else bad "前端 vitest 失败"; fi
  if (cd "$WEB" && npm run build:only); then ok "前端 build 通过"; else bad "前端 build 失败"; fi
fi

echo
echo "== 结果：PASS=$PASS FAIL=$FAIL =="
if [ "$FAIL" -eq 0 ]; then
  echo "CI 通过"
  exit 0
fi
echo "CI 未通过"
exit 1
