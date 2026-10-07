"""Clamping for the per-scan file ceiling.

The ceiling itself is :data:`depfix.config.MAX_SCANNABLE_FILES` -- config is
the lowest layer in this codebase, so the constant lives there and nothing
duplicates it. This module exists so every scanner enforces that ceiling on
whatever it is *handed*, not just on whatever was configured: a caller that
constructs a scanner directly (a test, an eval harness, a future ecosystem
adapter) must not be able to ask for an unbounded walk.

``None`` means "no opinion", which resolves to the ceiling rather than to
"unlimited" -- an omitted cap is the case most likely to be an oversight.
"""

from __future__ import annotations

from depfix.config import MAX_SCANNABLE_FILES

__all__ = ["MAX_SCANNABLE_FILES", "clamp_max_files"]


def clamp_max_files(requested: int | None) -> int:
    """Return a file cap guaranteed to be within ``1..MAX_SCANNABLE_FILES``."""
    if requested is None:
        return MAX_SCANNABLE_FILES
    return max(1, min(int(requested), MAX_SCANNABLE_FILES))
