#!/usr/bin/env bash
# =============================================================================
# deploy/scripts/install.sh —— EKBS macOS 原生一体化安装（**幂等，可重复执行**）
# -----------------------------------------------------------------------------
# 步骤与权威来源一一对应：
#   §1 目录树      → 01 §1（/opt/ekbs/{app,venv,etc,log,run}、/data/ekbs/{raw,parse,index,backup,tmp}）
#   §2 配置与脚本  → 01 §7（application.yaml）、04 §5.2、04 §2.6、01 §13/§14、04 §7.2/§7.6
#   §3 Python venv → 01 §3（venv + requirements）
#   §4 SQLite 初始化 → 01 §5.7 + 04 §7.1.3（建库 → WAL → integrity_check → 表数/策略版本）
#   §5 launchd     → 01 §8.3（cp 到 ~/Library/LaunchAgents + launchctl unload/load -w）
#   §6 备份作业    → 01 §13（crontab 每日 02:00）
#
# 幂等策略：
#   · 目录 mkdir -p；venv 已存在则复用；requirements 用 sha256 指纹判断是否需要重装；
#   · SQL 脚本全部 IF NOT EXISTS，仅在「库不存在或表数 ≠ 29」时执行；
#   · plist 每次覆盖安装源（deploy/launchd 是唯一事实源）后 unload+load；
#   · application.yaml / secrets.env **不覆盖**已有文件（避免抹掉现场变更与真实密钥）。
#
# 用法：bash deploy/scripts/install.sh [--skip-deps] [--no-load]
#   --skip-deps  跳过 pip 安装（仅创建 venv / 建库 / 装 plist，用于快速验证）
#   --no-load    不执行 launchctl load（仅安装文件，用于 CI 或先装后验）
# =============================================================================
set -euo pipefail

# ---- 路径口径（01 §1；代码 /opt/ekbs/app、配置 /opt/ekbs/etc、日志 /opt/ekbs/log、
#      venv /opt/ekbs/venv、release /opt/ekbs/releases/<时间戳>、数据 /data/ekbs/*）----
OPT=/opt/ekbs
APP="$OPT/app"
ETC="$OPT/etc"
LOG="$OPT/log"
RUN="$OPT/run"
VENV="$OPT/venv"
RELEASES="$OPT/releases"
DATA=/data/ekbs
DB="$DATA/index/ekbs.db"
CFG="$ETC/application.yaml"
SECRETS_DIR="$HOME/.ekbs"
SECRETS="$SECRETS_DIR/secrets.env"
PLIST_DIR="$HOME/Library/LaunchAgents"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # deploy/
SQL_SRC="$ROOT/sql/001_init.sql"
EXPECTED_TABLES=29                                 # 01 §5：29 张表（含 ekbs_chunk_fts 虚拟表）

SKIP_DEPS=0; NO_LOAD=0
usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; }
for a in "$@"; do
  case "$a" in
    --skip-deps) SKIP_DEPS=1 ;;
    --no-load)   NO_LOAD=1 ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "未知参数：$a"; echo "用法：install.sh [--skip-deps] [--no-load]"; exit 2 ;;
  esac
done

step()  { printf '\n==> %s\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
die()   { printf '\n[FATAL] %s\n' "$*" >&2; exit 1; }

# 目录创建：普通 mkdir → 免密 sudo → 打印 01 §1 的原始命令后退出
ensure_dir() {
  local d="$1"
  [ -d "$d" ] && [ -w "$d" ] && return 0
  mkdir -p "$d" 2>/dev/null || true
  [ -d "$d" ] && [ -w "$d" ] && return 0
  if command -v sudo >/dev/null 2>&1 && sudo -n mkdir -p "$d" 2>/dev/null; then
    sudo -n chown -R "$(id -u):$(id -g)" "$d" 2>/dev/null || true
    [ -w "$d" ] && return 0
  fi
  cat >&2 <<EOF

[FATAL] 无法创建或写入目录：$d
        请先以管理员身份执行 01 §1 的目录准备命令，再重跑本脚本：

          sudo mkdir -p $OPT/{app,venv,etc,log,run}
          sudo mkdir -p $DATA/{raw,parse,index,backup,tmp}
          sudo chown -R "\$(id -u):\$(id -g)" $OPT $DATA
          mkdir -p $SECRETS_DIR && chmod 700 $SECRETS_DIR
EOF
  exit 1
}

[ "$(uname -s)" = "Darwin" ] || die "本脚本仅用于 macOS（当前 $(uname -s)）；Linux 迁移路线见 01 §15"

step "0/6 环境预检"
info "macOS $(sw_vers -productVersion)  架构 $(uname -m)  用户 $(id -un)"
if [ "$(uname -m)" != "arm64" ]; then
  info "[WARN] 非 arm64：faiss-cpu 的 Accelerate 加速不可用，且本方案只在 Apple Silicon 上验证过"
fi
for t in sqlite3 plutil launchctl; do
  command -v "$t" >/dev/null 2>&1 || die "缺少系统命令 ${t}（macOS 自带；被删请恢复后再试）"
done

step "1/6 创建目录树（01 §1 + §4 的模型缓存目录）"
for d in "$OPT" "$APP" "$ETC" "$ETC/launchd" "$ETC/sql" "$LOG" "$RUN" "$VENV" "$RELEASES"; do
  ensure_dir "$d"; info "ok  $d"
done
for d in "$DATA" "$DATA/raw" "$DATA/parse" "$DATA/index" "$DATA/backup" "$DATA/tmp" "$DATA/models"; do
  ensure_dir "$d"; info "ok  $d"
done
ensure_dir "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
info "ok  $SECRETS_DIR (700)"

step "2/6 安装配置与脚本到 $ETC"
# 2.1 application.yaml：不覆盖已有文件（现场可能已改过阈值）
if [ -f "$ROOT/config/application.yaml" ]; then
  if [ ! -f "$CFG" ]; then
    cp "$ROOT/config/application.yaml" "$CFG"; info "已安装 $CFG"
  elif cmp -s "$ROOT/config/application.yaml" "$CFG"; then
    info "已存在且与模板一致，跳过 $CFG"
  else
    cp "$ROOT/config/application.yaml" "$CFG.new"
    info "[WARN] $CFG 与模板不同（现场已修改？）。模板已写到 $CFG.new，请人工 diff 合并后再替换"
  fi
else
  die "找不到 $ROOT/config/application.yaml"
fi

# 2.2 secrets.env：只在缺失时用模板初始化，绝不覆盖真实密钥
if [ -f "$SECRETS" ]; then
  info "已存在 ${SECRETS}（不覆盖）"
else
  cp "$ROOT/config/secrets.env.example" "$SECRETS"
  chmod 600 "$SECRETS"
  info "[ACTION] 已生成 ${SECRETS}（占位值 CHANGE_ME_*）—— 必须填入真实凭据后才能启动服务"
fi
chmod 600 "$SECRETS"

# 2.3 SQL 初始化脚本：同时落到 /opt/ekbs/etc/sql 与 /opt/ekbs/app/sql（`01` §5.7 用的是后者）
cp "$SQL_SRC" "$ETC/sql/001_init.sql"; info "已安装 $ETC/sql/001_init.sql"
if [ -d "$APP" ]; then mkdir -p "$APP/sql" 2>/dev/null && cp "$SQL_SRC" "$APP/sql/001_init.sql" 2>/dev/null \
  && info "已安装 $APP/sql/001_init.sql（`01` §5.7 的引用路径）" || true; fi

# 2.4 运维脚本：01 §14 把它们放在 /opt/ekbs/etc/ 下
cp "$ROOT/requirements.txt" "$ETC/requirements.txt"; info "已安装 $ETC/requirements.txt"
for s in check-secrets.sh preflight.sh smoke.sh backup.sh release.sh install.sh; do
  if [ -f "$ROOT/scripts/$s" ]; then
    cp "$ROOT/scripts/$s" "$ETC/$s"; chmod +x "$ETC/$s"; info "已安装 $ETC/$s"
  fi
done
# 敏感词库占位（04 §5.2 的 security.sensitive_words_path 指向它，缺失时 L1 层不生效）
if [ ! -f "$ETC/sensitive_words.txt" ]; then
  printf '# EKBS L1 敏感词库：每行一个词；由 pyahocorasick AC 自动机加载，支持热加载（04 §6.4）\n' > "$ETC/sensitive_words.txt"
  info "已创建空的 $ETC/sensitive_words.txt（请按公司口径补词）"
fi

step "3/6 Python venv 与依赖（01 §3）"
PY_BIN=""
if [ -x "$VENV/bin/python" ]; then
  PY_BIN="$VENV/bin/python"; info "复用已有 venv：$("$PY_BIN" -V 2>&1)"
else
  for cand in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY_BIN="$(command -v "$cand")"; break; fi
  done
  [ -n "$PY_BIN" ] || die "找不到 python3（01 §3：brew install python@3.11 / python@3.12）"
  ver="$("$PY_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  case "$ver" in
    3.12|3.13) info "使用 ${PY_BIN}（Python ${ver}）" ;;
    3.11) info "[WARN] 使用 Python 3.11（01 §3 口径）；requirements.txt 写的是 ≥3.12 —— 两处口径待统一，见交付说明" ;;
    *) die "Python $ver 过旧（需要 ≥3.11）" ;;
  esac
  "$PY_BIN" -m venv "$VENV"; PY_BIN="$VENV/bin/python"
  info "已创建 venv：$("$PY_BIN" -V 2>&1)"
fi
"$PY_BIN" -c 'import sqlite3;c=sqlite3.connect(":memory:");c.execute("CREATE VIRTUAL TABLE t USING fts5(x)");c.execute("SELECT json_array_length(\"[1,2,3]\")")' \
  || die "venv 的 sqlite3 缺少 FTS5/JSON1（01 §3 注：改用 Homebrew Python 重装，或退回 rank-bm25）"
info "FTS5 + JSON1 可用"

if [ "$SKIP_DEPS" = "1" ]; then
  info "[跳过] --skip-deps：不执行 pip 安装"
else
  "$VENV/bin/pip" install -q -U pip wheel setuptools
  REQ_HASH="$(shasum -a 256 "$ROOT/requirements.txt" | awk '{print $1}')"
  MARK="$VENV/.requirements.sha256"
  if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$REQ_HASH" ]; then
    info "requirements.txt 未变化（sha256 ${REQ_HASH:0:12}…），跳过 pip install"
  else
    info "安装依赖（MinerU 首次会拉取较多包，耗时较长）…"
    "$VENV/bin/pip" install -q -r "$ROOT/requirements.txt"
    printf '%s\n' "$REQ_HASH" > "$MARK"
    info "依赖安装完成，已记录指纹 ${REQ_HASH:0:12}…"
  fi
fi

step "4/6 初始化 SQLite（`01` §5.7 + 04 §7.1.3）"
tbl_count() {
  sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') AND name LIKE 'ekbs%' AND name NOT LIKE 'ekbs_chunk_fts_%';" 2>/dev/null || echo -1
}
if [ ! -f "$DB" ]; then
  info "数据库不存在，执行 $SQL_SRC"
  sqlite3 "$DB" < "$SQL_SRC"
else
  cur="$(tbl_count)"
  if [ "$cur" != "$EXPECTED_TABLES" ]; then
    info "当前表数 $cur ≠ ${EXPECTED_TABLES}，重复执行初始化脚本（全部 IF NOT EXISTS，幂等）"
    sqlite3 "$DB" < "$SQL_SRC"
  else
    info "数据库已初始化（表数 ${EXPECTED_TABLES}），跳过建表"
  fi
fi

sqlite3 "$DB" "PRAGMA journal_mode=WAL;" | grep -qi wal && info "journal_mode=WAL 已生效" \
  || die "PRAGMA journal_mode=WAL 未生效（库文件所在卷可能不支持 WAL）"
integ="$(sqlite3 "$DB" 'PRAGMA integrity_check;')"
[ "$integ" = "ok" ] || die "PRAGMA integrity_check 失败：$integ"
info "PRAGMA integrity_check = ok"

final="$(tbl_count)"
[ "$final" = "$EXPECTED_TABLES" ] || die "表数校验失败：期望 ${EXPECTED_TABLES}，实际 ${final}（对照 01 §5 的表清单）"
info "表数校验通过：$final 张（不含 FTS5 的 5 张影子表；`01` §5.7 的校验语句已显式排除影子表）"

pv="$(sqlite3 "$DB" 'SELECT version FROM ekbs_policy_version;')"
[ "$pv" = "1" ] || die "ekbs_policy_version.version 期望 1，实际 '$pv'（01 §5.3 的种子数据缺失）"
info "ekbs_policy_version.version = 1"
info "注意：foreign_keys 是连接级 PRAGMA，需由 ekbs/db.py 在每次建连时设置（01 §5.0、04 §7.1.2）"

step "5/6 安装 launchd 服务（01 §8.3）"
mkdir -p "$PLIST_DIR"
for s in api dingtalk worker scheduler; do
  src="$ROOT/launchd/com.ekbs.$s.plist"
  [ -f "$src" ] || die "找不到 $src"
  plutil -lint "$src" >/dev/null || die "plist 语法错误：$src"
  cp "$src" "$ETC/launchd/com.ekbs.$s.plist"     # 01 §8.1 的规格存放路径（preflight 会核对这里）
  cp "$src" "$PLIST_DIR/com.ekbs.$s.plist"
  if [ "$NO_LOAD" = "1" ]; then
    info "已安装 $PLIST_DIR/com.ekbs.$s.plist（--no-load，未加载）"
    continue
  fi
  launchctl unload "$PLIST_DIR/com.ekbs.$s.plist" 2>/dev/null || true
  launchctl load -w "$PLIST_DIR/com.ekbs.$s.plist"
  info "已加载 com.ekbs.$s"
done

if [ "$NO_LOAD" = "0" ]; then
  sleep 2
  info "launchctl list | grep ekbs ："
  launchctl list | grep ekbs | sed 's/^/      /' || info "[WARN] 未看到 ekbs 服务（若 secrets.env 仍是占位值，进程会启动即失败，见 01 §8.1 的注入纪律）"
fi

step "6/6 备份作业（01 §13，每日 02:00）"
if [ "$NO_LOAD" = "1" ]; then
  info "[跳过] --no-load"
else
  CRON_LINE="0 2 * * * $ETC/backup.sh >> $LOG/backup.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "$ETC/backup.sh"; then
    info "crontab 已存在备份作业，跳过"
  else
    ( crontab -l 2>/dev/null || true; echo "$CRON_LINE" ) | crontab -
    info "已写入 crontab：$CRON_LINE"
  fi
fi

cat <<EOF

================================================================================
安装完成（幂等，可重复执行）
--------------------------------------------------------------------------------
后续必做：
  1) 填密钥：编辑 ${SECRETS}，把 CHANGE_ME_* 全部替换为真实值
     （DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / 钉钉 4 项 / EKBS_JWT_SECRET / EKBS_INTERNAL_TOKEN）
  2) 校验：bash $ETC/preflight.sh            # 逐条 [OK]/[FAIL]
  3) 重启：bash $ETC/release.sh \$(readlink $APP)   # 或逐个 launchctl kickstart -k
  4) 冒烟：bash $ETC/smoke.sh                 # 需要真实问答时加 --with-qa
  5) 首次入库前：python -m ekbs.tools.new_shard --shard-id kb_hr_L2 --dim 1024（01 §6.3）
  6) 组织全量同步：python -m ekbs.tools.sync_org --mode full（01 §10）

提醒：本机最大风险是磁盘故障 —— 01 §13 要求配置外接盘/NAS 同步或启用 Time Machine，
      并每季度在 /data/ekbs/restore-test/ 做一次恢复演练。
================================================================================
EOF
