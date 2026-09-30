"""接收检查脚本（`scripts/acceptance.sh`）—— 上线判定的可执行件。

两条断言：**覆盖 DoD 的每一项**，以及**它真的有牙**（配置写坏时必须 NOT-READY）。
一个永远说 READY 的检查比没有检查更危险。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "acceptance.sh"


def test_script_is_valid_and_covers_dod() -> None:
    assert SCRIPT.is_file()
    syntax = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr
    text = SCRIPT.read_text(encoding="utf-8")
    # `01` §0.3 的 DoD 与关键检查项都要在里面
    for marker in (
        "schema", "migrate_permissions", "workers", "l4_enabled", "l3_ingest_enabled",
        "corpus_egress_ack", "dev_smoke.sh", "ekbs.tools.backup", "恢复演练",
        "DASHSCOPE_API_KEY", "DINGTALK_CLIENT_ID", "mineru", "/data/ekbs",
    ):
        assert marker in text, f"接收检查缺少：{marker}"
    # 退出码约定
    assert '[ "$FAIL" -eq 0 ] || exit 1' in text


def test_broken_config_yields_not_ready(tmp_path: Path) -> None:
    """把 workers 改成 2（违反 04 §7.2）→ 必须判 NOT-READY 并指出原因。"""
    text = (REPO / "config" / "application.dev.yaml").read_text(encoding="utf-8")
    broken = tmp_path / "application.yaml"
    broken.write_text(text.replace("workers: 1", "workers: 2"), encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--quick", "--no-tests", "--json"],
        capture_output=True, text=True, timeout=300,
        cwd=str(REPO), env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                            "EKBS_CONFIG": str(broken)},
    )
    assert result.returncode == 1, result.stdout
    assert "NOT-READY" in result.stdout
    assert "workers" in result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["verdict"] == "NOT-READY" and payload["fail"] >= 1


def test_healthy_config_yields_ready(tmp_path: Path) -> None:
    """正常配置（quick + 不跑测试）→ READY，且外部条件单独列出。"""
    result = subprocess.run(
        ["bash", str(SCRIPT), "--quick", "--no-tests", "--json"],
        capture_output=True, text=True, timeout=300,
        cwd=str(REPO), env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                            "EKBS_CONFIG": "config/application.dev.yaml"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["verdict"] == "READY"
    assert payload["fail"] == 0
    # 外部条件必须被计入而不是被忽略
    assert payload["blocked_on_external"] >= 3
    assert "DASHSCOPE_API_KEY" in result.stdout
