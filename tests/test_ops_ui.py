"""离线运维 UI：状态解析与控制校验（不真正启停服务）。"""

from __future__ import annotations

from ekbs.tools.ops_ui import parse_status, run_services


def test_parse_status_table() -> None:
    stdout = """
SERVICE      STATE    PID
api          running  123
dingtalk     stopped  -
worker       running  456
scheduler    stopped  -
"""
    parsed = parse_status(stdout)
    assert parsed["api"] == {"state": "running", "pid": "123"}
    assert parsed["dingtalk"] == {"state": "stopped", "pid": None}
    assert parsed["worker"]["pid"] == "456"
    assert parsed["scheduler"]["state"] == "stopped"


def test_run_services_rejects_bad_action(monkeypatch, tmp_path) -> None:
    # 即使脚本存在，非法 action 也应被拒绝，不落 shell
    monkeypatch.setattr(
        "ekbs.tools.ops_ui.services_script",
        lambda: tmp_path / "dev_services.sh",
    )
    (tmp_path / "dev_services.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    bad = run_services("explode", "api")
    assert bad["ok"] is False
    assert "非法 action" in str(bad["error"])
