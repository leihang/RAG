"""EKBS 企业知识库 Tool 客户端（LangChain / LangGraph / 脚本共用）。

OpenClaw 侧见 ``adapters/openclaw-ekbs/``（Node 插件，HTTP 语义与本包一致）。
"""

from .client import EkbsAskResult, EkbsClient

__all__ = ["EkbsAskResult", "EkbsClient", "build_langchain_tool", "ekbs_ask_node"]


def build_langchain_tool(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    from .langchain_tool import build_langchain_tool as _build

    return _build(*args, **kwargs)


def ekbs_ask_node(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    from .langgraph_node import ekbs_ask_node as _node

    return _node(*args, **kwargs)
