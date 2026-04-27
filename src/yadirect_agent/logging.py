"""Structured logging.

Why structlog:
- JSON logs by default = trivial to ingest into any log system later.
- 'console' renderer for local dev when you want readable output.
- trace_id / campaign_id / operation attached as structured fields, not
  string-concatenated into the message. Makes `jq` queries painless.

Usage:
    logger = structlog.get_logger().bind(component="direct_client")
    logger.info("campaigns.fetched", count=len(campaigns), account=client_login)
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog

from .config import Settings


def configure_logging(settings: Settings) -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        # Check stderr (where the logs actually go) for tty, not
        # stdout — under ``yadirect-agent mcp serve`` stdout is the
        # MCP protocol stream and isatty() should not influence
        # log-renderer styling decisions. Auditor M3 LOW-1.
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    """Get a logger bound to a component name. Always prefer this over raw structlog."""
    # structlog.get_logger() is typed as Any; cast to the bound-logger protocol
    # so callers see a real type.
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger().bind(component=component))
