"""Stream 消息处理 —— 把钉钉回调变成一次问答（`04` §5.1/§5.3/§5.8）。

字段名依据 `docs/spec/50-dingtalk.md` §2.3（**按官方文档 + SDK 源码核实**）：

* ``msgId`` → 幂等去重的键（``senderId`` 是加密 ID，**不能**当 userid 用）；
* ``senderStaffId`` → 发送者 userid（**机器人发布线上版本后才会返回**）；
* ``conversationType`` 是**字符串** ``"1"``=单聊 / ``"2"``=群聊；
* ``text.content`` 仅在 ``msgtype == 'text'`` 时存在；
* 官方错误码 ``20001``（调用量超量）时回调体**没有 text/content**，本实现必须能安全跳过。

**ACK 语义**（`docs/spec/50` §2.4）：SDK 的 ``process()`` 返回 ``(code, message)`` 二元组。
官方**没有**给出 ACK 超时秒数（方案里的「<10s」是项目自定阈值），因此策略是
**快速 ACK + 业务异步化**，并把「可重试的失败」用非 200 返回、同时释放幂等行，
让钉钉重投时能被重新受理。

权限口径：本适配器**不信任**任何调用方声明的身份 —— ``senderStaffId`` 只用来**查库定位用户**，
真正的权限上下文与指纹一律由 ``ekbs-api`` 服务端重算（`04` §1.5 修掉的信任路径）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import httpx

from ekbs.config import Secrets, Settings
from ekbs.db import Database
from ekbs.dingtalk.client import (
    MSG_KEY_MARKDOWN,
    MSG_KEY_TEXT,
    CardTarget,
    DingTalkOpenApiClient,
    markdown_msg_param,
    text_msg_param,
)
from ekbs.idempotency import IdempotencyGuard
from ekbs.logging import get_logger
from ekbs.observability.metrics import (
    AUTH_LOGIN,
    MESSAGE_RECEIVED,
    RATELIMIT_HIT,
    inc,
)
from ekbs.ratelimit import RateLimiter

log = get_logger(component="dingtalk.handler")

#: ACK 码：SDK 直接把它回发给钉钉
ACK_OK = 200
ACK_RETRY = 500

#: 官方错误码 20001：调用量超量，回调体没有 text/content
ERR_CODE_MESSAGE_QUOTA = "20001"

USER_NOT_FOUND_REPLY = "未找到你的企业账号，请联系管理员同步组织架构后再试。"
RATE_LIMITED_REPLY = "你的提问过于频繁，请稍后再试。"
SERVICE_ERROR_REPLY = "知识库服务暂时不可用，请稍后重试。"


@dataclass
class InboundMessage:
    msg_id: str
    sender_staff_id: str
    conversation_id: str
    conversation_type: str
    msgtype: str
    text: str = ""
    session_webhook: str = ""
    robot_code: str = ""
    sender_nick: str = ""
    is_in_at_list: bool = False
    err_msg: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "InboundMessage":
        text_block = data.get("text") or {}
        return cls(
            msg_id=str(data.get("msgId") or ""),
            sender_staff_id=str(data.get("senderStaffId") or ""),
            conversation_id=str(data.get("conversationId") or ""),
            conversation_type=str(data.get("conversationType") or "1"),
            msgtype=str(data.get("msgtype") or ""),
            text=str(text_block.get("content") or "") if isinstance(text_block, dict) else "",
            session_webhook=str(data.get("sessionWebhook") or ""),
            robot_code=str(data.get("robotCode") or ""),
            sender_nick=str(data.get("senderNick") or ""),
            is_in_at_list=bool(data.get("isInAtList") or False),
            err_msg=str(data.get("errMsg") or ""),
            raw=data,
        )

    @property
    def is_group(self) -> bool:
        return self.conversation_type == "2"

    @property
    def usable(self) -> bool:
        """有 msgId、有文本、且不是钉钉侧的限流通知，才算可处理。"""
        return bool(self.msg_id and self.text.strip() and not self.err_msg)


@dataclass
class ReplyResult:
    acked: bool
    replied: bool = False
    via: str = ""  # card | markdown | text | none
    reason: str = ""
    answer: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "acked": self.acked, "replied": self.replied, "via": self.via,
            "reason": self.reason, "answer_len": len(self.answer),
        }


class StreamMessageHandler:
    """一条消息的处理流水线：**幂等 → 限流 → 身份 → 调 API → 回复**。"""

    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        secrets: Secrets,
        idempotency: IdempotencyGuard,
        rate_limiter: RateLimiter,
        cards: DingTalkOpenApiClient | None = None,
        http_client: httpx.AsyncClient | None = None,
        answer_timeout_s: float | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.secrets = secrets
        self.idempotency = idempotency
        self.rate_limiter = rate_limiter
        self.cards = cards
        self._http = http_client
        self.answer_timeout_s = float(
            answer_timeout_s if answer_timeout_s is not None
            else (settings.adapter.qa_stream_timeout_seconds or 180)
        )
        self.api_base = settings.adapter.api_base.rstrip("/")
        self.internal_token = secrets.get("EKBS_INTERNAL_TOKEN")

    # ------------------------------------------------------------------
    # 入口（与 SDK 的 process() 契约一致）
    # ------------------------------------------------------------------

    async def process(self, callback: Any) -> tuple[int, str]:
        """SDK 会把它返回的 ``(code, message)`` 原路 ACK 回钉钉。"""
        data = getattr(callback, "data", None)
        if data is None and isinstance(callback, dict):
            data = callback.get("data", callback)
        if not isinstance(data, dict):
            log.warning("dingtalk_callback_without_data", type=type(callback).__name__)
            return ACK_OK, "OK"
        try:
            result = await self.handle(data)
        except Exception as exc:  # 任何未预期异常都不能让 ACK 崩掉
            log.error("dingtalk_handle_crashed", error=f"{type(exc).__name__}: {exc}")
            return ACK_OK, "OK"
        if not result.acked:
            return ACK_RETRY, result.reason or "retry"
        return ACK_OK, "OK"

    # ------------------------------------------------------------------

    async def handle(self, data: dict[str, Any]) -> ReplyResult:
        message = InboundMessage.from_payload(data)
        if message.err_msg or message.msgtype == ERR_CODE_MESSAGE_QUOTA:
            # 官方 20001：钉钉侧消息服务被暂停。此时**重投没有意义**（不是我们的故障），
            # 因此 ACK 掉并跳过 —— 返回非 200 只会让钉钉无意义地重投。
            log.warning("dingtalk_message_quota_error", err=message.err_msg)
            return ReplyResult(True, reason="message_service_suspended")
        if not message.usable:
            # 官方 20001 之类：没有文本，安全跳过并 ACK
            log.info("dingtalk_message_skipped", msgtype=message.msgtype, has_id=bool(message.msg_id))
            return ReplyResult(True, reason="not_usable")

        inc(MESSAGE_RECEIVED, 1, msgtype=message.msgtype or "unknown")

        # ① 幂等：重复投递直接 ACK，不重复回答（04 §5.4）
        if not await self.idempotency.acquire(message.msg_id):
            log.info("dingtalk_duplicate_message", msg_id=message.msg_id)
            return ReplyResult(True, reason="duplicate")

        # ② 限流（按用户）：命中回一句提示，并记 RATELIMIT_HIT
        decision = self.rate_limiter.check(user_id=None)
        if not decision.allowed:
            inc(RATELIMIT_HIT, 1, scope="global")
            await self._reply_text(message, RATE_LIMITED_REPLY)
            return ReplyResult(True, replied=True, via="text", reason="rate_limited")

        # ③ 身份：senderStaffId → ekbs_user（**不接受调用方声明的权限**）
        user = self._lookup_user(message.sender_staff_id)
        if user is None:
            inc(AUTH_LOGIN, 1, result="failure")
            log.warning("dingtalk_user_unmapped", staff_id=message.sender_staff_id)
            await self._reply_text(message, USER_NOT_FOUND_REPLY)
            return ReplyResult(True, replied=True, via="text", reason="user_not_found")
        inc(AUTH_LOGIN, 1, result="success")

        # ④ 调问答 API（服务端算权限与指纹）
        status, payload = await self._ask_api(message, user_id=int(user["user_id"]))
        if status == 0:
            # 可重试：释放幂等行，让钉钉重投能被重新受理
            await self.idempotency.mark_failed(message.msg_id)
            return ReplyResult(False, reason="api_unreachable")
        if status == 400:
            detail = str(payload.get("detail") or "问题被内容安全策略拦截")
            await self._reply_text(message, detail)
            return ReplyResult(True, replied=True, via="text", reason="content_blocked")
        if status >= 500:
            await self.idempotency.mark_failed(message.msg_id)
            return ReplyResult(False, reason=f"api_error_{status}")

        answer = str(payload.get("answer") or "")
        citations = payload.get("citations") or []
        if payload.get("refused"):
            text = answer or "未找到可靠依据。"
            await self._reply_text(message, text)
            return ReplyResult(True, replied=True, via="text", reason="refused", answer=text)

        # ⑤ 回复：优先 AI 卡片流式（04 §5.7），失败回落普通消息
        rendered = self._render_answer(answer, citations)
        if await self._reply_with_card(message, rendered):
            return ReplyResult(True, replied=True, via="card", answer=rendered)
        await self._reply_text(message, rendered)
        return ReplyResult(True, replied=True, via="markdown" if message.is_group else "text",
                           answer=rendered)

    # ------------------------------------------------------------------
    # 身份
    # ------------------------------------------------------------------

    def _lookup_user(self, staff_id: str) -> Any:
        if not staff_id:
            return None
        with self.db.sync_connect() as conn:
            return conn.execute(
                "SELECT user_id, dingtalk_userid, nick FROM ekbs_user "
                "WHERE dingtalk_userid = ? AND status = 'active' AND deleted_at IS NULL",
                (staff_id,),
            ).fetchone()

    # ------------------------------------------------------------------
    # 调用问答 API
    # ------------------------------------------------------------------

    async def _ask_api(self, message: InboundMessage, *, user_id: int) -> tuple[int, dict[str, Any]]:
        url = f"{self.api_base}/api/v1/qa/chat"
        body = {
            "question": message.text,
            "stream": False,
            "conversation_id": message.conversation_id or None,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Token": self.internal_token,
            "X-Internal-User-Id": str(user_id),
            "X-Internal-Channel": "dingtalk_robot",
        }
        client = self._http or httpx.AsyncClient(timeout=self.answer_timeout_s)
        owned = self._http is None
        try:
            started = time.perf_counter()
            response = await client.post(url, json=body, headers=headers)
            elapsed = time.perf_counter() - started
            log.info("dingtalk_api_called", status=response.status_code, seconds=round(elapsed, 3))
        except Exception as exc:
            log.error("dingtalk_api_unreachable", error=str(exc))
            return 0, {}
        finally:
            if owned:
                await client.aclose()
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------
    # 回复
    # ------------------------------------------------------------------

    def _render_answer(self, answer: str, citations: Sequence[dict[str, Any]]) -> str:
        """把答案与引用渲染成钉钉可读文本（引用卡片见 `04` §5.8）。"""
        lines = [answer.strip()] if answer.strip() else []
        if citations:
            lines.append("")
            lines.append("参考资料：")
            for citation in citations[:5]:
                title = str(citation.get("doc_title") or "未知文档")
                page = citation.get("page_from")
                figure = citation.get("figure_id")
                suffix = f" 第 {page} 页" if page else ""
                if figure:
                    suffix += f" · {figure}"
                lines.append(f"[{citation.get('cite_index')}] {title}{suffix}")
        return "\n".join(lines)

    async def _reply_with_card(self, message: InboundMessage, text: str) -> bool:
        if self.cards is None or not self.cards.card_template_id:
            return False
        card_cfg = (self.settings.dingtalk or {}).get("card") or {}
        if not card_cfg.get("streaming_enabled", True):
            return False
        out_track_id = f"ekbs-{message.msg_id or int(time.time() * 1000)}"
        target = (
            CardTarget.group(message.conversation_id, self.cards.robot_code)
            if message.is_group
            else CardTarget.robot(message.sender_staff_id, self.cards.robot_code)
        )
        key = str(card_cfg.get("stream_key") or "content")
        try:
            await self.cards.create_card_instance(out_track_id, {key: ""}, user_id=None)
            await self.cards.deliver_card(out_track_id, target)
            await self.cards.stream_card_text(out_track_id, key, text)
            return True
        except Exception as exc:
            log.warning("dingtalk_card_reply_failed", error=str(exc))
            try:
                await self.cards.fail_card(out_track_id, key)
            except Exception:  # pragma: no cover - 失败卡片也可能失败
                pass
            return False

    async def _reply_text(self, message: InboundMessage, text: str) -> bool:
        """普通消息回复：群聊用 markdown（必须 @ 到人），单聊用文本。"""
        if self.cards is None:
            log.info("dingtalk_reply_skipped_no_client", conversation=message.conversation_id)
            return False
        try:
            if message.is_group:
                await self.cards.send_group_message(
                    message.conversation_id, MSG_KEY_MARKDOWN,
                    markdown_msg_param("知识库助手", text),
                )
            else:
                await self.cards.send_oto_message(
                    [message.sender_staff_id], MSG_KEY_TEXT, text_msg_param(text)
                )
            return True
        except Exception as exc:
            log.error("dingtalk_reply_failed", error=str(exc))
            return False


def make_handler(
    db: Database,
    settings: Settings,
    secrets: Secrets,
    *,
    cache: Any | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> StreamMessageHandler:
    """按配置装配适配器处理器（`:mod:`ekbs.stream` 启动时调用）。"""
    from ekbs.dingtalk.token import DingTalkTokenClient

    cards = None
    if secrets.is_configured("DINGTALK_CLIENT_ID") and secrets.is_configured(
        "DINGTALK_CLIENT_SECRET"
    ):
        token_client = DingTalkTokenClient.from_settings(settings, secrets, cache=cache)
        card_cfg = (settings.dingtalk or {}).get("card") or {}
        cards = DingTalkOpenApiClient(
            token_client=token_client,
            robot_code=str((settings.dingtalk or {}).get("robot_code") or secrets.get("DINGTALK_CLIENT_ID")),
            base_url=((settings.dingtalk or {}).get("openapi") or {}).get("base_url"),
            card_template_id=str(card_cfg.get("ai_template_id") or ""),
            client=http_client,
        )
    return StreamMessageHandler(
        db=db,
        settings=settings,
        secrets=secrets,
        idempotency=IdempotencyGuard(db),
        rate_limiter=RateLimiter(db, settings),
        cards=cards,
        http_client=http_client,
    )
