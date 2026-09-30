#!/usr/bin/env bash
# =============================================================================
# acceptance.sh —— 上线接收检查（把 DoD 变成一次命令的结论）
# -----------------------------------------------------------------------------
# 为什么需要它：`01` §0.3 的 DoD 是 7 条清单、`02` 是 11 阶段测试、`deploy/scripts`
# 有 preflight/smoke、权限是数据要迁移、备份要演练 —— 散落各处，没人能一眼说清
# 「现在能不能上」。本脚本把这些检查**按顺序跑一遍**，最后给一个明确的判定：
#
#   READY       —— 开发机侧全部就绪（外部条件项另列）
#   NOT-READY   —— 有 FAIL，逐条列出并给出对应文档章节
#
# 三个立场（刻意的）：
#   ① **不把"需要外部输入"伪装成 PASS**：云端 key、钉钉凭据、MinerU、真机部署
#      一律归入「待外部条件」，不计入 FAIL，但也绝不算通过；
#   ② **权限数据迁移是硬闸门**：库里权限点与代码目录不一致（迁移会改东西）→ FAIL，
#      因为那会让线上出现大片 403；
#   ③ **备份必须演练过**：只产包不恢复不算达标（`01` §13）。
#
# 用法：
#   bash scripts/acceptance.sh                # 全量（会启停四个服务、做备份演练）
#   bash scripts/acceptance.sh --quick        # 跳过服务启停与备份演练（CI 友好）
#   bash scripts/acceptance.sh --json         # 末尾输出机器可读结论
#
# 退出码：0 = 无 FAIL；1 = 至少一项 FAIL
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export EKBS_CONFIG="${EKBS_CONFIG:-config/application.dev.yaml}"
export EKBS_INTERNAL_TOKEN="${EKBS_INTERNAL_TOKEN:-dev-internal-token}"
PY="$ROOT/.venv/bin/python"
DB="$ROOT/var/index/ekbs.db"

QUICK=0
JSON=0
NO_TESTS=0
for arg in "$@"; do
  case "$arg" in
    --quick)    QUICK=1 ;;
    --json)     JSON=1 ;;
    --no-tests) NO_TESTS=1 ;;   # 供脚本自身的用例使用（避免 pytest 里嵌套跑 pytest）
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

PASS=0; FAIL=0; SKIP=0; BLOCKED=0
declare -a FAIL_LINES=()
declare -a BLOCKED_LINES=()

p()  { printf '  [PASS]    %s\n' "$1"; PASS=$((PASS+1)); }
f()  { printf '  [FAIL]    %s\n' "$1"; FAIL=$((FAIL+1)); FAIL_LINES+=("$1"); }
s()  { printf '  [SKIP]    %s\n' "$1"; SKIP=$((SKIP+1)); }
b()  { printf '  [外需]    %s\n' "$1"; BLOCKED=$((BLOCKED+1)); BLOCKED_LINES+=("$1"); }
stage() { printf '\n== %s ==\n' "$1"; }

echo "== EKBS 上线接收检查 =="
echo "  时间：$(date '+%F %T')  仓库：$ROOT"
echo "  配置：$EKBS_CONFIG  模式：$([ "$QUICK" = 1 ] && echo quick || echo full)"

# ---------------------------------------------------------------------------
stage "1. 环境前置（01 §0.1 / §1）"
if [ -x "$PY" ]; then p "Python 虚拟环境存在（.venv）"; else f "缺少 .venv（先按 README §一 安装依赖）"; fi
if [ -r "$EKBS_CONFIG" ]; then p "配置文件可读：$EKBS_CONFIG"; else f "配置文件不可读：$EKBS_CONFIG"; fi
free_gb="$(df -g "$ROOT" | awk 'NR==2 {print $4}')"
if [ "${free_gb:-0}" -ge 12 ]; then p "磁盘余量 ${free_gb} GB ≥ 12 GB（00 §5 的数据需求下限）"
else f "磁盘余量 ${free_gb} GB < 12 GB（00 §5 要求 12–41 GB）"; fi

# ---------------------------------------------------------------------------
stage "2. Schema 与权限数据（01 §5 / 迁移闸门）"
if [ -x "$PY" ]; then
  schema_out="$("$PY" -m ekbs.tools.init_db 2>&1 | tail -8)"
  if printf '%s' "$schema_out" | grep -q '自检通过'; then
    p "Schema 自检通过（29 表 / 12 触发器 / WAL / integrity）"
  else
    f "Schema 自检未通过：$(printf '%s' "$schema_out" | tr '\n' ' ' | cut -c1-160)"
  fi

  # 权限数据迁移是**硬闸门**：库里的权限点与代码目录不一致会让线上大片 403
  mig_json="$("$PY" -m ekbs.tools.migrate_permissions --dry-run --json 2>/dev/null || echo '{}')"
  pending="$("$PY" -c '
import json,sys
try: d=json.loads(sys.stdin.read() or "{}")
except Exception: print(-1); raise SystemExit(0)
print(len(d.get("permissions_added",[])) + len(d.get("permissions_renamed",{})) + len(d.get("permissions_retired",[])) + int(d.get("grants_added",0)))
' <<<"$mig_json")"
  case "$pending" in
    0) p "权限数据与 05 §8.2 目录一致（迁移演练 0 变更）" ;;
    -1) f "权限迁移演练无法解析输出（请手动跑 ekbs.tools.migrate_permissions --dry-run）" ;;
    *) f "权限数据待迁移：$pending 处变更（跑 ekbs.tools.migrate_permissions；否则线上会出现 403）" ;;
  esac
else
  s "无 python，跳过 Schema 与迁移检查"
fi

# ---------------------------------------------------------------------------
stage "3. 配置硬断言（04 §7.2 preflight 同口径）"
cfg="$(cat "$EKBS_CONFIG" 2>/dev/null || true)"
grep -qE '^[[:space:]]*host:[[:space:]]*127\.0\.0\.1' <<<"$cfg" && p "监听地址为回环 127.0.0.1" || f "server.host/adapter.host 必须为回环（04 §7.2）"
grep -qE '^[[:space:]]*workers:[[:space:]]*1' <<<"$cfg" && p "server.workers = 1（权限缓存单进程前提）" || f "server.workers 必须为 1（04 §7.2）"
grep -qE '^[[:space:]]*l4_enabled:[[:space:]]*false' <<<"$cfg" && p "permission.l4_enabled = false（首期 L4 不入库）" || b "l4_enabled 非 false：需确认是否已按 00 D11 启用 L4"
if grep -qE '^[[:space:]]*l3_ingest_enabled:[[:space:]]*true' <<<"$cfg"; then
  b "l3_ingest_enabled=true：须有 C5 验收记录（TC-PERM-033）"
else
  p "permission.l3_ingest_enabled = false（C5 未验收前禁止真实 L3 入库）"
fi

# 密钥纪律（04 §2.6）：配置里不得出现密钥；secrets.env 必须 600 且不被 git 跟踪
if [ -f "$ROOT/scripts/check-secrets.sh" ]; then
  s "方案侧另有 deploy/scripts/check-secrets.sh（真机用）"
fi
if [ -f "$HOME/.ekbs/secrets.env" ]; then
  perm_bits="$(stat -f '%Lp' "$HOME/.ekbs/secrets.env" 2>/dev/null || echo '')"
  [ "$perm_bits" = "600" ] && p "~/.ekbs/secrets.env 权限为 600" || f "~/.ekbs/secrets.env 权限为 ${perm_bits}（必须 600，04 §2.6）"
else
  b "未找到 ~/.ekbs/secrets.env（联调/上线前需按 deploy/config/secrets.env.example 准备）"
fi

# ---------------------------------------------------------------------------
stage "4. 契约测试（离线用例，不出网）"
if [ "$NO_TESTS" = 1 ]; then
  s "--no-tests：跳过契约测试"
elif [ -x "$PY" ]; then
  # 两个坑都踩过，注释留在这里免得重犯：
  #   ① 不要用 `| tail -N` 取汇总行 —— 缓冲交错时汇总行可能不在最后几行；
  #   ② **不要再传 `-q`**：pyproject 的 addopts 里已有 `-q`，再传一次就是 `-qq`，
  #      而 pytest 在双 quiet 下**会抑制汇总行**（表现为"明明全绿却读不到 passed"）。
  test_out="$("$PY" -m pytest tests/ 2>&1)"
  summary="$(printf '%s\n' "$test_out" | grep -oE '[0-9]+ (passed|failed|error)[^\n]*' | tail -1)"
  if printf '%s' "$summary" | grep -qE '^[0-9]+ passed'; then
    p "pytest 全绿：${summary}"
  else
    f "pytest 未全绿：${summary:-无汇总输出}"
  fi
else
  s "无 python，跳过契约测试"
fi

# ---------------------------------------------------------------------------
stage "5. 冒烟与四服务（01 §14 / 04 §7.3）"
if [ "$QUICK" = 1 ]; then
  s "--quick：跳过服务启停与冒烟"
else
  # 端口预检：dev 冒烟要在 8000/8100 起服务。若不先检查，占用会表现成
  # 「api 启动失败，见 var/log/api.err.log」—— 排查方向被引到错误的地方
  # （本仓库实测踩过两次：测试台服务、上一次未停干净的服务）。
  busy=""
  for port in 8000 8100; do
    if lsof -nP -iTCP:$port -sTCP:LISTEN -t >/dev/null 2>&1; then
      busy="${busy} ${port}($(lsof -nP -iTCP:$port -sTCP:LISTEN -t 2>/dev/null | head -1))"
    fi
  done
  if [ -n "$busy" ]; then
    b "端口被占用：${busy} —— 冒烟无法启服务（先停掉占用进程，或改端口）"
    smoke_out=""
  else
    smoke_out="$(bash "$ROOT/scripts/dev_smoke.sh" --start --with-qa --redline 2>&1)"
  fi
  smoke_tail="$(printf '%s\n' "$smoke_out" | grep -E '^== 结果' | tail -1 || true)"
  if [ -z "$smoke_out" ]; then
    :   # 端口被占用，已在上面记为「待外部条件」
  elif printf '%s' "$smoke_out" | grep -q '冒烟通过'; then
    p "冒烟通过：${smoke_tail:-见 dev_smoke.sh 输出}"
  else
    f "冒烟未通过：$(printf '%s' "$smoke_out" | grep -E '\[FAIL\]' | head -5 | tr '\n' ' ')"
  fi
fi

# ---------------------------------------------------------------------------
stage "6. 备份与恢复演练（01 §13，DoD 要求"已执行一次且可恢复"）"
if [ "$QUICK" = 1 ]; then
  s "--quick：跳过备份演练"
elif [ -x "$PY" ]; then
  bdir="$ROOT/var/backup"
  # 只取 stdout：structlog 走 stderr，混进来会破坏 JSON 解析
  backup_out="$("$PY" -m ekbs.tools.backup backup --out-dir "$bdir" 2>/dev/null)"
  archive="$(printf '%s' "$backup_out" | "$PY" -c '
import json,sys
raw=sys.stdin.read()
i=raw.find("{")
try: print(json.loads(raw[i:raw.rfind("}")+1]).get("archive","") if i>=0 else "")
except Exception: print("")
' 2>/dev/null || true)"
  if [ -n "$archive" ] && [ -f "$archive" ]; then
    p "备份已生成：$(basename "$archive")"
    restore_out="$("$PY" -m ekbs.tools.backup restore "$archive" --target "$ROOT/var/restore-test" 2>/dev/null)"
    if printf '%s' "$restore_out" | grep -q '恢复演练通过'; then
      p "恢复演练通过（sha256 / 29 表 / 12 触发器 / policy_version / 分片 dim 与 ntotal 全部核对）"
    else
      f "恢复演练未通过：$(printf '%s' "$restore_out" | grep -oE '\"problems\": \[[^]]*\]' | head -1 | cut -c1-200)"
    fi
  else
    f "备份未生成（${backup_out}）"
  fi
else
  s "无 python，跳过备份演练"
fi

# ---------------------------------------------------------------------------
stage "7. 合规与外部条件（不计 FAIL，但未就绪不能上真实语料）"
ack="$(grep -E '^[[:space:]]*corpus_egress_ack:' "$EKBS_CONFIG" 2>/dev/null | awk '{print $2}')"
if [ "$ack" = "true" ]; then
  p "corpus_egress_ack=true（已声明接受全量语料出网，00 §7）"
else
  b "corpus_egress_ack≠true：**真实制度语料不得入库**（00 §7 硬闸门）；POC 只能用合成/公开样本"
fi
if [ -n "${DASHSCOPE_API_KEY:-}" ]; then
  p "DASHSCOPE_API_KEY 已注入（可跑真实云端连通性）"
else
  # 措辞要紧：云端 M2/M3 **已经实测通过**（2026-09-27，config/application.cloud.yaml），
  # 这里缺的只是"当前 shell 没导出 key"。早先的文案写成"M2/M3 待做"，会让人误判进度。
  if [ -f "config/application.cloud.yaml" ]; then
    b "当前 shell 未导出 DASHSCOPE_API_KEY：本次无法实时复跑云端链路（M2/M3 已于 2026-09-27 用真实 key 实测通过，见 README §三）"
  else
    b "缺 DASHSCOPE_API_KEY 且无 config/application.cloud.yaml：云端嵌入/rerank/LLM 无法实测（00 §11 的 M2/M3 待做）"
  fi
fi
if [ -n "${DINGTALK_CLIENT_ID:-}" ] && [ -n "${DINGTALK_CLIENT_SECRET:-}" ]; then
  p "钉钉凭据已注入（可做 Stream 联调）"
else
  b "缺钉钉凭据（DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET）：适配器只能跑 not_ready"
fi
# MinerU 可能在独立 venv（推荐：依赖体积/内存峰值大，别污染主 venv），
# 因此不能只看主 venv 能否 import —— 按 EKBS_MINERU_BIN → 仓库内独立 venv → 主 venv 顺序探测。
mineru_bin="${EKBS_MINERU_BIN:-}"
if [ -z "$mineru_bin" ] && [ -x "$ROOT/var/mineru-venv/bin/mineru" ]; then
  mineru_bin="$ROOT/var/mineru-venv/bin/mineru"
fi
if [ -n "$mineru_bin" ] && [ -x "$mineru_bin" ]; then
  p "MinerU 可用（$("$mineru_bin" --version 2>/dev/null | tail -1)）；PDF/图片解析可用（EKBS_MINERU_BIN=${mineru_bin}）"
elif [ -x "$PY" ] && "$PY" -c 'import mineru' >/dev/null 2>&1; then
  p "MinerU 已安装（主 venv 内，PDF/图片解析可用）"
else
  b "未找到 MinerU：真实 PDF 解析不可用（01 §4，00 §11 的 M1）。安装：pip install 'mineru[core]==2.1.10' 且 MINERU_MODEL_SOURCE=modelscope"
fi
if [ -d /opt/ekbs ] && [ -d /data/ekbs ]; then
  p "检测到生产布局（/opt/ekbs、/data/ekbs）"
else
  b "本机不是生产布局（无 /opt/ekbs、/data/ekbs）：四服务真机部署与 RPO/RTO 实测需在 Mac mini 上做"
fi

# ---------------------------------------------------------------------------
printf '\n== 结果 ==\n'
printf '  PASS=%d  FAIL=%d  SKIP=%d  待外部条件=%d\n' "$PASS" "$FAIL" "$SKIP" "$BLOCKED"
if [ "${#FAIL_LINES[@]}" -gt 0 ]; then
  printf '\n  未通过项（先修这些）：\n'
  for line in "${FAIL_LINES[@]}"; do printf '   · %s\n' "$line"; done
fi
if [ "${#BLOCKED_LINES[@]}" -gt 0 ]; then
  printf '\n  待外部条件（不影响开发机判定，但上线前必须解决）：\n'
  for line in "${BLOCKED_LINES[@]}"; do printf '   · %s\n' "$line"; done
fi

verdict="READY"; [ "$FAIL" -gt 0 ] && verdict="NOT-READY"
printf '\n  判定：%s%s\n' "$verdict" \
  "$([ "$BLOCKED" -gt 0 ] && echo "（开发机侧；${BLOCKED} 项待外部条件）" || echo "")"

if [ "$JSON" = 1 ]; then
  "$PY" - "$verdict" "$PASS" "$FAIL" "$SKIP" "$BLOCKED" <<'PY'
import json, sys
verdict, passed, failed, skipped, blocked = sys.argv[1:6]
print(json.dumps({
    "verdict": verdict,
    "pass": int(passed), "fail": int(failed),
    "skip": int(skipped), "blocked_on_external": int(blocked),
}, ensure_ascii=False))
PY
fi

[ "$FAIL" -eq 0 ] || exit 1
exit 0
