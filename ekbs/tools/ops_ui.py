"""离线运维 UI：主 API 挂了也能启停四服务。

独立进程、只绑 ``127.0.0.1``，通过 ``scripts/dev_services.sh`` 控
api / dingtalk / worker / scheduler，不依赖 ``ekbs.main``。

用法::

    .venv/bin/python -m ekbs.tools.ops_ui
    # 或双击 scripts/open_ops.command

默认 http://127.0.0.1:8010/
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SERVICES = ("api", "dingtalk", "worker", "scheduler")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010
_STATUS_LINE = re.compile(
    r"^(api|dingtalk|worker|scheduler)\s+(running|stopped)\s+(\S+)\s*$"
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def services_script() -> Path:
    return repo_root() / "scripts" / "dev_services.sh"


def run_services(action: str, target: str = "all", *, timeout: float = 120.0) -> dict[str, Any]:
    script = services_script()
    if not script.is_file():
        return {"ok": False, "error": f"找不到 {script}", "stdout": "", "stderr": ""}
    if action not in {"start", "stop", "restart", "status"}:
        return {"ok": False, "error": f"非法 action: {action}", "stdout": "", "stderr": ""}
    if target not in {"all", *SERVICES}:
        return {"ok": False, "error": f"非法 target: {target}", "stdout": "", "stderr": ""}
    cmd = ["bash", str(script), action]
    if action != "status":
        cmd.append(target)
    env = os.environ.copy()
    # 与 open_admin / 日常开发一致：未指定时用 cloud
    env.setdefault("EKBS_CONFIG", str(repo_root() / "config" / "application.cloud.yaml"))
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(repo_root()),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": f"超时（>{timeout:.0f}s）",
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
        }
    except OSError as exc:
        return {"ok": False, "error": str(exc), "stdout": "", "stderr": ""}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "error": None if completed.returncode == 0 else f"exit {completed.returncode}",
    }


def parse_status(stdout: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        name: {"state": "unknown", "pid": None} for name in SERVICES
    }
    for line in (stdout or "").splitlines():
        match = _STATUS_LINE.match(line.strip())
        if not match:
            continue
        name, state, pid = match.group(1), match.group(2), match.group(3)
        out[name] = {
            "state": state,
            "pid": None if pid in {"-", "None", ""} else pid,
        }
    return out


def probe_api() -> dict[str, Any]:
    """主 API 是否活着（不影响本页；仅展示）。"""
    import urllib.error
    import urllib.request

    result: dict[str, Any] = {"livez": False, "readyz": None}
    for path, key in (("/livez", "livez"), ("/readyz", "readyz")):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:8000{path}", timeout=1.5
            ) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if key == "livez":
                    result["livez"] = resp.status == 200
                else:
                    try:
                        result["readyz"] = json.loads(body)
                    except json.JSONDecodeError:
                        result["readyz"] = {"raw": body[:200]}
        except (urllib.error.URLError, TimeoutError, OSError):
            if key == "readyz":
                result["readyz"] = None
    return result


def collect_status() -> dict[str, Any]:
    raw = run_services("status")
    services = parse_status(raw.get("stdout") or "")
    return {
        "ok": True,
        "services": services,
        "api_probe": probe_api(),
        "raw": {
            "ok": raw.get("ok"),
            "stdout": raw.get("stdout") or "",
            "stderr": raw.get("stderr") or "",
        },
        "config": os.environ.get(
            "EKBS_CONFIG", str(repo_root() / "config" / "application.cloud.yaml")
        ),
    }


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EKBS 离线运维</title>
<style>
  :root {
    --bg: #0f1419;
    --card: #1a2332;
    --text: #e7ecf3;
    --muted: #8b9bb4;
    --ok: #3dd68c;
    --bad: #ff6b6b;
    --warn: #f0b429;
    --line: #2a3648;
    --btn: #2d6cdf;
    --btn2: #3a4658;
    --danger: #c23b3b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: "SF Pro Text", "PingFang SC", "Segoe UI", sans-serif;
    background: radial-gradient(1200px 600px at 10% -10%, #1c2a44, var(--bg));
    color: var(--text); min-height: 100vh; padding: 28px 18px 48px;
  }
  main { max-width: 880px; margin: 0 auto; }
  h1 { font-size: 1.55rem; margin: 0 0 6px; letter-spacing: .02em; }
  .sub { color: var(--muted); margin-bottom: 22px; font-size: .95rem; line-height: 1.5; }
  .card {
    background: color-mix(in srgb, var(--card) 92%, #000);
    border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; margin-bottom: 14px;
  }
  .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .svc {
    flex: 1 1 180px; background: #121a26; border: 1px solid var(--line);
    border-radius: 12px; padding: 12px 14px;
  }
  .svc .name { font-weight: 600; margin-bottom: 4px; }
  .svc .meta { color: var(--muted); font-size: .85rem; }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: .78rem; font-weight: 600;
  }
  .pill.on { background: color-mix(in srgb, var(--ok) 22%, transparent); color: var(--ok); }
  .pill.off { background: color-mix(in srgb, var(--bad) 22%, transparent); color: var(--bad); }
  .pill.unk { background: color-mix(in srgb, var(--warn) 22%, transparent); color: var(--warn); }
  button {
    border: 0; border-radius: 10px; padding: 9px 14px; color: #fff;
    background: var(--btn); cursor: pointer; font-size: .92rem;
  }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.secondary { background: var(--btn2); }
  button.danger { background: var(--danger); }
  button.ghost { background: transparent; border: 1px solid var(--line); color: var(--text); }
  select, .mono {
    background: #121a26; color: var(--text); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px 10px;
  }
  pre {
    white-space: pre-wrap; word-break: break-word; background: #0c121a;
    border-radius: 10px; padding: 12px; border: 1px solid var(--line);
    color: #c5d0e0; font-size: .82rem; max-height: 280px; overflow: auto;
  }
  a { color: #7eb6ff; }
  .toast { color: var(--muted); font-size: .88rem; min-height: 1.2em; }
</style>
</head>
<body>
<main>
  <h1>EKBS 离线运维</h1>
  <p class="sub">
    本页不依赖主 API（:8000）。服务启停走本机 <code>scripts/dev_services.sh</code>。
    仅监听 <code>127.0.0.1</code>。管理台：
    <a href="http://127.0.0.1:8000/admin/" target="_blank" rel="noreferrer">http://127.0.0.1:8000/admin/</a>
  </p>

  <div class="card">
    <div class="row" style="justify-content:space-between; margin-bottom:12px;">
      <div>
        <strong>四服务状态</strong>
        <div class="meta toast" id="configLine"></div>
      </div>
      <div class="row">
        <button class="ghost" type="button" id="btnRefresh">刷新</button>
        <span class="toast" id="busy"></span>
      </div>
    </div>
    <div class="row" id="svcGrid"></div>
    <div style="margin-top:12px;" class="toast" id="probeLine"></div>
  </div>

  <div class="card">
    <div class="row" style="margin-bottom:12px;">
      <strong>操作</strong>
      <select id="target">
        <option value="all">全部</option>
        <option value="api">api</option>
        <option value="dingtalk">dingtalk</option>
        <option value="worker">worker</option>
        <option value="scheduler">scheduler</option>
      </select>
      <button type="button" data-act="start">开机</button>
      <button class="secondary" type="button" data-act="restart">重启</button>
      <button class="danger" type="button" data-act="stop">关机</button>
    </div>
    <div class="toast" id="actionMsg"></div>
    <pre id="log">（操作输出）</pre>
  </div>
</main>
<script>
const TOKEN = __TOKEN__;
const grid = document.getElementById('svcGrid');
const logEl = document.getElementById('log');
const busy = document.getElementById('busy');
const actionMsg = document.getElementById('actionMsg');
const probeLine = document.getElementById('probeLine');
const configLine = document.getElementById('configLine');
let pending = false;

function pill(state) {
  if (state === 'running') return '<span class="pill on">running</span>';
  if (state === 'stopped') return '<span class="pill off">stopped</span>';
  return '<span class="pill unk">' + (state || 'unknown') + '</span>';
}

async function api(path, opts = {}) {
  const headers = Object.assign({'X-Ops-Token': TOKEN}, opts.headers || {});
  if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { ok: false, error: text || res.statusText }; }
  if (!res.ok && data && !data.error) data.error = 'HTTP ' + res.status;
  return data;
}

function renderStatus(data) {
  const services = (data && data.services) || {};
  grid.innerHTML = ['api','dingtalk','worker','scheduler'].map((name) => {
    const s = services[name] || {};
    return '<div class="svc"><div class="name">' + name + ' ' + pill(s.state)
      + '</div><div class="meta">pid ' + (s.pid == null ? '—' : s.pid) + '</div></div>';
  }).join('');
  const probe = (data && data.api_probe) || {};
  const ready = probe.readyz && probe.readyz.status ? probe.readyz.status : (probe.livez ? 'live' : 'down');
  probeLine.textContent = '主 API :8000 → livez=' + (probe.livez ? 'ok' : 'fail')
    + ' · readyz=' + ready;
  configLine.textContent = 'EKBS_CONFIG=' + ((data && data.config) || '');
}

async function refresh() {
  busy.textContent = '读取中…';
  try {
    const data = await api('/api/status');
    renderStatus(data);
    if (data.raw && (data.raw.stdout || data.raw.stderr)) {
      // 保持日志区不刷屏；仅在空时写入
      if (logEl.textContent === '（操作输出）') {
        logEl.textContent = (data.raw.stdout || '') + (data.raw.stderr || '');
      }
    }
  } catch (err) {
    actionMsg.textContent = String(err);
  } finally {
    busy.textContent = '';
  }
}

async function control(action) {
  if (pending) return;
  const target = document.getElementById('target').value;
  if (action === 'stop' && target === 'all') {
    if (!confirm('确认关闭全部四服务？管理台与钉钉将不可用，本运维页仍可打开。')) return;
  }
  pending = true;
  busy.textContent = action + ' ' + target + '…';
  actionMsg.textContent = '';
  try {
    const data = await api('/api/control', {
      method: 'POST',
      body: JSON.stringify({ action, target }),
    });
    logEl.textContent = [data.stdout || '', data.stderr || '', data.error ? ('ERROR: ' + data.error) : '']
      .filter(Boolean).join('\\n') || '(无输出)';
    actionMsg.textContent = data.ok ? ('完成：' + action + ' ' + target) : ('失败：' + (data.error || 'unknown'));
    await refresh();
  } catch (err) {
    actionMsg.textContent = String(err);
  } finally {
    pending = false;
    busy.textContent = '';
  }
}

document.getElementById('btnRefresh').onclick = () => refresh();
document.querySelectorAll('button[data-act]').forEach((btn) => {
  btn.onclick = () => control(btn.getAttribute('data-act'));
});
refresh();
setInterval(refresh, 8000);
</script>
</body>
</html>
"""


class OpsHandler(BaseHTTPRequestHandler):
    server_version = "EKBSOpsUI/1.0"
    token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _client_ok(self) -> bool:
        host = self.client_address[0]
        return host in {"127.0.0.1", "::1", "localhost"}

    def _check_token(self) -> bool:
        got = self.headers.get("X-Ops-Token") or ""
        return secrets.compare_digest(got, self.token)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        if not self._client_ok():
            self._json(403, {"ok": False, "error": "仅允许本机访问"})
            return
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            html = HTML_PAGE.replace("__TOKEN__", json.dumps(self.token))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            if not self._check_token():
                self._json(401, {"ok": False, "error": "缺少或错误的 X-Ops-Token"})
                return
            self._json(200, collect_status())
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._client_ok():
            self._json(403, {"ok": False, "error": "仅允许本机访问"})
            return
        if not self._check_token():
            self._json(401, {"ok": False, "error": "缺少或错误的 X-Ops-Token"})
            return
        path = urlparse(self.path).path
        if path != "/api/control":
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "JSON 无效"})
            return
        action = str(payload.get("action") or "").strip().lower()
        target = str(payload.get("target") or "all").strip().lower()
        if action not in {"start", "stop", "restart"}:
            self._json(400, {"ok": False, "error": "action 须为 start|stop|restart"})
            return
        result = run_services(action, target)
        status = collect_status()
        self._json(
            200 if result.get("ok") else 500,
            {
                "ok": bool(result.get("ok")),
                "action": action,
                "target": target,
                "error": result.get("error"),
                "stdout": result.get("stdout") or "",
                "stderr": result.get("stderr") or "",
                "services": status.get("services"),
                "api_probe": status.get("api_probe"),
            },
        )


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    open_browser = True
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
            continue
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
            continue
        if args[i] == "--no-open":
            open_browser = False
            i += 1
            continue
        print(f"未知参数：{args[i]}", file=sys.stderr)
        print("用法：python -m ekbs.tools.ops_ui [--port 8010] [--no-open]", file=sys.stderr)
        return 2

    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("拒绝非本机绑定；离线运维页只允许 127.0.0.1", file=sys.stderr)
        return 2

    token = secrets.token_urlsafe(18)
    OpsHandler.token = token
    server = ThreadingHTTPServer((host, port), OpsHandler)
    url = f"http://{host}:{port}/"
    print(f"EKBS 离线运维 UI：{url}")
    print(f"仓库根：{repo_root()}")
    print("Ctrl+C 结束本页（不会自动停四服务）")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出运维 UI")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
