"""部署契约测试 —— 把「launchd 四服务」这一半做成**可验证**的。

真机部署（`/opt/ekbs` + Mac mini）现阶段做不了，但部署件与代码之间的一致性**现在就能验**：

* 四个 plist 能解析、label 正确、注入 ``EKBS_CONFIG``；
* plist 里 ``exec`` 的入口（``ekbs.main:app`` / ``-m ekbs.worker`` / ``-m ekbs.scheduler`` /
  ``-m ekbs.stream``）在本仓库里**确实存在且能启动到 argparse**；
* ``preflight.sh`` 的硬断言（回环监听 / workers=1 / L4 关闭）在配置上成立；
* 应用路由包含 ``smoke.sh`` 会探测的全部路径。

这样「key 与真机就绪后要做的只剩跑 install.sh」就不是一句口号，而是有据可查。
"""

from __future__ import annotations

import importlib.util
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHD = REPO / "deploy" / "launchd"
SCRIPTS = REPO / "deploy" / "scripts"

#: 四个 launchd 服务与各自的入口（01 §8）
SERVICES = {
    "api": "ekbs.main:app",
    "dingtalk": "ekbs.stream",
    "worker": "ekbs.worker",
    "scheduler": "ekbs.scheduler",
}


@pytest.mark.parametrize("service,entry", SERVICES.items())
def test_plist_matches_entrypoint(service: str, entry: str) -> None:
    plist_path = LAUNCHD / f"com.ekbs.{service}.plist"
    assert plist_path.is_file(), f"缺少 plist：{plist_path}"
    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["Label"] == f"com.ekbs.{service}"
    assert payload["WorkingDirectory"] == "/opt/ekbs/app"
    assert payload["EnvironmentVariables"]["EKBS_CONFIG"] == "/opt/ekbs/etc/application.yaml"

    command = payload["ProgramArguments"][-1]
    assert entry in command, f"{service} 的入口应为 {entry}，实际：{command}"
    # 密钥纪律：plist 里不得出现明文密钥（04 §2.6 / check-secrets.sh）
    assert "API_KEY" not in command and "SECRET" not in command
    assert "source \"$HOME/.ekbs/secrets.env\"" in command


#: launchd 语义的**逐服务**期望（01 §8.1 的 plist 模板 + §8.2 的服务清单）。
#:
#: ⚠️ ``KeepAlive`` 按服务不同，**不能一刀切** —— 这是本仓库实测（`scripts/deploy_rehearsal.sh`
#: 用真实 launchd 跑四服务）之后固化下来的：
#:   · api / worker / scheduler → ``{SuccessfulExit: false}``：仅「异常退出」才拉起；
#:   · dingtalk → ``true``：01 §8.2 写明「**必须 true**」—— Stream 长连接即便干净退出
#:     也必须重连，不允许停在"已退出"状态（04 §4 说它是 SuccessfulExit=false，与 plist 不符）。
LAUNCHD_SEMANTICS = {
    "api": {"keepalive": {"SuccessfulExit": False}},
    "worker": {"keepalive": {"SuccessfulExit": False}},
    "scheduler": {"keepalive": {"SuccessfulExit": False}},
    "dingtalk": {"keepalive": True},
}


@pytest.mark.parametrize("service", sorted(LAUNCHD_SEMANTICS))
def test_launchd_semantics_are_per_service(service: str) -> None:
    """自启、保活、重启节流、密钥注入包装 —— 这几项错了真机上会以「服务起不来/起不来第二次」出现。"""
    with (LAUNCHD / f"com.ekbs.{service}.plist").open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["RunAtLoad"] is True, f"{service} 未设置 RunAtLoad，开机不会自启"
    assert payload["KeepAlive"] == LAUNCHD_SEMANTICS[service]["keepalive"], (
        f"{service} 的 KeepAlive 与 01 §8 口径不符：{payload['KeepAlive']}"
    )
    assert int(payload["ThrottleInterval"]) == 10, "崩溃后最小重启间隔应为 10s（04 §7.3）"

    # 密钥注入纪律：必须经 zsh 显式 source，launchd 不读 shell profile
    assert payload["ProgramArguments"][0] == "/bin/zsh"
    assert payload["ProgramArguments"][1] == "-lc"
    command = payload["ProgramArguments"][-1]
    assert command.startswith("set -a; source"), f"{service} 的包装链不对：{command[:60]}"
    assert "exec " in command, f"{service} 必须用 exec 顶替，否则信号收不到"

    # 日志落点：smoke.sh 与运维排查都按这两个路径找
    assert payload["StandardOutPath"] == f"/opt/ekbs/log/{service}.out.log"
    assert payload["StandardErrorPath"] == f"/opt/ekbs/log/{service}.err.log"


def test_release_script_replaces_real_directory_before_symlink() -> None:
    """**回归（真机演练实测踩到）**：`install.sh` 会把 `/opt/ekbs/app` 建成**真实目录**。

    此时 `ln -sfn "$NEW" "$APP"` 不会替换它，而是把链接建到该目录**里面**
    （`/opt/ekbs/app/<时间戳> -> .../releases/<时间戳>`）—— `04 §7.6` 的
    `app -> releases/<ts>` 布局**静默失效**，且每次发布/回滚再套一层。
    release.sh 必须先把真实目录收掉（保留其中的 sql 引用文件）再建链接。
    """
    text = (SCRIPTS / "release.sh").read_text()
    assert '[ ! -L "$APP" ]' in text, "缺少「目标是真实目录」的判断"
    assert 'rm -rf "$APP"' in text, "缺少收掉真实目录的动作"
    # 用「行首」定位真正的命令，避免命中注释里引用的同一串
    assert text.index('[ ! -L "$APP" ]') < text.index('\nln -sfn "$NEW" "$APP"'), \
        "顺序必须是：先收目录，再建符号链接"

    # 04 §7.6 的示例代码同样不能留着这个坑：取该示例块（从 set -euo 到 pip install 之间）
    # 《方案》文档是**独立交付物**（不入代码库，见 .gitignore）—— 不在时就跳过这段文档一致性检查，
    # 否则新克隆的仓库跑测试会因缺文件而失败（实测踩到）。
    plan_doc = REPO / "方案" / "04-钉钉接入方案.md"
    if not plan_doc.is_file():
        pytest.skip("《方案》文档未随仓库分发（独立交付物），跳过文档示例一致性检查")
    doc = plan_doc.read_text()
    start = doc.index("#!/usr/bin/env bash\n# deploy/scripts/release.sh")
    end = doc.index("pip install -q -e", start)
    snippet = doc[start:end]
    assert "ln -sfn" in snippet, "定位到的不是 release.sh 示例块"
    assert '[ ! -L "$APP" ]' in snippet, "04 §7.6 的示例代码未同步该修正"


@pytest.mark.parametrize("script", ["deploy_rehearsal.sh", "install_rehearsal.sh"])
def test_rehearsal_scripts_exist_valid_and_avoid_tcc_paths(script: str) -> None:
    """演练脚本必须存在、语法正确，且**根目录放在 `$HOME` 下**。

    为什么根目录不能放仓库里：实测 launchd 起的进程读取 `~/Documents/**` 会被
    macOS TCC 拒（`zsh:source:1: operation not permitted`），表现为
    「缺少必需密钥」这种**误导性**报错。演练根放 `$HOME` 下才不受该类环境干扰。
    """
    path = REPO / "scripts" / script
    assert path.is_file(), f"缺少演练脚本：{path}"
    checked = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert checked.returncode == 0, f"{script} 语法错误：{checked.stderr}"
    text = path.read_text()
    assert "$HOME/.ekbs-rehearsal" in text, f"{script} 的演练根应放在 $HOME 下（避免 TCC）"
    assert 'ROOT="$REPO/var/' not in text, f"{script} 的演练根不应落在仓库（~/Documents）内"


def test_shell_scripts_brace_vars_before_non_ascii() -> None:
    """**仓库级 lint（踩过三次）**：`$var` 紧跟非 ASCII 字符时，macOS 自带的 bash 3.2
    会把那个多字节字符当成变量名的一部分，报 `svc?: unbound variable`，
    而且报错行号指向的位置常常让人看不出真凶。

    修法一律加花括号：`${var}：`。部署脚本在真机上默认就是 bash 3.2，必须守住。
    """
    offenders = []
    targets = sorted(REPO.glob("scripts/*.sh")) + sorted(SCRIPTS.glob("*.sh"))
    assert targets, "没找到任何 shell 脚本，测试自身失效"
    for path in targets:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for match in re.finditer(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])", line):
                offenders.append(f"{path.name}:{lineno}: {match.group(0)}")
    assert not offenders, "变量后紧跟非 ASCII 字符，需加花括号：" + "; ".join(offenders)


def test_api_plist_pins_single_worker() -> None:
    """workers 必须为 1（04 §1.5：多 worker 会出现 ≤300s 的旧权限窗口）。"""
    with (LAUNCHD / "com.ekbs.api.plist").open("rb") as handle:
        payload = plistlib.load(handle)
    command = payload["ProgramArguments"][-1]
    assert "--workers 1" in command
    assert "--host 127.0.0.1" in command


@pytest.mark.parametrize("module", ["ekbs.main", "ekbs.stream", "ekbs.worker", "ekbs.scheduler"])
def test_entry_modules_exist(module: str) -> None:
    assert importlib.util.find_spec(module) is not None, f"入口模块不存在：{module}"


@pytest.mark.parametrize("module", ["ekbs.worker", "ekbs.scheduler", "ekbs.stream"])
def test_entrypoints_start_and_expose_help(module: str) -> None:
    """入口可执行（`--help` 在 argparse 阶段退出，不依赖数据库与凭据）。"""
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True, text=True, timeout=60, cwd=str(REPO),
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in (result.stdout + result.stderr).lower()


def test_tool_entrypoints_match_install_script_hints() -> None:
    """`install.sh` 结尾提示的三条命令必须真实存在（01 §6.3/§10）。"""
    for module, args in (
        ("ekbs.tools.new_shard", ["--help"]),
        ("ekbs.tools.sync_org", ["--help"]),
        ("ekbs.tools.init_db", ["--help"]),
    ):
        result = subprocess.run(
            [sys.executable, "-m", module, *args],
            capture_output=True, text=True, timeout=60, cwd=str(REPO),
        )
        assert result.returncode == 0, f"{module}: {result.stderr}"


def test_config_satisfies_preflight_hard_assertions() -> None:
    """`preflight.sh` 的两条 FAIL 级断言 + 两条 WARN 级断言，在配置上成立。"""
    from ekbs.config import load_settings
    from tests.conftest import DEV_CONFIG

    settings = load_settings(DEV_CONFIG)
    assert settings.server.host == "127.0.0.1"
    assert settings.adapter.host == "127.0.0.1"
    assert settings.server.workers == 1
    # WARN 级（preflight 只提示不阻断）：三个闸门当前处于**测试期全开**状态 ——
    # 2026-10-09 用户确认「全部打开，因为要测试」（合规书暂缓补）。
    # 闸门开合属部署决策，本条只断言「可解析且为布尔」；
    # 具体值的回归看 deploy 配置与 00 §7 的合规口径。
    assert isinstance(settings.permission.l4_enabled, bool)
    assert isinstance(settings.permission.l3_ingest_enabled, bool)
    assert isinstance(settings.permission.corpus_egress_ack, bool)


def test_smoke_probed_routes_are_registered() -> None:
    """`smoke.sh` 会打的路径必须都在应用里注册（字段名已由 tests/test_api.py 锁定）。"""
    from ekbs.config import load_settings
    from ekbs.api.app import create_app
    from tests.conftest import DEV_CONFIG

    app = create_app(load_settings(DEV_CONFIG))
    routes = {getattr(route, "path", "") for route in app.routes}
    for path in (
        "/livez", "/healthz", "/readyz", "/health/deps",
        "/api/v1/qa/chat", "/api/v1/qa/chat/stream", "/api/v1/qa/retrieve",
        "/api/v1/auth/me", "/api/v1/auth/perm",
        "/api/v1/assets/figure/{chunk_id}",
        "/v1/qa/chat",  # 口径冲突期的兼容别名
    ):
        assert path in routes, f"路由缺失：{path}"


def test_schema_path_matches_install_and_smoke_expectations() -> None:
    """`install.sh` 从 `$APP_DIR/sql/001_init.sql` 建库；smoke 期望 34 张表。"""
    from ekbs.db import EXPECTED_TABLES, EXPECTED_TRIGGERS
    from tests.conftest import DEV_CONFIG
    from ekbs.config import load_settings

    settings = load_settings(DEV_CONFIG)
    assert settings.schema_sql_path == REPO / "sql" / "001_init.sql"
    assert settings.schema_sql_path.is_file()
    assert EXPECTED_TABLES == 34
    assert EXPECTED_TRIGGERS == 12

    deploy_copy = REPO / "deploy" / "sql" / "001_init.sql"
    assert deploy_copy.is_file()
    # 两份必须逐字一致：否则「部署建库」与「代码自检」会对不上
    assert settings.schema_sql_path.read_text(encoding="utf-8") == deploy_copy.read_text(
        encoding="utf-8"
    ), "sql/001_init.sql 与 deploy/sql/001_init.sql 已漂移，请同步"


def test_backup_script_is_present_for_rpo_target() -> None:
    """RPO ≤24h 依赖每日备份脚本存在（01 §13）。"""
    backup = SCRIPTS / "backup.sh"
    assert backup.is_file()
    text = backup.read_text(encoding="utf-8")
    assert ".backup" in text  # 必须用 sqlite3 .backup，不能直接 cp WAL 模式的库


def test_dev_services_script_mirrors_launchd_services() -> None:
    """开发机脚本必须与 launchd 用**同一套入口命令**，否则「本地能跑、上机就跑不起来」。"""
    script = REPO / "scripts" / "dev_services.sh"
    assert script.is_file()
    syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    text = script.read_text(encoding="utf-8")
    assert "SERVICES=(api dingtalk worker scheduler)" in text
    # 四个入口与 plist 逐字对应
    assert "uvicorn" in text and "ekbs.main:app" in text
    for module in ("ekbs.stream", "ekbs.worker", "ekbs.scheduler"):
        assert f"-m {module}" in text
    assert "--workers 1" in text


def test_schema_sql_resolves_in_production_layout(tmp_path) -> None:
    """生产的三个目录是分开的：`install.sh` 会把建表脚本放到 `$APP_DIR/sql` 与 `$ETC/sql`。

    回归测试：早先 `schema_sql_path` 只按 `repo_root/sql` 推，推出
    `/opt/ekbs/sql/001_init.sql` —— 而 install.sh 从不创建那个目录，真机上必然找不到脚本。
    """
    import shutil

    from ekbs.config import load_settings

    repo = Path(__file__).resolve().parent.parent
    # 模拟生产布局：/opt/ekbs/etc/application.yaml + /opt/ekbs/app/sql/001_init.sql
    opt = tmp_path / "opt" / "ekbs"
    (opt / "etc").mkdir(parents=True)
    (opt / "app" / "sql").mkdir(parents=True)
    shutil.copy(repo / "sql" / "001_init.sql", opt / "app" / "sql" / "001_init.sql")

    text = (repo / "config" / "application.dev.yaml").read_text(encoding="utf-8")
    text = (
        text.replace("  log_dir: var/log", f"  log_dir: {opt}/log")
        .replace("  data_dir: var\n", f"  data_dir: {opt}/data\n")
        .replace("  path: var/index/ekbs.db", f"  path: {opt}/data/index/ekbs.db")
    )
    (opt / "etc" / "application.yaml").write_text(text, encoding="utf-8")

    settings = load_settings(opt / "etc" / "application.yaml")
    assert settings.schema_sql_path == opt / "app" / "sql" / "001_init.sql"
    assert settings.schema_sql_path.is_file()

    # 另一种生产形态：脚本在 $ETC/sql
    (opt / "app").rename(opt / "app-moved")
    (opt / "etc" / "sql").mkdir()
    shutil.copy(repo / "sql" / "001_init.sql", opt / "etc" / "sql" / "001_init.sql")
    settings2 = load_settings(opt / "etc" / "application.yaml")
    assert settings2.schema_sql_path == opt / "etc" / "sql" / "001_init.sql"


async def test_adapter_app_reports_not_ready_honestly(settings) -> None:
    """适配器必须诚实报 not_ready，且两种原因要能区分（方案 04 §5.3 的 D3）。

    ① ``stream_enabled=false``（开发默认）→ 明说配置关着；
    ② 开着但缺凭据 → 明说缺哪几个。
    """
    from httpx import ASGITransport, AsyncClient

    from ekbs.config import Secrets
    from ekbs.stream import create_adapter_app

    # ① 配置关闭
    app = create_adapter_app(settings, Secrets({"EKBS_INTERNAL_TOKEN": "t"}))
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://adapter"
    ) as client:
        live = await client.get("/livez")
        ready = await client.get("/readyz")
    assert live.status_code == 200
    assert ready.status_code == 503
    body = ready.json()
    assert body["status"] == "not_ready" and body["stream_connected"] is False
    assert "stream_enabled" in body["reason"]

    # ② 开着但缺凭据
    settings.dingtalk["stream_enabled"] = True
    app = create_adapter_app(settings, Secrets({"EKBS_INTERNAL_TOKEN": "t"}))
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://adapter"
    ) as client:
        ready = await client.get("/readyz")
    assert ready.status_code == 503
    assert "凭据" in ready.json()["reason"]


async def test_sdk_handler_delegates_and_acks() -> None:
    """SDK 的 ACK 契约：`process()` 返回 `(code, message)` 二元组（docs/spec/50 §2.1）。"""
    from ekbs.stream import StreamState, build_sdk_handler

    class Inner:
        def __init__(self) -> None:
            self.seen: list[object] = []

        async def process(self, callback):
            self.seen.append(callback)
            return (200, "OK")

    state = StreamState()
    state.mark_started()
    inner = Inner()
    handler = build_sdk_handler(inner, state)

    class Callback:
        data = {"msgId": "m1"}

    code, message = await handler.process(Callback())
    assert (code, message) == (200, "OK")
    assert inner.seen and isinstance(inner.seen[0], Callback)
    assert state.connected is True, "收到回调即证明连接可用"


async def test_stream_state_only_connects_after_a_callback() -> None:
    """D3 回归：进程起来 ≠ 已连接；只有收到回调才置为 connected。"""
    from ekbs.observability import metrics as m
    from ekbs.stream import StreamState

    m.reset()
    state = StreamState()
    state.mark_started()
    assert state.connected is False
    assert m.current(m.STREAM_CONNECTED) == 0 or m.current(m.STREAM_CONNECTED) == 0.0
    state.mark_connected()
    assert state.connected is True and state.last_message_at is not None
    assert m.current(m.STREAM_CONNECTED) == 1


def test_dev_smoke_mirrors_deploy_smoke_assertions() -> None:
    """开发机冒烟必须与 `deploy/scripts/smoke.sh` **同口径**（否则两套标准会漂移）。

    这里不跑服务，只校验两件事：脚本语法正确、且覆盖同一批断言类别。
    """
    script = REPO / "scripts" / "dev_smoke.sh"
    assert script.is_file()
    syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr
    text = script.read_text(encoding="utf-8")

    # 与生产冒烟一致的检查项
    for marker in (
        "EXPECTED_TABLES=34",
        "EXPECTED_TRIGGERS=12",
        "integrity_check",
        "journal_mode",
        "ekbs_chunk_fts",
        "faiss.read_index",
        "ekbs_shard",
        "/healthz",
        "/readyz",
        "api/v1/qa/chat",
        "--with-qa",
    ):
        assert marker in text, f"开发机冒烟缺少检查项：{marker}"

    # 四个服务都要检查，且用开发机自己的管理器
    for service in ("api", "dingtalk", "worker", "scheduler"):
        assert service in text
    assert "scripts/dev_services.sh" in text

    # 退出码约定与生产一致：0 通过 / 1 至少一项 FAIL
    assert '[ "$FAIL" -eq 0 ]' in text


def test_dev_services_accepts_multiple_service_names() -> None:
    """回归测试：早期 `targets()` 只取第二个参数，`start api worker` 会**静默漏掉** worker。"""
    script = (REPO / "scripts" / "dev_services.sh").read_text(encoding="utf-8")
    assert 'printf \'%s\\n\' "$@"' in script, "必须把全部服务名都展开"
    assert "shift 2>/dev/null" in script
    # 真的跑一次：只列状态，不启停
    result = subprocess.run(
        ["bash", str(REPO / "scripts" / "dev_services.sh"), "status"],
        capture_output=True, text=True, timeout=60, cwd=str(REPO),
    )
    assert result.returncode == 0
    for service in ("api", "dingtalk", "worker", "scheduler"):
        assert service in result.stdout


def test_dev_services_refuses_dual_worker_restart() -> None:
    """停不干净不得再起；禁止靠删 worker.lock 清锁（inode 分裂 → 双 worker）。"""
    script = (REPO / "scripts" / "dev_services.sh").read_text(encoding="utf-8")
    assert "list_worker_pids" in script
    assert "跳过启动以免双开" in script
    assert "禁止删除" in script and "worker.lock" in script
    assert "kill_pid_wait" in script
    assert "保留 pidfile" in script or "未删 pidfile" in script
