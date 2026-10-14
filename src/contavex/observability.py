"""Structured logging and metrics."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from prometheus_client import CollectorRegistry, Counter, Histogram

from contavex.secrets import redactor


def _redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    # Last line of defence: every value of every record passes through here.
    for key in list(event):
        event[key] = redactor.scrub_obj(event[key])
    return event


class _StdoutLogger:
    """Writes each record as one line to whatever stdout currently is.

    Looked up per write rather than captured at configuration time, so a
    replaced or redirected stdout is followed.
    """

    def msg(self, message: str) -> None:
        print(message, file=sys.stdout, flush=True)

    log = debug = info = warning = error = critical = exception = fatal = msg


def configure_logging(level: str = "INFO") -> None:
    # The HTTP client logs full request URLs through stdlib logging, and a
    # Telegram URL contains the bot token. Those loggers stay silent.
    for name in ("httpx2", "httpcore2"):
        noisy = logging.getLogger(name)
        noisy.setLevel(logging.WARNING)
        noisy.propagate = False
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=lambda *_: _StdoutLogger(),
        cache_logger_on_first_use=False,
    )


log: structlog.stdlib.BoundLogger = structlog.get_logger("contavex")


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.accepted = Counter(
            "contavex_publications_accepted_total",
            "Publications accepted",
            ["platform"],
            registry=self.registry,
        )
        self.finished = Counter(
            "contavex_publications_finished_total",
            "Publications that reached a terminal state",
            ["platform", "status", "category"],
            registry=self.registry,
        )
        self.retries = Counter(
            "contavex_step_retries_total",
            "Provider steps rescheduled after a retryable error",
            ["platform", "category"],
            registry=self.registry,
        )
        self.step_seconds = Histogram(
            "contavex_provider_step_seconds",
            "Duration of one provider step",
            ["platform", "implementation"],
            registry=self.registry,
        )
