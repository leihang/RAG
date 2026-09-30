"""钉钉 OpenAPI 客户端（契约依据 `docs/spec/50-dingtalk.md` §3，已核实）。

两条**回归断言**专门盯 `方案/04` 写错的地方：
`stream_card` 必须发全量（``isFull=True``）、``updateCardDataByKey`` 必须在 ``cardUpdateOptions`` 内。
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
    CARD_UPDATE_PATH,
    GROUP_SEND_PATH,
    MAX_MSG_PARAM_BYTES,
    MAX_STREAM_CHUNK_BYTES,
    MAX_STREAM_TOTAL_BYTES,
    OTO_SEND_PATH,
    CardTarget,
    DingTalkOpenApiClient,
    markdown_msg_param,
    text_msg_param,
)
from ekbs.dingtalk.token import TOKEN_PATH
from ekbs.observability import metrics as m

BASE = "https://api.dingtalk.com"
TOKEN_URL = f"{BASE}{TOKEN_PATH}"


class FakeTokenClient:
    def __init__(self, token: str = "tok-test") -> None:
        self.token = token
        self.calls = 0

    async def get_token(self, *, force: bool = False) -> str:
        self.calls += 1
        return self.token


@pytest.fixture(autouse=True)
def _reset_metrics():
    m.reset()
    yield
    m.reset()


def make_client(*, card_template_id: str = "tmpl-1") -> tuple[DingTalkOpenApiClient, FakeTokenClient]:
    tokens = FakeTokenClient()
    client = DingTalkOpenApiClient(
        token_client=tokens, robot_code="robot-key",
        card_template_id=card_template_id, retry_base_delay=0.0,
    )
    return client, tokens


# ---------------------------------------------------------------------------
# 鉴权头
# ---------------------------------------------------------------------------


@respx.mock
async def test_uses_x_acs_dingtalk_access_token_header() -> None:
    route = respx.post(f"{BASE}{OTO_SEND_PATH}").mock(
        return_value=httpx.Response(200, json={"processQueryKey": "pq-1"})
    )
    client, _ = make_client()
    await client.send_oto_message(["u1"], "sampleText", text_msg_param("你好"))
    request = route.calls[0].request
    assert request.headers["x-acs-dingtalk-access-token"] == "tok-test"
    assert "authorization" not in {key.lower() for key in request.headers}


# ---------------------------------------------------------------------------
# 机器人消息
# ---------------------------------------------------------------------------


@respx.mock
async def test_oto_message_encodes_msg_param_as_json_string() -> None:
    route = respx.post(f"{BASE}{OTO_SEND_PATH}").mock(
        return_value=httpx.Response(200, json={"processQueryKey": "pq-2"})
    )
    client, _ = make_client()
    await client.send_oto_message(["u1", "u2"], "sampleMarkdown", markdown_msg_param("标题", "正文"))
    body = json.loads(route.calls[0].request.content)
    assert body["robotCode"] == "robot-key"
    assert body["userIds"] == ["u1", "u2"]
    assert body["msgKey"] == "sampleMarkdown"
    assert isinstance(body["msgParam"], str), "msgParam 必须是 JSON 序列化后的字符串"
    assert json.loads(body["msgParam"]) == {"title": "标题", "text": "正文"}


async def test_oto_rejects_more_than_twenty_users() -> None:
    """文档 20 与 100 两个口径，取严取 20。"""
    client, _ = make_client()
    with pytest.raises(ValueError):
        await client.send_oto_message([f"u{i}" for i in range(21)], "sampleText", {"content": "x"})


@respx.mock
async def test_flow_controlled_users_are_surfaced() -> None:
    respx.post(f"{BASE}{OTO_SEND_PATH}").mock(
        return_value=httpx.Response(
            200, json={"processQueryKey": "pq-3", "flowControlledStaffIdList": ["u9"]}
        )
    )
    client, _ = make_client()
    await client.send_oto_message(["u9"], "sampleText", text_msg_param("hi"))
    assert m.current(m.CARD_SEND, result="flow_controlled") == 1


@respx.mock
async def test_group_message_requires_open_conversation_id() -> None:
    route = respx.post(f"{BASE}{GROUP_SEND_PATH}").mock(
        return_value=httpx.Response(200, json={"processQueryKey": "pq-4"})
    )
    client, _ = make_client()
    await client.send_group_message("cid-1", "sampleMarkdown", markdown_msg_param("t", "body"))
    body = json.loads(route.calls[0].request.content)
    assert body["openConversationId"] == "cid-1"
    assert body["robotCode"] == "robot-key"


@respx.mock
async def test_msg_param_size_is_guarded() -> None:
    client, _ = make_client()
    with pytest.raises(ValueError):
        await client.send_group_message("cid", "sampleText", {"content": "x" * (MAX_MSG_PARAM_BYTES + 10)})


# ---------------------------------------------------------------------------
# 卡片
# ---------------------------------------------------------------------------


def test_card_target_open_space_id_format() -> None:
    assert CardTarget.group("cid", "rk").open_space_id == "dtv1.card//IM_GROUP.cid"
    assert CardTarget.robot("staff", "rk").open_space_id == "dtv1.card//IM_ROBOT.staff"


@respx.mock
async def test_create_card_instance_stringifies_card_data() -> None:
    route = respx.post(f"{BASE}{CARD_CREATE_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "result": "ot-1"})
    )
    client, _ = make_client()
    out = await client.create_card_instance("ot-1", {"content": "", "count": 3})
    assert out == "ot-1"
    body = json.loads(route.calls[0].request.content)
    assert body["cardTemplateId"] == "tmpl-1"
    assert body["outTrackId"] == "ot-1"
    assert body["callbackType"] == "STREAM"
    assert body["cardData"]["cardParamMap"] == {"content": "", "count": "3"}, "value 只支持 String"


@respx.mock
async def test_deliver_card_group_and_robot() -> None:
    route = respx.post(f"{BASE}{CARD_DELIVER_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    client, _ = make_client()
    await client.deliver_card("ot-2", CardTarget.group("cid", "robot-key"))
    body = json.loads(route.calls[0].request.content)
    assert body["openSpaceId"] == "dtv1.card//IM_GROUP.cid"
    assert body["imGroupOpenDeliverModel"]["robotCode"] == "robot-key"

    await client.deliver_card("ot-3", CardTarget.robot("staff-1", "robot-key"))
    body = json.loads(route.calls[1].request.content)
    assert body["openSpaceId"] == "dtv1.card//IM_ROBOT.staff-1"
    assert body["imRobotOpenDeliverModel"]["spaceType"] == "IM_ROBOT"


@respx.mock
async def test_stream_card_is_put_full_and_idempotent() -> None:
    """**回归测试（D1）**：markdown 流式必须 `isFull=true`，且方法必须是 PUT。"""
    route = respx.put(f"{BASE}{CARD_STREAMING_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "result": True})
    )
    client, _ = make_client()
    ok = await client.stream_card("ot-4", "content", "年假 5 天")
    assert ok is True
    request = route.calls[0].request
    assert request.method == "PUT"
    body = json.loads(request.content)
    assert body["isFull"] is True, "方案 04 §5.7 的增量写法违反官方约束"
    assert body["key"] == "content"
    assert body["outTrackId"] == "ot-4"
    assert body["guid"], "guid 用于服务端幂等"
    assert body["isFinalize"] is False and body["isError"] is False


@respx.mock
async def test_stream_card_marks_finalize_and_error() -> None:
    route = respx.put(f"{BASE}{CARD_STREAMING_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "result": True})
    )
    client, _ = make_client()
    await client.stream_card("ot-5", "content", "done", is_finalize=True)
    await client.fail_card("ot-5", "content")
    finalize = json.loads(route.calls[0].request.content)
    error = json.loads(route.calls[1].request.content)
    assert finalize["isFinalize"] is True and finalize["isError"] is False
    assert error["isError"] is True


async def test_stream_card_rejects_oversized_full_text() -> None:
    """全量模式下内容字段是累计全文，因此上限取「总量 3KB」。"""
    client, _ = make_client()
    with pytest.raises(ValueError):
        await client.stream_card("ot", "content", "x" * (MAX_STREAM_TOTAL_BYTES + 1))


@respx.mock
async def test_stream_card_text_chunks_and_finalizes() -> None:
    route = respx.put(f"{BASE}{CARD_STREAMING_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "result": True})
    )
    client, _ = make_client()
    frames = await client.stream_card_text("ot-6", "content", "年假" * 300, chunk_chars=100)
    assert frames >= 2
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies[-1]["isFinalize"] is True
    # 每次都是全量（内容递增）
    assert all(b["isFull"] is True for b in bodies)
    assert all(len(b["content"].encode("utf-8")) <= MAX_STREAM_TOTAL_BYTES for b in bodies)
    lengths = [len(b["content"]) for b in bodies]
    assert lengths == sorted(lengths), "流式帧的内容应是单调递增的全量"
    # 本帧新增文本 ≤1KB（增量的字节上限）
    deltas = [lengths[0]] + [lengths[i] - lengths[i - 1] for i in range(1, len(lengths))]
    assert all(len("年" * delta) <= MAX_STREAM_CHUNK_BYTES for delta in deltas)


@respx.mock
async def test_stream_card_text_stops_at_total_limit() -> None:
    """总量建议 ≤3KB：超出的部分不再推进卡片（应改用普通消息补发，04 §5.8）。"""
    route = respx.put(f"{BASE}{CARD_STREAMING_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "result": True})
    )
    client, _ = make_client()
    await client.stream_card_text("ot-7", "content", "长" * 5000, chunk_chars=200)
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert len(bodies[-1]["content"].encode("utf-8")) <= 3072


@respx.mock
async def test_update_card_data_puts_options_nested() -> None:
    """**回归测试（D2）**：`updateCardDataByKey` 必须在 `cardUpdateOptions` 内，否则会清空内容。"""
    route = respx.put(f"{BASE}{CARD_UPDATE_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client, _ = make_client()
    await client.update_card_data("ot-8", {"content": "新内容"})
    body = json.loads(route.calls[0].request.content)
    assert body["cardUpdateOptions"] == {"updateCardDataByKey": True}
    assert "updateCardDataByKey" not in body, "平铺到顶层会被当成无效字段（04 §5.7 的错法）"
    assert body["cardData"]["cardParamMap"] == {"content": "新内容"}
