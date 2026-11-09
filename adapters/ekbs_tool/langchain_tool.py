"""LangChain Tool 适配：内部只调 ``EkbsClient``。"""

from __future__ import annotations

from typing import Any

from .client import EkbsClient


def build_langchain_tool(
    client: EkbsClient | None = None,
    *,
    user_id: int | None = None,
    name: str = "ekbs_ask",
) -> Any:
    """返回可挂到 Agent / LangGraph ``ToolNode`` 的 LangChain tool。

    依赖可选：``pip install langchain-core``（或 ``pip install -e '.[langchain]'``）。
    """
    try:
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "需要 langchain-core：pip install langchain-core"
        ) from exc

    ekbs = client or EkbsClient.from_env()

    class EkbsAskInput(BaseModel):
        question: str = Field(description="要向企业知识库查询的问题（制度/手册/表格等）")
        conversation_id: str | None = Field(
            default=None,
            description="多轮会话 id；与钉钉/管理台同口径，便于改写",
        )

    def _run(question: str, conversation_id: str | None = None) -> str:
        return ekbs.ask_text(
            question,
            user_id=user_id,
            conversation_id=conversation_id or None,
        )

    return StructuredTool.from_function(
        func=_run,
        name=name,
        description=(
            "查询企业知识库（EKBS）。制度、手册、合同条款、表格口径等问题应调用本工具。"
            "若返回以 [EKBS 拒答 开头，表示知识库无可靠依据，不要编造。"
        ),
        args_schema=EkbsAskInput,
    )
