"""Stream 消息处理器（`04` §5.1/§5.4/§5.8 + `docs/spec/50` §2 的字段契约）。

用例覆盖「一条消息从回调到回复」的每条分支，且**全程离线**（respx 拦钉钉接口与
ekbs-api 回环调用）。最要紧的三条：

* 未知 userid → 友好回复，**不调用问答 API**；
* 重复 msgId → 不重复回答；
* 可重试失败（API 5xx）→ ACK 非 200 且**释放幂等行**，让钉钉重投能被重新受理。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ekbs.dingtalk.client import (
    CARD_CREATE_PATH,
    CARD_DELIVER_PATH,
    CARD_STREAMING_PATH,
    GROUP_SEND_PATH,
    OTO_SEND_PATH,
    DingTalkOpenApiClient,
)
from ekbs.dingtalk.handler import (
    ACK_OK,
    ACK_RETRY,
    RATE_LIMITED_REPLY,
    USER_NOT_FOUND_REPLY,
    InboundMessage,
    StreamMessageHandler,
)
from ekbs.dingtalk.token import TOKEN_PATH
from ekbs.idempotency import IdempotencyGuard
from ekbs.ratelimit import RateLimiter
from tests.conftest import user_id_of

DING_BASE = "https://api.dingtalk.com"
API_BASE = "http://127.0.0.1:8000"
API_URL = f"{API_BASE}/api/v1/qa/chat"


class FakeTokenClient:
    async def get_token(self, *, force: bool = False) -> str:
        return "tok"


def payload(*, msg_id="msg-1", staff="demo_alice", conversation="cid-1",
            conversation_type="1", text="年假有多少天", msgtype="text", **extra) -> dict:
    data = {
        "msgId": msg_id,
        "senderStaffId": staff,
        "conversationId": conversation,
        "conversationType": conversation_type,
        "msgtype": msgtype,
        "text": {"content": text},
        "senderNick": "Alice",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=x",
    }
    data.update(extra)
    return data


@pytest.fixture
def cards() -> DingTalkOpenApiClient:
    return DingTalkOpenApiClient(
        token_client=FakeTokenClient(), robot_code="robot-key",
        card_template_id="tmpl-ai", retry_base_delay=0.0,
    )


@pytest.fixture
def handler(seeded, cards) -> StreamMessageHandler:
    """用 seed 数据里的 alice 作为已映射用户；卡片流式保持开启。

    注意依赖 ``seeded``（而不是空的 ``runtime``）：没有组织数据就查不到发送者，
    所有用例都会走到「未知用户」分支。
    """
    runtime, _summary = seeded
    runtime.settings.dingtalk["card"] = {
        "ai_template_id": "tmpl-ai", "streaming_enabled": True, "stream_key": "content",
    }
    return StreamMessageHandler(
        db=runtime.db, settings=runtime.settings, secrets=runtime.secrets,
        idempotency=IdempotencyGuard(runtime.db),
        rate_limiter=RateLimiter(runtime.db, runtime.settings),
        cards=cards,
    )


def mock_dingtalk(*, card_ok: bool = True) -> dict[str, respx.Route]:
    routes = {
        "token": respx.post(f"{DING_BASE}{TOKEN_PATH}").mock(
            return_value=httpx.Response(200, json={"accessToken": "tok", "expireIn": 7200})
        ),
        "create": respx.post(f"{DING_BASE}{CARD_CREATE_PATH}").mock(
            return_value=httpx.Response(200, json={"success": True, "result": "ot"})
            if card_ok
            else httpx.Response(500, json={"code": "SystemError"})
        ),
        "deliver": respx.post(f"{DING_BASE}{CARD_DELIVER_PATH}").mock(
            return_value=httpx.Response(200, json={"success": True, "result": []})
            if card_ok
            else httpx.Response(500, json={"code": "SystemError"})
        ),
        "stream": respx.put(f"{DING_BASE}{CARD_STREAMING_PATH}").mock(
            return_value=httpx.Response(200, json={"success": True, "result": True})
        ),
        "oto": respx.post(f"{DING_BASE}{OTO_SEND_PATH}").mock(
            return_value=httpx.Response(200, json={"processQueryKey": "pq"})
        ),
        "group": respx.post(f"{DING_BASE}{GROUP_SEND_PATH}").mock(
            return_value=httpx.Response(200, json={"processQueryKey": "pq"})
        ),
    }
    return routes


def answer_body(*, refused: bool = False) -> dict:
    return {
        "answer": "员工年假按司龄为 5/10/15 天 [1]",
        "citations": [
            {"cite_index": 1, "chunk_id": 6, "doc_id": 2, "doc_title": "年假管理制度", "page_from": 1}
        ],
        "refused": refused,
        "refusal_reason": "" if not refused else "below_threshold",
        "perm_fingerprint": "fp",
    }


def test_inbound_message_parses_verified_field_names() -> None:
    """字段名以 docs/spec/50 §2.3 为准（conversationType 是字符串 "1"/"2"）。"""
    message = InboundMessage.from_payload(payload(conversation_type="1"))
    assert message.msg_id == "msg-1"
    assert message.sender_staff_id == "demo_alice"
    assert message.conversation_type == "1" and message.is_group is False
    assert message.text == "年假有多少天"
    assert InboundMessage.from_payload(payload(conversation_type="2")).is_group is True


@respx.mock
async def test_success_single_chat_streams_card(handler, cards) -> None:
    routes = mock_dingtalk()
    api = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))

    code, message = await handler.process({"data": payload()})
    assert (code, message) == (ACK_OK, "OK")
    assert api.call_count == 1
    # 回环调用必须带内部 token 与用户 ID（权限由服务端算）
    request = api.calls[0].request
    assert request.headers["X-Internal-Token"]
    assert request.headers["X-Internal-User-Id"]
    body = json.loads(request.content)
    assert body["question"] == "年假有多少天" and body["conversation_id"] == "cid-1"

    assert routes["create"].call_count == 1 and routes["deliver"].call_count == 1
    stream_calls = [json.loads(call.request.content) for call in routes["stream"].calls]
    assert stream_calls, "答案应通过卡片流式推送"
    assert all(call["isFull"] is True for call in stream_calls)
    assert stream_calls[-1]["isFinalize"] is True
    assert "参考资料" in stream_calls[-1]["content"]
    assert "年假管理制度 第 1 页" in stream_calls[-1]["content"]


@respx.mock
async def test_duplicate_message_is_not_answered_twice(handler) -> None:
    mock_dingtalk()
    api = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    await handler.process({"data": payload(msg_id="dup-1")})
    code, _ = await handler.process({"data": payload(msg_id="dup-1")})
    assert code == ACK_OK
    assert api.call_count == 1, "重复投递不得重复回答（04 §5.4）"


@respx.mock
async def test_unknown_user_gets_polite_reply_without_asking_api(handler) -> None:
    routes = mock_dingtalk()
    api = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process({"data": payload(staff="ghost-user")})
    assert code == ACK_OK
    assert api.call_count == 0, "未知 userid 不该调用问答 API"
    sent = json.loads(routes["oto"].calls[0].request.content)
    assert USER_NOT_FOUND_REPLY in json.loads(sent["msgParam"])["content"]


@respx.mock
async def test_api_failure_is_retryable_and_releases_idempotency(handler, runtime) -> None:
    """API 5xx：ACK 非 200 + 释放幂等行，让钉钉重投能被重新受理。"""
    mock_dingtalk()
    respx.post(API_URL).mock(return_value=httpx.Response(503, json={"detail": "down"}))
    code, message = await handler.process({"data": payload(msg_id="retry-1")})
    assert code == ACK_RETRY
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM ekbs_msg_dedup WHERE msg_id = 'retry-1'"
        ).fetchone()
    assert row is None, "失败必须释放去重行，否则重投会被当成重复而永久吞掉"

    # 重投（钉钉会重发同一条）现在应能被受理
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code2, _ = await handler.process({"data": payload(msg_id="retry-1")})
    assert code2 == ACK_OK


@respx.mock
async def test_content_blocked_question_is_acked_with_reason(handler) -> None:
    routes = mock_dingtalk()
    respx.post(API_URL).mock(
        return_value=httpx.Response(400, json={"detail": "CONTENT_BLOCKED: 命中敏感词"})
    )
    code, _ = await handler.process({"data": payload(text="公司绝密文件在哪")})
    assert code == ACK_OK
    sent = json.loads(routes["oto"].calls[0].request.content)
    assert "CONTENT_BLOCKED" in json.loads(sent["msgParam"])["content"]


@respx.mock
async def test_refusal_replies_with_plain_message(handler) -> None:
    routes = mock_dingtalk()
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json={**answer_body(refused=True), "answer": "未找到可靠依据。"})
    )
    code, _ = await handler.process({"data": payload()})
    assert code == ACK_OK
    assert routes["oto"].call_count == 1
    assert routes["create"].call_count == 0, "拒答不需要卡片"


@respx.mock
async def test_card_failure_falls_back_to_plain_message(handler) -> None:
    """卡片创建失败不能让用户收不到答案（回落普通消息）。"""
    routes = mock_dingtalk(card_ok=False)
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process({"data": payload()})
    assert code == ACK_OK
    assert routes["oto"].call_count == 1


@respx.mock
async def test_group_message_replies_via_group_endpoint(handler) -> None:
    routes = mock_dingtalk()
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process(
        {"data": payload(conversation_type="2", conversation="cid-group")}
    )
    assert code == ACK_OK
    # 群聊：卡片投放用 IM_GROUP，且没有走单聊接口
    deliver = json.loads(routes["deliver"].calls[0].request.content)
    assert deliver["openSpaceId"] == "dtv1.card//IM_GROUP.cid-group"
    assert routes["oto"].call_count == 0


@respx.mock
async def test_group_without_card_uses_group_message(handler, cards) -> None:
    routes = mock_dingtalk()
    handler.cards.card_template_id = ""  # 未配模板 → 走普通消息
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    await handler.process({"data": payload(conversation_type="2", conversation="cid-g2")})
    body = json.loads(routes["group"].calls[0].request.content)
    assert body["openConversationId"] == "cid-g2"
    assert body["msgKey"] == "sampleMarkdown"


@respx.mock
async def test_message_quota_error_is_skipped(handler) -> None:
    """官方 20001：回调体没有 text/content，必须能安全跳过。"""
    routes = mock_dingtalk()
    api = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process(
        {"data": {"msgId": "q1", "errMsg": "受调用量超量影响，我的消息服务已经暂停"}}
    )
    assert code == ACK_OK
    assert api.call_count == 0 and routes["oto"].call_count == 0


@respx.mock
async def test_non_text_message_is_ignored(handler) -> None:
    routes = mock_dingtalk()
    api = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process(
        {"data": payload(msgtype="picture", text="")}
    )
    assert code == ACK_OK
    assert api.call_count == 0


@respx.mock
async def test_rate_limit_blocks_and_replies(handler, runtime) -> None:
    routes = mock_dingtalk()
    runtime.settings.limits = {"global_qps": 1, "per_user_qps": 0, "per_conversation_qps": 0}
    # 用**冻结时钟**避免窗口抖动：真实时间跨秒会让第二次调用落到新窗口而不再触发限流
    frozen = [1_700_000_000.0]
    handler.rate_limiter = RateLimiter(
        runtime.db, runtime.settings, clock=lambda: frozen[0]
    )
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    await handler.process({"data": payload(msg_id="rl-1")})
    code, _ = await handler.process({"data": payload(msg_id="rl-2")})
    assert code == ACK_OK
    assert routes["oto"].call_count >= 1
    replies = [
        json.loads(json.loads(call.request.content)["msgParam"])["content"]
        for call in routes["oto"].calls
    ]
    assert any(RATE_LIMITED_REPLY in text for text in replies)


@respx.mock
async def test_process_never_raises_on_malformed_callback(handler) -> None:
    mock_dingtalk()
    for weird in ({}, {"data": None}, {"data": {"msgId": None}}, "not-a-dict"):
        code, _ = await handler.process(weird)
        assert code == ACK_OK
