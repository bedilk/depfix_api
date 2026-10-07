"""Comment- and string-aware scanning for JavaScript/TypeScript source.

A codemod must never rewrite text that only *looks* like a call site: a
match inside a comment, a string literal, or a regex literal is the classic
way a "safe" deterministic rule corrupts a file. Pulling a real JS parser
into depfix for this would be defensible, but it is a large dependency for
a narrow need, so instead we build a **mask**: a string the same length as
the source, with the same newlines, where the body of every comment,
string, template literal and regex literal has been replaced by spaces.

Because the mask preserves offsets exactly, every regex is run against the
mask while every slice is taken from the original source. A match on the
mask is therefore always a match on real code, and match spans are directly
usable as source spans.

Known limitation: interpolations inside template literals *are* treated as
live code (correctly), but a call site written inside a nested template
inside an interpolation is masked conservatively. Conservative here means
"missed", never "corrupted", which is the right direction for this
tradeoff.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

__all__ = [
    "code_only_finditer",
    "count_code_occurrences",
    "line_end",
    "line_start",
    "mask_code",
    "matching_paren",
]

# A `/` begins a regex literal (rather than division) when the previous
# significant token cannot end an expression. This is the standard
# heuristic; it is wrong only for pathological code that no migration
# target contains.
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%~^<>")
_REGEX_PRECEDING_KEYWORDS = frozenset(
    {
        "await",
        "case",
        "delete",
        "do",
        "else",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "typeof",
        "void",
        "yield",
    }
)


def mask_code(source: str) -> str:
    """Return ``source`` with all non-code regions replaced by spaces.

    Newlines are preserved so line/column arithmetic on the mask matches the
    original. The returned string always has ``len(source)`` characters.
    """
    out = list(source)
    i = 0
    n = len(source)
    while i < n:
        pair = source[i : i + 2]
        ch = source[i]
        if pair == "//":
            i = _mask_line_comment(source, out, i)
        elif pair == "/*":
            i = _mask_block_comment(source, out, i)
        elif ch in "'\"":
            i = _mask_quoted(source, out, i, ch)
        elif ch == "`":
            i = _mask_template(source, out, i)
        elif ch == "/" and _is_regex_start(out, i):
            i = _mask_regex(source, out, i)
        else:
            i += 1
    return "".join(out)


def code_only_finditer(
    pattern: re.Pattern[str] | str, source: str, *, mask: str | None = None
) -> Iterator[re.Match[str]]:
    """Iterate matches of ``pattern`` that fall in real code.

    The returned matches index into ``source`` (identical offsets), but their
    ``group()`` text comes from the mask. For every pattern in this package
    the two are identical, because the patterns only ever match identifiers,
    dots and parens -- none of which are masked. Slice ``source`` directly if
    you need text that could span a masked region.
    """
    masked = mask_code(source) if mask is None else mask
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    return compiled.finditer(masked)


def count_code_occurrences(
    pattern: re.Pattern[str] | str, source: str, *, mask: str | None = None
) -> int:
    """Count matches of ``pattern`` in real code only."""
    return sum(1 for _ in code_only_finditer(pattern, source, mask=mask))


def matching_paren(source: str, open_index: int, *, mask: str | None = None) -> int:
    """Index of the ``)`` closing the ``(`` at ``open_index``, or ``-1``.

    Parens inside comments and strings are ignored, which is the whole point
    of doing this against the mask rather than by counting characters.
    """
    masked = mask_code(source) if mask is None else mask
    if open_index < 0 or open_index >= len(masked) or masked[open_index] != "(":
        return -1
    depth = 0
    for i in range(open_index, len(masked)):
        char = masked[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def line_start(source: str, index: int) -> int:
    """Index of the first character on ``index``'s line."""
    found = source.rfind("\n", 0, index)
    return 0 if found == -1 else found + 1


def line_end(source: str, index: int) -> int:
    """Index just past the last character on ``index``'s line."""
    found = source.find("\n", index)
    return len(source) if found == -1 else found


# --- internals ------------------------------------------------------------


def _mask_line_comment(source: str, out: list[str], start: int) -> int:
    end = source.find("\n", start)
    end = len(source) if end == -1 else end
    for i in range(start, end):
        out[i] = " "
    return end


def _mask_block_comment(source: str, out: list[str], start: int) -> int:
    end = source.find("*/", start + 2)
    end = len(source) if end == -1 else end + 2
    for i in range(start, end):
        if source[i] != "\n":
            out[i] = " "
    return end


def _mask_quoted(source: str, out: list[str], start: int, quote: str) -> int:
    n = len(source)
    out[start] = " "
    i = start + 1
    while i < n:
        char = source[i]
        if char == "\\":
            out[i] = " "
            if i + 1 < n and source[i + 1] != "\n":
                out[i + 1] = " "
            i += 2
            continue
        if char == quote:
            out[i] = " "
            return i + 1
        if char == "\n":
            # Unterminated literal. Stop at the newline rather than masking
            # the rest of the file on a syntax error we didn't cause.
            return i
        out[i] = " "
        i += 1
    return n


def _mask_template(source: str, out: list[str], start: int) -> int:
    n = len(source)
    out[start] = " "
    i = start + 1
    while i < n:
        char = source[i]
        if char == "\\":
            out[i] = " "
            if i + 1 < n and source[i + 1] != "\n":
                out[i + 1] = " "
            i += 2
            continue
        if char == "`":
            out[i] = " "
            return i + 1
        if source[i : i + 2] == "${":
            # An interpolation is live code and may hold a real call site,
            # so mask only the `${` and `}` delimiters and walk the interior.
            out[i] = " "
            out[i + 1] = " "
            i = _mask_interpolation(source, out, i + 2)
            continue
        if char != "\n":
            out[i] = " "
        i += 1
    return n


def _mask_interpolation(source: str, out: list[str], start: int) -> int:
    n = len(source)
    depth = 1
    i = start
    while i < n:
        char = source[i]
        pair = source[i : i + 2]
        if pair == "//":
            i = _mask_line_comment(source, out, i)
        elif pair == "/*":
            i = _mask_block_comment(source, out, i)
        elif char in "'\"":
            i = _mask_quoted(source, out, i, char)
        elif char == "`":
            i = _mask_template(source, out, i)
        elif char == "{":
            depth += 1
            i += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                out[i] = " "
                return i + 1
            i += 1
        else:
            i += 1
    return n


def _is_regex_start(out: list[str], index: int) -> bool:
    i = index - 1
    while i >= 0 and out[i] in " \t\r\n":
        i -= 1
    if i < 0:
        return True
    char = out[i]
    if char in _REGEX_PRECEDERS:
        return True
    if not (char.isalnum() or char in "_$"):
        return False
    end = i
    while i >= 0 and (out[i].isalnum() or out[i] in "_$"):
        i -= 1
    return "".join(out[i + 1 : end + 1]) in _REGEX_PRECEDING_KEYWORDS


def _mask_regex(source: str, out: list[str], start: int) -> int:
    n = len(source)
    i = start + 1
    in_class = False
    while i < n:
        char = source[i]
        if char == "\\":
            i += 2
            continue
        if char == "\n":
            # Not a regex after all -- leave the slash as code.
            return start + 1
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            for j in range(start, i + 1):
                out[j] = " "
            j = i + 1
            while j < n and source[j].isalpha():
                out[j] = " "
                j += 1
            return j
        i += 1
    return start + 1
