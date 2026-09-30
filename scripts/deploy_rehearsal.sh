#!/usr/bin/env bash
# =============================================================================
# scripts/deploy_rehearsal.sh —— 用**真实 launchd** 演练 01 §8 的四服务
# -----------------------------------------------------------------------------
# 为什么需要它：`tests/test_deployment_contract.py` 只能证明「plist 的命令行与
# entrypoint 对得上」这类**静态**一致性；它证不了那条命令行**真的能把服务拉起来** ——
# 而真机首次部署最可能踩的恰恰是后者（zsh 包装、source secrets、cwd、
# 模块能否 import、日志路径能否写）。本脚本把四份 plist 逐字搬进本机 launchd
# 跑一遍，跑完拆掉。
#
# 与真机部署（deploy/scripts/install.sh）的差异，**只有路径**：
#   /opt/ekbs/venv/bin/*      → <repo>/.venv/bin/*
#   /opt/ekbs/app             → <repo>
#   /opt/ekbs/etc/application.yaml → <repo>/config/application.dev.yaml
#   $HOME/.ekbs/secrets.env   → <repo>/var/rehearsal/secrets.env
#   /opt/ekbs/log/*.log       → <repo>/var/rehearsal/log/*.log
# 其余（zsh -lc 包装、set -a/source/set +a、exec、RunAtLoad、KeepAlive
# SuccessfulExit=false、ThrottleInterval、ProcessType）**逐字保留** ——
# 这些正是 launchd 特有的、静态测试覆盖不到的部分。
#
# 用 dev 配置（mock provider）：不出网、不花钱、不需要 API Key。
# 用 gui/<uid> 域 + LaunchAgent：**不需要 root**（install.sh 也是装到
# ~/Library/LaunchAgents，见 01 §8.3）。
#
# 退出码：0 = 无 FAIL；1 = 至少一项 FAIL。
# 用法：bash scripts/deploy_rehearsal.sh [--keep]
#   --keep  演练结束后保留 launchd 作业（默认会全部拆除）
# =============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO/deploy/launchd"
DEV_CFG="$REPO/config/application.dev.yaml"
DEV_DB="$REPO/var/index/ekbs.db"
# 演练根放在 **$HOME 下、不放仓库内**：实测 launchd 起的进程读取 ~/Documents 下的
# 文件会被 macOS TCC 拒（`zsh:source:1: operation not permitted`），而 $HOME 下正常。
# 把根放到这里，演练结果才不受该类环境因素干扰（生产路径 /opt/ekbs + ~/.ekbs 同属免打扰区）。
ROOT="${EKBS_REHEARSAL_ROOT:-$HOME/.ekbs-rehearsal-launchd}"
DOMAIN="gui/$(id -u)"
API="http://127.0.0.1:8000"
SERVICES=(api worker scheduler dingtalk)

# 每个服务起来后应当出现的日志标志（对应 ekbs/<svc>.py 里的 log.info）
marker_for() {
  case "$1" in
    worker)    echo "worker_started" ;;
    scheduler) echo "scheduler_started" ;;
    dingtalk)  echo "stream_disabled" ;;   # 无凭据时走这条（不谎报就绪）
    *)         echo "" ;;
  esac
}

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

PASS=0; FAIL=0; FINDINGS=0
p() { printf '  [PASS] %s\n' "$1"; PASS=$((PASS + 1)); }
f() { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL + 1)); }
step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }

label_of() { echo "com.ekbs.rehearsal.$1"; }
plist_of() { echo "$ROOT/launchd/$(label_of "$1").plist"; }

# ---------------------------------------------------------------------------
# 拆除：无论如何都要跑（含中途失败），否则会在本机留下一堆被 KeepAlive
# 反复拉起的作业 —— 这比演练本身失败更难排查。
# ---------------------------------------------------------------------------
cleanup() {
  local svc label
  for svc in "${SERVICES[@]}"; do
    label="$(label_of "$svc")"
    launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1
    launchctl bootout "$DOMAIN" "$(plist_of "$svc")" >/dev/null 2>&1
  done
  # 兜底：命令行走的是 exec，进程名即 uvicorn / python -m ekbs.*
  pkill -f "ekbs.rehearsal" >/dev/null 2>&1
  for svc in worker scheduler; do
    pkill -f "\.venv/bin/python -m ekbs\.$svc" >/dev/null 2>&1
  done
  pkill -f "\.venv/bin/python -m ekbs\.stream" >/dev/null 2>&1
  pkill -f "\.venv/bin/uvicorn ekbs.main:app" >/dev/null 2>&1
  sleep 1
}
trap 'if [ "$KEEP" -eq 0 ]; then cleanup; fi' EXIT

# ---------------------------------------------------------------------------
step "0/5 前置检查"
# ---------------------------------------------------------------------------
[ -x "$REPO/.venv/bin/uvicorn" ] \
  && p "venv 可用：.venv/bin/uvicorn" \
  || f "缺 .venv/bin/uvicorn（先跑 pip install -r requirements.txt）"

[ -f "$DEV_CFG" ] \
  && p "dev 配置存在：${DEV_CFG#$REPO/}" \
  || f "缺 dev 配置（演练用 mock provider，不出网）"

if curl -sf --max-time 2 "$API/livez" >/dev/null 2>&1; then
  f "端口 8000 已被占用：演练无法绑端口（先停掉正在跑的服务）"
else
  p "端口 8000 空闲"
fi

for svc in "${SERVICES[@]}"; do
  src="$SRC_DIR/com.ekbs.$svc.plist"
  if plutil -lint "$src" >/dev/null 2>&1; then
    p "源 plist 语法通过：$(basename "$src")"
  else
    f "源 plist 语法错误：$src"
  fi
done

if [ -f "$DEV_DB" ]; then
  p "dev 数据库已就绪：${DEV_DB#$REPO/}"
else
  info "dev 数据库不存在，先初始化（init_db + seed_demo）"
  if EKBS_CONFIG="$DEV_CFG" "$REPO/.venv/bin/python" -m ekbs.tools.init_db >/dev/null 2>&1 \
     && EKBS_CONFIG="$DEV_CFG" "$REPO/.venv/bin/python" -m ekbs.tools.seed_demo >/dev/null 2>&1; then
    p "dev 数据库初始化完成"
  else
    f "dev 数据库初始化失败（手工跑 init_db / seed_demo 看报错）"
  fi
fi

# ---------------------------------------------------------------------------
step "1/5 生成演练用 plist（只改路径，launchd 语义逐字保留）"
# ---------------------------------------------------------------------------
rm -rf "$ROOT"
mkdir -p "$ROOT/log" "$ROOT/launchd"

cat > "$ROOT/secrets.env" <<'SECRETS'
# 演练用密钥文件（真实部署是 ~/.ekbs/secrets.env，权限 600）
# 这里只放一个非敏感占位值：目的是证明 `set -a; source ...; set +a`
# 这条包装链能把变量导出给 exec 的进程，而不是真的需要什么凭据。
EKBS_INTERNAL_TOKEN=rehearsal-token
SECRETS
chmod 600 "$ROOT/secrets.env"
p "生成演练密钥文件（权限 $(stat -f '%Lp' "$ROOT/secrets.env")，对应 preflight 的 600 断言）"

for svc in "${SERVICES[@]}"; do
  src="$SRC_DIR/com.ekbs.$svc.plist"
  dst="$(plist_of "$svc")"
  label="$(label_of "$svc")"
  "$REPO/.venv/bin/python" - "$src" "$dst" "$label" "$svc" "$REPO" "$ROOT" "$DEV_CFG" <<'PY'
import plistlib, sys

src, dst, label, svc, repo, root, cfg = sys.argv[1:8]
with open(src, "rb") as fh:
    data = plistlib.load(fh)

data["Label"] = label
data["ProgramArguments"] = [
    arg.replace("/opt/ekbs/venv", f"{repo}/.venv")
       .replace("$HOME/.ekbs/secrets.env", f"{root}/secrets.env")
       .replace("/opt/ekbs/etc/application.yaml", cfg)
    for arg in data["ProgramArguments"]
]
data["WorkingDirectory"] = repo
env = dict(data.get("EnvironmentVariables") or {})
env["EKBS_CONFIG"] = cfg
data["EnvironmentVariables"] = env
data["StandardOutPath"] = f"{root}/log/{svc}.out.log"
data["StandardErrorPath"] = f"{root}/log/{svc}.err.log"

with open(dst, "wb") as fh:
    plistlib.dump(data, fh)
PY
  if ! plutil -lint "$dst" >/dev/null 2>&1; then
    f "${svc}：改写后的 plist 语法错误"
    continue
  fi
  if grep -q "/opt/ekbs" "$dst"; then
    f "${svc}：改写后仍残留 /opt/ekbs（演练会去碰真机路径）"
  else
    p "${svc}：plist 已改写且无 /opt/ekbs 残留"
  fi
done

# 改写不能把 launchd 语义弄丢 —— 这几项是 01 §8.1 模板的关键。
# ⚠️ KeepAlive **按服务不同**，不能一刀切：
#   · api / worker / scheduler → {SuccessfulExit: false}（仅异常退出才拉起，
#     这样维护窗口里 `launchctl stop` 不会被自己拉回来）
#   · dingtalk → true（01 §8.2 明确写「**必须 true**」：Stream 长连接即便正常退出
#     也要重连，不允许停在"干净退出"状态）
for svc in "${SERVICES[@]}"; do
  dst="$(plist_of "$svc")"
  [ -f "$dst" ] || continue
  expect_keepalive="SuccessfulExit=false"
  [ "$svc" = dingtalk ] && expect_keepalive="true"
  if "$REPO/.venv/bin/python" -c "
import plistlib, sys
d = plistlib.load(open(sys.argv[1], 'rb'))
assert d['RunAtLoad'] is True
assert int(d['ThrottleInterval']) == 10
assert d['ProgramArguments'][0] == '/bin/zsh'
assert '$ROOT/secrets.env' in d['ProgramArguments'][-1]
assert 'set -a' in d['ProgramArguments'][-1] and 'source' in d['ProgramArguments'][-1]
ka = d['KeepAlive']
if sys.argv[2] == 'true':
    assert ka is True, ka
else:
    assert ka == {'SuccessfulExit': False}, ka
" "$dst" "$expect_keepalive" 2>/dev/null; then
    p "${svc}：RunAtLoad / KeepAlive($expect_keepalive) / ThrottleInterval / zsh 包装 均保留"
  else
    f "${svc}：改写后 launchd 语义丢失（RunAtLoad / KeepAlive=$expect_keepalive / ThrottleInterval / zsh 包装）"
  fi
done

# ---------------------------------------------------------------------------
step "2/5 launchctl bootstrap 四服务"
# ---------------------------------------------------------------------------
for svc in "${SERVICES[@]}"; do
  dst="$(plist_of "$svc")"
  label="$(label_of "$svc")"
  [ -f "$dst" ] || { f "${svc}：无 plist 可加载"; continue; }
  launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1
  if launchctl bootstrap "$DOMAIN" "$dst" 2>"$ROOT/log/$svc.bootstrap.err"; then
    p "${svc}：launchctl bootstrap 成功"
  else
    f "${svc}：launchctl bootstrap 失败 —— $(head -1 "$ROOT/log/$svc.bootstrap.err" 2>/dev/null)"
  fi
done

# ---------------------------------------------------------------------------
step "3/5 等待并探测（api 探针 + 三守护进程日志标志）"
# ---------------------------------------------------------------------------
ready=0
for _ in $(seq 1 30); do
  if curl -sf --max-time 2 "$API/readyz" 2>/dev/null | grep -q '"status":"ready"'; then
    ready=1; break
  fi
  sleep 1
done
if [ "$ready" -eq 1 ]; then
  p "api /readyz → ready（$(curl -s --max-time 5 "$API/readyz" | head -c 160)）"
else
  f "api /readyz 30s 内未就绪 —— 末尾日志：$(tail -2 "$ROOT/log/api.err.log" 2>/dev/null | tr '\n' ' ' | head -c 200)"
fi

if curl -sf --max-time 5 "$API/healthz" >/dev/null 2>&1; then
  p "api /healthz → ok"
else
  f "api /healthz 无响应"
fi

for svc in "${SERVICES[@]}"; do
  label="$(label_of "$svc")"
  if launchctl list 2>/dev/null | awk '{print $3}' | grep -qx "$label"; then
    p "${svc}：出现在 launchctl list（label=${label}）"
  else
    f "${svc}：未出现在 launchctl list"
  fi

  marker="$(marker_for "$svc")"
  [ -n "$marker" ] || continue
  found=0
  for _ in $(seq 1 20); do
    if grep -qs "$marker" "$ROOT/log/$svc.out.log" "$ROOT/log/$svc.err.log" 2>/dev/null; then
      found=1; break
    fi
    sleep 1
  done
  if [ "$found" -eq 1 ]; then
    p "${svc}：日志出现启动标志 $marker"
  else
    f "${svc}：20s 内未见 $marker —— 末尾日志：$(tail -2 "$ROOT/log/$svc.err.log" 2>/dev/null | tr '\n' ' ' | head -c 200)"
  fi
done

# ---------------------------------------------------------------------------
step "4/5 日志落点、自愈与停机语义"
# ---------------------------------------------------------------------------
for svc in "${SERVICES[@]}"; do
  out="$ROOT/log/$svc.out.log"
  if [ -f "$out" ]; then
    p "${svc}：stdout 落在 plist 声明的路径（$(wc -c < "$out" | tr -d ' ') 字节）"
  else
    f "${svc}：StandardOutPath 未生成 —— 日志重定向没生效"
  fi
done

pid_of() {  # 取 launchd 记录的服务 pid
  launchctl print "$DOMAIN/$(label_of "$1")" 2>/dev/null \
    | awk -F'= ' '/^[[:space:]]*pid = /{print $2; exit}' | tr -d ' '
}

# --- 4.1 `launchctl stop` 到底做了什么（实测，不是照抄文档）-------------------
# 文档假设：`launchctl stop` → 进程收到信号 → 退出码 0 → KeepAlive(SuccessfulExit=false)
# 不重启，因此可当维护窗口手段（02 TC-ENV-023 / 04 §4 的信号处理表）。
pid_before="$(pid_of api)"
launchctl stop "$DOMAIN/$(label_of api)" >/dev/null 2>&1
sleep 3
pid_after="$(pid_of api)"
if [ -n "$pid_before" ] && [ "$pid_before" = "$pid_after" ]; then
  info "[发现] launchctl stop 未终止 api（pid 仍为 ${pid_before}）：macOS 26 上这条命令是**空操作**，"
  info "       故 02 里以 launchctl stop 注入故障的用例（TC-ENV-023 / F02 / F04 / TC-DT-046）实际没注入故障，"
  info "       需改用 kill -TERM <pid> 或 launchctl kickstart -k"
  FINDINGS=$((FINDINGS + 1))
else
  p "api：launchctl stop 生效（pid $pid_before → ${pid_after:-已退出}）"
fi

# --- 4.2 崩溃自愈：kill -TERM 后应被 launchd 拉起（02 TC-ENV-022，P0，≤15s）----
# 计时口径要小心：uvicorn 收到 SIGTERM 会**优雅退出**，期间端口仍在应答。
# 若从 kill 那一刻就开始等 /livez 成功，会得到一个假的「0s 自愈」——
# 实际量到的是「旧进程还没死」。因此先等到它**真的下线**，再量到恢复。
pid_api="$(pid_of api)"
if [ -n "$pid_api" ]; then
  healed_ms="$("$REPO/.venv/bin/python" - "$pid_api" "$API" <<'PYSELFHEAL'
import os, signal, sys, time, urllib.request

pid, api = int(sys.argv[1]), sys.argv[2]

def up() -> bool:
    try:
        urllib.request.urlopen(api + "/livez", timeout=1)
        return True
    except Exception:
        return False

os.kill(pid, signal.SIGTERM)
t0 = time.perf_counter()
while time.perf_counter() - t0 < 15:      # 等旧进程真正下线
    if not up():
        break
    time.sleep(0.2)
while time.perf_counter() - t0 < 40:      # 等 launchd 拉起并可用
    if up():
        print(f"{(time.perf_counter() - t0) * 1000:.0f}")
        sys.exit(0)
    time.sleep(0.2)
print("timeout")
PYSELFHEAL
)"
  case "$healed_ms" in
    timeout) f "api：被 kill -TERM 后 40s 未恢复 —— launchd 没拉起（检查 KeepAlive/ThrottleInterval）" ;;
    *) if [ "$healed_ms" -le 15000 ]; then
         p "api：被 kill -TERM 后 ${healed_ms}ms 自愈（02 TC-ENV-022 要求 ≤15s）"
       else
         f "api：自愈耗时 ${healed_ms}ms，超过 02 TC-ENV-022 的 ≤15s"
       fi ;;
  esac
else
  f "api：拿不到 pid，无法做自愈测试"
fi

# --- 4.3 dingtalk 的自愈（它用的是 KeepAlive=true，语义与其余三个不同）--------
pid_dt="$(pid_of dingtalk)"
if [ -n "$pid_dt" ]; then
  : > "$ROOT/log/dingtalk.out.log"; : > "$ROOT/log/dingtalk.err.log"
  kill -TERM "$pid_dt" 2>/dev/null
  back=0
  for _ in $(seq 1 20); do
    if grep -qs "stream_disabled\|stream_starting\|stream_credentials_missing" \
        "$ROOT/log/dingtalk.out.log" "$ROOT/log/dingtalk.err.log" 2>/dev/null; then
      back=1; break
    fi
    sleep 1
  done
  [ "$back" -eq 1 ] && p "dingtalk：被 kill 后重新拉起（KeepAlive=true 符合 01 §8.2「必须 true」）" \
                    || f "dingtalk：被 kill 后未重新拉起，与 01 §8.2 的 KeepAlive=true 不符"
else
  f "dingtalk：拿不到 pid，无法做自愈测试"
fi

# --- 4.4 维护窗口该用什么：bootout 才是真正的停机手段 -------------------------
launchctl bootout "$DOMAIN/$(label_of api)" >/dev/null 2>&1
sleep 3
if curl -sf --max-time 2 "$API/livez" >/dev/null 2>&1; then
  f "api：bootout 后仍在跑 —— 没有可靠的停机手段"
else
  p "api：bootout 后保持停止（这才是维护窗口/停机换版该用的命令）"
fi

step "5/5 拆除并确认无残留"
# ---------------------------------------------------------------------------
if [ "$KEEP" -eq 1 ]; then
  info "--keep：保留 launchd 作业，手工拆除："
  for svc in "${SERVICES[@]}"; do info "  launchctl bootout $DOMAIN/$(label_of "$svc")"; done
else
  cleanup
  leftover=0
  for svc in "${SERVICES[@]}"; do
    launchctl list 2>/dev/null | awk '{print $3}' | grep -qx "$(label_of "$svc")" && leftover=1
  done
  pgrep -f "\.venv/bin/(uvicorn ekbs.main:app|python -m ekbs\.(worker|scheduler|stream))" >/dev/null 2>&1 \
    && leftover=1
  if [ "$leftover" -eq 0 ]; then
    p "四个作业与进程均已拆除，无残留"
  else
    f "仍有残留作业/进程（手工清理：launchctl bootout $DOMAIN/<label>）"
  fi
fi

printf '\n== 结果 ==\n  PASS=%d  FAIL=%d  实测发现（文档与实际不符，需改文档）= %d\n' "$PASS" "$FAIL" "$FINDINGS"
info "演练根目录（日志可查）：${ROOT#$REPO/}"
if [ "$FAIL" -eq 0 ]; then
  printf '\n  判定：PASS（四服务在本机 launchd 下可拉起、可探测、可拆除）\n'
  exit 0
fi
printf '\n  判定：FAIL（真机部署前必须先修）\n'
exit 1
