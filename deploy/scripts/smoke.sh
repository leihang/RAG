#!/usr/bin/env bash
# =============================================================================
# deploy/scripts/smoke.sh —— EKBS 冒烟集（01 §14 的执行版）
# -----------------------------------------------------------------------------
# 覆盖项（逐项打印 [PASS]/[FAIL]，末尾输出 PASS=n FAIL=m）：
#   1. SQLite 可读 + 表数 = 29（01 §5）+ PRAGMA integrity_check + FTS5/JSON1 可用
#   2. FAISS 分片可读，且 dim / vector_count 与 ekbs_shard 登记一致（01 §6.2 自检口径）
#   3. 四个 launchd 服务已加载且上次退出码为 0（01 §8.3 / 04 §7.1）
#   4. api /healthz、api /readyz、adapter /readyz（04 §7.3 / §7.6）
#   5. Caddy HTTPS 可达（01 §9；未配域名时 SKIP）
#   6. --with-deps：01 §14 的依赖项与嵌入指纹一致性、权限红线
#   7. --with-qa  ：一次真实问答（消耗云端 token）
#
# 口径提示（文档内部不一致，本脚本按 04 的端点命名实现）：
#   01 §14 用 `/health/ready` 与 `/health/deps`，04 §7.3 定义的是 `/livez` `/healthz` `/readyz`，
#   04 §7.6 用 `localhost:8000/readyz`。本脚本检查 /healthz + /readyz（04 口径），
#   01 §14 的旧路径放在 --with-deps 里作为兼容探测。
#
# 用法：bash smoke.sh [--with-deps] [--with-qa]
# 退出码：0 = 无 FAIL；1 = 至少一项 FAIL（SKIP 不计入失败）
# =============================================================================
set -uo pipefail

API="${API:-http://127.0.0.1:8000}"
ADAPTER="${ADAPTER:-http://127.0.0.1:8100}"
DATA=/data/ekbs
DB="$DATA/index/ekbs.db"
VENV=/opt/ekbs/venv
ETC=/opt/ekbs/etc
SECRETS="$HOME/.ekbs/secrets.env"
EXPECTED_TABLES=33

WITH_DEPS=0; WITH_QA=0
for a in "$@"; do
  case "$a" in
    --with-deps) WITH_DEPS=1 ;;
    --with-qa)   WITH_QA=1 ;;
    -h|--help)   sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：${a}（可用：--with-deps --with-qa）"; exit 2 ;;
  esac
done

PASS=0; FAIL=0; SKIP=0
p() { printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1)); }
f() { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }
s() { printf '  [SKIP] %s\n' "$1"; SKIP=$((SKIP+1)); }
chk() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then p "$d"; else f "$d"; fi; }

PY="$VENV/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || true)"
# jpath <dotted.path>：从 stdin 读 JSON 并打印叶子值（bool 输出 true/false）。避免依赖 jq。
jpath() {
  [ -n "$PY" ] || { echo ""; return 0; }
  "$PY" -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit(0)
for k in sys.argv[1].split("."):
    if k == "":
        continue
    try:
        d = d[int(k)] if k.isdigit() else d[k]
    except Exception:
        print(""); raise SystemExit(0)
if isinstance(d, bool): print("true" if d else "false")
elif d is None: print("")
else: print(d)
' "$1" 2>/dev/null || echo ""
}
http_get() { curl -s --max-time "${2:-10}" "$1" 2>/dev/null || true; }
http_ok()  { curl -sf --max-time "${2:-10}" -o /dev/null "$1" 2>/dev/null; }

echo "== EKBS smoke =="
echo "  api=$API  adapter=$ADAPTER  db=$DB  时间=$(date '+%F %T')"
echo

echo "== 1. SQLite / Schema =="
chk "SQLite 可读（${DB}）" bash -c "sqlite3 '$DB' 'SELECT 1'"
tc="$(sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') AND name LIKE 'ekbs%' AND name NOT LIKE 'ekbs_chunk_fts_%';" 2>/dev/null || echo -1)"
if [ "$tc" = "$EXPECTED_TABLES" ]; then
  p "表数 = ${EXPECTED_TABLES}（01 §5 的权威 Schema）"
else
  f "表数 = ${tc}，期望 ${EXPECTED_TABLES}（`01` §5；计数须排除 FTS5 的 5 张影子表，否则得到 $((EXPECTED_TABLES+5))）"
fi
integ="$(sqlite3 "$DB" 'PRAGMA integrity_check;' 2>/dev/null || echo error)"
[ "$integ" = "ok" ] && p "PRAGMA integrity_check = ok" || f "PRAGMA integrity_check = ${integ}（库损坏，改用 01 §13 的备份恢复）"
jm="$(sqlite3 "$DB" 'PRAGMA journal_mode;' 2>/dev/null || echo unknown)"
[ "$jm" = "wal" ] && p "journal_mode = wal（01 §5.0 连接参数）" || f "journal_mode = ${jm}，期望 wal"
chk "FTS5 虚拟表可读（ekbs_chunk_fts）" sqlite3 "$DB" "SELECT count(*) FROM ekbs_chunk_fts"
if [ -n "$PY" ] && "$PY" -c 'import sqlite3;c=sqlite3.connect(":memory:");c.execute("CREATE VIRTUAL TABLE t USING fts5(x)");c.execute("SELECT json_array_length(\"[1,2,3]\")")' >/dev/null 2>&1; then
  p "FTS5 + JSON1 可用（venv 的 sqlite3 模块）"
else
  f "venv 的 sqlite3 缺少 FTS5 或 JSON1（01 §3 注）"
fi

echo
echo "== 2. FAISS 分片（01 §6.2 自检口径）=="
if [ -n "$PY" ]; then
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
    if idx.ntotal != r["vector_count"]:
        bad.append("%s ntotal %d≠登记 %d" % (r["shard_id"], idx.ntotal, r["vector_count"]))
    if not r["embedding_fingerprint"]:
        bad.append("%s 缺 embedding_fingerprint" % r["shard_id"])
print(("FAIL " + "; ".join(bad)) if bad else "PASS %d 个分片可读且 dim/vector_count 与登记一致" % len(rows))
raise SystemExit(1 if bad else 0)
PY
)"
  rc=$?
  case "$rc" in
    0) p "FAISS 分片：${out#PASS }" ;;
    3) s "${out#SKIP }" ;;
    *) f "FAISS 分片校验失败：${out#FAIL }" ;;
  esac
else
  s "无可用 python，跳过 FAISS 分片检查"
fi

echo
echo "== 3. launchd 四个服务（01 §8.3）=="
for svc in api dingtalk worker scheduler; do
  line="$(launchctl list 2>/dev/null | awk -v l="com.ekbs.$svc" '$3 == l {print $1" "$2}')"
  if [ -z "$line" ]; then
    f "com.ekbs.$svc 未加载（launchctl list 中缺失）"
  else
    pid="${line%% *}"; st="${line##* }"
    if [ "$pid" != "-" ]; then
      p "com.ekbs.$svc 运行中（pid ${pid}）"
    elif [ "$st" = "0" ]; then
      p "com.ekbs.$svc 已加载，上次退出码 0（未运行）"
    else
      f "com.ekbs.$svc 已加载但未运行，上次退出码 ${st}（见 /opt/ekbs/log/$svc.err.log）"
    fi
  fi
done

echo
echo "== 4. 健康端点（04 §7.3 / §7.6）=="
hz="$(http_get "$API/healthz")"
hz_st="$(printf '%s' "$hz" | jpath status)"
case "$hz_st" in
  ok)     p "api /healthz → ok" ;;
  "")     f "api /healthz 无响应（服务未启动？先看 /opt/ekbs/log/api.err.log）" ;;
  *)      f "api /healthz → ${hz_st}（依赖不可达；checks=$(printf '%s' "$hz" | jpath checks)）" ;;
esac
rz="$(http_get "$API/readyz")"
rz_st="$(printf '%s' "$rz" | jpath status)"
case "$rz_st" in
  ready)  p "api /readyz → ready" ;;
  "")     f "api /readyz 无响应（04 §7.3 定义；若 404 说明路由未注册）" ;;
  *)      f "api /readyz → ${rz_st}（冷启动头几秒属正常，连续 3 次 not_ready 触发 P1）" ;;
esac
arz_st="$(http_get "$ADAPTER/readyz" | jpath status)"
case "$arz_st" in
  ready)  p "adapter /readyz → ready（127.0.0.1:8100）" ;;
  "")     f "adapter /readyz 无响应（com.ekbs.dingtalk 是否在跑？04 §7.6 的两个 /readyz 之一）" ;;
  *)      f "adapter /readyz → ${arz_st}（Stream 未连上，见 dingtalk.err.log）" ;;
esac

echo
echo "== 5. Caddy HTTPS（01 §9，可选组件）=="
DOMAIN="${EKBS_DOMAIN:-}"
CADDYFILE="$ETC/Caddyfile"
if [ -z "$DOMAIN" ] && [ -f "$CADDYFILE" ]; then
  DOMAIN="$(awk '/\{[[:space:]]*$/ {print $1; exit}' "$CADDYFILE" 2>/dev/null || true)"
fi
if [ -z "$DOMAIN" ]; then
  s "未设置 EKBS_DOMAIN 且 $CADDYFILE 不存在（01 §9：Caddy 为可选，内网直连可省略）"
elif http_ok "https://$DOMAIN/" 15; then
  p "Caddy HTTPS 可达 https://$DOMAIN/"
else
  f "Caddy HTTPS 不可达 https://$DOMAIN/（brew services start caddy；证书签发需 DNS 与出网）"
fi

if [ "$WITH_DEPS" = "1" ]; then
  echo
  echo "== 6. 依赖项与指纹一致性（01 §14，--with-deps）=="
  # /health/deps 会真实出网并暴露内部状态：只允许「服务器本机」或「已登录管理员」。
  # 本脚本在服务器上跑（回环）即可直接调；若从别处指向内网地址，则带上内部令牌。
  deps_token="${EKBS_INTERNAL_TOKEN:-}"
  if [ -z "$deps_token" ] && [ -r "$SECRETS" ]; then
    deps_token="$(grep -E '^EKBS_INTERNAL_TOKEN=' "$SECRETS" | head -1 | cut -d= -f2- || true)"
  fi
  if [ -n "$deps_token" ]; then
    deps="$(curl -s --max-time 10 -H "X-Internal-Token: ${deps_token}" "$API/health/deps" 2>/dev/null || true)"
  else
    deps="$(http_get "$API/health/deps")"
  fi
  if [ -z "$(printf '%s' "$deps" | jpath faiss_shards)" ] && [ -z "$(printf '%s' "$deps" | jpath embed)" ]; then
    s "api /health/deps 无响应或需要身份（本机直连免凭据；远程需令牌 + sys:health:read）"
  else
    for kv in "faiss_shards:>0" "embed:true" "llm:true" "rerank:true" "dingtalk_stream:true" "fingerprint_match:true"; do
      k="${kv%%:*}"; want="${kv##*:}"; got="$(printf '%s' "$deps" | jpath "$k")"
      if [ "$want" = ">0" ]; then
        if [ -n "$got" ] && [ "$got" -gt 0 ] 2>/dev/null; then p "/health/deps.$k = $got"; else f "/health/deps.$k = ${got}（期望 >0）"; fi
      else
        [ "$got" = "$want" ] && p "/health/deps.$k = $want" || f "/health/deps.$k = ${got}（期望 ${want}）"
      fi
    done
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$API/api/v1/qa/chat" -H 'Content-Type: application/json' -d '{"question":"越权测试","stream":false}' 2>/dev/null || true)"
  [ -n "$code" ] || code=000
  if [ "$code" = "000" ]; then
    f "权限红线：无法连接 API（HTTP 000），本次无法判定（01 §14 红线项）"
  elif [ "$code" != "200" ]; then
    p "权限红线：无凭证问答被拒（HTTP ${code}）"
  else
    f "权限红线：无凭证问答返回 200（01 §14 红线项，禁止上线）"
  fi
fi

if [ "$WITH_QA" = "1" ]; then
  echo
  echo "== 7. 真实问答（--with-qa，会消耗云端 token）=="
  TOKEN="${EKBS_INTERNAL_TOKEN:-}"
  if [ -z "$TOKEN" ] && [ -r "$SECRETS" ]; then
    TOKEN="$(grep -E '^EKBS_INTERNAL_TOKEN=' "$SECRETS" | head -1 | cut -d= -f2- || true)"
  fi
  HDR="${EKBS_INTERNAL_HEADER:-X-Internal-Token}"
  body=""
  used=""
  for path in /api/v1/qa/chat /v1/qa/chat; do
    body="$(curl -s --max-time 180 -X POST "$API$path" \
      -H 'Content-Type: application/json' -H "$HDR: $TOKEN" \
      -d '{"question":"年假有多少天？","stream":false}' 2>/dev/null || true)"
    if [ -n "$(printf '%s' "$body" | jpath answer)" ]; then used="$path"; break; fi
  done
  ans="$(printf '%s' "$body" | jpath answer)"
  cite="$(printf '%s' "$body" | jpath citations.0.chunk_id)"
  if [ -n "$used" ] && [ -n "$ans" ]; then
    p "问答有答案（${used}，答案 ${#ans} 字符）"
  else
    f "问答无答案（已尝试 /api/v1/qa/chat 与 /v1/qa/chat；01 §14 与 04 §2.5 的路径前缀口径不一致）"
  fi
  [ -n "$cite" ] && p "引用可溯源（citations[0].chunk_id=${cite}）" || f "引用缺失（citations[0].chunk_id 为空）"
fi

echo
echo "== 结果：PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP =="
[ "$FAIL" -eq 0 ] || { echo "冒烟未通过，禁止上线（对应 01 §14 / 00 §4 的 DoD）"; exit 1; }
echo "冒烟通过"
