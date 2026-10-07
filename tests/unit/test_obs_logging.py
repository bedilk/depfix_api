"""Unit tests for structured logging context and JSON formatting.

Covers `log_context`'s nesting/restore-on-exit semantics, `JsonFormatter`'s
exact JSON shape (context fields + exception info), and `configure_sentry`'s
no-op-when-empty-dsn behavior (so call sites can call it unconditionally).
"""

from __future__ import annotations

import json
import logging

import pytest

from depfix.obs.logging import JsonFormatter, _context, configure_sentry, log_context


@pytest.fixture(autouse=True)
def _reset_context() -> None:
    # Each `log_context` block restores its own token on exit, but guard
    # against any test leaving a stray value if it ever raises before
    # entering the `with` block.
    _context.set(None)


def test_log_context_binds_fields_for_the_duration_of_the_block() -> None:
    assert _context.get() is None
    with log_context(run_id="abc123"):
        assert _context.get() == {"run_id": "abc123"}
    assert _context.get() is None


def test_log_context_nests_and_merges_with_outer_fields() -> None:
    with log_context(run_id="abc123"):
        with log_context(repo="org/repo"):
            assert _context.get() == {"run_id": "abc123", "repo": "org/repo"}
        # Inner block's fields are gone; outer's are restored exactly.
        assert _context.get() == {"run_id": "abc123"}


def test_log_context_inner_block_overrides_outer_on_key_conflict() -> None:
    with log_context(attempt=1):
        with log_context(attempt=2):
            assert _context.get() == {"attempt": 2}
        assert _context.get() == {"attempt": 1}


def test_log_context_restores_outer_fields_even_if_block_raises() -> None:
    with log_context(run_id="abc123"):
        with pytest.raises(ValueError):
            with log_context(repo="org/repo"):
                raise ValueError("boom")
        assert _context.get() == {"run_id": "abc123"}


def test_json_formatter_includes_message_level_logger_and_context_fields() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="depfix.orchestrator",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="opened PR %s",
        args=("https://example/pr/1",),
        exc_info=None,
    )
    record.run_id = "abc123"
    record.repo = "org/repo"

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "depfix.orchestrator"
    assert payload["message"] == "opened PR https://example/pr/1"
    assert payload["run_id"] == "abc123"
    assert payload["repo"] == "org/repo"
    assert "exc_info" not in payload


def test_json_formatter_includes_formatted_exception() -> None:
    formatter = JsonFormatter()
    try:
        raise RuntimeError("nope")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            name="depfix",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))

    assert "RuntimeError: nope" in payload["exc_info"]


def test_configure_sentry_is_a_noop_for_empty_dsn() -> None:
    # Must not raise or attempt to import sentry_sdk's network-touching init
    # when no DSN is configured -- the default for every environment that
    # hasn't opted in.
    configure_sentry("")
