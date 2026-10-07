"""Deterministic, reviewed rewrites for known breaking changes.

A codemod is the honest answer to "can you fix this reliably?" for
migrations whose shape is known in advance: it is free, it produces
byte-identical output on every run, it is unit-tested, and its diff can be
reviewed once instead of once per pull request. depfix tries a codemod
before spending an LLM call on any file.

The contract every codemod in this package obeys is *decline rather than
half-migrate*. A rule that rewrites four of five call sites in a file and
leaves the fifth has produced code that compiles, passes review, and breaks
at runtime -- strictly worse than not touching the file at all. So every
codemod verifies its own completeness after rewriting, and returns a
decline with a stated reason if it cannot. Declined files fall through to
the language model, which is allowed to reason about the cases a regex
can't.
"""

from __future__ import annotations

from depfix.codemods.base import Codemod, CodemodResult
from depfix.codemods.javascript import (
    MethodRenameCodemod,
    OpenAIV4ConstructorCodemod,
    OpenAIV4MethodCodemod,
)
from depfix.codemods.registry import CodemodRegistry
from depfix.codemods.symbols import RenamePaths, normalize_api, rename_paths
from depfix.codemods.text import (
    code_only_finditer,
    count_code_occurrences,
    line_start,
    mask_code,
    matching_paren,
)

__all__ = [
    "Codemod",
    "CodemodRegistry",
    "CodemodResult",
    "MethodRenameCodemod",
    "OpenAIV4ConstructorCodemod",
    "OpenAIV4MethodCodemod",
    "RenamePaths",
    "code_only_finditer",
    "count_code_occurrences",
    "line_start",
    "mask_code",
    "matching_paren",
    "normalize_api",
    "rename_paths",
]
