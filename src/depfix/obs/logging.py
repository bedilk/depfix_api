"""Structured (JSON) logging with request-scoped context, and optional
Sentry error reporting.

The fleet orchestrator (:mod:`depfix.orchestrator.runner`) processes many
``(repo, change)`` pairs per run; a plain ``%(message)s`` log line gives no
way to grep "everything about run abc123's attempt at repo/change xyz"
without fragile string matching. :func:`log_context` binds fields (run_id,
repo, dedupe_key, attempt, pr_url, ...) onto every log record emitted within
its ``with`` block -- via :mod:`contextvars`, so it survives nested calls
without threading these values through every function signature -- and
:class:`JsonFormatter` renders each record (context fields included) as one
JSON object per line, the shape a log aggregator can index directly instead
of needing a regex parser.

:func:`configure_sentry` is a no-op when ``dsn`` is empty (the default), so
every call site can call it unconditionally rather than branching on whether
Sentry is configured.
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

_context: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar(
    "depfix_log_context", default=None
)

#: Attributes every stdlib `LogRecord` already carries -- excluded from the
#: JSON payload's context fields since they're either redundant with a field
#: `JsonFormatter` computes itself (message, asctime) or internal bookkeeping
#: a log aggregator has no use for.
_RESERVED_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Bind ``fields`` onto every log record emitted within this block.

    Nests: a block opened while already inside another ``log_context``
    inherits the outer block's fields, merged with (and, on key conflict,
    overridden by) its own -- and the outer block's fields are restored
    exactly on exit, regardless of whether this block raised.
    """
    parent = _context.get() or {}
    token = _context.set({**parent, **fields})
    try:
        yield
    finally:
        _context.reset(token)


class _ContextFilter(logging.Filter):
    """Copies the current :func:`log_context` fields onto every record
    passing through a handler this filter is attached to."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_context.get() or {}).items():
            setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """Renders each record as one JSON object per line: timestamp, level,
    logger name, message, any exception, and every non-reserved attribute
    on the record -- which includes both `log_context()` fields (via
    `_ContextFilter`) and a caller's own `logger.info(..., extra={...})`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, *, json_output: bool = True) -> None:
    """Point the root logger at one handler that emits `JsonFormatter`
    lines (or a plain human-readable line under ``json_output=False``, for
    a developer's terminal), with `_ContextFilter` attached so
    `log_context()` fields show up on every line regardless of format.

    Replaces any handlers a prior call (or `logging.basicConfig`) already
    installed, so repeated calls -- e.g. once per CLI invocation -- don't
    pile up duplicate handlers and double-log every line.
    """
    handler = logging.StreamHandler()
    handler.addFilter(_ContextFilter())
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def configure_sentry(dsn: str, *, environment: str = "development") -> None:
    """Initialize the Sentry SDK for uncaught-exception reporting, or do
    nothing if ``dsn`` is empty -- the default, and the case for every
    environment that hasn't opted in by setting ``SENTRY_DSN``.
    """
    if not dsn:
        return
    import sentry_sdk

    sentry_sdk.init(dsn=dsn, environment=environment, traces_sample_rate=0.0)
