"""Comment and string masking shared by every language scanner.

Replaces comment and string content with spaces, preserving line/column
offsets so downstream regex match positions map back to the original file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MaskingRules:
    line_comment: tuple[str, ...] = ()
    block_comment: tuple[tuple[str, str], ...] = ()
    string_delims: tuple[str, ...] = ('"', "'")
    triple_string_delims: tuple[str, ...] = ()
    mask_strings: bool = True


PYTHON_RULES = MaskingRules(
    line_comment=("#",),
    triple_string_delims=('"""', "'''"),
    string_delims=('"', "'"),
)
JS_RULES = MaskingRules(
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    string_delims=('"', "'", "`"),
)
GO_RULES = MaskingRules(
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    string_delims=('"', "`"),
)
JAVA_RULES = JS_RULES
RUBY_RULES = MaskingRules(
    line_comment=("#",),
    block_comment=(("=begin", "=end"),),
    string_delims=('"', "'"),
)


def mask_source(source: str, rules: MaskingRules) -> str:
    """Return source with comments and string contents replaced by spaces.

    Line and column positions are preserved exactly so regex match offsets
    still map to the original file.
    """
    out = list(source)
    i = 0
    n = len(source)
    in_block: tuple[str, str] | None = None
    in_string: str | None = None
    in_triple: str | None = None

    while i < n:
        ch = source[i]

        if in_block is not None:
            close = in_block[1]
            if source.startswith(close, i):
                for k in range(len(close)):
                    if out[i + k] != "\n":
                        out[i + k] = " "
                i += len(close)
                in_block = None
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue

        if in_triple is not None:
            if source.startswith(in_triple, i):
                for k in range(len(in_triple)):
                    if out[i + k] != "\n":
                        out[i + k] = " "
                i += len(in_triple)
                in_triple = None
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue

        if in_string is not None:
            if ch == "\\" and i + 1 < n and source[i + 1] != "\n":
                out[i] = " "
                out[i + 1] = " "
                i += 2
                continue
            if ch == in_string:
                out[i] = " "
                in_string = None
                i += 1
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue

        for triple in rules.triple_string_delims:
            if source.startswith(triple, i):
                for k in range(len(triple)):
                    out[i + k] = " "
                in_triple = triple
                i += len(triple)
                break
        else:
            for open_close in rules.block_comment:
                open_, _ = open_close
                if source.startswith(open_, i):
                    for k in range(len(open_)):
                        out[i + k] = " "
                    in_block = open_close
                    i += len(open_)
                    break
            else:
                matched_line = False
                for prefix in rules.line_comment:
                    if source.startswith(prefix, i):
                        j = i
                        while j < n and source[j] != "\n":
                            out[j] = " "
                            j += 1
                        i = j
                        matched_line = True
                        break
                if matched_line:
                    continue

                if rules.mask_strings and ch in rules.string_delims:
                    out[i] = " "
                    in_string = ch
                    i += 1
                    continue

                i += 1

    return "".join(out)
