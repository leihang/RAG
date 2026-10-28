"""结构化日志（stdlib logging，可注入自定义 logger）。

为什么不用 ``warnings.warn``：生产环境需要「可路由、可分级、带上下文」的日志，
而 warnings 无法被集中采集；同时库不应该在 import 时配置 root logger。
"""

from __future__ import annotations

import logging

_LOGGER_NAME = "excel_rag_parser"


def get_logger(name: str | None = None) -> logging.Logger:
    """取库 logger；``name`` 会拼成 ``excel_rag_parser.<name>``。"""
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)


def configure(level: int = logging.INFO) -> logging.Logger:
    """便利函数：给库 logger 挂一个 stderr handler（幂等）。CLI 用。"""
    logger = get_logger()
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger
