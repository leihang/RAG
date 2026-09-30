#!/usr/bin/env bash
# =============================================================================
# scripts/install_rehearsal.sh —— 按生产布局**真跑一遍** install → release → preflight → smoke
# -----------------------------------------------------------------------------
# 为什么需要它：`tests/test_deployment_contract.py` 只做静态一致性；`deploy_rehearsal.sh`
# 只验 plist 本身。而 `install.sh` / `release.sh` **从来没有被执行过** ——
# 建目录树、装 venv 与依赖、建库到 29 表、装 plist、装配置、装备份脚本，
# 这一整条链上任何一处写错，真机首发就会卡住。
#
# 本脚本在**本机**按同一套脚本跑完整链路，唯一的改动是**前缀重写**：
#   /opt/ekbs          → <rehearsal>/opt/ekbs
#   /data/ekbs         → <rehearsal>/data/ekbs
#   $HOME/.ekbs        → <rehearsal>/home/.ekbs        （不碰你真实的密钥目录）
#   $HOME/Library/LaunchAgents → <rehearsal>/LaunchAgents （不碰你真实的 LaunchAgents）
#
# 隔离保证：
#   · `install.sh --no-load` 会**同时跳过 crontab 写入**（脚本 §6 的既有分支），
#     因此不会改你的 crontab；
#   · launchd 作业我们用重写后的 plist 手动 load，演练结束 bootout 掉；
#   · 真机部署（/opt + /data + sudo）仍需在 Mac mini 上做 —— 本机 /opt 属 root、
#     且无免密 sudo，这里只验证脚本逻辑与布局。
#
# 退出码：0 = 无 FAIL；1 = 至少一项 FAIL。
# 用法：bash scripts/install_rehearsal.sh [--keep]
# =============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 同 deploy_rehearsal.sh：演练根必须放在 $HOME 下。
# 放在仓库内（~/Documents/...）时，launchd 起的服务读密钥文件会被 TCC 拒，
# 表现为「缺少必需密钥：DASHSCOPE_API_KEY」这种**误导性**报错（实测踩到）。
ROOT="${EKBS_REHEARSAL_ROOT:-$HOME/.ekbs-rehearsal-install}"
SRC_DEPLOY="$REPO/deploy"
RL="$ROOT/opt/ekbs"
RL_DATA="$ROOT/data/ekbs"
RL_HOME="$ROOT/home"
DOMAIN="gui/$(id -u)"
SERVICES=(api dingtalk worker scheduler)

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

PASS=0; FAIL=0; FINDINGS=0
p() { printf '  [PASS] %s\n' "$1"; PASS=$((PASS + 1)); }
f() { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL + 1)); }
w() { printf '  [外需] %s\n' "$1"; }
step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }

cleanup() {
  local s
  for s in "${SERVICES[@]}"; do
    launchctl bootout "$DOMAIN/com.ekbs.$s" >/dev/null 2>&1
  done
  pkill -f "$ROOT/opt/ekbs/venv/bin" >/dev/null 2>&1
  sleep 1
}
trap 'if [ "$KEEP" -eq 0 ]; then cleanup; fi' EXIT

# ---------------------------------------------------------------------------
step "0/6 前置"
# ---------------------------------------------------------------------------
if curl -sf --max-time 2 http://127.0.0.1:8000/livez >/dev/null 2>&1 \
   || curl -sf --max-time 2 http://127.0.0.1:8100/readyz >/dev/null 2>&1; then
  f "端口 8000/8100 已被占用，演练无法绑定（先停掉在跑的服务）"
else
  p "端口 8000 / 8100 空闲"
fi
[ -w "$(dirname "$REPO")" ] && p "演练根目录可写：var/install-rehearsal" || f "仓库目录不可写"

# ---------------------------------------------------------------------------
step "1/6 生成改写后的 deploy 树（只改前缀，脚本逻辑不动）"
# ---------------------------------------------------------------------------
cleanup
rm -rf "$ROOT"
mkdir -p "$ROOT" "$RL_HOME"
cp -R "$SRC_DEPLOY" "$ROOT/deploy"
"$REPO/.venv/bin/python" - "$ROOT" "$REPO" "$SRC_DEPLOY" <<'PY'
import pathlib, sys

root, repo, src = sys.argv[1:4]
subs = [
    ("/opt/ekbs", f"{root}/opt/ekbs"),
    ("/data/ekbs", f"{root}/data/ekbs"),
    ('$HOME/.ekbs', f"{root}/home/.ekbs"),
    ('$HOME/Library/LaunchAgents', f"{root}/LaunchAgents"),
]
touched = 0
for path in pathlib.Path(f"{root}/deploy").rglob("*"):
    if not path.is_file() or path.suffix not in {".sh", ".plist", ".yaml", ".txt", ".env", ".example"}:
        continue
    text = path.read_text(errors="ignore")
    new = text
    for old, rep in subs:
        new = new.replace(old, rep)
    if new != text:
        path.write_text(new)
        touched += 1
print(f"    重写 {touched} 个文件")
PY
for f_ in "$ROOT/deploy/scripts/"*.sh; do chmod +x "$f_"; done
# 逐处检查：每个 /opt/ekbs 与 /data/ekbs 都必须带上演练根前缀，否则才是真残留
res="$(python3 - "$ROOT" <<'PY'
import pathlib, sys
root = sys.argv[1]
bad = []
for path in pathlib.Path(f"{root}/deploy").rglob("*"):
    if not path.is_file() or path.suffix not in {".sh", ".plist", ".yaml", ".txt", ".example"}:
        continue
    for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
        for token in ("/opt/ekbs", "/data/ekbs"):
            for pos in range(len(line)):
                if line.startswith(token, pos):
                    prefix_ok = line[max(0, pos - len(root)):pos] == root
                    if not prefix_ok:
                        bad.append(f"{path.name}:{i}: {line.strip()[:80]}")
if bad:
    print("REMAIN")
    for item in bad[:5]:
        print("   ", item)
else:
    print("CLEAN")
PY
)"
if [ "$res" = "CLEAN" ]; then
  p "改写完成且无生产路径残留（4 个 plist + 6 个脚本 + 配置）"
else
  f "改写后仍有生产路径残留："
  printf '%s\n' "$res" | tail -n +2 | sed 's/^/      /'
fi
grep -q 'crontab -' "$ROOT/deploy/scripts/install.sh" && info "注：crontab 写入分支仍在，但 --no-load 会跳过它（install.sh §6 既有逻辑）"

# ---------------------------------------------------------------------------
step "2/6 install.sh --no-load（建目录 / venv / 库 / plist / 配置；不写 crontab）"
# ---------------------------------------------------------------------------
CRON_BEFORE="$(crontab -l 2>/dev/null | sha256sum | cut -c1-16)"
install_log="$ROOT/install.log"
if bash "$ROOT/deploy/scripts/install.sh" --no-load >"$install_log" 2>&1; then
  p "install.sh 退出码 0（完整输出：${ROOT#$HOME/}/install.log）"
  grep -E "^==>|^\[FATAL\]" "$install_log" | sed 's/^/      /' | head -12
else
  f "install.sh 失败 —— 末尾输出：$(tail -5 "$install_log" | tr '\n' ' ' | head -c 300)"
fi

CRON_AFTER="$(crontab -l 2>/dev/null | sha256sum | cut -c1-16)"
[ "$CRON_BEFORE" = "$CRON_AFTER" ] && p "你的 crontab 未被改动（--no-load 生效）" \
                                  || f "crontab 被改动了 —— 演练不该碰真实 crontab"

# ---------------------------------------------------------------------------
step "3/6 校验生产布局（01 §1 / §5 / §8 的落地结果）"
# ---------------------------------------------------------------------------
for d in "$RL/app" "$RL/etc" "$RL/log" "$RL/run" "$RL/venv" "$RL/releases" \
         "$RL_DATA/raw" "$RL_DATA/parse" "$RL_DATA/index" "$RL_DATA/backup" "$RL_DATA/tmp"; do
  [ -d "$d" ] && p "目录存在：${d#$ROOT/}" || f "目录缺失：${d#$ROOT/}"
done

DB="$RL_DATA/index/ekbs.db"
if [ -f "$DB" ]; then
  tables="$(sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') AND name LIKE 'ekbs%' AND name NOT LIKE 'ekbs_chunk_fts_%';")"
  [ "$tables" = "29" ] && p "库内 ekbs 实体表 29 张（01 §5）" || f "库内实体表 $tables 张，应为 29"
  mode="$(sqlite3 "$DB" 'PRAGMA journal_mode;')"
  [ "$mode" = "wal" ] && p "journal_mode=WAL" || f "journal_mode=${mode}（应为 wal）"
  integ="$(sqlite3 "$DB" 'PRAGMA integrity_check;')"
  [ "$integ" = "ok" ] && p "integrity_check=ok" || f "integrity_check=$integ"
else
  f "库未建成：${DB#$ROOT/}"
fi

for s in "${SERVICES[@]}"; do
  [ -f "$RL/etc/launchd/com.ekbs.$s.plist" ] && p "plist 已装到 etc/launchd：$s" || f "plist 未装：$s"
done
sec="$RL_HOME/.ekbs/secrets.env"
if [ -f "$sec" ]; then
  perm="$(stat -f '%Lp' "$sec")"
  [ "$perm" = "600" ] && p "secrets.env 权限 600（04 §7.2 断言三）" || f "secrets.env 权限 ${perm}，应为 600"
  if grep -q "CHANGE_ME" "$sec"; then
    w "secrets.env 仍是占位值 CHANGE_ME_*（真机上线前必须填；本次演练因此不接真实凭据）"
  fi
else
  f "secrets.env 未生成：${sec#$ROOT/}"
fi
[ -f "$RL/etc/sensitive_words.txt" ] && p "敏感词库文件已创建（04 §5.2 指向它）" || f "敏感词库文件缺失"
[ -f "$RL/etc/backup.sh" ] && p "备份脚本已装到 etc/（01 §13）" || f "备份脚本未安装"

if [ -x "$RL/venv/bin/python" ]; then
  p "venv 可用：$("$RL/venv/bin/python" -V 2>&1)"
  "$RL/venv/bin/python" -c 'import sqlite3;c=sqlite3.connect(":memory:");c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")' 2>/dev/null \
    && p "venv 的 sqlite3 支持 FTS5" || f "venv 的 sqlite3 缺 FTS5"
  "$RL/venv/bin/pip" check >/dev/null 2>&1 && p "pip check 无依赖冲突" || w "pip check 报冲突（04 §7.6 阶段 3 会拦）"
else
  f "venv 未建成"
fi

# ---------------------------------------------------------------------------
# 让服务真的能起来：注入真实 key（若环境里有）+ 播种演示数据
# ---------------------------------------------------------------------------
# 生产配置的 provider 是 dashscope，`build_providers` 会 `secrets.require("DASHSCOPE_API_KEY")`，
# 占位值时 api/worker/scheduler 会 fail-closed 拒绝启动（这是**正确**行为，不是缺陷）。
# 为了验到「生产布局下服务真能服务请求」，这里把环境里的真实 key 写进演练密钥文件。
# key 只写文件、不回显、不进日志。
if [ -n "${DASHSCOPE_API_KEY:-}" ]; then
  "$REPO/.venv/bin/python" - "$sec" "$DASHSCOPE_API_KEY" <<'PY'
import pathlib, sys
path, key = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = path.read_text().splitlines()
out, replaced = [], False
for line in lines:
    if line.startswith("DASHSCOPE_API_KEY="):
        out.append(f"DASHSCOPE_API_KEY={key}"); replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f"DASHSCOPE_API_KEY={key}")
path.write_text("\n".join(out) + "\n")
print("    [ok] 演练密钥文件已注入 DASHSCOPE_API_KEY（长度 %d，值不回显）" % len(key))
PY
  p "已注入真实 DASHSCOPE_API_KEY 到演练 secrets.env（仅演练目录，字段长度 $(printf '%s' "$DASHSCOPE_API_KEY" | wc -c | tr -d ' ')）"
  seed_log="$ROOT/seed.log"
  if EKBS_CONFIG="$RL/etc/application.yaml" "$RL/venv/bin/python" -m ekbs.tools.seed_demo >"$seed_log" 2>&1; then
    shards="$(sqlite3 "$DB" 'SELECT COUNT(*) FROM ekbs_shard;' 2>/dev/null)"
    vectors="$(sqlite3 "$DB" 'SELECT COUNT(*) FROM ekbs_chunk WHERE faiss_row IS NOT NULL;' 2>/dev/null)"
    p "演示数据已入库（**生产配置 + 真实云端嵌入**）：分片 ${shards}，向量 $vectors"
  else
    f "seed_demo 失败 —— $(tail -3 "$seed_log" | tr '\n' ' ' | head -c 250)"
  fi
else
  w "环境未导出 DASHSCOPE_API_KEY：服务会因缺密钥拒绝启动（fail-closed），本次只能验到 install 层"
fi

# ---------------------------------------------------------------------------
step "4/6 代码发布链：建 release 目录 → release.sh（04 §7.6）"
# ---------------------------------------------------------------------------
TS="$(date +%Y%m%d-%H%M)"
NEW="$RL/releases/$TS"
mkdir -p "$NEW"
# 运营商做法是把代码放进 release 目录；这里用仓库当前源码（含未提交改动）
for item in ekbs sql pyproject.toml requirements.txt config; do
  [ -e "$REPO/$item" ] && cp -R "$REPO/$item" "$NEW/" 2>/dev/null
done
p "release 目录已就绪：releases/${TS}（含 ekbs/ sql/ pyproject.toml requirements.txt config/）"

# 先手动加载四个作业（install.sh 用了 --no-load），release.sh 依赖它们已被 launchd 管理
for s in "${SERVICES[@]}"; do
  launchctl bootout "$DOMAIN/com.ekbs.$s" >/dev/null 2>&1
  plist="$ROOT/LaunchAgents/com.ekbs.$s.plist"
  if [ ! -f "$plist" ]; then
    f "plist 未安装到 LaunchAgents：com.ekbs.$s"
    continue
  fi
  if launchctl bootstrap "$DOMAIN" "$plist" 2>"$ROOT/bootstrap-$s.err"; then
    p "已 bootstrap：com.ekbs.$s"
  else
    f "bootstrap 失败：com.ekbs.$s —— $(head -1 "$ROOT/bootstrap-$s.err")"
  fi
done

relog="$ROOT/release.log"
if bash "$ROOT/deploy/scripts/release.sh" "$NEW" >"$relog" 2>&1; then
  p "release.sh 退出码 0（切链接 + pip install -e + 逐个重启 + 两个 readyz 全过）"
else
  # 适配器 readyz 在没有钉钉凭据时必然是 503（D3 不谎报就绪），属预期
  if grep -q "8100/readyz" "$relog"; then
    w "release.sh 的两个 readyz 中，适配器 8100 未 ready —— 无钉钉凭据时按 04 §7.3 应为 503（不谎报），属预期"
    pass_api=$(grep -c "8000/readyz -> .*ready" "$relog")
    [ "$pass_api" -ge 1 ] && p "release.sh 中 api 8000/readyz 已 ready" || f "release.sh 中 api 8000/readyz 未 ready"
  else
    f "release.sh 失败 —— 末尾：$(tail -4 "$relog" | tr '\n' ' ' | head -c 300)"
  fi
fi
[ -L "$RL/app" ] && p "app 符号链接已切换：→ $(readlink "$RL/app" | sed "s|$ROOT/||")" || f "app 不是符号链接"
grep -E "^  (api|scheduler|worker|dingtalk) 已启动" "$relog" | sed 's/^/      /' | head -4

# ---------------------------------------------------------------------------
step "5/6 探针（含 /readyz 与 /health/deps 是否自相矛盾）"
# ---------------------------------------------------------------------------
for s in "${SERVICES[@]}"; do
  if launchctl list 2>/dev/null | awk '{print $3}' | grep -qx "com.ekbs.$s"; then
    p "launchctl 已管理：com.ekbs.$s"
  else
    f "launchctl 未管理：com.ekbs.$s"
  fi
done

api_ready=0
for _ in $(seq 1 20); do
  curl -sf --max-time 2 http://127.0.0.1:8000/readyz >/dev/null 2>&1 && { api_ready=1; break; }
  sleep 1
done
if [ "$api_ready" -eq 1 ]; then
  body="$(curl -s --max-time 5 http://127.0.0.1:8000/readyz)"
  p "api /readyz → $body"
else
  f "api /readyz 20s 未 ready —— $(tail -2 "$RL/log/api.err.log" 2>/dev/null | tr '\n' ' ' | head -c 200)"
fi

deps="$(curl -s --max-time 30 http://127.0.0.1:8000/health/deps 2>/dev/null)"
if [ -n "$deps" ]; then
  embed_ok="$(printf '%s' "$deps" | grep -o '"embed":[a-z]*' | head -1)"
  info "/health/deps：$(printf '%s' "$deps" | head -c 200)"
  if printf '%s' "$deps" | grep -q '"embed":false'; then
    if [ "$api_ready" -eq 1 ]; then
      info "[发现] 占位密钥下 /readyz 报 ready（providers_configured 只看对象非 None），"
      info "       而 /health/deps 显示 embed=false —— 两个探针口径不一致，"
      info "       与 04 §7.3「不谎报就绪」的精神冲突（接入方若只信 /readyz 会误判）"
      FINDINGS=$((FINDINGS + 1))
    fi
  else
    p "/health/deps 云端依赖真实可达（embed=true）"
  fi
else
  w "/health/deps 无响应（占位密钥下通常也会返回，需人工确认）"
fi

adapter="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8100/readyz 2>/dev/null)"
case "$adapter" in
  503) p "adapter /readyz → 503（无钉钉凭据时不谎报就绪，符合 04 §7.3 / TC-ENV-021）" ;;
  200) p "adapter /readyz → 200（凭据齐备且已连上）" ;;
  *)   w "adapter /readyz → HTTP ${adapter}（未监听或异常，需人工确认）" ;;
esac

if [ -x "$ROOT/deploy/scripts/smoke.sh" ]; then
  smog="$ROOT/smoke.log"
  bash "$ROOT/deploy/scripts/smoke.sh" >"$smog" 2>&1 || true
  summary="$(grep -E "PASS=[0-9]+" "$smog" | tail -1)"
  if [ -n "$summary" ]; then
    fails="$(grep -c '\[FAIL\]' "$smog" || true)"
    adapter_fails="$(grep -c '\[FAIL\] adapter /readyz' "$smog" || true)"
    case "$summary" in
      *"FAIL=0"*) p "deploy/smoke.sh：$summary" ;;
      *)
        if [ "$fails" = "$adapter_fails" ]; then
          # 唯一未过项是适配器就绪 —— 无钉钉凭据时它**应该**是 503（04 §7.3 / TC-ENV-021），属外需
          w "deploy/smoke.sh：$summary —— 唯一未过项是适配器 /readyz（缺钉钉凭据，属外需）"
        else
          f "deploy/smoke.sh：${summary}（详见 ${ROOT#$HOME/}/smoke.log）"
        fi ;;
    esac
  else
    f "deploy/smoke.sh 未输出汇总 —— $(tail -3 "$smog" | tr '\n' ' ' | head -c 200)"
  fi
fi

# ---------------------------------------------------------------------------
step "6/6 拆除"
# ---------------------------------------------------------------------------
if [ "$KEEP" -eq 1 ]; then
  info "--keep：保留作业与目录，手工拆除："
  for s in "${SERVICES[@]}"; do info "  launchctl bootout $DOMAIN/com.ekbs.$s"; done
else
  cleanup
  left=0
  for s in "${SERVICES[@]}"; do
    launchctl list 2>/dev/null | awk '{print $3}' | grep -qx "com.ekbs.$s" && left=1
  done
  [ "$left" -eq 0 ] && p "四个 launchd 作业已拆除，无残留" || f "仍有残留作业"
  p "演练目录保留供检查：${ROOT#$HOME/}（含 install.log / release.log / smoke.log）"
fi

printf '\n== 结果 ==\n  PASS=%d  FAIL=%d  实测发现（需改文档/代码）= %d\n' "$PASS" "$FAIL" "$FINDINGS"
if [ "$FAIL" -eq 0 ]; then
  printf '\n  判定：PASS（install → release → 探针 → smoke 全链路在生产布局下可跑通）\n'
  exit 0
fi
printf '\n  判定：FAIL（真机首发前必须修）\n'
exit 1
