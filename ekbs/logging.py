"""结构化日志（`01` §3：structlog，JSON，含 trace_id）。

两个落点：

* **文件**：``{log_dir}/{service}.json.log``（launchd 另有 stdout/stderr 重定向到
  ``{log_dir}/{service}.out.log`` / ``.err.log``，见 ``01`` §8.1）；
* **控制台**：开发环境用彩色可读格式，生产用 JSON（对齐 launchd 日志采集）。

密钥纪律：本模块对日志事件做**值级脱敏**，凡键名命中密钥模式的字段一律替换为 ``***``，
避免「不小心的 ``logger.info(settings=...)``」把 key 写进日志（`04` §2.6）。
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import structlog

from ekbs.config import Settings

#: 请求级 trace_id（贯穿检索链路与审计写入）
trace_id_var: ContextVar[str] = ContextVar("ekbs_trace_id", default="")

_REDACT_RE = re.compile(
    r"(?i)(api_?key|secret|password|passwd|private_?key|access_?token|refresh_?token|"
    r"internal_?token|authorization|bearer)"
)

_REDACTED = "***"


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if _REDACT_RE.search(str(key)):
            event_dict[key] = _REDACTED
    return event_dict


def _add_trace_id(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    trace_id = trace_id_var.get()
    if trace_id:
        event_dict.setdefault("trace_id", trace_id)
    return event_dict


def configure_logging(settings: Settings, service: str) -> structlog.stdlib.BoundLogger:
    """初始化日志并返回该 service 的 logger。

    ``service`` 取 ``api`` / ``dingtalk`` / ``worker`` / ``scheduler``（`01` §8.2 的四个服务）。
    """
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(settings.log_dir) / f"{service}.json.log"

    level = logging.DEBUG if settings.is_dev else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):  # 幂等：重复调用不叠加 handler
        root.removeHandler(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(file_handler)

    if settings.is_dev:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(console)

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        if settings.is_dev
        else structlog.processors.JSONRenderer(ensure_ascii=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger(service=service, env=settings.app.env)


def get_logger(**initial: Any) -> structlog.stdlib.BoundLogger:
    """取 logger（未调用 :func:`configure_logging` 时 structlog 有默认配置，不会炸）。"""
    return structlog.get_logger(**initial)
