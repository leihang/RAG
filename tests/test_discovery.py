"""D22-P0：发现层（可见文档导览 + 示例问 + 钉钉帮助 + 拒答提示）。"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from ekbs.discovery import (
    format_document_list_text,
    format_examples_text,
    format_help_text_short,
    format_permission_text,
    format_private_manage_text,
    format_send_source_text,
    friendly_title,
    is_document_list_command,
    is_example_command,
    is_greeting_command,
    is_permission_command,
    is_personal_manage_command,
    is_send_source_command,
    display_doc_title,
    file_request_hint,
    format_help_text,
    format_refusal_hints,
    is_catalog_query,
    is_help_command,
    load_seed_questions,
    suggested_questions,
    title_match_score,
)
from ekbs.dingtalk.handler import ACK_OK, StreamMessageHandler
from ekbs.dingtalk.client import DingTalkOpenApiClient
from ekbs.idempotency import IdempotencyGuard
from ekbs.ratelimit import RateLimiter
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of
from tests.test_dingtalk_handler import FakeTokenClient, mock_dingtalk, payload

API_BASE = "http://127.0.0.1:8000"
CATALOG_URL = f"{API_BASE}/api/v1/discovery/catalog"
QA_URL = f"{API_BASE}/api/v1/qa/chat"


@pytest.fixture
async def client(seeded):
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as instance:
            yield instance


def headers_for(user_id: int) -> dict[str, str]:
    return {
        "X-Internal-Token": TEST_INTERNAL_TOKEN,
        "X-Internal-User-Id": str(user_id),
        "X-Internal-Channel": "dingtalk_robot",
    }


def test_is_help_command() -> None:
    assert is_help_command("帮助")
    assert is_help_command(" HELP ")
    assert is_help_command("能问什么")
    assert is_help_command("你好啊")
    assert is_help_command("帮助一下")
    assert is_help_command("hello~")
    assert not is_help_command("你好，请问年假有多少天")
    assert not is_help_command("年假有多少天")
    assert not is_help_command("")


def test_catalog_query_and_file_hint() -> None:
    assert is_catalog_query("我有查询哪些文档的权限")
    assert is_catalog_query("我能看哪些文件")
    assert not is_catalog_query("交接班制度有些什么要求内容")
    assert not is_catalog_query("设备怎么编号")
    assert not is_catalog_query("帮助")
    assert file_request_hint("将熙物联使用教程文件发给我") == "熙物联使用教程"
    assert file_request_hint("将物联网使用说明文件发给我") == "物联网使用说明"
    assert file_request_hint("把图片发我") is None
    assert file_request_hint("我有查询哪些文档的权限") is None
    assert not is_catalog_query("将熙物联使用教程文件发给我")
    assert title_match_score("熙物联使用教程", "doc文档/熙物联使用教程V4") >= 100
    assert title_match_score("物联网使用说明", "doc文档/熙物联使用教程V4") < 4
    assert "原文件" in format_help_text([{"title": "年假管理制度"}], [])


def test_file_request_hint_accepts_send_geiwo_phrasings() -> None:
    """用户实测：「发送XX原文件给我」「给我发原文件」「发一下原文件」都不能漏。

    handler 只有 ``file_request_hint`` 命中才走 ``/discovery/file``；旧实现只认
    「发给我」连写，上面这些常见说法会被当成普通提问丢进 RAG（用户反馈「没理解」）。
    """
    assert (
        file_request_hint("把XJWY-GC-14电力增容作业指导书的原文件发给我")
        == "XJWY-GC-14电力增容作业指导书"
    )
    assert file_request_hint("发送XJWY-GC-14原文件给我") == "XJWY-GC-14"
    assert file_request_hint("把装饰拆除作业指导书的原件传给我") == "装饰拆除作业指导书"
    assert file_request_hint("发一下XJWY-GC-14的原文件") == "XJWY-GC-14"
    assert file_request_hint("给我发XJWY-GC-14的原始文件") == "XJWY-GC-14"
    # 反例：问「怎么发送文件」「文件发给谁」仍是提问，不该抢问答
    assert file_request_hint("如何发送文件给客户？") is None
    assert file_request_hint("这个文件发送给谁？") is None


def test_list_accessible_files_is_catalog_not_send_or_qa() -> None:
    """用户原话：两句「列出…清单」必须和「我可以查询哪些文件」一样走可见清单。"""
    assert is_catalog_query("列出我可以查询文件的目录清单")
    assert is_catalog_query("列出我可以查阅文件的清单")
    assert is_catalog_query("我可以查询哪些文件")
    assert is_catalog_query("列出我可以查询文档的目录清单")
    assert is_catalog_query("列出我可以查阅文档的清单")
    assert file_request_hint("列出我可以查询文件的目录清单") is None
    assert file_request_hint("列出我可以查阅文件的清单") is None
    assert not is_catalog_query("把年假管理制度文件发给我")
    assert file_request_hint("把年假管理制度文件发给我") == "年假管理制度"
    assert not is_catalog_query("年假制度是什么")
    assert file_request_hint("年假制度是什么") is None
    assert not is_catalog_query("删除我的文件 标题")
    assert not is_catalog_query("我的文件")


def test_display_doc_title_strips_import_prefix() -> None:
    from ekbs.discovery import display_doc_title

    assert display_doc_title("doc文档/XJWY-GC-01-01交接班管理制度") == "XJWY-GC-01-01交接班管理制度"
    assert display_doc_title("年假管理制度") == "年假管理制度"


def test_format_help_and_refusal() -> None:
    docs = [{"title": "年假管理制度", "doc_id": 1}]
    suggestions = [{"question": "年假有多少天", "source": "seed"}]
    text = format_help_text(docs, suggestions)
    assert "年假管理制度" in text
    assert "年假有多少天" in text
    assert "你可以试试问" in format_refusal_hints(suggestions)


def test_seed_loader_reads_config(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "discovery_questions.json").write_text(
        json.dumps({"questions": [{"question": "自定义问", "title_keywords": ["年假"]}]}),
        encoding="utf-8",
    )
    seeds = load_seed_questions(tmp_path)
    assert seeds[0]["question"] == "自定义问"


async def test_catalog_requires_auth(client) -> None:
    assert (await client.get("/api/v1/discovery/catalog")).status_code == 401


async def test_catalog_returns_visible_published_and_suggestions(client, seeded) -> None:
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    resp = await client.get("/api/v1/discovery/catalog", headers=headers_for(alice))
    assert resp.status_code == 200
    body = resp.json()
    assert "documents" in body and "suggestions" in body and "help_text" in body
    titles = {d["title"] for d in body["documents"]}
    assert "年假管理制度" in titles
    # 种子「年假有多少天」应对可见标题命中
    questions = [s["question"] for s in body["suggestions"]]
    assert any("年假" in q for q in questions)
    assert "可见的文档" in body["help_text"] or "可以这样问" in body["help_text"]


async def test_catalog_hides_docs_above_clearance(client, seeded) -> None:
    """密级超限的文档不得出现在发现层清单（D22 硬约束）。

    seed 里「财务保密制度」为 L3，alice 密级为 2，默认就看不见。
    """
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    bob = user_id_of(runtime.db, "demo_bob")
    alice_body = (
        await client.get("/api/v1/discovery/catalog", headers=headers_for(alice))
    ).json()
    bob_body = (
        await client.get("/api/v1/discovery/catalog", headers=headers_for(bob))
    ).json()
    assert "财务保密制度" not in {d["title"] for d in alice_body["documents"]}
    assert "财务保密制度" in {d["title"] for d in bob_body["documents"]}


async def test_file_endpoint_returns_original_only_when_visible(client, seeded) -> None:
    import base64

    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    catalog = (
        await client.get("/api/v1/discovery/catalog", headers=headers_for(alice))
    ).json()
    visible = catalog["documents"]
    assert visible
    target = visible[0]
    raw = runtime.settings.data_paths()["raw"] / "发给我.docx"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"PK\x03\x04tutorial")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?",
            (str(raw), int(target["doc_id"])),
        )
        conn.commit()
    name = display_doc_title(target["title"])
    headers = headers_for(alice)
    sent = await client.post(
        "/api/v1/discovery/file",
        headers=headers,
        json={"text": f"将{name}文件发给我"},
    )
    assert sent.status_code == 200
    body = sent.json()
    assert body["action"] == "send"
    assert base64.b64decode(body["content_base64"]) == b"PK\x03\x04tutorial"
    assert body["filename"].endswith(".docx")

    hidden = await client.post(
        "/api/v1/discovery/file",
        headers=headers,
        json={"text": "将财务保密制度文件发给我"},
    )
    assert hidden.status_code == 200
    assert hidden.json()["action"] != "send"
    assert "content_base64" not in hidden.json() or not hidden.json().get("content_base64")


async def test_file_endpoint_rejects_over_dingtalk_send_limit(client, seeded) -> None:
    """超过钉钉机器人能发回的体积时，要说明是发送上限，文件仍在知识库里。"""
    from ekbs.api.discovery import dingtalk_send_limit_message
    from ekbs.dingtalk.client import MAX_SAMPLE_FILE_BYTES

    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    catalog = (
        await client.get("/api/v1/discovery/catalog", headers=headers_for(alice))
    ).json()
    target = catalog["documents"][0]
    raw = runtime.settings.data_paths()["raw"] / "超限原件.docx"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"\0" * (MAX_SAMPLE_FILE_BYTES + 1))
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?",
            (str(raw), int(target["doc_id"])),
        )
        conn.commit()
    name = display_doc_title(target["title"])
    sent = await client.post(
        "/api/v1/discovery/file",
        headers=headers_for(alice),
        json={"text": f"将{name}文件发给我"},
    )
    assert sent.status_code == 200
    body = sent.json()
    assert body["action"] == "none"
    assert not body.get("content_base64")
    assert body["message"] == dingtalk_send_limit_message(name, MAX_SAMPLE_FILE_BYTES + 1)
    assert "钉钉机器人发文件的上限" in body["message"]
    assert "不是知识库没有这份文件" in body["message"]
    assert "不在库里" not in body["message"]


async def test_suggested_questions_only_match_visible_titles(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    items = suggested_questions(runtime, alice, limit=10)
    questions = [i["question"] for i in items]
    # 无「电梯」类可见文档时，电梯种子不得泄漏
    assert not any("电梯" in q for q in questions)


async def test_refusal_suggestions_do_not_dump_unrelated_seeds(seeded) -> None:
    """拒答「二维码」不得退回默认中央空调合同题（用户实测踩过）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    items = suggested_questions(
        runtime, alice, limit=3, query="二维码不对没有显示出来", for_refusal=True
    )
    questions = [i["question"] for i in items]
    assert not any("中央空调" in q for q in questions)
    assert not any("电梯维护" in q for q in questions)


@respx.mock
async def test_dingtalk_help_uses_catalog_not_qa(seeded) -> None:
    runtime, _ = seeded
    runtime.settings.dingtalk["card"] = {
        "ai_template_id": "tmpl-ai",
        "streaming_enabled": False,
        "stream_key": "content",
    }
    cards = DingTalkOpenApiClient(
        token_client=FakeTokenClient(),
        robot_code="robot-key",
        card_template_id="tmpl-ai",
        retry_base_delay=0.0,
    )
    handler = StreamMessageHandler(
        db=runtime.db,
        settings=runtime.settings,
        secrets=runtime.secrets,
        idempotency=IdempotencyGuard(runtime.db),
        rate_limiter=RateLimiter(runtime.db, runtime.settings),
        cards=cards,
    )
    routes = mock_dingtalk(card_ok=False)
    catalog = respx.get(f"{CATALOG_URL}?mode=help").mock(
        return_value=httpx.Response(
            200,
            json={
                "documents": [{"doc_id": 1, "title": "年假管理制度"}],
                "suggestions": [{"question": "年假有多少天", "source": "seed"}],
                "help_text": "你好，我是企业知识库助手。\n可以这样问我：\n1. 年假有多少天",
            },
        )
    )
    qa = respx.post(QA_URL).mock(
        return_value=httpx.Response(200, json={"answer": "不应调用", "refused": False})
    )

    code, _ = await handler.process({"data": payload(text="帮助", msg_id="help-1")})
    assert code == ACK_OK
    assert catalog.call_count == 1
    assert qa.call_count == 0
    assert routes["oto"].call_count == 1
    sent = json.loads(routes["oto"].calls[0].request.content)
    assert "年假有多少天" in json.loads(sent["msgParam"])["content"]


@respx.mock
async def test_permission_question_lists_docs_not_retrieval(seeded) -> None:
    runtime, _ = seeded
    runtime.settings.dingtalk["card"] = {
        "ai_template_id": "tmpl-ai",
        "streaming_enabled": False,
        "stream_key": "content",
    }
    cards = DingTalkOpenApiClient(
        token_client=FakeTokenClient(),
        robot_code="robot-key",
        card_template_id="tmpl-ai",
        retry_base_delay=0.0,
    )
    handler = StreamMessageHandler(
        db=runtime.db,
        settings=runtime.settings,
        secrets=runtime.secrets,
        idempotency=IdempotencyGuard(runtime.db),
        rate_limiter=RateLimiter(runtime.db, runtime.settings),
        cards=cards,
    )
    routes = mock_dingtalk(card_ok=False)
    catalog = respx.get(f"{CATALOG_URL}?doc_limit=80&mode=docs").mock(
        return_value=httpx.Response(
            200,
            json={
                "help_text": "你当前可见的文档：\n1. 熙物联使用教程V4",
                "documents": [{"doc_id": 109, "title": "熙物联使用教程V4"}],
                "suggestions": [],
            },
        )
    )
    qa = respx.post(QA_URL).mock(
        return_value=httpx.Response(200, json={"answer": "不应调用", "refused": False})
    )
    code, _ = await handler.process(
        {"data": payload(text="我有查询哪些文档的权限", msg_id="perm-docs-1")}
    )
    assert code == ACK_OK
    assert catalog.call_count == 1
    assert qa.call_count == 0
    sent = json.loads(routes["oto"].calls[0].request.content)
    assert "熙物联使用教程V4" in json.loads(sent["msgParam"])["content"]


@respx.mock
async def test_named_file_is_sent_not_summarized(seeded) -> None:
    import base64

    from ekbs.dingtalk.client import MEDIA_UPLOAD_PATH, MSG_KEY_FILE, OAPI_BASE_URL

    runtime, _ = seeded
    runtime.settings.dingtalk["card"] = {
        "ai_template_id": "tmpl-ai",
        "streaming_enabled": False,
        "stream_key": "content",
    }
    cards = DingTalkOpenApiClient(
        token_client=FakeTokenClient(),
        robot_code="robot-key",
        card_template_id="tmpl-ai",
        retry_base_delay=0.0,
    )
    handler = StreamMessageHandler(
        db=runtime.db,
        settings=runtime.settings,
        secrets=runtime.secrets,
        idempotency=IdempotencyGuard(runtime.db),
        rate_limiter=RateLimiter(runtime.db, runtime.settings),
        cards=cards,
    )
    routes = mock_dingtalk(card_ok=False)
    media = respx.post(f"{OAPI_BASE_URL}{MEDIA_UPLOAD_PATH}").mock(
        return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok", "media_id": "media-doc"})
    )
    respx.post(f"{API_BASE}/api/v1/discovery/file").mock(
        return_value=httpx.Response(
            200,
            json={
                "action": "send",
                "message": "已按你的权限发送原文：熙物联使用教程V4",
                "filename": "熙物联使用教程V4.docx",
                "content_base64": base64.b64encode(b"PK-tutorial").decode(),
            },
        )
    )
    qa = respx.post(QA_URL).mock(
        return_value=httpx.Response(200, json={"answer": "不应调用", "refused": False})
    )
    code, _ = await handler.process(
        {"data": payload(text="将熙物联使用教程文件发给我", msg_id="send-file-1")}
    )
    assert code == ACK_OK
    assert qa.call_count == 0
    assert media.call_count == 1
    file_sends = [
        json.loads(call.request.content)
        for call in routes["oto"].calls
        if json.loads(call.request.content).get("msgKey") == MSG_KEY_FILE
    ]
    assert len(file_sends) == 1
    param = json.loads(file_sends[0]["msgParam"])
    assert param["fileName"] == "熙物联使用教程V4.docx"
    assert param["fileType"] == "docx"
    texts = [
        json.loads(json.loads(call.request.content)["msgParam"]).get("content", "")
        for call in routes["oto"].calls
        if json.loads(call.request.content).get("msgKey") != MSG_KEY_FILE
    ]
    assert any("熙物联使用教程V4" in item for item in texts)


@respx.mock
async def test_dingtalk_refusal_appends_suggestions_if_missing(seeded) -> None:
    runtime, _ = seeded
    runtime.settings.dingtalk["card"] = {
        "ai_template_id": "tmpl-ai",
        "streaming_enabled": False,
        "stream_key": "content",
    }
    cards = DingTalkOpenApiClient(
        token_client=FakeTokenClient(),
        robot_code="robot-key",
        card_template_id="tmpl-ai",
        retry_base_delay=0.0,
    )
    handler = StreamMessageHandler(
        db=runtime.db,
        settings=runtime.settings,
        secrets=runtime.secrets,
        idempotency=IdempotencyGuard(runtime.db),
        rate_limiter=RateLimiter(runtime.db, runtime.settings),
        cards=cards,
    )
    routes = mock_dingtalk(card_ok=False)
    respx.post(QA_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "answer": "未找到可靠依据。",
                "refused": True,
                "refusal_reason": "below_threshold",
                "suggested_questions": ["年假有多少天", "员工手册里有哪些总则"],
                "citations": [],
            },
        )
    )
    code, _ = await handler.process({"data": payload(text="火星政策", msg_id="refuse-1")})
    assert code == ACK_OK
    assert routes["oto"].call_count == 1
    sent = json.loads(routes["oto"].calls[0].request.content)
    content = json.loads(sent["msgParam"])["content"]
    assert "你可以试试问" in content
    assert "年假有多少天" in content


def test_greeting_and_doc_commands_are_separate() -> None:
    assert is_greeting_command("你好")
    assert is_greeting_command("您好呀")
    assert is_greeting_command("hello~")
    assert not is_greeting_command("你好，请问年假有多少天")
    assert is_document_list_command("文档")
    assert is_document_list_command("有哪些文档")
    assert not is_document_list_command("维修作业指导书里日常小修多久完成")


def test_friendly_title_hides_document_codes() -> None:
    assert friendly_title("工程部/XJWY-GC-09维修作业指导书") == "维修作业指导书"
    assert friendly_title("XJWY-GC-12能耗管理作业指导书 - 定稿") == "能耗管理作业指导书（定稿）"
    assert friendly_title("XJWY-GCYJ-05跑水突发事件处理预案") == "跑水突发事件处理预案"


def test_short_help_lists_exactly_four_commands() -> None:
    text = format_help_text_short([])
    assert text.startswith("可用命令")
    for command in ("查询权限", "问答示例", "管理个人私有文件", "发原文"):
        assert command in text
    assert "可用功能" not in text
    assert "文档清单" not in text
    assert "XJWY-" not in text
    assert "主要讲了什么" not in text


def test_command_predicates() -> None:
    assert is_permission_command("查询权限")
    assert is_example_command("问答示例")
    assert is_personal_manage_command("管理个人私有文件")
    assert is_send_source_command("发原文")
    assert not is_permission_command("权限是什么")
    assert not is_example_command("举个例子说明")


def test_command_outputs_are_names_only() -> None:
    docs = [
        {"title": "工程部/XJWY-GC-09维修作业指导书", "doc_id": 1, "kb_code": "gongkai"},
        {"title": "XJWY-GCYJ-05跑水突发事件处理预案", "doc_id": 2, "kb_code": "personal-5"},
    ]
    permission = format_permission_text(docs, 3)
    assert "可查文档 2 篇（私有 1 篇），密级 1~3" in permission
    assert "维修作业指导书" in permission and "XJWY-" not in permission
    examples = format_examples_text([{"question": "跑水事件里环境部第一时间做什么"}])
    assert "跑水事件里环境部第一时间做什么" in examples
    assert "答案" not in examples
    assert "私有库" in format_private_manage_text()
    assert "发我《跑水预案》" in format_send_source_text()


def test_document_list_text_uses_friendly_titles() -> None:
    text = format_document_list_text(
        [
            {"title": "工程部/XJWY-GC-09维修作业指导书", "doc_id": 1},
            {"title": "XJWY-GC-12能耗管理作业指导书 - 定稿", "doc_id": 2},
        ]
    )
    assert "可查文档（2）" in text
    assert "维修作业指导书" in text
    assert "能耗管理作业指导书（定稿）" in text
    assert "XJWY-" not in text


async def test_catalog_help_and_docs_modes(client, seeded) -> None:
    """P0：帮助只给短示例；文档清单只给去掉编号的标题。"""
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")

    help_resp = await client.get(
        "/api/v1/discovery/catalog?mode=help", headers=headers_for(alice)
    )
    assert help_resp.status_code == 200
    help_text = help_resp.json()["help_text"]
    assert help_text.startswith("可用命令")
    assert "查询权限" in help_text and "问答示例" in help_text
    assert "文档清单" not in help_text
    assert "当前可见的文档" not in help_text

    docs_resp = await client.get(
        "/api/v1/discovery/catalog?mode=docs", headers=headers_for(alice)
    )
    assert docs_resp.status_code == 200
    docs_text = docs_resp.json()["help_text"]
    assert "可查文档（" in docs_text
    assert "年假管理制度" in docs_text
    assert "XJWY-" not in docs_text
    assert "主要讲了什么" not in docs_text

    perm_resp = await client.get(
        "/api/v1/discovery/catalog?mode=permissions", headers=headers_for(alice)
    )
    assert perm_resp.status_code == 200
    perm_text = perm_resp.json()["help_text"]
    assert "可查文档" in perm_text and "密级 1~" in perm_text
    assert "年假管理制度" in perm_text

    ex_resp = await client.get(
        "/api/v1/discovery/catalog?mode=examples", headers=headers_for(alice)
    )
    assert ex_resp.status_code == 200
    assert "问答示例" in ex_resp.json()["help_text"]
