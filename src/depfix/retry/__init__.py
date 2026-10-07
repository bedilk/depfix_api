"""Week 5 bounded retry loop: feeds concrete syntax/test failures back to
the LLM for a re-attempt, capped at a fixed number of rounds.

See :class:`depfix.retry.loop.RetryLoop`.
"""

from __future__ import annotations

from depfix.retry.loop import RetryLoop

__all__ = ["RetryLoop"]
