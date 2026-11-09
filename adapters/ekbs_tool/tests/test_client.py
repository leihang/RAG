"""核心客户端单测：不依赖真实 API（httpx mock）。"""

from __future__ import annotations

import httpx
import pytest

from ekbs_tool.client import EkbsAskResult, EkbsClient


def test_as_tool_text_refused() -> None:
    r = EkbsAskResult(answer="未找到可靠依据。", refused=True, refusal_reason="below_threshold")
    text = r.as_tool_text()
    assert text.startswith("[EKBS 拒答")
    assert "below_threshold" in text


def test_as_tool_text_with_citations() -> None:
    r = EkbsAskResult(
        answer="年假 5 天。",
        citations=[{"cite_index": 1, "doc_title": "休假制度", "page_from": 3}],
    )
    assert "年假 5 天" in r.as_tool_text()
    assert "[1] 休假制度" in r.as_tool_text()
    assert "p.3" in r.as_tool_text()


def test_ask_posts_qa_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = httpx.Response(200).json if False else None
        import json

        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "answer": "ok",
                "refused": False,
                "citations": [],
                "trace_id": "t1",
            },
        )

    transport = httpx.MockTransport(handler)
    client = EkbsClient(
        base_url="http://example.test/api/v1",
        internal_token="tok",
        default_user_id=7,
    )

    real_client = httpx.Client

    def fake_client(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    result = client.ask("年假？", conversation_id="c1")
    assert result.answer == "ok"
    assert captured["url"].endswith("/qa/chat")
    assert captured["headers"]["x-internal-token"] == "tok"
    assert captured["headers"]["x-internal-user-id"] == "7"
    assert captured["body"]["question"] == "年假？"
    assert captured["body"]["conversation_id"] == "c1"


def test_requires_auth() -> None:
    client = EkbsClient(base_url="http://example.test/api/v1")
    with pytest.raises(ValueError, match="鉴权"):
        client.ask("x")
