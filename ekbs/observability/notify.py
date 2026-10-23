"""告警通知通道（`08` §5.2：钉钉机器人主通道 + 邮件带外通道 + 通道心跳）。

为什么必须带外：告警主通道是钉钉机器人，但当故障正是「Stream 断开/出网中断」时，
**告警与被监控对象同归于尽**（v3 评估 `04`/`07` 两路独立发现）。邮件走 SMTP，
与钉钉出网无共享依赖，是 P0/P1 的保底通道。

配置（全部经 ``~/.ekbs/secrets.env`` 注入，缺省 = 该通道停用，只记日志）：

* 主通道：``EKBS_ALERT_DINGTALK_WEBHOOK``（自定义机器人 webhook），
  可选 ``EKBS_ALERT_DINGTALK_SECRET``（加签密钥，机器人开启加签时必填）；
* 带外：``EKBS_ALERT_SMTP_HOST`` / ``_PORT`` / ``_USER`` / ``_PASSWORD`` /
  ``_FROM`` / ``_TO``（逗号分隔多收件人；端口 465 走 SSL，其余走 STARTTLS）。

纪律：
* **通道失败绝不拖垮主链路**：发送异常只记日志 + ``alert_channel_down`` 安全事件；
* 同一规则的告警有 **30 分钟冷却**（进程内），避免 60s 采集节奏把通道刷爆；
  冷却状态不落盘 —— 进程重启最多补发一次，属可接受代价；
* P0/P1 双通道并发，P2 仅主通道，P3 不推送（与 record_alerts 的分级一致）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from email.message import EmailMessage
from typing import Any, Sequence

import httpx

from ekbs.config import Secrets
from ekbs.db import Database
from ekbs.logging import get_logger
from ekbs.observability.alerts import AlertHit

log = get_logger(component="alert_notify")

#: 同规则推送冷却（秒）
COOLDOWN_SECONDS = 1800
#: 网络超时（秒）：告警通道绝不能阻塞采集作业
TIMEOUT_SECONDS = 5.0

_PUSH_SEVERITIES = {"P1", "P2"}


class AlertNotifier:
    """多通道告警推送器（同步实现，scheduler 作业内直接调用）。"""

    def __init__(self, db: Database, secrets: Secrets, *, http_client: Any = None) -> None:
        self.db = db
        self.webhook = (secrets.get("EKBS_ALERT_DINGTALK_WEBHOOK") or "").strip()
        self.webhook_secret = (secrets.get("EKBS_ALERT_DINGTALK_SECRET") or "").strip()
        self.smtp_host = (secrets.get("EKBS_ALERT_SMTP_HOST") or "").strip()
        self.smtp_port = int((secrets.get("EKBS_ALERT_SMTP_PORT") or "465").strip() or 465)
        self.smtp_user = (secrets.get("EKBS_ALERT_SMTP_USER") or "").strip()
        self.smtp_password = (secrets.get("EKBS_ALERT_SMTP_PASSWORD") or "").strip()
        self.smtp_from = (secrets.get("EKBS_ALERT_SMTP_FROM") or self.smtp_user).strip()
        self.smtp_to = [
            item.strip()
            for item in (secrets.get("EKBS_ALERT_SMTP_TO") or "").split(",")
            if item.strip()
        ]
        self._http = http_client
        self._last_sent: dict[str, float] = {}

    @property
    def channels(self) -> list[str]:
        out = []
        if self.webhook:
            out.append("dingtalk")
        if self.smtp_host and self.smtp_to:
            out.append("email")
        return out

    # ------------------------------------------------------------------
    # 告警推送
    # ------------------------------------------------------------------

    def notify(self, hits: Sequence[AlertHit]) -> int:
        """把命中的告警推到各通道；返回成功发送的通道次数。"""
        sent = 0
        for hit in hits:
            if hit.severity not in _PUSH_SEVERITIES:
                continue
            key = f"{hit.rule.name}|{hit.severity}"
            last = self._last_sent.get(key, 0.0)
            if time.time() - last < COOLDOWN_SECONDS:
                log.info("alert_notify_cooldown", rule=hit.rule.name)
                continue
            title, text = self._render(hit)
            delivered = False
            # P1 双通道并发；P2 仅主通道（降级噪音）
            if self.webhook:
                delivered |= self._send_dingtalk(title, text)
            if hit.severity == "P1" and self.smtp_host and self.smtp_to:
                delivered |= self._send_email(title, text)
            if delivered:
                self._last_sent[key] = time.time()
                sent += 1
            else:
                # 没有任何通道可用/全部失败：这本身必须留痕（告警黑洞）
                log.error("alert_notify_no_channel", rule=hit.rule.name)
        return sent

    def _render(self, hit: AlertHit) -> tuple[str, str]:
        d = hit.as_dict()
        title = f"[EKBS {hit.severity}] {hit.rule.description}"
        lines = [
            f"**级别**：{hit.severity}",
            f"**规则**：{d['description']}",
            f"**指标**：{d['metric']}（{d['kind']}，当前值 {d['value']:.4g}，阈值 {d['comparison']} {d['threshold']:g}）",
            f"**时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        return title, "\n\n".join(lines)

    # ------------------------------------------------------------------
    # 通道实现
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        if self._http is not None:
            return self._http, False
        return httpx.Client(timeout=TIMEOUT_SECONDS), True

    def _send_dingtalk(self, title: str, text: str) -> bool:
        url = self.webhook
        if self.webhook_secret:  # 加签（官方算法：timestamp\nsecret → HmacSHA256 → base64 → urlencode）
            timestamp = str(round(time.time() * 1000))
            sign_src = f"{timestamp}\n{self.webhook_secret}".encode()
            digest = hmac.new(self.webhook_secret.encode(), sign_src, hashlib.sha256).digest()
            import urllib.parse

            sign = urllib.parse.quote_plus(base64.b64encode(digest))
            url = f"{url}&timestamp={timestamp}&sign={sign}"
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
        client, owned = self._client()
        try:
            resp = client.post(url, json=payload)
            body = resp.json() if resp.content else {}
            if resp.status_code == 200 and int(body.get("errcode", -1)) == 0:
                return True
            # 非 2xx 或 errcode != 0 同样是「消息没送出去」：必须留痕，不能只记日志
            self._channel_down(
                "dingtalk", RuntimeError(f"HTTP {resp.status_code} errcode={body.get('errcode')}")
            )
        except Exception as exc:  # noqa: BLE001
            self._channel_down("dingtalk", exc)
        finally:
            if owned:
                client.close()
        return False

    def _send_email(self, title: str, text: str) -> bool:
        import smtplib

        message = EmailMessage()
        message["Subject"] = title
        message["From"] = self.smtp_from
        message["To"] = ", ".join(self.smtp_to)
        message.set_content(text)
        try:
            if self.smtp_port == 465:
                smtp: Any = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=TIMEOUT_SECONDS)
            else:
                smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=TIMEOUT_SECONDS)
                smtp.starttls()
            with smtp:
                if self.smtp_user:
                    smtp.login(self.smtp_user, self.smtp_password)
                smtp.send_message(message)
            return True
        except Exception as exc:  # noqa: BLE001
            self._channel_down("email", exc)
            return False

    def _channel_down(self, channel: str, exc: Exception) -> None:
        log.error("alert_channel_send_failed", channel=channel, error=str(exc)[:200])
        try:
            with self.db.sync_connect() as conn:
                conn.execute(
                    "INSERT INTO ekbs_security_event(event_type, user_id, severity, detail)"
                    " VALUES ('alert_channel_down', NULL, 'high', ?)",
                    (json.dumps({"channel": channel, "error": str(exc)[:300]}, ensure_ascii=False),),
                )
                conn.commit()
        except Exception:  # noqa: BLE001 - 留痕失败也不能再抛
            pass

    # ------------------------------------------------------------------
    # 通道心跳（08 §5.2：每日 09:00 一条测试消息；未送达即降级公告）
    # ------------------------------------------------------------------

    def heartbeat(self) -> dict[str, bool]:
        """逐通道发心跳；返回各通道结果。未配置任何通道时记 warning（告警黑洞风险）。"""
        results: dict[str, bool] = {}
        if not self.channels:
            log.warning("alert_heartbeat_no_channel", note="未配置任何告警通道，P0 故障将无人知晓")
            return results
        text = f"EKBS 告警通道心跳（{time.strftime('%Y-%m-%d %H:%M')}）：本消息仅验证告警可达性。"
        if self.webhook:
            ok = self._send_dingtalk("EKBS 告警心跳", text)
            results["dingtalk"] = ok
            if not ok:
                log.error("alert_heartbeat_failed", channel="dingtalk")
        if self.smtp_host and self.smtp_to:
            ok = self._send_email("EKBS 告警心跳", text)
            results["email"] = ok
            if not ok:
                log.error("alert_heartbeat_failed", channel="email")
        return results
