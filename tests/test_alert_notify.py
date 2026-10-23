"""告警通知通道（08 §5.2）：钉钉主通道 + 邮件带外 + 冷却 + 心跳。

重点断言：
* 告警真的推出去（webhook payload 是 markdown，含规则与阈值）；
* 同一规则 30 分钟内不重复推送（60s 采集节奏不能刷爆通道）；
* 邮件通道可独立工作（smoke 用假 SMTP 服务器）；
* 通道失败**留痕**（alert_channel_down）而不是静默失败；
* 未配置通道时是 no-op，不得抛异常（开发环境常态）。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ekbs.config import Secrets
from ekbs.observability import metrics as m
from ekbs.observability.alerts import ALERT_RULES, AlertHit
from ekbs.observability.notify import AlertNotifier

WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=test-token"


def _p1_hit() -> AlertHit:
    rule = next(r for r in ALERT_RULES if r.name == m.QUALITY_DEGRADED)
    return AlertHit(rule=rule, value=1.0, samples=1)


def _notifier(db, env: dict[str, str]) -> AlertNotifier:
    return AlertNotifier(db, Secrets(env))


@respx.mock
def test_dingtalk_alert_pushes_markdown(db) -> None:
    route = respx.post(WEBHOOK).mock(
        return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
    )
    notifier = _notifier(db, {"EKBS_ALERT_DINGTALK_WEBHOOK": WEBHOOK})
    assert notifier.channels == ["dingtalk"]
    assert notifier.notify([_p1_hit()]) == 1

    body = json.loads(route.calls[0].request.content)
    assert body["msgtype"] == "markdown"
    assert "质量" in body["markdown"]["title"]
    assert "P1" in body["markdown"]["text"]


@respx.mock
def test_alert_cooldown_suppresses_duplicate(db) -> None:
    route = respx.post(WEBHOOK).mock(
        return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
    )
    notifier = _notifier(db, {"EKBS_ALERT_DINGTALK_WEBHOOK": WEBHOOK})
    assert notifier.notify([_p1_hit()]) == 1
    assert notifier.notify([_p1_hit()]) == 0, "冷却期内不得重复推送"
    assert route.call_count == 1


@respx.mock
def test_dingtalk_failure_records_channel_down(db) -> None:
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500, json={"errcode": 310000}))
    notifier = _notifier(db, {"EKBS_ALERT_DINGTALK_WEBHOOK": WEBHOOK})
    assert notifier.notify([_p1_hit()]) == 0
    with db.sync_connect() as conn:
        row = conn.execute(
            "SELECT detail FROM ekbs_security_event WHERE event_type = 'alert_channel_down'"
        ).fetchone()
    assert row is not None, "通道失败必须留痕（告警黑洞不能静默）"
    assert "dingtalk" in str(row["detail"])


def test_email_channel_sends_out_of_band(db, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[object] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, message):
            sent.append(("send", message["Subject"]))

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    notifier = _notifier(
        db,
        {
            "EKBS_ALERT_SMTP_HOST": "smtp.example.com",
            "EKBS_ALERT_SMTP_PORT": "465",
            "EKBS_ALERT_SMTP_USER": "bot@example.com",
            "EKBS_ALERT_SMTP_PASSWORD": "pw",
            "EKBS_ALERT_SMTP_TO": "ops@example.com,sec@example.com",
        },
    )
    assert notifier.channels == ["email"]
    assert notifier.notify([_p1_hit()]) == 1
    assert ("connect", "smtp.example.com", 465) in sent
    assert any(item[0] == "send" and "EKBS" in item[1] for item in sent)


@respx.mock
def test_heartbeat_reports_channel_status(db) -> None:
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    notifier = _notifier(db, {"EKBS_ALERT_DINGTALK_WEBHOOK": WEBHOOK})
    assert notifier.heartbeat() == {"dingtalk": True}

    respx.post(WEBHOOK).mock(return_value=httpx.Response(500, json={"errcode": 310000}))
    notifier2 = _notifier(db, {"EKBS_ALERT_DINGTALK_WEBHOOK": WEBHOOK})
    assert notifier2.heartbeat() == {"dingtalk": False}
    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM ekbs_security_event WHERE event_type = 'alert_channel_down'"
        ).fetchone()[0]
    assert int(rows) >= 1


def test_unconfigured_channels_are_noop(db) -> None:
    notifier = _notifier(db, {})
    assert notifier.channels == []
    assert notifier.notify([_p1_hit()]) == 0  # 不抛异常
    assert notifier.heartbeat() == {}
