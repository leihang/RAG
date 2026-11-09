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
import contextlib
import json
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
    format_private_manage_text,
    format_send_source_text,
    is_document_list_command,
    is_example_command,
    is_greeting_command,
    is_help_command,
    is_permission_command,
    is_personal_manage_command,
    is_send_figures_command,
    parse_figure_send_count,
    is_send_source_command,
    is_personal_list_command,
    personal_delete_title,
)
from ekbs.ingest.parser import SUFFIX_ENGINES, normalize_suffix
from ekbs.personal import personal_upload_exceeds, personal_upload_reject_text
from ekbs.ratelimit import RateLimiter

log = get_logger(component="dingtalk.handler")

#: ACK 码：SDK 直接把它回发给钉钉
ACK_OK = 200
#: 单次指令最多发几张原图（「发全图」也按此分批，避免钉钉刷屏）
MAX_REPLY_IMAGES = 8
#: 可提取图片清单在 ekbs_cache 里保留多久（秒）
PENDING_FIGURES_TTL_SECONDS = 2 * 3600
PENDING_FIGURES_KEY_PREFIX = "ekbs:dt:pending_figs:"
ACK_RETRY = 500

#: 官方错误码 20001：调用量超量，回调体没有 text/content
ERR_CODE_MESSAGE_QUOTA = "20001"

USER_NOT_FOUND_REPLY = "未找到你的企业账号，请联系管理员同步组织架构后再试。"
RATE_LIMITED_REPLY = "你的提问过于频繁，请稍后再试。"
SERVICE_ERROR_REPLY = "知识库服务暂时不可用，请稍后重试。"
NO_PENDING_FIGURES_REPLY = (
    "当前没有可提取的图片。请先问一个相关问题，系统会提示可提取张数。"
)
#: 私有库入库只认钉钉 ``msgtype=file``；audio/picture/video 也有 downloadCode，不能靠码判断。
PERSONAL_FILE_MSGTYPE = "file"
#: 原生媒体消息：一律不当「用户要入向量库」。
_MEDIA_MSGTYPES = frozenset({"audio", "picture", "video"})
#: 即便 msgtype=file，这些后缀也是媒体，禁止走私有库入库（png/jpg 虽在解析白名单，钉钉侧不收）。
_MEDIA_FILE_SUFFIXES = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "bmp",
        "heic",
        "tif",
        "tiff",
        "mp3",
        "wav",
        "amr",
        "m4a",
        "aac",
        "ogg",
        "flac",
        "mp4",
        "mov",
        "avi",
        "mkv",
        "webm",
        "m4v",
        "3gp",
    }
)
UNSUPPORTED_MEDIA_UPLOAD_REPLY = (
    "私有库只接收文档（PDF / Word / Excel / PPT / 文本等），"
    "语音、图片、视频不会入库。请发送文档文件，或用文字提问。"
)
AUDIO_NO_RECOGNITION_REPLY = (
    "收到语音，但没识别出文字。请再说一次，或直接打字提问。"
)


def _dingtalk_personal_doc_suffixes() -> frozenset[str]:
    """钉钉私有库可入库后缀：解析白名单减去图片（管理台仍可收图，单聊机器人不收）。"""
    return frozenset(
        suffix.lstrip(".").lower()
        for suffix in SUFFIX_ENGINES
        if suffix.lstrip(".").lower() not in _MEDIA_FILE_SUFFIXES
    )


def is_dingtalk_personal_document(*, msgtype: str, file_name: str, download_code: str) -> bool:
    """是否判定为「用户要入私有向量库」：仅文档类 file 消息。"""
    if str(msgtype or "") != PERSONAL_FILE_MSGTYPE or not str(download_code or "").strip():
        return False
    suffix = normalize_suffix(file_name or "")
    if not suffix or suffix in _MEDIA_FILE_SUFFIXES:
        return False
    return suffix in _dingtalk_personal_doc_suffixes()


def is_dingtalk_media_payload(*, msgtype: str, file_name: str, download_code: str) -> bool:
    """原生语音/图/视频，或文件名是媒体后缀 —— 都不当入库意图。"""
    kind = str(msgtype or "")
    if kind in _MEDIA_MSGTYPES:
        return True
    if not str(download_code or "").strip():
        return False
    suffix = normalize_suffix(file_name or "")
    return bool(suffix and suffix in _MEDIA_FILE_SUFFIXES)


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
        recognition = ""
        if isinstance(content, dict):
            download_code = str(content.get("downloadCode") or "")
            file_name = str(content.get("fileName") or content.get("file_name") or "")
            # 语音 ASR：官方 content.recognition（见钉钉「接收消息」协议）
            recognition = str(content.get("recognition") or "").strip()
        msgtype = str(data.get("msgtype") or "")
        text = str(text_block.get("content") or "") if isinstance(text_block, dict) else ""
        if msgtype == "audio" and recognition or not text.strip() and recognition:
            text = recognition
        return cls(
            msg_id=str(data.get("msgId") or ""),
            sender_staff_id=str(data.get("senderStaffId") or ""),
            conversation_id=str(data.get("conversationId") or ""),
            conversation_type=str(data.get("conversationType") or "1"),
            msgtype=msgtype,
            text=text,
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
    def is_personal_file(self) -> bool:
        """单聊私有库入库入口：仅文档类 file（排除语音/图片/视频）。"""
        return is_dingtalk_personal_document(
            msgtype=self.msgtype,
            file_name=self.file_name,
            download_code=self.download_code,
        )

    @property
    def is_media_payload(self) -> bool:
        """语音/图片/视频（含按文件发来的媒体后缀）。"""
        return is_dingtalk_media_payload(
            msgtype=self.msgtype,
            file_name=self.file_name,
            download_code=self.download_code,
        )

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

    async def handle(self, data: dict[str, Any]) -> ReplyResult:  # noqa: C901 - 历史债：消息路由巨型函数待拆分
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

        if message.is_group:
            # 群聊一律不回复（含问答/帮助/目录/文件/个人库命令）。
            # 仍 ACK 并标记 inbox 完成，避免钉钉重投；不调用任何下游 API、不发消息。
            if message.msg_id:
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
            log.info(
                "dingtalk_group_message_ignored",
                conversation=message.conversation_id,
                msg_id=message.msg_id,
            )
            return ReplyResult(True, reason="group_chat_disabled")

        # ②~⑤ 在同会话串行锁内完成（08 §3.2 per-user FIFO：多轮上下文顺序的前提）
        # 大文件入库不占锁：先即时回执，下载/登记在锁外做，避免「帮助」被 50MB 下载堵死。
        pending_personal_upload: tuple[InboundMessage, int] | None = None
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

            # ③.6 自我介绍：**每用户只发一次**（首次私聊 / 首次在群被 @ 都算）。
            # 发完继续正常处理，不要 return —— 否则用户第一句问的真问题会被吞。
            intro = self._first_session_intro(message)
            if intro:
                await self._reply_text(message, intro)

            if message.msgtype == "audio" and message.text.strip():
                # 语音有 ASR：当提问继续往下走；二进制绝不入私有库。
                log.info(
                    "dingtalk_audio_as_question",
                    msg_id=message.msg_id,
                    text_len=len(message.text.strip()),
                )

            if message.is_personal_file:
                filename = (message.file_name or "").strip() or "未命名文件"
                await self._reply_text(
                    message,
                    f"已收到「{filename}」，正在下载并放入私有库；"
                    "完成后会再通知你。期间可以继续发「帮助」或提问。",
                )
                pending_personal_upload = (message, user_id)
                # 立刻出锁去做下载/登记，避免占住会话通道
            elif message.msgtype == "audio" and not message.text.strip():
                log.info("dingtalk_audio_no_recognition", msg_id=message.msg_id)
                await self._reply_text(message, AUDIO_NO_RECOGNITION_REPLY)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(
                    True,
                    replied=True,
                    via="text",
                    reason="audio_no_recognition",
                    answer=AUDIO_NO_RECOGNITION_REPLY,
                )
            elif message.msgtype != "audio" and (
                message.is_media_payload
                or (
                    message.download_code
                    and message.msgtype != PERSONAL_FILE_MSGTYPE
                )
            ):
                # 图片/视频等禁止误判为「要入向量库」（语音有 ASR 的已在上方放行）。
                log.info(
                    "dingtalk_media_not_ingest",
                    msgtype=message.msgtype,
                    file_name=message.file_name or "",
                    msg_id=message.msg_id,
                )
                await self._reply_text(message, UNSUPPORTED_MEDIA_UPLOAD_REPLY)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(
                    True,
                    replied=True,
                    via="text",
                    reason="unsupported_media",
                    answer=UNSUPPORTED_MEDIA_UPLOAD_REPLY,
                )
            elif message.download_code and message.msgtype == PERSONAL_FILE_MSGTYPE:
                # file 但后缀不在文档白名单（或空后缀）：也不走入库话术。
                log.info(
                    "dingtalk_non_doc_file_skipped",
                    msgtype=message.msgtype,
                    file_name=message.file_name or "",
                    msg_id=message.msg_id,
                )
                await self._reply_text(message, UNSUPPORTED_MEDIA_UPLOAD_REPLY)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(
                    True,
                    replied=True,
                    via="text",
                    reason="unsupported_file_type",
                    answer=UNSUPPORTED_MEDIA_UPLOAD_REPLY,
                )
            elif is_greeting_command(message.text):
                # P0：纯问候只回一句介绍；帮助/文档另行触发，不在首屏堆清单。
                if not intro:
                    await self._reply_text(
                        message,
                        str(
                            self.settings.adapter.self_intro
                            or "你好，我是企业知识库助手。\n"
                            "你可以直接问制度、合同、表格里的具体条款；也可以发送「帮助」"
                        ),
                    )
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="greeting")
            elif is_personal_manage_command(message.text):
                reply = format_private_manage_text()
                await self._reply_text(message, reply)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="personal_manage_help", answer=reply)

            elif is_send_source_command(message.text):
                reply = format_send_source_text()
                await self._reply_text(message, reply)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="send_source_help", answer=reply)

            elif is_send_figures_command(message.text):
                count = parse_figure_send_count(message.text) or 1
                reply = await self._reply_pending_figures(message, count=count)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(
                    True, replied=True, via="text", reason="send_figures", answer=reply
                )

            elif is_permission_command(message.text):
                status, payload = await self._discovery_catalog(
                    user_id=user_id,
                    conversation_type=message.conversation_type,
                    mode="permissions",
                )
                if status == 0 or status >= 500:
                    await asyncio.to_thread(self.inbox.mark_failed, message.msg_id, f"api_error_{status}")
                    return ReplyResult(False, reason=f"api_error_{status}")
                reply = str(payload.get("help_text") or "暂时查不到权限。")
                await self._reply_text(message, reply)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="permission", answer=reply)

            elif is_example_command(message.text):
                status, payload = await self._discovery_catalog(
                    user_id=user_id,
                    conversation_type=message.conversation_type,
                    mode="examples",
                )
                if status == 0 or status >= 500:
                    await asyncio.to_thread(self.inbox.mark_failed, message.msg_id, f"api_error_{status}")
                    return ReplyResult(False, reason=f"api_error_{status}")
                reply = str(payload.get("help_text") or "暂无问答示例，直接提问即可。")
                await self._reply_text(message, reply)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="examples", answer=reply)

            elif message.is_group and (
                is_personal_list_command(message.text)
                or personal_delete_title(message.text) is not None
            ):
                # 群聊回复是公开的：个人库列表/删除只允许单聊，避免把私有文件信息发群。
                reply = "个人文件/私有库仅支持单聊，请私聊我处理。"
                await self._reply_text(message, reply)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="personal_group_blocked", answer=reply)

            elif is_personal_list_command(message.text):
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

            elif (delete_title := personal_delete_title(message.text)) is not None:
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
            elif is_help_command(message.text):
                status, payload = await self._discovery_catalog(
                    user_id=user_id,
                    conversation_type=message.conversation_type,
                    mode="help",
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
                help_text = str(payload.get("help_text") or "发送问题即可查询知识库；回复「帮助」查看导览。")
                await self._reply_text(message, help_text)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="help", answer=help_text)

            elif is_document_list_command(message.text) or is_catalog_query(message.text):
                status, payload = await self._discovery_catalog(
                    user_id=user_id,
                    doc_limit=80,
                    conversation_type=message.conversation_type,
                    mode="docs",
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
                help_text = str(payload.get("help_text") or "发送问题即可查询知识库；回复「帮助」查看导览。")
                await self._reply_text(message, help_text)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(True, replied=True, via="text", reason="catalog", answer=help_text)

            elif file_request_hint(message.text):
                status, payload = await self._api_request(
                    "POST",
                    "/api/v1/discovery/file",
                    user_id=user_id,
                    json_body={"text": message.text, "conversation_type": message.conversation_type},
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

            elif pending_personal_upload is not None:
                # 文件已即时回执；下载/登记在锁外继续，此处不再进问答
                pass

            else:
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
                # 可提取清单：默认不推图，只缓存 + 文案提示张数；用户「发N张」再按序取
                available = self._figure_image_candidates(
                    citations,
                    payload.get("available_figures") or payload.get("figure_attachments"),
                )
                self._save_pending_figures(message, available, total=len(available), sent=0)
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
                    text = self._ensure_figure_inventory_hint(text, len(available))
                    await self._reply_text(message, text)
                    await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                    return ReplyResult(True, replied=True, via="text", reason="refused", answer=text)

                # ⑤ 回复：文字 + 可提取张数提示；图片不主动发
                rendered = self._render_answer(answer, citations)
                rendered = self._with_suggestions(
                    rendered, payload.get("suggested_questions") or []
                )
                rendered = self._ensure_figure_inventory_hint(rendered, len(available))
                if await self._reply_with_card(message, rendered):
                    await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                    return ReplyResult(True, replied=True, via="card", answer=rendered)
                # 无 AI 卡片模板时：OTO 走 sampleMarkdown（接近管理台排版），失败再 sampleText
                replied = await self._reply_text(message, rendered)
                await asyncio.to_thread(self.inbox.mark_done, message.msg_id)
                return ReplyResult(
                    True,
                    replied=replied,
                    via="markdown" if replied else "none",
                    answer=rendered,
                )

        if pending_personal_upload is not None:
            upload_message, upload_user_id = pending_personal_upload
            try:
                reply = await self._accept_personal_file(upload_message, upload_user_id)
            except Exception as exc:  # noqa: BLE001 — 下载/入库失败也要回用户一句
                log.warning(
                    "dingtalk_personal_upload_failed",
                    error=f"{type(exc).__name__}: {exc}"[:200],
                    msg_id=upload_message.msg_id,
                )
                reply = "文件处理失败，请稍后再发一次；也可以先发「帮助」查看用法。"
            await self._reply_text(upload_message, reply)
            await asyncio.to_thread(self.inbox.mark_done, upload_message.msg_id)
            return ReplyResult(
                True,
                replied=True,
                via="text",
                reason="personal_upload",
                answer=reply,
            )

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

    def _first_session_intro(self, message: InboundMessage) -> str:
        """每用户只欢迎一次：该 sender 在钉钉渠道的**第一条**消息才发自我介绍。

        判定依据 ``ekbs_msg_inbox`` —— ``handle()`` 一进来就 ``accept()`` 落库，
        所以首条消息时该 sender 的行数恰为 1。加群瞬间没有可靠事件可订阅
        （钉钉未投递「机器人入群」），故以「首次被 @ / 首次私聊」近似。
        """
        intro = str(self.settings.adapter.self_intro or "")
        if not intro:
            return ""
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ekbs_msg_inbox "
                "WHERE sender_staff_id = ? AND channel = 'dingtalk_robot'",
                (message.sender_staff_id,),
            ).fetchone()
        return intro if int(row["n"] if row is not None else 0) <= 1 else ""

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
        """单聊文件进入本人私有库。群里不收，避免把私人文件摊在群里。

        调用方应已发过「已收到」即时回执；本方法只负责下载/登记后的结果句。
        """
        if message.is_group:
            return "私有库只收单聊文件。请打开和机器人的单独会话，再把文件发过来。"
        if not message.is_personal_file:
            return UNSUPPORTED_MEDIA_UPLOAD_REPLY
        if self.cards is None:
            return "文件通道未就绪，请稍后再发一次。"
        try:
            content = await self.cards.download_message_file(message.download_code)
        except Exception as exc:  # noqa: BLE001 - 下载失败要回一句人话，不能掉进问答
            log.warning("dingtalk_personal_download_failed", error=str(exc)[:160])
            return "没能从钉钉取到这个文件，请再发一次。"
        if not content:
            return "收到的文件是空的，没有存入私有库。"
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
        if status == 0:
            return "私有库暂时连不上，文件没有存入，请稍后再发一次。"
        if status >= 500:
            return "私有库暂时存不进去，请稍后再发一次。"
        if status >= 400:
            return str(payload.get("message") or "文件未能放入私有库，请换一份再发。")
        return str(
            payload.get("message")
            or "文件已收下，正在解析入库；完成后会再通知你。"
        )

    async def _discovery_catalog(
        self,
        *,
        user_id: int,
        doc_limit: int | None = None,
        conversation_type: str = "1",
        mode: str = "full",
    ) -> tuple[int, dict[str, Any]]:
        path = "/api/v1/discovery/catalog"
        query: list[str] = []
        if doc_limit:
            query.append(f"doc_limit={int(doc_limit)}")
        if mode != "full":
            query.append(f"mode={mode}")
        if conversation_type == "2":
            # 群聊只允许公开文档：API 侧会剪掉个人库；单聊保持原 URL 兼容。
            query.append("conversation_type=2")
        if query:
            path = f"{path}?{'&'.join(query)}"
        return await self._api_request("GET", path, user_id=user_id)

    async def _ask_api(self, message: InboundMessage, *, user_id: int) -> tuple[int, dict[str, Any]]:
        body = {
            "question": message.text,
            "stream": False,
            "conversation_id": message.conversation_id or None,
            # 钉钉 auto；重复提问时忽略该问题的旧回答，避免修复后仍被旧单值答案带偏
            "mode": "auto",
            "fresh_on_repeat": True,
            "conversation_type": message.conversation_type,
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

        管道表：AI 卡片支持 Markdown 表，但表头前必须空一行（见
        ``normalize_answer_markdown_tables``）；本方法再做一次规范化，防止缓存/旧答漏空行。
        """
        from ekbs.retrieval.answer_table_format import normalize_answer_markdown_tables

        body = normalize_answer_markdown_tables(answer).strip()
        lines = [body] if body else []
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

    @staticmethod
    def _with_suggestions(text: str, hints: Sequence[Any]) -> str:
        """正常作答后附 2~3 条推荐问（延展 / 相关 / 预判）。

        拒答时后端已经把「你可以试试问」拼进答案正文，这里不重复；正常作答后端只回
        结构化 ``suggested_questions``，由本通道排版展示。
        """
        questions = [str(q).strip() for q in (hints or []) if str(q).strip()][:3]
        if not questions:
            return text
        if "你可以接着问" in text or "你可以试试问" in text:
            return text
        lines = ["", "你可以接着问："]
        for i, question in enumerate(questions, 1):
            lines.append(f"{i}. {question}")
        return text.rstrip() + "\n" + "\n".join(lines)

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
            with contextlib.suppress(Exception):  # pragma: no cover - 失败卡片也可能失败
                await self.cards.fail_card(out_track_id, key)
            return False

    @staticmethod
    def _markdown_title(text: str, *, fallback: str = "知识库回答") -> str:
        """sampleMarkdown 的 title 是首屏透出文案；取首行并去掉标记。"""
        from ekbs.dingtalk.markdown_format import markdown_preview_title

        return markdown_preview_title(text, fallback=fallback)

    async def _reply_text(self, message: InboundMessage, text: str) -> bool:
        """单聊回复（生产主路径：群聊已禁用，卡片模板空时走这里）。

        优先 ``sampleMarkdown``（官方子集：标题/加粗/列表/引用/链接），
        按 https://open.dingtalk.com/document/development/robot-message-type
        收成可渲染排版；管道表转栏目块，``【节】`` 升为四级标题。
        Markdown 发送失败再降级 ``sampleText``。
        """
        from ekbs.dingtalk.markdown_format import format_for_sample_markdown

        if self.cards is None:
            log.info("dingtalk_reply_skipped_no_client", conversation=message.conversation_id)
            return False
        if message.is_group:
            # 入口已拦截群聊；此处兜底，避免误发群。
            log.info("dingtalk_reply_text_skipped_group", conversation=message.conversation_id)
            return False
        body = format_for_sample_markdown(text)
        title = self._markdown_title(body)
        try:
            await self.cards.send_oto_message(
                [message.sender_staff_id],
                MSG_KEY_MARKDOWN,
                markdown_msg_param(title, body),
            )
            return True
        except Exception as md_exc:
            log.warning(
                "dingtalk_markdown_reply_failed",
                error=str(md_exc)[:160],
                fallback="sampleText",
            )
        try:
            await self.cards.send_oto_message(
                [message.sender_staff_id], MSG_KEY_TEXT, text_msg_param(body)
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

    @staticmethod
    def _figure_image_candidates(
        citations: Sequence[dict[str, Any]],
        figure_attachments: Sequence[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """去重后的可提取图列表：优先后端清单，否则引用里的图。"""
        raw: list[dict[str, Any]] = list(figure_attachments or [])
        if not raw:
            raw = [
                c
                for c in citations
                if c.get("figure_id") or c.get("image_path")
            ]
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            rel = str(item.get("image_path") or "").strip()
            if not rel or rel in seen:
                continue
            seen.add(rel)
            out.append(item)
        return out

    @staticmethod
    def _ensure_figure_inventory_hint(text: str, total: int) -> str:
        """答案末尾补可提取张数（后端已写则不重复）。"""
        if total <= 0:
            return text or ""
        body = (text or "").rstrip()
        if "可提取图片" in body:
            return body
        return (
            f"{body}\n\n本回答可提取图片 {total} 张。"
            "需要时回复「发1张」「发3张」或「发全图」（按序提取，不重复）。"
        )

    def _pending_figures_key(self, message: InboundMessage) -> str:
        conv = message.conversation_id or ""
        staff = message.sender_staff_id or ""
        return f"{PENDING_FIGURES_KEY_PREFIX}{conv}:{staff}"

    def _load_pending_figures_state(
        self, message: InboundMessage
    ) -> tuple[list[dict[str, Any]], int, int]:
        """返回 ``(剩余 items, 总张数, 已发张数)``。"""
        key = self._pending_figures_key(message)
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT value, expires_at FROM ekbs_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None or int(row["expires_at"]) < int(time.time()):
            return [], 0, 0
        try:
            payload = json.loads(row["value"])
        except (TypeError, ValueError):
            return [], 0, 0
        if isinstance(payload, list):
            items = payload
            total = len(items)
            sent = 0
        elif isinstance(payload, dict):
            items = payload.get("items") or []
            total = int(payload.get("total") or len(items) or 0)
            sent = int(payload.get("sent") or 0)
        else:
            return [], 0, 0
        if not isinstance(items, list):
            return [], 0, 0
        clean = [
            item for item in items if isinstance(item, dict) and item.get("image_path")
        ]
        if total < sent + len(clean):
            total = sent + len(clean)
        return clean, total, sent

    def _save_pending_figures(
        self,
        message: InboundMessage,
        items: Sequence[dict[str, Any]],
        *,
        total: int | None = None,
        sent: int = 0,
    ) -> None:
        key = self._pending_figures_key(message)
        with self.db.sync_connect() as conn:
            if not items and sent <= 0:
                conn.execute("DELETE FROM ekbs_cache WHERE cache_key = ?", (key,))
            else:
                slim = [
                    {
                        "image_path": str(item.get("image_path") or ""),
                        "figure_id": str(item.get("figure_id") or ""),
                        "doc_title": str(item.get("doc_title") or ""),
                        "page_from": item.get("page_from"),
                        "page_to": item.get("page_to"),
                        "chunk_id": item.get("chunk_id"),
                    }
                    for item in items
                    if str(item.get("image_path") or "").strip()
                ]
                eff_total = int(total if total is not None else sent + len(slim))
                conn.execute(
                    "INSERT INTO ekbs_cache(cache_key, value, expires_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET value = excluded.value, "
                    "expires_at = excluded.expires_at",
                    (
                        key,
                        json.dumps(
                            {
                                "items": slim,
                                "total": eff_total,
                                "sent": int(sent),
                            },
                            ensure_ascii=False,
                        ),
                        int(time.time()) + PENDING_FIGURES_TTL_SECONDS,
                    ),
                )
            conn.commit()

    async def _send_image_batch(
        self,
        message: InboundMessage,
        items: Sequence[dict[str, Any]],
        *,
        limit: int | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        """按序发送最多 ``limit`` 张；返回 ``(成功数, 仍未发出的列表)``。

        已尝试过的路径不会回到 remaining（失败的除外，可下次重试）。
        """
        cap = MAX_REPLY_IMAGES if limit is None else max(0, int(limit))
        if self.cards is None or not items or cap <= 0:
            return 0, list(items)
        data_dir = Path(self.settings.data_dir)
        sent = 0
        deferred: list[dict[str, Any]] = []
        idx = 0
        while idx < len(items) and sent < cap:
            item = items[idx]
            idx += 1
            rel = str(item.get("image_path") or "").strip()
            if not rel:
                continue
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
                deferred.append(item)
        remaining = deferred + list(items[idx:])
        return sent, remaining

    async def _reply_pending_figures(
        self, message: InboundMessage, *, count: int
    ) -> str:
        """按用户指定张数从缓存按序发图（``count=-1`` 表示本批发剩余，受 ``MAX_REPLY_IMAGES`` 限制）。"""
        pending, total, already_sent = self._load_pending_figures_state(message)
        if not pending:
            await self._reply_text(message, NO_PENDING_FIGURES_REPLY)
            return NO_PENDING_FIGURES_REPLY
        limit = min(len(pending), MAX_REPLY_IMAGES) if count < 0 else min(int(count), len(pending), MAX_REPLY_IMAGES)
        sent, remaining = await self._send_image_batch(message, pending, limit=limit)
        new_sent = already_sent + sent
        self._save_pending_figures(
            message, remaining, total=total or (new_sent + len(remaining)), sent=new_sent
        )
        if sent == 0 and remaining:
            reply = "图片发送失败，请稍后再试「发1张」或「发全图」。"
            await self._reply_text(message, reply)
            return reply
        if remaining:
            reply = (
                f"已发送 {sent} 张（累计 {new_sent}/{total or new_sent + len(remaining)}），"
                f"还剩 {len(remaining)} 张。"
                "可继续「发1张」「发3张」或「发全图」。"
            )
        else:
            reply = (
                f"已发送 {sent} 张，"
                f"全部 {total or new_sent} 张可提取图片已发完（不重复）。"
            )
        await self._reply_text(message, reply)
        log.info(
            "dingtalk_pending_figures_replied",
            sent=sent,
            remaining=len(remaining),
            total=total,
            cumulative=new_sent,
            requested=count,
            msg_id=message.msg_id,
        )
        return reply


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
