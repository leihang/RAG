#!/usr/bin/env bash
# =============================================================================
# deploy/scripts/preflight.sh —— 上线前静态检查（只读，不修改任何状态）
# -----------------------------------------------------------------------------
# 来源：
#   · 04-钉钉接入方案.md §7.2 的四条硬检查（host 回环 / workers=1 / secrets.env 600 /
#     SQLite 可读）—— 断言内容逐字保留；
#   · 01-实施步骤与本地部署.md §0.1（机型与磁盘）、§1（目录）、§3（Python）、§8（plist）。
#
# 与 04 §7.2 节选的差异：原文用 `set -euo pipefail` + `exit 1` 在**第一条**失败就退出。
# 本脚本要「逐条打印 [OK]/[FAIL] 后统一退出」，因此对检查函数关闭 errexit
# （否则第一处 grep 不匹配就会中断脚本），并在末尾按 FAIL 数决定退出码。
#
# 退出码：0 = 全部通过（WARN 不影响）；1 = 至少一条 FAIL。
# 用法：bash /opt/ekbs/etc/preflight.sh   （或直接执行）
# =============================================================================
set -uo pipefail

# ---- 路径口径（01 §1 / §8）----
OPT=/opt/ekbs
APP="$OPT/app"
ETC="$OPT/etc"
LOG="$OPT/log"
VENV="$OPT/venv"
DATA=/data/ekbs
DB="$DATA/index/ekbs.db"
CFG="$ETC/application.yaml"
SECRETS="$HOME/.ekbs/secrets.env"
PLIST_DIR="$HOME/Library/LaunchAgents"

OK=0; WARN=0; FAIL=0
ok()   { printf '  [OK]   %s\n' "$1"; OK=$((OK+1)); }
warn() { printf '  [WARN] %s\n' "$1"; WARN=$((WARN+1)); }
ng()   { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }

# check <描述> <命令...>：命令成功即 OK（静默执行，失败细节由后续专项检查给出）
check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else ng "$desc"; fi
}

# 版本比较：ver_ge <实际> <要求> → 实际 ≥ 要求 时返回 0（用 awk 做数值分段比较，不依赖 sort -V）
ver_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN{
    n=split(a,A,"."); m=split(b,B,".");
    for(i=1;i<=(n>m?n:m);i++){ x=A[i]+0; y=B[i]+0; if(x>y) exit 0; if(x<y) exit 1 }
    exit 0
  }'
}

echo "== EKBS preflight =="
echo "  主机: $(hostname)   用户: $(id -un)   时间: $(date '+%F %T')"
echo

echo "== 1. 系统与硬件（01 §0.1）=="
os_ver="$(sw_vers -productVersion 2>/dev/null || echo 0)"
if ver_ge "$os_ver" "13.0"; then
  ok "macOS 版本 ${os_ver}（要求 ≥ 13.0；方案目标机为 26.6.2）"
else
  ng "macOS 版本 $os_ver < 13.0（launchd/plist 与依赖不保证可用）"
fi

arch="$(uname -m)"
if [ "$arch" = "arm64" ]; then ok "芯片架构 ${arch}（Apple Silicon，faiss-cpu 走 Accelerate）"
else ng "芯片架构 $arch ≠ arm64（本方案仅支持 Apple Silicon）"; fi

if command -v pmset >/dev/null 2>&1 && pmset -g 2>/dev/null | grep -q "sleep[[:space:]]*0"; then
  ok "系统睡眠已禁用（pmset sleep 0，01 §2）"
else
  warn "未确认已禁用睡眠（应执行 sudo pmset -a sleep 0 disablesleep 1，01 §2）"
fi

free_gb="$(df -g / 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "${free_gb:-}" ] && [ "$free_gb" -ge 45 ] 2>/dev/null; then
  ok "根分区可用空间 ${free_gb} GB（≥ 45 GB，覆盖 00 §5 的 12–41 GB 需求 + 余量）"
elif [ -n "${free_gb:-}" ] && [ "$free_gb" -ge 12 ] 2>/dev/null; then
  warn "根分区可用空间 ${free_gb} GB 仅够下限（00 §5 需求 12–41 GB；01 §0.1 要求 ≥256 GB 建议 ≥512 GB）"
else
  ng "根分区可用空间不足（当前 ${free_gb:-未知} GB；00 §8 的 O7 磁盘是唯一阻塞项）"
fi

echo
echo "== 2. Python / SQLite / 目录（01 §1 / §3 / §4）=="
if [ -x "$VENV/bin/python" ]; then
  py_ver="$("$VENV/bin/python" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo 0.0.0)"
  py_mm="$(printf '%s' "$py_ver" | awk -F. '{print $1"."$2}')"
  if ver_ge "$py_mm" "3.12"; then
    ok "venv Python ${py_ver}（≥ 3.12，deploy/requirements.txt 口径）"
  elif ver_ge "$py_mm" "3.11"; then
    warn "venv Python $py_ver —— 01 §3 用 python@3.11，requirements.txt 口径写的是 ≥3.12；请二选一并固定"
  else
    ng "venv Python $py_ver < 3.11（低于 MinerU 与依赖的兼容下限）"
  fi
  if "$VENV/bin/python" -c 'import sqlite3;c=sqlite3.connect(":memory:");c.execute("CREATE VIRTUAL TABLE t USING fts5(x)");c.execute("SELECT json_array_length(\"[1,2,3]\")")' >/dev/null 2>&1; then
    ok "venv Python 内置 sqlite3 支持 FTS5 + JSON1（`01` §3 / §5.7）"
  else
    ng "venv Python 的 sqlite3 缺少 FTS5 或 JSON1（01 §3 注：需重装 Python 或改用 rank-bm25）"
  fi
else
  ng "$VENV/bin/python 不存在（先执行 deploy/scripts/install.sh 创建 venv）"
fi

if command -v sqlite3 >/dev/null 2>&1; then
  sq_ver="$(sqlite3 --version | awk '{print $1}')"
  if ver_ge "$sq_ver" "3.35.0"; then ok "sqlite3 ${sq_ver}（≥ 3.35，满足 RETURNING / upsert）"
  else ng "sqlite3 $sq_ver < 3.35（应用层的 RETURNING 与 ON CONFLICT DO UPDATE 不可用）"; fi
else
  ng "找不到 sqlite3 命令（macOS 自带 /usr/bin/sqlite3；被删则需 brew install sqlite）"
fi

for d in "$OPT" "$OPT/run" "$LOG" "$DATA" "$DATA/raw" "$DATA/parse" "$DATA/index" "$DATA/backup" "$DATA/tmp"; do
  if [ -d "$d" ] && [ -w "$d" ]; then ok "目录可写 $d"
  else ng "目录不存在或不可写 ${d}（见 01 §1 的目录创建与 chown）"; fi
done
[ -d "$DATA/models" ] && ok "MinerU 模型目录存在 $DATA/models（01 §4）" \
                      || warn "缺少 $DATA/models（首次跑 MinerU 前需 mkdir -p，01 §4）"

echo
echo "== 3. 配置与密钥（04 §7.2 / §2.6）=="
if [ -f "$CFG" ]; then
  ok "配置文件存在 $CFG"
  # 04 §7.2 断言一：server.host 与 adapter.host 必须为回环
  if grep -qE '^[[:space:]]*host:[[:space:]]*127\.0\.0\.1' "$CFG"; then
    ok "监听地址为回环 127.0.0.1（server.host / adapter.host；对外只有 Caddy）"
  else
    ng "未找到 'host: 127.0.0.1'（server.host / adapter.host 必须为回环，04 §7.2）"
  fi
  # 04 §7.2 断言二：workers 必须为 1
  if grep -qE '^[[:space:]]*workers:[[:space:]]*1' "$CFG"; then
    ok "server.workers = 1（权限缓存单进程前提，04 §1.5）"
  else
    ng "server.workers 必须为 1（多 worker 会出现 ≤300s 旧权限窗口，04 §7.2）"
  fi
  if grep -qE '^[[:space:]]*l4_enabled:[[:space:]]*false' "$CFG"; then
    ok "permission.l4_enabled = false（首期 L4 不入库）"
  else
    warn "permission.l4_enabled 非 false：请确认是否已按 00 D11 启用 L4"
  fi
  if grep -qE '^[[:space:]]*l3_ingest_enabled:[[:space:]]*false' "$CFG"; then
    ok "permission.l3_ingest_enabled = false（C5 未验收前禁止真实 L3 入库）"
  else
    warn "l3_ingest_enabled=true：须已落地 00 C5 / 03 §5.2.1 并有 TC-PERM-033 通过记录"
  fi
  if grep -qE '^[[:space:]]*corpus_egress_ack:[[:space:]]*true' "$CFG"; then
    ok "corpus_egress_ack = true（已声明接受全量语料出网，00 §7）"
  else
    warn "corpus_egress_ack=false：真实制度语料不得做云端嵌入；POC 仅用合成/公开样本（00 §7）"
  fi
  if grep -qE '^[[:space:]]*port:[[:space:]]*8000' "$CFG" && grep -qE '^[[:space:]]*port:[[:space:]]*8100' "$CFG"; then
    ok "端口口径：api 8000 / adapter 8100"
  else
    ng "端口口径不符（应为 api 127.0.0.1:8000、adapter 127.0.0.1:8100）"
  fi
else
  ng "配置文件不存在 ${CFG}（install.sh 会从 deploy/config/application.yaml 安装）"
fi

# 04 §7.2 断言三：secrets.env 权限必须恰为 600
if [ -f "$SECRETS" ]; then
  sp="$(stat -f '%Lp' "$SECRETS")"
  if [ "$sp" = "600" ]; then ok "secrets.env 权限 600"
  else ng "$SECRETS 权限必须为 600，当前 ${sp}（04 §2.6 硬纪律①）"; fi
  dperm="$(stat -f '%Lp' "$HOME/.ekbs")"
  if [ "$dperm" = "700" ]; then ok "~/.ekbs 目录权限 700"
  else warn "~/.ekbs 权限为 ${dperm}，建议 chmod 700（01 §1）"; fi
else
  ng "缺少 ${SECRETS}（复制 deploy/config/secrets.env.example 并填入真实值）"
fi

# 04 §7.2 断言四：SQLite 可读
if [ -r "$DB" ]; then
  if sqlite3 "$DB" 'SELECT 1' >/dev/null 2>&1; then ok "SQLite 可读 $DB"
  else ng "SQLite 存在但无法查询（文件损坏或权限不足）：$DB"; fi
else
  ng "SQLite 不可读 ${DB}（先执行 `01` §5.7 的初始化；install.sh 会自动完成）"
fi

echo
echo "== 4. launchd 四个 plist（01 §8 / 04 §7.1）=="
for s in api dingtalk worker scheduler; do
  src="$ETC/launchd/com.ekbs.$s.plist"
  dst="$PLIST_DIR/com.ekbs.$s.plist"
  [ -f "$src" ] && ok "源 plist 存在 $src" || ng "源 plist 缺失 $src"
  if [ -f "$dst" ]; then
    if plutil -lint "$dst" >/dev/null 2>&1; then ok "已安装且 plutil -lint 通过：$dst"
    else ng "已安装但 plutil -lint 失败：$dst"; fi
  else
    ng "未安装到 ${dst}（install.sh 会执行 cp + launchctl load -w）"
  fi
done

loaded="$(launchctl list 2>/dev/null | awk '$3 ~ /^com\.ekbs\./ {n++} END {print n+0}')"
if [ "$loaded" -eq 4 ]; then ok "launchctl 已加载 4 个 ekbs 服务"
else warn "launchctl 当前仅加载 $loaded/4 个 ekbs 服务（install.sh 末尾会 load；或服务已停止）"; fi

echo
echo "== 5. 凭据纪律（04 §2.6）=="
if [ -x "$(dirname "$0")/check-secrets.sh" ]; then
  if "$(dirname "$0")/check-secrets.sh" >/dev/null 2>&1; then ok "check-secrets.sh 通过（权限 600 / 未被 git 跟踪 / plist 无明文）"
  else ng "check-secrets.sh 失败：$(cd "$(dirname "$0")" && ./check-secrets.sh 2>&1 | tail -1)"; fi
else
  warn "未找到可执行的 check-secrets.sh，跳过凭据纪律断言"
fi

echo
echo "== 结果：OK=$OK  WARN=$WARN  FAIL=$FAIL =="
if [ "$FAIL" -gt 0 ]; then
  echo "preflight 未通过：请先修复上述 [FAIL] 项（对应 01/04 的章节已标注在每条后面）"
  exit 1
fi
echo "preflight 通过"
