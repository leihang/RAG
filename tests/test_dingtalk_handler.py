"""Stream 消息处理器（`04` §5.1/§5.4/§5.8 + `docs/spec/50` §2 的字段契约）。

用例覆盖「一条消息从回调到回复」的每条分支，且**全程离线**（respx 拦钉钉接口与
ekbs-api 回环调用）。最要紧的三条：

* 未知 userid → 友好回复，**不调用问答 API**；
* 重复 msgId → 不重复回答；
* 可重试失败（API 5xx）→ ACK 非 200 且**释放幂等行**，让钉钉重投能被重新受理。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from ekbs.dingtalk.client import (
    CARD_CREATE_PATH,
    CARD_DELIVER_PATH,
    CARD_STREAMING_PATH,
    GROUP_SEND_PATH,
    MEDIA_UPLOAD_PATH,
    MSG_KEY_IMAGE,
    OAPI_BASE_URL,
    OTO_SEND_PATH,
    DingTalkOpenApiClient,
)
from ekbs.dingtalk import handler as dingtalk_handler
from ekbs.dingtalk.handler import (
    ACK_OK,
    ACK_RETRY,
    NO_PENDING_FIGURES_REPLY,
    RATE_LIMITED_REPLY,
    USER_NOT_FOUND_REPLY,
    InboundMessage,
    StreamMessageHandler,
)
from ekbs.dingtalk.token import TOKEN_PATH
from ekbs.dingtalk.inbox import MessageInbox
from ekbs.idempotency import IdempotencyGuard
from ekbs.ratelimit import RateLimiter

DING_BASE = "https://api.dingtalk.com"
API_BASE = "http://127.0.0.1:8000"
API_URL = f"{API_BASE}/api/v1/qa/chat"


def oto_body(call_or_param: dict | httpx.Request | object) -> str:
    """兼容 sampleMarkdown（text）与 sampleText（content）。"""
    if hasattr(call_or_param, "request"):
        payload = json.loads(call_or_param.request.content)
        param = json.loads(payload["msgParam"])
    elif isinstance(call_or_param, dict) and "msgParam" in call_or_param:
        param = json.loads(call_or_param["msgParam"])
    elif isinstance(call_or_param, dict):
        param = call_or_param
    else:
        param = {}
    return str(param.get("text") or param.get("content") or "")


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
    # 默认关闭自我介绍，避免所有用例都被多一条问候干扰；专门用例里再显式打开
    runtime.settings.adapter.self_intro = ""
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
    assert body.get("mode") == "auto"

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
    assert USER_NOT_FOUND_REPLY in oto_body(routes["oto"].calls[0])


@respx.mock
async def test_api_failure_retries_via_inbox_backoff(handler, runtime) -> None:
    """API 5xx（D16）：ACK 非 200 + inbox 记 failed；退避到期后重投能被重新受理。"""
    mock_dingtalk()
    api = respx.post(API_URL).mock(return_value=httpx.Response(503, json={"detail": "down"}))
    code, _ = await handler.process({"data": payload(msg_id="retry-1")})
    assert code == ACK_RETRY
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT status, next_retry_at FROM ekbs_msg_inbox WHERE msg_id = 'retry-1'"
        ).fetchone()
        assert row is not None, "失败必须落库登记（D16/C6）"
        assert row["status"] == "failed"
        assert row["next_retry_at"] is not None, "失败必须带退避时间，供重投/恢复扫描"
        # 模拟退避到期（等价于钉钉稍晚重投 / 恢复扫描到点）
        conn.execute(
            "UPDATE ekbs_msg_inbox SET next_retry_at = datetime('now', '-1 seconds')"
            " WHERE msg_id = 'retry-1'"
        )
        conn.commit()

    respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    calls_before = api.call_count
    code2, _ = await handler.process({"data": payload(msg_id="retry-1")})
    assert code2 == ACK_OK
    assert api.call_count == calls_before + 1, "退避到期后重投必须被重新受理（at-least-once）"
    with runtime.db.sync_connect() as conn:
        status = conn.execute(
            "SELECT status FROM ekbs_msg_inbox WHERE msg_id = 'retry-1'"
        ).fetchone()["status"]
    assert status == "done"


@respx.mock
async def test_inbox_recovers_stale_processing(handler, runtime) -> None:
    """崩溃残留（D16 at-least-once 的核心场景）：processing 超租约的行被恢复重驱动，
    用户最终收到回答 —— 旧实现里这条消息会被幂等表永久吞掉。"""
    mock_dingtalk()
    api = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    # 模拟「上一次处理到一半进程崩溃」：直接写入一条超租约的 processing 行
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_msg_inbox(msg_id, sender_staff_id, conversation_id,"
            " conversation_type, text, status, retry_count, updated_at)"
            " VALUES ('crash-1', 'demo_alice', 'cid-1', '1', '年假有多少天',"
            " 'processing', 0, datetime('now', '-1 hour'))"
        )
        conn.commit()

    recovered = await handler.recover_pending()
    assert recovered == 1, "超租约的 processing 行必须被重驱动"
    assert api.call_count == 1, "重驱动必须真正调用问答 API 并回复用户"
    with runtime.db.sync_connect() as conn:
        status = conn.execute(
            "SELECT status FROM ekbs_msg_inbox WHERE msg_id = 'crash-1'"
        ).fetchone()["status"]
    assert status == "done"


@respx.mock
async def test_inbox_deadletter_after_exhausted_retries(handler, runtime) -> None:
    """重试耗尽 → 死信表 + inbox 行终态（防「删档后重投被当新消息」）。"""
    mock_dingtalk()
    respx.post(API_URL).mock(return_value=httpx.Response(503, json={"detail": "down"}))
    handler.inbox = MessageInbox(runtime.db, max_retries=1)  # 压低预算加速耗尽

    code, _ = await handler.process({"data": payload(msg_id="dead-1")})
    assert code == ACK_RETRY  # 首次失败：failed + 退避
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_msg_inbox SET next_retry_at = datetime('now', '-1 seconds')"
            " WHERE msg_id = 'dead-1'"
        )
        conn.commit()
    code2, _ = await handler.process({"data": payload(msg_id="dead-1")})
    assert code2 == ACK_RETRY  # 第二次失败：预算耗尽 → 死信
    with runtime.db.sync_connect() as conn:
        dead = conn.execute(
            "SELECT reason FROM ekbs_msg_deadletter WHERE msg_id = 'dead-1'"
        ).fetchone()
        assert dead is not None, "耗尽后必须入死信表（人工处置入口）"
        row = conn.execute(
            "SELECT status, next_retry_at FROM ekbs_msg_inbox WHERE msg_id = 'dead-1'"
        ).fetchone()
        assert row["status"] == "failed" and row["next_retry_at"] is None
    code3, _ = await handler.process({"data": payload(msg_id="dead-1")})
    assert code3 == ACK_OK  # 终态：重投不再受理，也不报错


@respx.mock
async def test_content_blocked_question_is_acked_with_reason(handler) -> None:
    routes = mock_dingtalk()
    respx.post(API_URL).mock(
        return_value=httpx.Response(400, json={"detail": "CONTENT_BLOCKED: 命中敏感词"})
    )
    code, _ = await handler.process({"data": payload(text="公司绝密文件在哪")})
    assert code == ACK_OK
    assert "CONTENT_BLOCKED" in oto_body(routes["oto"].calls[0])


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
async def test_group_message_is_ignored_without_reply(handler) -> None:
    """群聊一律不回复：不调 QA、不写卡片、不发单聊/群消息。"""
    routes = mock_dingtalk()
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process(
        {"data": payload(conversation_type="2", conversation="cid-group")}
    )
    assert code == ACK_OK
    assert qa.call_count == 0
    assert routes["oto"].call_count == 0
    assert routes["group"].call_count == 0
    assert routes["deliver"].call_count == 0


@respx.mock
async def test_group_message_ignored_even_without_card(handler, cards) -> None:
    routes = mock_dingtalk()
    handler.cards.card_template_id = ""
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process({"data": payload(conversation_type="2", conversation="cid-g2")})
    assert code == ACK_OK
    assert qa.call_count == 0
    assert routes["oto"].call_count == 0
    assert routes["group"].call_count == 0


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
    _ = mock_dingtalk()  # 注册回环路由（本用例不校验路由计数）
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
    replies = [oto_body(call) for call in routes["oto"].calls]
    assert any(RATE_LIMITED_REPLY in text for text in replies)


@respx.mock
async def test_process_never_raises_on_malformed_callback(handler) -> None:
    mock_dingtalk()
    for weird in ({}, {"data": None}, {"data": {"msgId": None}}, "not-a-dict"):
        code, _ = await handler.process(weird)
        assert code == ACK_OK


def test_render_answer_strips_path_shows_heading_dedupes(handler) -> None:
    """引用不得五条同名；应去路径前缀并展示末级小节。"""
    text = handler._render_answer(
        "交班需说明停水停电等事项 [1][2]",
        [
            {
                "cite_index": 1,
                "doc_id": 65,
                "doc_title": "doc文档/XJWY-GC-01-01交接班管理制度",
                "heading_path": "4． 制度 > 4.6 重要事项交接，如：停水停电",
            },
            {
                "cite_index": 2,
                "doc_id": 65,
                "doc_title": "doc文档/XJWY-GC-01-01交接班管理制度",
                "heading_path": "4． 制度 > 4.7 交班人应向接班人详细介绍",
            },
            {
                "cite_index": 3,
                "doc_id": 65,
                "doc_title": "doc文档/XJWY-GC-01-01交接班管理制度",
                "heading_path": "4． 制度 > 4.6 重要事项交接，如：停水停电",
            },
        ],
    )
    assert "doc文档/" not in text
    assert "XJWY-GC-01-01交接班管理制度 · 4.6" in text
    assert "4.7" in text
    assert text.count("参考资料") == 1
    assert text.count("\n[") == 2  # 同小节去重后仅两条


def test_render_answer_keeps_markdown_table_with_blank_line(handler) -> None:
    """AI 卡片路径保留管道表，并保证表前空行。"""
    text = handler._render_answer(
        "明细如下：\n| 项目 | 用量 |\n| --- | --- |\n| 活动用电 | 1343 |\n",
        [],
    )
    assert "如下：\n\n| 项目 |" in text
    assert "| 活动用电 |" in text


@respx.mock
@pytest.mark.asyncio
async def test_reply_text_converts_tables_to_labeled_blocks(handler) -> None:
    """单聊优先 sampleMarkdown；管道表仍转栏目明示块（接近管理台可读性）。"""
    from ekbs.dingtalk.client import MSG_KEY_MARKDOWN

    routes = mock_dingtalk()
    msg = InboundMessage.from_payload(payload(conversation_type="1", text="q"))
    body = "明细：\n| 项目 | 9月用量 |\n| --- | --- |\n| 活动用电 | 1343.00 |\n"
    ok = await handler._reply_text(msg, body)
    assert ok is True
    assert routes["oto"].called
    assert not routes["group"].called
    payload_json = json.loads(routes["oto"].calls.last.request.content.decode("utf-8"))
    assert payload_json.get("msgKey") == MSG_KEY_MARKDOWN
    msg_param = json.loads(payload_json["msgParam"])
    sent_text = str(msg_param.get("text") or msg_param.get("content") or "")
    assert "| 项目 |" not in sent_text
    assert "#### 活动用电" in sent_text
    assert "9月用量：1343.00" in sent_text
    assert msg_param.get("title")


def test_markdown_title_uses_first_line(handler) -> None:
    assert handler._markdown_title("《方案》要点如下：\n\n- a") == "《方案》要点如下："
    assert handler._markdown_title("   \n") == "知识库回答"


@respx.mock
async def test_answer_hints_figure_count_without_autosend(handler, cards) -> None:
    """问答只提示可提取张数，不主动推图；清单写入 pending。"""
    rel = "parse/68/images/qr.png"
    abs_path = handler.settings.data_dir / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x00IEND"
        b"\xaeB`\x82"
    )
    routes = mock_dingtalk(card_ok=False)
    body = answer_body(refused=True)
    body["answer"] = "未找到可靠依据。"
    body["available_figures"] = [
        {"chunk_id": 1, "figure_id": "img-qr", "image_path": rel, "doc_title": "熙物联"}
    ]
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=body))
    code, _ = await handler.process({"data": payload(msg_id="msg-fig-refuse", text="二维码呢")})
    assert code == ACK_OK
    text = oto_body(routes["oto"].calls[0])
    assert "可提取图片 1 张" in text
    image_sends = [
        json.loads(call.request.content)
        for call in routes["oto"].calls
        if json.loads(call.request.content).get("msgKey") == MSG_KEY_IMAGE
    ]
    assert image_sends == []


@respx.mock
async def test_send_n_figures_sequential_no_dup(handler, cards, monkeypatch) -> None:
    """答完缓存清单 → 发1张 → 发2张；按序计数，路径不重复。"""
    monkeypatch.setattr(dingtalk_handler, "MAX_REPLY_IMAGES", 8)
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx"
        b"\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND"
        b"\xaeB`\x82"
    )
    figs = []
    for i in (1, 2, 3):
        rel = f"parse/68/images/demo-{i}.png"
        path = handler.settings.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        figs.append(
            {
                "chunk_id": 100 + i,
                "figure_id": f"img-{i}",
                "image_path": rel,
                "doc_title": "工程汇报",
                "page_from": i,
            }
        )
    routes = mock_dingtalk(card_ok=False)
    respx.post(f"{OAPI_BASE_URL}{MEDIA_UPLOAD_PATH}").mock(
        return_value=httpx.Response(
            200, json={"errcode": 0, "errmsg": "ok", "media_id": "media-batch"}
        )
    )
    body = answer_body()
    body["available_figures"] = figs
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=body))

    code, _ = await handler.process(
        {"data": payload(msg_id="msg-fig-q", text="创新突破有哪些")}
    )
    assert code == ACK_OK
    text = oto_body(routes["oto"].calls[0])
    assert "可提取图片 3 张" in text
    assert not any(
        json.loads(c.request.content).get("msgKey") == MSG_KEY_IMAGE
        for c in routes["oto"].calls
    )

    def _image_count() -> int:
        return sum(
            1
            for call in routes["oto"].calls
            if json.loads(call.request.content).get("msgKey") == MSG_KEY_IMAGE
        )

    def _text_bodies() -> list[str]:
        out = []
        for call in routes["oto"].calls:
            body = json.loads(call.request.content)
            if body.get("msgKey") == MSG_KEY_IMAGE:
                continue
            out.append(oto_body(call))
        return out

    code, _ = await handler.process(
        {"data": payload(msg_id="msg-fig-1", text="发1张")}
    )
    assert code == ACK_OK
    assert _image_count() == 1
    assert "累计 1/3" in _text_bodies()[-1]

    code, _ = await handler.process(
        {"data": payload(msg_id="msg-fig-2", text="发2张")}
    )
    assert code == ACK_OK
    assert _image_count() == 3
    done = _text_bodies()[-1]
    assert "全部" in done and "3" in done


@respx.mock
async def test_send_all_figures_without_pending(handler) -> None:
    """无待提取缓存时，发全图给友好提示，不进问答。"""
    routes = mock_dingtalk(card_ok=False)
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process(
        {"data": payload(msg_id="msg-fig-none", text="发全图")}
    )
    assert code == ACK_OK
    assert qa.call_count == 0
    assert NO_PENDING_FIGURES_REPLY in oto_body(routes["oto"].calls[0])


@respx.mock
async def test_single_chat_file_enters_personal_library(handler) -> None:
    """单聊发文件：先即时「已收到」，再登记私有库；不进问答。"""
    routes = mock_dingtalk(card_ok=False)
    respx.post(f"{DING_BASE}/v1.0/robot/messageFiles/download").mock(
        return_value=httpx.Response(200, json={"downloadUrl": "https://files.example/note.txt"})
    )
    respx.get("https://files.example/note.txt").mock(
        return_value=httpx.Response(200, content="只有本人".encode())
    )
    api = respx.post(f"{API_BASE}/api/v1/personal/files").mock(
        return_value=httpx.Response(200, json={"message": "文件已收下，正在解析入库；完成后会再通知你。"})
    )
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    body = payload(msg_id="file-1", text="", msgtype="file")
    body["content"] = {"downloadCode": "dc-1", "fileName": "note.txt"}
    code, _ = await handler.process({"data": body})
    assert code == ACK_OK
    assert qa.call_count == 0
    assert api.call_count == 1
    texts = [oto_body(c) for c in routes["oto"].calls]
    assert any("已收到「note.txt」" in t for t in texts)
    assert any("解析入库" in t or "私有库" in t for t in texts)


@respx.mock
async def test_single_chat_file_over_configured_limit(handler, seeded) -> None:
    """超限：仍先即时回执，再告知未入库；接受钉钉 OSS 的 http 下载地址。"""
    runtime, _ = seeded
    runtime.settings.limits["max_file_size_mb"] = 1
    routes = mock_dingtalk(card_ok=False)
    blob = b"x" * (1 * 1024 * 1024 + 1)
    respx.post(f"{DING_BASE}/v1.0/robot/messageFiles/download").mock(
        return_value=httpx.Response(200, json={"downloadUrl": "http://files.example/big.txt"})
    )
    respx.get("http://files.example/big.txt").mock(
        return_value=httpx.Response(200, content=blob)
    )
    api = respx.post(f"{API_BASE}/api/v1/personal/files").mock(
        return_value=httpx.Response(200, json={"message": "不应入库"})
    )
    body = payload(msg_id="file-big", text="", msgtype="file")
    body["content"] = {"downloadCode": "dc-big", "fileName": "big.txt"}
    code, _ = await handler.process({"data": body})
    assert code == ACK_OK
    assert api.call_count == 0
    texts = [oto_body(c) for c in routes["oto"].calls]
    assert any("已收到「big.txt」" in t for t in texts)
    assert any("文件超过 1 MB，私有库暂不接收。" in t for t in texts)


@respx.mock
async def test_personal_file_download_failure_notifies_user(handler) -> None:
    """下载失败也要有结果反馈（在「已收到」之后）。"""
    routes = mock_dingtalk(card_ok=False)
    respx.post(f"{DING_BASE}/v1.0/robot/messageFiles/download").mock(
        return_value=httpx.Response(500, json={"errcode": 1, "errmsg": "down"})
    )
    body = payload(msg_id="file-fail", text="", msgtype="file")
    body["content"] = {"downloadCode": "dc-fail", "fileName": "deck.pptx"}
    code, _ = await handler.process({"data": body})
    assert code == ACK_OK
    texts = [oto_body(c) for c in routes["oto"].calls]
    assert any("已收到「deck.pptx」" in t for t in texts)
    assert any("没能从钉钉取到" in t or "处理失败" in t for t in texts)


@respx.mock
async def test_help_not_blocked_while_large_file_downloads(handler) -> None:
    """大文件下载在锁外：同会话「帮助」不必等下载结束。"""
    routes = mock_dingtalk(card_ok=False)
    download_gate = asyncio.Event()
    ack_seen = asyncio.Event()

    async def slow_download(request: httpx.Request) -> httpx.Response:
        ack_seen.set()
        await download_gate.wait()
        return httpx.Response(200, json={"downloadUrl": "https://files.example/deck.pptx"})

    respx.post(f"{DING_BASE}/v1.0/robot/messageFiles/download").mock(side_effect=slow_download)
    respx.get("https://files.example/deck.pptx").mock(
        return_value=httpx.Response(200, content=b"pptx-bytes")
    )
    respx.post(f"{API_BASE}/api/v1/personal/files").mock(
        return_value=httpx.Response(
            200, json={"message": "文件已收下，正在解析入库；完成后会再通知你。"}
        )
    )
    respx.get(f"{API_BASE}/api/v1/discovery/catalog?mode=help").mock(
        return_value=httpx.Response(
            200, json={"help_text": "帮助导览内容", "documents": [], "suggestions": []}
        )
    )
    file_body = payload(msg_id="file-slow", text="", msgtype="file", conversation="cid-busy")
    file_body["content"] = {"downloadCode": "dc-slow", "fileName": "deck.pptx"}
    file_task = asyncio.create_task(handler.process({"data": file_body}))
    await asyncio.wait_for(ack_seen.wait(), timeout=2.0)
    help_code, _ = await handler.process(
        {"data": payload(msg_id="help-during", text="帮助", conversation="cid-busy")}
    )
    assert help_code == ACK_OK
    texts_mid = [oto_body(c) for c in routes["oto"].calls]
    assert any("已收到「deck.pptx」" in t for t in texts_mid)
    assert any("帮助导览内容" in t for t in texts_mid)
    download_gate.set()
    file_code, _ = await asyncio.wait_for(file_task, timeout=2.0)
    assert file_code == ACK_OK
    texts_end = [oto_body(c) for c in routes["oto"].calls]
    assert any("解析入库" in t for t in texts_end)


@respx.mock
async def test_group_file_is_ignored(handler) -> None:
    routes = mock_dingtalk()
    download = respx.post(f"{DING_BASE}/v1.0/robot/messageFiles/download").mock(
        return_value=httpx.Response(200, json={"downloadUrl": "https://files.example/note.txt"})
    )
    body = payload(msg_id="file-group", text="", msgtype="file", conversation_type="2")
    body["content"] = {"downloadCode": "dc-2", "fileName": "note.txt"}
    code, _ = await handler.process({"data": body})
    assert code == ACK_OK
    assert download.call_count == 0
    assert routes["group"].call_count == 0
    assert routes["oto"].call_count == 0


@respx.mock
async def test_audio_recognition_goes_to_qa_not_personal_library(handler) -> None:
    """截图场景：语音带 recognition，应问答，绝不「未命名文件」入库。"""
    routes = mock_dingtalk(card_ok=False)
    download = respx.post(f"{DING_BASE}/v1.0/robot/messageFiles/download").mock(
        return_value=httpx.Response(200, json={"downloadUrl": "https://files.example/voice.ogg"})
    )
    personal = respx.post(f"{API_BASE}/api/v1/personal/files").mock(
        return_value=httpx.Response(200, json={"message": "不应入库"})
    )
    qa_payload = answer_body()
    qa_payload["answer"] = "10 月总用电量见资料。"
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=qa_payload))
    body = payload(msg_id="audio-asr", text="", msgtype="audio")
    body["content"] = {
        "downloadCode": "dc-audio",
        "duration": 12000,
        "recognition": "帮我查一下10月份期间的总用电量",
    }
    code, _ = await handler.process({"data": body})
    assert code == ACK_OK
    assert download.call_count == 0
    assert personal.call_count == 0
    assert qa.call_count == 1
    asked = json.loads(qa.calls[0].request.content.decode())
    assert "10月份期间的总用电量" in asked["question"]
    texts = [oto_body(c) for c in routes["oto"].calls]
    assert not any("放入私有库" in t or "未命名文件" in t for t in texts)
    assert any("10 月总用电量见资料" in t for t in texts)


@respx.mock
async def test_audio_without_recognition_does_not_ingest(handler) -> None:
    from ekbs.dingtalk.handler import AUDIO_NO_RECOGNITION_REPLY

    routes = mock_dingtalk(card_ok=False)
    download = respx.post(f"{DING_BASE}/v1.0/robot/messageFiles/download").mock(
        return_value=httpx.Response(200, json={"downloadUrl": "https://files.example/voice.ogg"})
    )
    personal = respx.post(f"{API_BASE}/api/v1/personal/files").mock(
        return_value=httpx.Response(200, json={"message": "不应入库"})
    )
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    body = payload(msg_id="audio-empty", text="", msgtype="audio")
    body["content"] = {"downloadCode": "dc-audio", "duration": 3}
    code, _ = await handler.process({"data": body})
    assert code == ACK_OK
    assert download.call_count == 0
    assert personal.call_count == 0
    assert qa.call_count == 0
    texts = [oto_body(c) for c in routes["oto"].calls]
    assert any(AUDIO_NO_RECOGNITION_REPLY in t for t in texts)
    assert not any("放入私有库" in t for t in texts)


@respx.mock
async def test_picture_with_download_code_does_not_enter_personal_library(handler) -> None:
    """图片同理：有 downloadCode 也不进私有库。"""
    from ekbs.dingtalk.handler import UNSUPPORTED_MEDIA_UPLOAD_REPLY

    routes = mock_dingtalk(card_ok=False)
    download = respx.post(f"{DING_BASE}/v1.0/robot/messageFiles/download").mock(
        return_value=httpx.Response(200, json={"downloadUrl": "https://files.example/a.png"})
    )
    api = respx.post(f"{API_BASE}/api/v1/personal/files").mock(
        return_value=httpx.Response(200, json={"message": "不应入库"})
    )
    body = payload(msg_id="pic-1", text="", msgtype="picture")
    body["content"] = {"downloadCode": "dc-pic"}
    code, _ = await handler.process({"data": body})
    assert code == ACK_OK
    assert download.call_count == 0
    assert api.call_count == 0
    texts = [oto_body(c) for c in routes["oto"].calls]
    assert any(UNSUPPORTED_MEDIA_UPLOAD_REPLY in t for t in texts)
    assert not any("放入私有库" in t for t in texts)


@respx.mock
async def test_media_file_suffix_does_not_enter_personal_library(handler) -> None:
    """按「文件」发出的语音/图片也不入库（后缀判定，避免误判入库意图）。"""
    from ekbs.dingtalk.handler import UNSUPPORTED_MEDIA_UPLOAD_REPLY

    routes = mock_dingtalk(card_ok=False)
    download = respx.post(f"{DING_BASE}/v1.0/robot/messageFiles/download").mock(
        return_value=httpx.Response(200, json={"downloadUrl": "https://files.example/x"})
    )
    api = respx.post(f"{API_BASE}/api/v1/personal/files").mock(
        return_value=httpx.Response(200, json={"message": "不应入库"})
    )
    for msg_id, name in (("media-amr", "留言.amr"), ("media-png", "截图.png"), ("media-mp4", "演示.mp4")):
        body = payload(msg_id=msg_id, text="", msgtype="file")
        body["content"] = {"downloadCode": f"dc-{msg_id}", "fileName": name}
        code, _ = await handler.process({"data": body})
        assert code == ACK_OK
    assert download.call_count == 0
    assert api.call_count == 0
    texts = [oto_body(c) for c in routes["oto"].calls]
    assert sum(1 for t in texts if UNSUPPORTED_MEDIA_UPLOAD_REPLY in t) >= 3
    assert not any("放入私有库" in t for t in texts)


def test_dingtalk_personal_document_helpers() -> None:
    from ekbs.dingtalk.handler import (
        is_dingtalk_media_payload,
        is_dingtalk_personal_document,
    )

    assert is_dingtalk_personal_document(
        msgtype="file", file_name="制度.pdf", download_code="dc"
    )
    assert not is_dingtalk_personal_document(
        msgtype="file", file_name="拍立得.png", download_code="dc"
    )
    assert not is_dingtalk_personal_document(
        msgtype="audio", file_name="", download_code="dc"
    )
    assert is_dingtalk_media_payload(msgtype="video", file_name="", download_code="dc")
    assert is_dingtalk_media_payload(
        msgtype="file", file_name="a.m4a", download_code="dc"
    )


@respx.mock
async def test_personal_list_command_does_not_enter_qa(handler) -> None:
    routes = mock_dingtalk()
    respx.get(f"{API_BASE}/api/v1/personal/files").mock(
        return_value=httpx.Response(200, json={"message": "你的私有库还是空的。"})
    )
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process({"data": payload(msg_id="plist-1", text="我的文件")})
    assert code == ACK_OK
    assert qa.call_count == 0
    assert "私有库" in oto_body(routes["oto"].calls[0])


@respx.mock
async def test_accessible_doc_list_uses_catalog_not_qa_or_file(handler) -> None:
    """「列出我可以查询/查阅的文件清单」回可见文档，不进问答，也不发原文。"""
    mock_dingtalk()
    catalog = respx.get(f"{API_BASE}/api/v1/discovery/catalog?doc_limit=80&mode=docs").mock(
        return_value=httpx.Response(
            200,
            json={"help_text": "你当前可见的文档：\n1. 年假管理制度", "documents": [], "suggestions": []},
        )
    )
    file_route = respx.post(f"{API_BASE}/api/v1/discovery/file").mock(
        return_value=httpx.Response(200, json={"action": "none", "message": "不应发原文"})
    )
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))

    for index, text in enumerate(
        (
            "列出我可以查询文件的目录清单",
            "列出我可以查阅文件的清单",
            "我可以查询哪些文件",
        )
    ):
        result = await handler.handle(payload(msg_id=f"cat-list-{index}", text=text))
        assert result.reason == "catalog", text
        assert "年假管理制度" in result.answer

    assert catalog.call_count == 3
    assert file_route.call_count == 0
    assert qa.call_count == 0

    sent = await handler.handle(
        payload(msg_id="cat-send-1", text="把年假管理制度文件发给我")
    )
    assert sent.reason == "file"
    assert file_route.call_count == 1
    assert catalog.call_count == 3
    assert qa.call_count == 0

    asked = await handler.handle(payload(msg_id="cat-qa-1", text="年假制度是什么"))
    assert asked.reason != "catalog"
    assert qa.call_count == 1
    assert catalog.call_count == 3
    assert file_route.call_count == 1


INTRO_TEXT = "你好，我是企业知识库助手，可以问我制度/合同/能耗。"


@respx.mock
async def test_first_message_gets_self_intro(handler) -> None:
    """首次会话（每用户只一次）先发自我介绍，再正常回答——问候不能替掉问题。"""
    handler.settings.adapter.self_intro = INTRO_TEXT
    routes = mock_dingtalk()
    api = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))

    code, _ = await handler.process({"data": payload(msg_id="intro-first")})
    assert code == ACK_OK
    assert api.call_count == 1, "自我介绍之后仍要回答用户的问题"
    oto = [json.loads(call.request.content) for call in routes["oto"].calls]
    assert len(oto) == 1
    assert INTRO_TEXT[:10] in json.dumps(oto[0], ensure_ascii=False)


@respx.mock
async def test_self_intro_only_once_per_user(handler) -> None:
    """每用户只欢迎一次：同一个人第二条消息不再发自我介绍。"""
    handler.settings.adapter.self_intro = INTRO_TEXT
    routes = mock_dingtalk()
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))

    await handler.process({"data": payload(msg_id="intro-a")})
    await handler.process({"data": payload(msg_id="intro-b")})
    assert routes["oto"].call_count == 1, "同一用户第二次会话不得重复欢迎"


@respx.mock
async def test_self_intro_disabled_when_empty(handler) -> None:
    """``self_intro`` 为空 = 关闭（默认不打扰用户）。"""
    handler.settings.adapter.self_intro = ""
    routes = mock_dingtalk()
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))

    await handler.process({"data": payload(msg_id="intro-off")})
    assert routes["oto"].call_count == 0


def test_answer_appends_recommended_questions_on_success() -> None:
    """正常作答后附 2~3 条推荐问（「你可以接着问：」），最多 3 条、拒答提示不重复。"""
    text = StreamMessageHandler._with_suggestions(
        "答案正文。", ["问题 A", "问题 B", "", "问题 C", "问题 D"]
    )
    assert "你可以接着问" in text
    assert "1. 问题 A" in text
    assert "2. 问题 B" in text
    assert "3. 问题 C" in text
    assert "问题 D" not in text, "最多 3 条"
    # 拒答时后端已拼过「你可以试试问」，不重复追加
    refused = "对不起…\n你可以试试问：\n1. x"
    assert StreamMessageHandler._with_suggestions(refused, ["y"]) == refused
    # 没有推荐问时原样返回
    assert StreamMessageHandler._with_suggestions("答案。", []) == "答案。"


@respx.mock
async def test_group_personal_list_never_calls_personal_api(handler) -> None:
    """群聊「我的文件」不回复、也不调私有库 API。"""
    routes = mock_dingtalk()
    personal = respx.get(f"{API_BASE}/api/v1/personal/files").mock(
        return_value=httpx.Response(200, json={"message": "不应调用"})
    )
    code, _ = await handler.process(
        {"data": payload(msg_id="plist-group", text="我的文件", conversation_type="2", conversation="cid-pg")}
    )
    assert code == ACK_OK
    assert personal.call_count == 0
    assert routes["group"].call_count == 0


@respx.mock
async def test_group_help_never_calls_catalog(handler) -> None:
    """群聊「帮助」不回复、也不调 catalog。"""
    routes = mock_dingtalk()
    catalog = respx.get(f"{API_BASE}/api/v1/discovery/catalog").mock(
        return_value=httpx.Response(200, json={"help_text": "不应调用", "documents": [], "suggestions": []})
    )
    code, _ = await handler.process(
        {"data": payload(msg_id="help-group", text="帮助", conversation_type="2", conversation="cid-hg")}
    )
    assert code == ACK_OK
    assert catalog.call_count == 0
    assert routes["group"].call_count == 0


@respx.mock
async def test_group_qa_never_calls_qa_api(handler) -> None:
    """群聊问答不回复、也不调 /qa/chat。"""
    routes = mock_dingtalk()
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process(
        {"data": payload(msg_id="qa-group", text="年假有多少天", conversation_type="2", conversation="cid-qg")}
    )
    assert code == ACK_OK
    assert qa.call_count == 0
    assert routes["group"].call_count == 0


@respx.mock
async def test_greeting_returns_only_short_intro(handler) -> None:
    """P0：纯问候只回一句介绍，不拉帮助/文档，也不进问答。"""
    routes = mock_dingtalk()
    handler.settings.adapter.self_intro = (
        "你好，我是企业知识库助手。\n"
        "你可以直接问制度、合同、表格里的具体条款；也可以发送「帮助」"
    )
    catalog = respx.get(f"{API_BASE}/api/v1/discovery/catalog?mode=help").mock(
        return_value=httpx.Response(200, json={"help_text": "不应调用", "documents": [], "suggestions": []})
    )
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process({"data": payload(msg_id="greet-1", text="你好")})
    assert code == ACK_OK
    assert catalog.call_count == 0
    assert qa.call_count == 0
    assert routes["oto"].call_count == 1
    assert oto_body(routes["oto"].calls[0]) == (
        "你好，我是企业知识库助手。\n"
        "你可以直接问制度、合同、表格里的具体条款；也可以发送「帮助」"
    )


@respx.mock
async def test_permission_command_uses_permissions_mode(handler) -> None:
    routes = mock_dingtalk()
    catalog = respx.get(
        f"{API_BASE}/api/v1/discovery/catalog?mode=permissions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"help_text": "可查文档 2 篇，密级 1~3：维修作业指导书", "documents": [], "suggestions": []},
        )
    )
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process({"data": payload(msg_id="perm-cmd", text="查询权限")})
    assert code == ACK_OK
    assert catalog.call_count == 1
    assert qa.call_count == 0
    assert "可查文档" in oto_body(routes["oto"].calls[0])


@respx.mock
async def test_example_command_uses_examples_mode(handler) -> None:
    routes = mock_dingtalk()
    catalog = respx.get(f"{API_BASE}/api/v1/discovery/catalog?mode=examples").mock(
        return_value=httpx.Response(
            200,
            json={"help_text": "问答示例：\n· 跑水事件里环境部第一时间做什么？", "documents": [], "suggestions": []},
        )
    )
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    code, _ = await handler.process({"data": payload(msg_id="ex-cmd", text="问答示例")})
    assert code == ACK_OK
    assert catalog.call_count == 1
    assert qa.call_count == 0
    assert "问答示例" in oto_body(routes["oto"].calls[0])


@respx.mock
async def test_private_manage_and_send_source_commands_are_local(handler) -> None:
    routes = mock_dingtalk()
    qa = respx.post(API_URL).mock(return_value=httpx.Response(200, json=answer_body()))
    for msg_id, text, expected in (
        ("manage-1", "管理个人私有文件", "单聊"),
        ("source-1", "发原文", "发我工程部10月工作汇报ppt原件"),
    ):
        code, _ = await handler.process({"data": payload(msg_id=msg_id, text=text)})
        assert code == ACK_OK
        assert expected in oto_body(routes["oto"].calls[-1])
    assert qa.call_count == 0
