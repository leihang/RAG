"""Stream 消息处理 —— 把钉钉回调变成一次问答（`04` §5.1/§5.3/§5.8）。

字段名依据 `docs/spec/50-dingtalk.md` §2.3（**按官方文档 + SDK 源码核实**）：

* ``msgId`` → 幂等去重的键（``senderId`` 是加密 ID，**不能**当 userid 用）；
* ``senderStaffId`` → 发送者 userid（**机器人发布线上版本后才会返回**）；
* ``conversationType`` 是**字符串** ``"1"``=单聊 / ``"2"``=群聊；
* ``text.content`` 仅在 ``msgtype == 'text'`` 时存在；
* 官方错误码 ``20001``（调用量超量）时回调体**没有 text/content**，本实现必须能安全跳过。

**ACK 语义**（`docs/spec/50` §2.4 + `00` v4.0 D16/C6）：SDK 的 ``process()`` 返回
``(code, message)`` 二元组。消息**先落库（``ekbs_msg_inbox``）再处理**，处理完成才 ACK；
可重试的失败返回非 200（钉钉会重投），同时状态机登记 failed + 指数退避，
由适配器启动/周期恢复扫描兜底重驱动 —— 崩溃、发布重启不再丢消息（at-least-once）。

权限口径：本适配器**不信任**任何调用方声明的身份 —— ``senderStaffId`` 只用来**查库定位用户**，
真正的权限上下文与指纹一律由 ``ekbs-api`` 服务端重算（`04` §1.5 修掉的信任路径）。
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

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
from ekbs.dingtalk.inbox import MessageInbox
from ekbs.idempotency import IdempotencyGuard
from ekbs.logging import get_logger
from ekbs.observability.metrics import (
    AUTH_LOGIN,
    IDEMPOTENT_HIT,
    MESSAGE_RECEIVED,
    RATELIMIT_HIT,
    inc,
)
from ekbs.discovery import (
    display_doc_title,
    file_request_hint,
    is_catalog_query,
    is_help_command,
    is_personal_list_command,
    personal_delete_title,
)
from ekbs.personal import personal_upload_exceeds, personal_upload_reject_text
from ekbs.ratelimit import RateLimiter

log = get_logger(component="dingtalk.handler")

#: ACK 码：SDK 直接把它回发给钉钉
ACK_OK = 200
#: 答案引用里最多再发几张原图（上传+单聊各一次，控制耗时）
MAX_REPLY_IMAGES = 3
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
    download_code: str = ""
    file_name: str = ""
    session_webhook: str = ""
    robot_code: str = ""
    sender_nick: str = ""
    is_in_at_list: bool = False
    err_msg: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "InboundMessage":
        text_block = data.get("text") or {}
        content = data.get("content") or {}
        if isinstance(content, str):
            try:
                import json
                content = json.loads(content)
            except ValueError:
                content = {}
        download_code = ""
        file_name = ""
        if isinstance(content, dict):
            download_code = str(content.get("downloadCode") or "")
            file_name = str(content.get("fileName") or content.get("file_name") or "")
        return cls(
            msg_id=str(data.get("msgId") or ""),
            sender_staff_id=str(data.get("senderStaffId") or ""),
            conversation_id=str(data.get("conversationId") or ""),
            conversation_type=str(data.get("conversationType") or "1"),
            msgtype=str(data.get("msgtype") or ""),
            text=str(text_block.get("content") or "") if isinstance(text_block, dict) else "",
            download_code=download_code,
            file_name=file_name,
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
        if not self.msg_id or self.err_msg:
            return False
        return bool(self.text.strip() or self.download_code)


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
        idempotency: IdempotencyGuard | None = None,
        rate_limiter: RateLimiter,
        cards: DingTalkOpenApiClient | None = None,
        http_client: httpx.AsyncClient | None = None,
        answer_timeout_s: float | None = None,
        inbox: MessageInbox | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.secrets = secrets
        self.idempotency = idempotency  # 兼容保留（D16 后由 inbox 承担幂等职责）
        self.rate_limiter = rate_limiter
        self.cards = cards
        self._http = http_client
        #: D16/C6 落库状态机（at-least-once）；缺省按库自建，测试可注入自定义预算
        self.inbox = inbox or MessageInbox(db)
        #: 同会话串行锁（08 §3.2 per-user FIFO）
        self._conv_locks: dict[str, asyncio.Lock] = {}
        self.answer_timeout_s = float(
            answer_timeout_s if answer_timeout_s is not None
            else (settings.adapter.qa_stream_timeout_seconds or 180)
        )
        self.api_base = settings.adapter.api_base.rstrip("/")
        self.internal_token = secrets.get("EKBS_INTERNAL_TOKEN")

    def _conv_lock(self, key: str) -> asyncio.Lock:
        """同会话串行锁：同一用户/会话的消息**串行**处理，多轮上下文追加顺序的前提。

        适配器是单进程（D13），进程内锁即可；锁表超 1 万条整体清空防内存缓慢增长
        （代价仅是极端情况下短暂失去串行化，不影响正确性）。
        """
        if len(self._conv_locks) > 10_000:
            self._conv_locks.clear()
        return self._conv_locks.setdefault(key, asyncio.Lock())

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

        # ① D16/C6 落库（00 v4.0）：ACK 前先把消息持久化到 ekbs_msg_inbox。
        #    重复投递/终态直接 ACK；上一次崩溃/失败到期的，由状态机判定 reclaim 重新受理。
        verdict = await asyncio.to_thread(
            self.inbox.accept,
            msg_id=message.msg_id,
            sender_staff_id=message.sender_staff_id,
            conversation_id=message.conversation_id,
            conversation_type=message.conversation_type,
            text=message.text,
        )
        if verdict == "duplicate":
            inc(IDEMPOTENT_HIT, 1)
            log.info("dingtalk_duplicate_message", msg_id=message.msg_id)
            return ReplyResult(True, reason="duplicate")

        # ②~⑤ 在同会话串行锁内完成（08 §3.2 per-user FIFO：多轮上下文顺序的前提）
        async with self._conv_lock(message.conversation_id or message.sender_staff_id):
            await asyncio.to_thread(self.inbox.mark_processing, message.msg_id)

            # ② 限流（按用户）：命中回一句提示，并记 RATELIMIT_HIT
            decision = self.rate_limiter.check(user_id=None)
            if not decision.allowed:
                inc(RATELIMIT_HIT, 1, scope="global")
                await self._reply_text(message, RATE_LIMITED_REPLY)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="rate_limited")

            # ③ 身份：senderStaffId → ekbs_user（**不接受调用方声明的权限**）
            user = self._lookup_user(message.sender_staff_id)
            if user is None:
                inc(AUTH_LOGIN, 1, result="failure")
                log.warning("dingtalk_user_unmapped", staff_id=message.sender_staff_id)
                await self._reply_text(message, USER_NOT_FOUND_REPLY)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="user_not_found")
            inc(AUTH_LOGIN, 1, result="success")
            user_id = int(user["user_id"])

            if message.download_code:
                reply = await self._accept_personal_file(message, user_id)
                await self._reply_text(message, reply)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="personal_upload", answer=reply)

            if is_personal_list_command(message.text):
                status, payload = await self._api_request(
                    "GET", "/api/v1/personal/files", user_id=user_id
                )
                if status == 0 or status >= 500:
                    await asyncio.to_thread(
                        self.inbox.mark_failed, message.msg_id, f"api_error_{status}"
                    )
                    return ReplyResult(False, reason=f"api_error_{status}")
                text = str(payload.get("message") or "暂时列不出私有库。")
                await self._reply_text(message, text)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="personal_list", answer=text)

            delete_title = personal_delete_title(message.text)
            if delete_title is not None:
                status, payload = await self._api_request(
                    "POST",
                    "/api/v1/personal/files/delete",
                    user_id=user_id,
                    json_body={"title": delete_title},
                )
                if status == 0 or status >= 500:
                    await asyncio.to_thread(
                        self.inbox.mark_failed, message.msg_id, f"api_error_{status}"
                    )
                    return ReplyResult(False, reason=f"api_error_{status}")
                text = str(payload.get("message") or "没能删除。")
                await self._reply_text(message, text)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="personal_delete", answer=text)

            # ③.5 D22-P0：欢迎/帮助短路 —— 可见文档 + 示例问（过 ACL，不进问答）
            if is_help_command(message.text):
                status, payload = await self._discovery_catalog(user_id=user_id)
                if status == 0:
                    await asyncio.to_thread(
                        self.inbox.mark_failed, message.msg_id, "api_unreachable"
                    )
                    return ReplyResult(False, reason="api_unreachable")
                if status >= 500:
                    await asyncio.to_thread(
                        self.inbox.mark_failed, message.msg_id, f"api_error_{status}"
                    )
                    return ReplyResult(False, reason=f"api_error_{status}")
                help_text = str(payload.get("help_text") or "发送问题即可查询知识库；回复「帮助」查看导览。")
                await self._reply_text(message, help_text)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="help", answer=help_text)

            if is_catalog_query(message.text):
                status, payload = await self._discovery_catalog(user_id=user_id, doc_limit=80)
                if status == 0:
                    await asyncio.to_thread(
                        self.inbox.mark_failed, message.msg_id, "api_unreachable"
                    )
                    return ReplyResult(False, reason="api_unreachable")
                if status >= 500:
                    await asyncio.to_thread(
                        self.inbox.mark_failed, message.msg_id, f"api_error_{status}"
                    )
                    return ReplyResult(False, reason=f"api_error_{status}")
                help_text = str(payload.get("help_text") or "发送问题即可查询知识库；回复「帮助」查看导览。")
                await self._reply_text(message, help_text)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="catalog", answer=help_text)

            if file_request_hint(message.text):
                status, payload = await self._api_request(
                    "POST",
                    "/api/v1/discovery/file",
                    user_id=user_id,
                    json_body={"text": message.text},
                )
                if status == 0:
                    await asyncio.to_thread(
                        self.inbox.mark_failed, message.msg_id, "api_unreachable"
                    )
                    return ReplyResult(False, reason="api_unreachable")
                if status >= 500:
                    await asyncio.to_thread(
                        self.inbox.mark_failed, message.msg_id, f"api_error_{status}"
                    )
                    return ReplyResult(False, reason=f"api_error_{status}")
                sent_text = await self._reply_requested_file(message, payload)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(
                    True, replied=True, via="file", reason="file", answer=sent_text
                )

            # ④ 调问答 API（服务端算权限与指纹）
            status, payload = await self._ask_api(message, user_id=user_id)
            if status == 0:
                # 可重试：状态机登记 failed + 退避；钉钉重投与恢复扫描都能再受理
                await asyncio.to_thread(
                    self.inbox.mark_failed, message.msg_id, "api_unreachable"
                )
                return ReplyResult(False, reason="api_unreachable")
            if status == 400:
                detail = str(payload.get("detail") or "问题被内容安全策略拦截")
                await self._reply_text(message, detail)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="content_blocked")
            if status >= 500:
                await asyncio.to_thread(
                    self.inbox.mark_failed, message.msg_id, f"api_error_{status}"
                )
                return ReplyResult(False, reason=f"api_error_{status}")

            answer = str(payload.get("answer") or "")
            citations = payload.get("citations") or []
            figure_attachments = payload.get("figure_attachments") or []
            if payload.get("refused"):
                text = answer or "未找到可靠依据。"
                # 后端已可能把 suggested_questions 拼进 answer；若只有结构化字段则补一段
                if "你可以试试问" not in text:
                    hints = payload.get("suggested_questions") or []
                    if hints:
                        lines = ["", "你可以试试问："]
                        for i, q in enumerate(list(hints)[:3], 1):
                            lines.append(f"{i}. {q}")
                        text = text.rstrip() + "\n".join(lines)
                await self._reply_text(message, text)
                # 拒答也可能带回源图（例如文字说「资料无图」但 figure 块其实在）
                await self._reply_cited_images(message, citations, figure_attachments)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="refused", answer=text)

            # ⑤ 回复：取数成功时答案已含「值+依据」；再附参考资料；有图则再发原图
            rendered = self._render_answer(answer, citations)
            if await self._reply_with_card(message, rendered):
                await self._reply_cited_images(message, citations, figure_attachments)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="card", answer=rendered)
            await self._reply_text(message, rendered)
            await self._reply_cited_images(message, citations, figure_attachments)
            await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
            return ReplyResult(True, replied=True,
                               via="markdown" if message.is_group else "text",
                               answer=rendered)

    # ------------------------------------------------------------------
    # 崩溃恢复（D16：启动时与周期任务各调一次，见 ekbs/stream.py）
    # ------------------------------------------------------------------

    async def recover_pending(self, *, limit: int = 20) -> int:
        """把卡在 received / processing 超租约 / failed 退避到期的消息重驱动一遍。

        重驱动走完整 ``handle()`` —— ``accept`` 会把这些行判定为 ``reclaimed`` 重新受理；
        幂等键仍是 msg_id：上次真正 ``mark_done`` 过的行只会判 duplicate，不会重复回答。
        返回本次成功重驱动的条数。
        """
        rows = await asyncio.to_thread(self.inbox.pending_recovery, limit=limit)
        recovered = 0
        for row in rows:
            log.warning(
                "msg_inbox_redrive",
                msg_id=row.msg_id,
                status=row.status,
                retry_count=row.retry_count,
            )
            try:
                result = await self.handle(row.to_callback_payload())
            except Exception as exc:  # noqa: BLE001 - 单条失败不得拖垮整批
                log.error("msg_inbox_redrive_crashed", msg_id=row.msg_id, error=str(exc))
                continue
            if result.acked:
                recovered += 1
        return recovered

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

    def _internal_headers(self, *, user_id: int) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Internal-Token": self.internal_token,
            "X-Internal-User-Id": str(user_id),
            "X-Internal-Channel": "dingtalk_robot",
        }

    async def _api_request(
        self, method: str, path: str, *, user_id: int, json_body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self.api_base}{path}"
        headers = self._internal_headers(user_id=user_id)
        client = self._http or httpx.AsyncClient(timeout=self.answer_timeout_s)
        owned = self._http is None
        try:
            started = time.perf_counter()
            response = await client.request(method, url, json=json_body, headers=headers)
            elapsed = time.perf_counter() - started
            log.info(
                "dingtalk_api_called",
                path=path,
                status=response.status_code,
                seconds=round(elapsed, 3),
            )
        except Exception as exc:
            log.error("dingtalk_api_unreachable", path=path, error=str(exc))
            return 0, {}
        finally:
            if owned:
                await client.aclose()
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload if isinstance(payload, dict) else {}

    async def _accept_personal_file(self, message: InboundMessage, user_id: int) -> str:
        """单聊文件进入本人私有库。群里不收，避免把私人文件摊在群里。"""
        if message.is_group:
            return "私有库只收单聊文件。请打开和机器人的单独会话，再把文件发过来。"
        try:
            content = await self.cards.download_message_file(message.download_code)
        except Exception as exc:  # noqa: BLE001 - 下载失败要回一句人话，不能掉进问答
            log.warning("dingtalk_personal_download_failed", error=str(exc)[:160])
            return "没能从钉钉取到这个文件，请再发一次。"
        if personal_upload_exceeds(len(content), self.settings):
            return personal_upload_reject_text(self.settings) + "。"
        filename = message.file_name or "未命名文件"
        status, payload = await self._api_request(
            "POST",
            "/api/v1/personal/files",
            user_id=user_id,
            json_body={
                "filename": filename,
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        )
        if status == 0 or status >= 500:
            return "私有库暂时存不进去，请稍后再发一次。"
        return str(payload.get("message") or "已放入你的私有库。")

    async def _discovery_catalog(
        self, *, user_id: int, doc_limit: int | None = None
    ) -> tuple[int, dict[str, Any]]:
        path = "/api/v1/discovery/catalog"
        if doc_limit:
            path = f"{path}?doc_limit={int(doc_limit)}"
        return await self._api_request("GET", path, user_id=user_id)

    async def _ask_api(self, message: InboundMessage, *, user_id: int) -> tuple[int, dict[str, Any]]:
        body = {
            "question": message.text,
            "stream": False,
            "conversation_id": message.conversation_id or None,
            # 钉钉 auto；重复提问时忽略该问题的旧回答，避免修复后仍被旧单值答案带偏
            "mode": "auto",
            "fresh_on_repeat": True,
        }
        return await self._api_request(
            "POST", "/api/v1/qa/chat", user_id=user_id, json_body=body
        )

    # ------------------------------------------------------------------
    # 回复
    # ------------------------------------------------------------------

    @staticmethod
    def _cite_leaf(heading_path: str, *, max_len: int = 36) -> str:
        """末级小节展示名：长条款截到句读或 max_len，避免钉钉引用刷屏。"""
        leaf = heading_path.split(" > ")[-1].strip() if heading_path else ""
        if not leaf:
            return ""
        if len(leaf) <= max_len:
            return leaf
        for sep in ("，", ",", "。", "；", ";", "：", ":"):
            cut = leaf.find(sep)
            if 8 <= cut <= max_len:
                return leaf[:cut]
        return leaf[: max_len - 1] + "…"

    def _render_answer(self, answer: str, citations: Sequence[dict[str, Any]]) -> str:
        """把答案与引用渲染成钉钉可读文本（引用卡片见 `04` §5.8）。

        展示规则：去路径前缀的文档名 + 末级小节（heading_path）；同文档同小节去重，
        避免「五条都是同一文件名」的无信息引用列表。
        """
        lines = [answer.strip()] if answer.strip() else []
        if citations:
            lines.append("")
            lines.append("参考资料：")
            seen: set[tuple[Any, ...]] = set()
            shown = 0
            for citation in citations:
                if shown >= 5:
                    break
                title = display_doc_title(citation.get("doc_title"))
                heading = str(citation.get("heading_path") or "").strip()
                leaf = self._cite_leaf(heading)
                if leaf and (leaf == title or leaf in title):
                    leaf = ""
                page = citation.get("page_from")
                figure = citation.get("figure_id")
                key = (
                    citation.get("doc_id"),
                    heading or title,
                    page,
                    figure,
                )
                if key in seen:
                    continue
                seen.add(key)
                label = f"{title} · {leaf}" if leaf else title
                suffix = f" 第 {page} 页" if page else ""
                if figure:
                    suffix += f" · {figure}"
                cite_index = citation.get("cite_index") or (shown + 1)
                lines.append(f"[{cite_index}] {label}{suffix}")
                shown += 1
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

    async def _reply_requested_file(
        self, message: InboundMessage, payload: dict[str, Any]
    ) -> str:
        """点名要原文：先发文件，再补一句说明。对不上就只回说明，不进检索。"""
        action = str(payload.get("action") or "none")
        text = str(payload.get("message") or "没有找到可发送的原文。")
        encoded = str(payload.get("content_base64") or "")
        filename = str(payload.get("filename") or "原文")
        if action == "send" and encoded:
            if self.cards is None:
                text = f"找到了原文「{filename}」，当前通道发不了文件。"
            else:
                try:
                    raw = base64.b64decode(encoded)
                    media_id = await self.cards.upload_file_media(raw, filename=filename)
                    if message.is_group:
                        await self.cards.send_file_group(
                            message.conversation_id, media_id, filename
                        )
                    else:
                        await self.cards.send_file_oto(
                            [message.sender_staff_id], media_id, filename
                        )
                except Exception as exc:  # noqa: BLE001 — 失败也要给人话，不能改走摘要
                    log.warning("dingtalk_file_reply_failed", file=filename, error=str(exc)[:160])
                    text = f"找到了原文「{filename}」，但发到钉钉失败，请稍后再试。"
        await self._reply_text(message, text)
        return text

    async def _reply_cited_images(
        self,
        message: InboundMessage,
        citations: Sequence[dict[str, Any]],
        figure_attachments: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        """把 figure 原图发到钉钉（先 media/upload，再 sampleImageMsg）。

        优先 ``figure_attachments``（找图意图时后端汇总的上下文图），否则用引用里的图。
        """
        if self.cards is None:
            return 0
        candidates: list[dict[str, Any]] = list(figure_attachments or [])
        if not candidates:
            candidates = [
                c
                for c in citations
                if c.get("figure_id") or c.get("image_path")
            ]
        if not candidates:
            return 0
        data_dir = Path(self.settings.data_dir)
        sent = 0
        seen: set[str] = set()
        for item in candidates:
            if sent >= MAX_REPLY_IMAGES:
                break
            rel = str(item.get("image_path") or "").strip()
            if not rel or rel in seen:
                continue
            seen.add(rel)
            abs_path = (data_dir / rel).resolve()
            try:
                abs_path.relative_to(data_dir.resolve())
            except ValueError:
                log.warning("dingtalk_image_path_escape", path=rel)
                continue
            if not abs_path.is_file():
                log.warning("dingtalk_image_missing", path=rel)
                continue
            try:
                media_id = await self.cards.upload_image_media(
                    abs_path.read_bytes(), filename=abs_path.name
                )
                if message.is_group:
                    await self.cards.send_image_group(message.conversation_id, media_id)
                else:
                    await self.cards.send_image_oto([message.sender_staff_id], media_id)
                sent += 1
            except Exception as exc:  # noqa: BLE001 — 单张失败不拖垮整条答复
                log.warning("dingtalk_image_reply_failed", path=rel, error=str(exc)[:160])
        if sent:
            log.info("dingtalk_images_replied", count=sent, msg_id=message.msg_id)
        return sent


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
        # robot_code：yaml 显式值 > secrets.DINGTALK_ROBOT_CODE > AppKey（常见等于 AppKey）
        robot_code = str(
            (settings.dingtalk or {}).get("robot_code")
            or secrets.get("DINGTALK_ROBOT_CODE")
            or secrets.get("DINGTALK_CLIENT_ID")
            or ""
        )
        cards = DingTalkOpenApiClient(
            token_client=token_client,
            robot_code=robot_code,
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
        inbox=MessageInbox(db),
    )
