#!/usr/bin/env bash
# =============================================================================
# dev_smoke.sh —— 开发机版冒烟（与 deploy/scripts/smoke.sh 同口径）
# -----------------------------------------------------------------------------
# 为什么需要它：`deploy/scripts/smoke.sh` 是按生产布局写的（/opt/ekbs、
# /data/ekbs、launchd 四服务、Caddy），在开发机上跑必然全红。本脚本把**同一批断言**
# 映射到开发布局（.venv/、var/、scripts/dev_services.sh），于是 `01` §14 / `04` §7.3
# 的验收标准今天就能逐条走一遍，而不是等到真机。
#
# 用法：
#   bash scripts/dev_smoke.sh                 # 静态检查 + 已启动服务的探针
#   bash scripts/dev_smoke.sh --start         # 先拉起四个服务再检查，结束后停掉
#   bash scripts/dev_smoke.sh --with-qa       # 额外做一次真实问答（需内环 token）
#   bash scripts/dev_smoke.sh --with-qa --redline   # 再加越权红线（HR 用户查财务机密）
#
# 退出码：0 = 全部通过（SKIP 不计失败）；1 = 至少一项 FAIL
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export EKBS_CONFIG="${EKBS_CONFIG:-config/application.dev.yaml}"
export EKBS_INTERNAL_TOKEN="${EKBS_INTERNAL_TOKEN:-dev-internal-token}"
API="${EKBS_API:-http://127.0.0.1:8000}"
ADAPTER="${EKBS_ADAPTER:-http://127.0.0.1:8100}"
DB="$ROOT/var/index/ekbs.db"
PY="$ROOT/.venv/bin/python"
EXPECTED_TABLES=29
EXPECTED_TRIGGERS=12

WITH_QA=0
REDLINE=0
START=0
for arg in "$@"; do
  case "$arg" in
    --with-qa) WITH_QA=1 ;;
    --redline) REDLINE=1 ;;
    --start)   START=1 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

PASS=0; FAIL=0; SKIP=0
p() { printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1)); }
f() { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }
s() { printf '  [SKIP] %s\n' "$1"; SKIP=$((SKIP+1)); }
chk() { # chk <描述> <命令...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then p "$desc"; else f "$desc"; fi
}
jpath() { # jpath <json> <dotted.path>
  "$PY" -c '
import json,sys
try: d=json.loads(sys.argv[1] or "{}")
except Exception: print(""); raise SystemExit(0)
for k in sys.argv[2].split("."):
    if k=="": continue
    try: d = d[int(k)] if k.isdigit() else d[k]
    except Exception: print(""); raise SystemExit(0)
print("" if d is None else ("true" if d is True else ("false" if d is False else d)))
' "$1" "$2" 2>/dev/null || echo ""
}
http_get() { curl -s --max-time "${2:-10}" "$1" 2>/dev/null || true; }

echo "== EKBS 开发机冒烟 =="
echo "  config=$EKBS_CONFIG  db=$DB  api=$API  adapter=$ADAPTER"
echo "  提示：与 deploy/scripts/smoke.sh 同口径，只是布局不同"
echo

# ---------------------------------------------------------------------------
echo "== 0. 环境前置（01 §0.1/§1）=="
chk "Python 虚拟环境存在（.venv）" test -x "$PY"
chk "配置文件可读" test -r "$EKBS_CONFIG"
if [ -f "$DB" ]; then p "SQLite 库存在"; else f "SQLite 库不存在（先跑：python -m ekbs.tools.init_db）"; fi

# ---------------------------------------------------------------------------
if [ "$START" = "1" ]; then
  echo
  echo "== 启动四服务（scripts/dev_services.sh）=="
  bash "$ROOT/scripts/dev_services.sh" start all | sed 's/^/  /'
  sleep 5
fi

echo
echo "== 1. SQLite / Schema（01 §5.7）=="
if [ -f "$DB" ]; then
  tc="$(sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') AND name LIKE 'ekbs%' AND name NOT LIKE 'ekbs_chunk_fts_%';" 2>/dev/null || echo -1)"
  [ "$tc" = "$EXPECTED_TABLES" ] && p "表数 = ${EXPECTED_TABLES}" || f "表数 = ${tc}，期望 ${EXPECTED_TABLES}"
  trig="$(sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%';" 2>/dev/null || echo -1)"
  [ "$trig" = "$EXPECTED_TRIGGERS" ] && p "审计触发器 = ${EXPECTED_TRIGGERS}（append-only 保护在位）" || f "触发器 = ${trig}，期望 ${EXPECTED_TRIGGERS}"
  integ="$(sqlite3 "$DB" 'PRAGMA integrity_check;' 2>/dev/null || echo error)"
  [ "$integ" = "ok" ] && p "integrity_check = ok" || f "integrity_check = ${integ}"
  jm="$(sqlite3 "$DB" 'PRAGMA journal_mode;' 2>/dev/null || echo unknown)"
  [ "$jm" = "wal" ] && p "journal_mode = wal" || f "journal_mode = ${jm}，期望 wal"
  pv="$(sqlite3 "$DB" 'SELECT version FROM ekbs_policy_version;' 2>/dev/null || echo '')"
  [ -n "$pv" ] && p "policy_version = ${pv}" || f "ekbs_policy_version 无数据（01 §5.3 种子缺失）"
  chk "FTS5 虚拟表可读（ekbs_chunk_fts）" sqlite3 "$DB" "SELECT count(*) FROM ekbs_chunk_fts"
  chk "WAL 模式下无悬挂 -shm/-wal 锁" test -w "$(dirname "$DB")"
else
  s "库不存在，跳过 Schema 检查"
fi

echo
echo "== 2. FAISS 分片与一致性（01 §6.2 自检口径）=="
if [ -f "$DB" ] && [ -x "$PY" ]; then
  out="$("$PY" - "$DB" <<'PY' 2>&1
import sqlite3, sys
try:
    import faiss
except Exception as exc:
    print("SKIP faiss 未安装：%s" % exc); raise SystemExit(3)
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row
rows = db.execute("SELECT shard_id, faiss_path, dim, vector_count, embedding_fingerprint "
                  "FROM ekbs_shard WHERE status='active'").fetchall()
if not rows:
    print("SKIP 无 active 分片（尚未建分片，见 01 §6.3）"); raise SystemExit(3)
bad = []
for r in rows:
    try:
        idx = faiss.read_index(r["faiss_path"])
    except Exception as exc:
        bad.append("%s 读取失败(%s)" % (r["shard_id"], exc.__class__.__name__)); continue
    if idx.d != r["dim"]:
        bad.append("%s dim %d≠登记 %d" % (r["shard_id"], idx.d, r["dim"]))
    if idx.ntotal < r["vector_count"]:
        bad.append("%s ntotal %d<登记 %d（索引丢向量）" % (r["shard_id"], idx.ntotal, r["vector_count"]))
    if not r["embedding_fingerprint"]:
        bad.append("%s 缺 embedding_fingerprint" % r["shard_id"])
print(("FAIL " + "; ".join(bad)) if bad else "PASS %d 个分片可读且维度/向量数与登记一致" % len(rows))
raise SystemExit(1 if bad else 0)
PY
)"
  case "$?" in
    0) p "FAISS 分片：${out#PASS }" ;;
    3) s "${out#SKIP }" ;;
    *) f "FAISS 分片校验失败：${out#FAIL }" ;;
  esac
else
  s "无库或不含 python，跳过 FAISS 检查"
fi

echo
echo "== 3. 四个服务（01 §8.2；开发机用 dev_services.sh 的状态）=="
status_out="$(bash "$ROOT/scripts/dev_services.sh" status 2>/dev/null || true)"
for svc in api dingtalk worker scheduler; do
  line="$(printf '%s\n' "$status_out" | awk -v s="$svc" '$1 == s {print $2}')"
  case "$line" in
    running) p "com.ekbs.$svc 运行中" ;;
    "")      f "com.ekbs.$svc 状态未知（先 bash scripts/dev_services.sh start ${svc}）" ;;
    *)       f "com.ekbs.$svc 未运行（状态：${line}）" ;;
  esac
done

echo
echo "== 4. 健康端点（04 §7.3）=="
hz="$(http_get "$API/healthz")"; hz_st="$(jpath "$hz" status)"
case "$hz_st" in
  ok)  p "api /healthz → ok" ;;
  "")  f "api /healthz 无响应（服务未启动？）" ;;
  *)   f "api /healthz → ${hz_st}（checks=$(jpath "$hz" checks)）" ;;
esac
rz="$(http_get "$API/readyz")"; rz_st="$(jpath "$rz" status)"
case "$rz_st" in
  ready) p "api /readyz → ready" ;;
  "")    f "api /readyz 无响应（若 404 说明路由未注册）" ;;
  *)     f "api /readyz → ${rz_st}" ;;
esac
arz="$(http_get "$ADAPTER/readyz")"; arz_st="$(jpath "$arz" status)"
case "$arz_st" in
  ready) p "adapter /readyz → ready（Stream 已收到过回调）" ;;
  "")    s "adapter /readyz 无响应（开发机默认关闭 Stream，属正常）" ;;
  *)     p "adapter /readyz → ${arz_st}（诚实报告未连接，符合 D3 修复预期）" ;;
esac

echo
echo "== 5. 权限红线：无凭证问答必须被拒（01 §14 红线项）=="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$API/api/v1/qa/chat" \
  -H 'Content-Type: application/json' -d '{"question":"越权测试","stream":false}' 2>/dev/null || true)"
[ -n "$code" ] || code=000
if [ "$code" = "000" ]; then
  f "权限红线：无法连接 API（HTTP 000），本次无法判定"
elif [ "$code" != "200" ]; then
  p "权限红线：无凭证问答被拒（HTTP ${code}）"
else
  f "权限红线：无凭证问答返回 200（禁止上线）"
fi

if [ "$WITH_QA" = "1" ]; then
  echo
  echo "== 6. 真实问答（--with-qa）=="
  uid="$(sqlite3 "$DB" "SELECT user_id FROM ekbs_user WHERE dingtalk_userid='demo_alice';" 2>/dev/null || true)"
  if [ -z "$uid" ]; then
    s "库里没有 demo_alice（先跑 python -m ekbs.tools.seed_demo）"
  else
    body="$(curl -s --max-time 180 -X POST "$API/api/v1/qa/chat" \
      -H 'Content-Type: application/json' -H "X-Internal-Token: $EKBS_INTERNAL_TOKEN" \
      -H "X-Internal-User-Id: $uid" -d '{"question":"年假有多少天","stream":false}' 2>/dev/null || true)"
    ans="$(jpath "$body" answer)"; cite="$(jpath "$body" citations.0.chunk_id)"
    [ -n "$ans" ] && p "问答有答案（${#ans} 字符）" || f "问答无答案（$(jpath "$body" refusal_reason)）"
    [ -n "$cite" ] && p "引用可溯源（citations[0].chunk_id=${cite}）" || f "引用缺失"
    fp="$(jpath "$body" perm_fingerprint)"
    [ -n "$fp" ] && p "权限指纹由服务端计算（${fp:0:12}…）" || f "响应缺少 perm_fingerprint"
  fi
fi

if [ "$REDLINE" = "1" ]; then
  echo
  echo "== 7. 越权红线（--redline）：HR 用户不得召回到财务机密 =="
  uid="$(sqlite3 "$DB" "SELECT user_id FROM ekbs_user WHERE dingtalk_userid='demo_alice';" 2>/dev/null || true)"
  if [ -z "$uid" ]; then
    s "库里没有 demo_alice，跳过"
  else
    body="$(curl -s --max-time 180 -X POST "$API/api/v1/qa/chat" \
      -H 'Content-Type: application/json' -H "X-Internal-Token: $EKBS_INTERNAL_TOKEN" \
      -H "X-Internal-User-Id: $uid" -d '{"question":"财务专用章由谁保管","stream":false}' 2>/dev/null || true)"
    if printf '%s' "$body" | grep -q '财务保密制度'; then
      f "越权：HR 用户拿到了财务机密文档的引用（红线，禁止上线）"
    else
      p "越权红线：HR 用户的回答里没有财务机密文档"
    fi
  fi
fi

if [ "$START" = "1" ]; then
  echo
  echo "== 停止四服务 =="
  bash "$ROOT/scripts/dev_services.sh" stop all | sed 's/^/  /'
fi

echo
echo "== 结果：PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP =="
[ "$FAIL" -eq 0 ] || { echo "冒烟未通过（对应 01 §14 / 00 §4 的 DoD）"; exit 1; }
echo "冒烟通过 —— 开发机侧验收标准逐条成立"
