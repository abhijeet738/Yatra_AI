# app/core/logging.py
"""Structured logging: pretty in dev, one JSON object per line in prod."""

import logging

import structlog
from structlog.typing import Processor


def configure_logging(env: str = "dev", level: str = "INFO") -> None:
    """Call once at startup (from main.py), before anything logs."""
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,  # adds fields bound via bind_trip_context()
        structlog.processors.add_log_level,  # adds "level": "info" / "warning" / ...
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # ISO timestamp in UTC
        structlog.processors.StackInfoRenderer(),  # supports stack_info=True on a log call
    ]
    if env == "prod":
        processors.append(
            structlog.processors.format_exc_info
        )  # exceptions become a plain string field
        processors.append(
            structlog.processors.JSONRenderer()
        )  # machine-readable, one object per line
    else:
        processors.append(
            structlog.dev.ConsoleRenderer()
        )  # coloured output that also prints tracebacks nicely

    structlog.configure(
        processors=processors,
        # Drop messages below `level` cheaply; unknown level names fall back to INFO
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),  # write to stdout, where Docker/Render collect logs
        cache_logger_on_first_use=True,  # small speed-up after the first call
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Use as: log = get_logger(__name__); log.info("provider_ok", provider="open-meteo")."""
    return structlog.get_logger(name)


def bind_trip_context(trip_id: str) -> None:
    """Attach trip_id to every log line emitted in the current request/task."""
    structlog.contextvars.bind_contextvars(trip_id=trip_id)


def clear_context() -> None:
    """Remove bound fields; call when a request finishes so they don't leak into the next one."""
    structlog.contextvars.clear_contextvars()
