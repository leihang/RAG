"""钉钉 OpenAPI 客户端 —— 依据 `docs/spec/50-dingtalk.md` §3（**已核实**的逐字段契约）。

覆盖本项目实际要用的四件事：

| 能力 | 端点 | 关键契约 |
|---|---|---|
| 单聊发消息 | ``POST /v1.0/robot/oToMessages/batchSend`` | ``userIds`` ≤20（文档另有 100 的矛盾口径，**取严**）；``msgParam`` 是 **JSON 字符串** |
| 群聊发消息 | ``POST /v1.0/robot/groupMessages/send`` | 必填 ``openConversationId``；``msgParam`` ≤15000 字节 |
| 创建/投放卡片 | ``POST /v1.0/card/instances`` / ``…/instances/deliver`` | ``openSpaceId = dtv1.card//{spaceType}.{spaceId}``；群聊 ``IM_GROUP.{cid}``、单聊 ``IM_ROBOT.{staffId}`` |
| **AI 卡片流式** | ``PUT /v1.0/card/streaming`` | markdown 变量**必须 ``isFull=true``**；``guid`` 幂等；单次 ≤1KB |

鉴权统一用新版规范的头部 ``x-acs-dingtalk-access-token``（不是 ``Authorization: Bearer``），
令牌由 :class:`ekbs.dingtalk.token.DingTalkTokenClient` 提供并缓存。

**两处刻意与 `方案/04` 不同**（官方文档核实结论，见 README §四之二）：

* :meth:`stream_card` 一律发**全量**（``isFull=True``）—— `04` §5.7 的增量写法违反官方约束
  （markdown 必须全量），轻则内容错乱、重则报错；
* :meth:`update_card_data` 把 ``updateCardDataByKey`` 放进 ``cardUpdateOptions`` 里 ——
  `04` §5.7 把它平铺到顶层，SDK 会当作无效字段，实际按「覆盖更新」执行，**会清掉卡片内容**。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from ekbs.errors import ConfigError, ModelUnavailable
from ekbs.logging import get_logger
from ekbs.observability.metrics import CARD_SEND, CARD_STREAMING, inc
from ekbs.provider.http_client import CloudError, request_with_retry, resolve_base_url

log = get_logger(component="dingtalk.client")

DEFAULT_BASE_URL = "https://api.dingtalk.com"

OTO_SEND_PATH = "/v1.0/robot/oToMessages/batchSend"
GROUP_SEND_PATH = "/v1.0/robot/groupMessages/send"
CARD_CREATE_PATH = "/v1.0/card/instances"
CARD_DELIVER_PATH = "/v1.0/card/instances/deliver"
CARD_UPDATE_PATH = "/v1.0/card/instances"
CARD_STREAMING_PATH = "/v1.0/card/streaming"

MSG_KEY_TEXT = "sampleText"
MSG_KEY_MARKDOWN = "sampleMarkdown"

#: 单聊批量发送的人数上限（文档 20 与 100 两个口径，**取严取 20**）
MAX_OTO_USERS = 20
#: ``msgParam`` 字节上限（群聊页给的 15000；单聊页未给，按从严）
MAX_MSG_PARAM_BYTES = 15000
#: 卡片流式的两个上限。
#:
#: ⚠️ 官方原文是「单次 ≤1KB，总量建议 ≤3KB」，但在**全量模式**（``isFull=true``，markdown 强制）
#: 下每次请求携带的都是**累计全文**，这两条无法同时成立 —— 属于官方文档的内部矛盾
#: （`docs/spec/50` §3.5 已记录，需真机核实）。本实现取可自洽的解释：
#: ``MAX_STREAM_CHUNK_BYTES`` 约束**本帧新增文本**，``MAX_STREAM_TOTAL_BYTES`` 约束**累计全文**。
MAX_STREAM_CHUNK_BYTES = 1024
MAX_STREAM_TOTAL_BYTES = 3072


@dataclass
class CardTarget:
    """卡片投放目标（``openSpaceId`` 的构造依据）。"""

    space_type: str  # IM_GROUP | IM_ROBOT
    space_id: str
    robot_code: str

    @property
    def open_space_id(self) -> str:
        return f"dtv1.card//{self.space_type}.{self.space_id}"

    @classmethod
    def group(cls, conversation_id: str, robot_code: str) -> "CardTarget":
        return cls("IM_GROUP", conversation_id, robot_code)

    @classmethod
    def robot(cls, staff_id: str, robot_code: str) -> "CardTarget":
        return cls("IM_ROBOT", staff_id, robot_code)


def text_msg_param(content: str) -> dict[str, str]:
    return {"content": content}


def markdown_msg_param(title: str, text: str) -> dict[str, str]:
    return {"title": title, "text": text}


class DingTalkOpenApiClient:
    def __init__(
        self,
        *,
        token_client: Any,
        robot_code: str,
        base_url: str | None = None,
        card_template_id: str = "",
        timeout_s: float = 5.0,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not robot_code:
            raise ConfigError("缺少 robot_code（钉钉机器人编码，见 01 §7 的 dingtalk.robot_code）")
        self.token_client = token_client
        self.robot_code = robot_code
        self.base_url = resolve_base_url(base_url, DEFAULT_BASE_URL)
        self.card_template_id = card_template_id
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = retry_base_delay
        self._client = client

    # ------------------------------------------------------------------

    async def _post(self, path: str, body: dict[str, Any], *, method: str = "POST") -> dict[str, Any]:
        token = await self.token_client.get_token()
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        owned = self._client is None
        try:
            response = await request_with_retry(
                client, method, f"{self.base_url}{path}",
                provider="dingtalk", endpoint=path.strip("/"),
                kind="llm", max_retries=self.max_retries, base_delay=self.retry_base_delay,
                json_body=body,
                headers={
                    # 新版规范用这个头，不是 Authorization: Bearer（docs/spec/50 §1.2）
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
            )
        except CloudError as exc:
            raise ModelUnavailable(f"钉钉接口失败（{path}）：{exc}") from exc
        finally:
            if owned:
                await client.aclose()
        try:
            return response.json() or {}
        except ValueError:
            return {}

    # ------------------------------------------------------------------
    # 机器人消息
    # ------------------------------------------------------------------

    async def send_oto_message(
        self, user_ids: Sequence[str], msg_key: str, msg_param: dict[str, Any]
    ) -> dict[str, Any]:
        """单聊发送。``flowControlledStaffIdList`` 表示部分被限流 —— **必须检查**。"""
        if not user_ids:
            raise ValueError("userIds 不能为空")
        if len(user_ids) > MAX_OTO_USERS:
            raise ValueError(f"单聊批量发送最多 {MAX_OTO_USERS} 人，收到 {len(user_ids)}")
        payload = self._msg_payload(msg_key, msg_param)
        result = await self._post(
            OTO_SEND_PATH,
            {"robotCode": self.robot_code, "userIds": list(user_ids), **payload},
        )
        if result.get("flowControlledStaffIdList"):
            inc(CARD_SEND, 1, result="flow_controlled")
            log.warning(
                "dingtalk_oto_flow_controlled",
                users=result["flowControlledStaffIdList"],
                note="部分用户被限流，需要退避重试（04 §5.4）",
            )
        else:
            inc(CARD_SEND, 1, result="success")
        return result

    async def send_group_message(
        self, open_conversation_id: str, msg_key: str, msg_param: dict[str, Any]
    ) -> dict[str, Any]:
        """群聊发送（必须 @ 到人，否则群里看不到）。"""
        payload = self._msg_payload(msg_key, msg_param)
        result = await self._post(
            GROUP_SEND_PATH,
            {
                "robotCode": self.robot_code,
                "openConversationId": open_conversation_id,
                **payload,
            },
        )
        inc(CARD_SEND, 1, result="success")
        return result

    @staticmethod
    def _msg_payload(msg_key: str, msg_param: dict[str, Any]) -> dict[str, str]:
        encoded = json.dumps(msg_param, ensure_ascii=False)
        size = len(encoded.encode("utf-8"))
        if size > MAX_MSG_PARAM_BYTES:
            raise ValueError(
                f"msgParam 过大：{size} 字节 > {MAX_MSG_PARAM_BYTES}（钉钉会拒收）"
            )
        return {"msgKey": msg_key, "msgParam": encoded}

    # ------------------------------------------------------------------
    # 卡片
    # ------------------------------------------------------------------

    async def create_card_instance(
        self,
        out_track_id: str,
        card_param_map: dict[str, str],
        *,
        template_id: str | None = None,
        callback_type: str = "STREAM",
        user_id: str | None = None,
    ) -> str:
        template = template_id or self.card_template_id
        if not template:
            raise ConfigError("缺少卡片模板 ID（dingtalk.card.ai_template_id）")
        body: dict[str, Any] = {
            "cardTemplateId": template,
            "outTrackId": out_track_id,
            # cardData 只支持 String 值；key ≤100B、value ≤1KB（docs/spec/50 §3.3）
            "cardData": {"cardParamMap": {k: str(v) for k, v in card_param_map.items()}},
            "callbackType": callback_type,
        }
        if user_id:
            body["userId"] = user_id
        result = await self._post(CARD_CREATE_PATH, body)
        if not result.get("success", True):
            raise ModelUnavailable(f"创建卡片实例失败：{result}")
        return str(result.get("result") or out_track_id)

    async def deliver_card(self, out_track_id: str, target: CardTarget) -> dict[str, Any]:
        body: dict[str, Any] = {
            "outTrackId": out_track_id,
            "openSpaceId": target.open_space_id,
        }
        if target.space_type == "IM_GROUP":
            # 非场景群的企业内部机器人用 AppKey 当 robotCode
            body["imGroupOpenDeliverModel"] = {"robotCode": target.robot_code}
        else:
            body["imRobotOpenDeliverModel"] = {"spaceType": "IM_ROBOT"}
        result = await self._post(CARD_DELIVER_PATH, body)
        spaces = result.get("result") or []
        failed = [s for s in spaces if isinstance(s, dict) and s.get("success") is False]
        if failed:
            log.warning("dingtalk_card_deliver_partial", failed=failed)
        return result

    async def update_card_data(
        self,
        out_track_id: str,
        card_param_map: dict[str, str],
        *,
        by_key: bool = True,
    ) -> bool:
        """更新卡片变量。

        ⚠️ ``updateCardDataByKey`` 必须放在 ``cardUpdateOptions`` 内（`docs/spec/50` §3.4）。
        `04` §5.7 的写法把它平铺到顶层 —— 那会被当作无效字段，服务端按「覆盖更新」执行，
        **把没传的变量清空**（实测口径来自官方字段表，不是推测）。
        """
        body: dict[str, Any] = {
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": {k: str(v) for k, v in card_param_map.items()}},
            "cardUpdateOptions": {"updateCardDataByKey": bool(by_key)},
        }
        result = await self._post(CARD_UPDATE_PATH, body, method="PUT")
        return bool(result.get("success", True))

    async def stream_card(
        self,
        out_track_id: str,
        key: str,
        content: str,
        *,
        is_finalize: bool = False,
        is_error: bool = False,
        guid: str | None = None,
    ) -> bool:
        """AI 卡片流式更新（``PUT /v1.0/card/streaming``）。

        ``isFull=True`` 是**硬约束**：流式变量绑定的是 markdown 时官方要求必须全量，
        否则报 ``param.stream.isFull`` 类错误。因此本方法每次都发全量文本。
        """
        size = len(content.encode("utf-8"))
        if size > MAX_STREAM_TOTAL_BYTES:
            raise ValueError(
                f"流式全文 {size} 字节 > {MAX_STREAM_TOTAL_BYTES}（钉钉会拒收；"
                "更长的答案应改用普通消息分段发送，见 04 §5.8）"
            )
        body = {
            "outTrackId": out_track_id,
            "guid": guid or str(uuid.uuid1()),
            "key": key,
            "content": content,
            "isFull": True,   # markdown 必须全量（04 §5.7 的增量写法是错的）
            "isFinalize": bool(is_finalize),
            "isError": bool(is_error),
        }
        result = await self._post(CARD_STREAMING_PATH, body, method="PUT")
        ok = bool(result.get("success", True)) and bool(result.get("result", True))
        inc(CARD_STREAMING, 1, result="success" if ok else "failure")
        return ok

    async def stream_card_text(
        self, out_track_id: str, key: str, text: str, *, chunk_chars: int = 240
    ) -> int:
        """把长答案按**字节上限**切块流式推送，最后补一帧 ``isFinalize=True``。

        官方限制：单次 ≤1KB、总量建议 ≤3KB。因此这里既按字符切块，又在接近总量上限时停止
        （超出部分由调用方改用普通消息补发，见 `04` §5.8 的长答案分段策略）。
        """
        frames = 0
        accumulated = ""
        for piece in _chunk_text(text, chunk_chars, MAX_STREAM_CHUNK_BYTES):
            if len((accumulated + piece).encode("utf-8")) > MAX_STREAM_TOTAL_BYTES:
                log.warning(
                    "dingtalk_stream_total_limit_reached",
                    sent_bytes=len(accumulated.encode("utf-8")),
                    limit=MAX_STREAM_TOTAL_BYTES,
                    note="超出总量的部分应改用普通消息补发（04 §5.8）",
                )
                break
            accumulated += piece
            await self.stream_card(out_track_id, key, accumulated)
            frames += 1
        await self.stream_card(out_track_id, key, accumulated, is_finalize=True)
        return frames + 1

    async def fail_card(self, out_track_id: str, key: str, content: str = "处理失败") -> bool:
        return await self.stream_card(out_track_id, key, content, is_error=True)


def _chunk_text(text: str, chunk_chars: int, max_bytes: int) -> list[str]:
    """切块：每块（= 每帧新增的文本）的 UTF-8 字节数 ≤ ``max_bytes``，且不超过 ``chunk_chars`` 字符。"""
    chunks: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if len(candidate.encode("utf-8")) > max_bytes or len(candidate) > chunk_chars:
            if current:
                chunks.append(current)
            current = char
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
