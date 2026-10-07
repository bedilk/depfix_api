"""JavaScript/TypeScript codemods.

Every rule here verifies its own completeness before returning a rewrite,
and declines with a stated reason when it can't. See the package docstring
for why that contract matters more than coverage.
"""

from __future__ import annotations

import re

from depfix.codemods.base import CodemodResult
from depfix.codemods.symbols import normalize_api, rename_paths
from depfix.codemods.text import (
    code_only_finditer,
    count_code_occurrences,
    line_start,
    mask_code,
    matching_paren,
)
from depfix.core.models import BreakingChange

__all__ = [
    "MethodRenameCodemod",
    "OpenAIV4ConstructorCodemod",
    "OpenAIV4MethodCodemod",
]

_IDENT = r"[A-Za-z_$][\w$]*"
#: A receiver expression: an optional `this.`, then a dotted identifier
#: chain. Lazy so the shortest receiver that still leaves the target path
#: matching wins, which makes `client.chat.completions.create(` resolve
#: with receiver `client.chat.completions`.
_RECEIVER = rf"(?:this\s*\.\s*)?{_IDENT}(?:\s*\.\s*{_IDENT})*?"


def _dotted(path: str) -> str:
    return r"\s*\.\s*".join(re.escape(part) for part in path.split("."))


def _method_call_re(path: str) -> re.Pattern[str]:
    """Match ``<receiver>.<path>(`` where ``<path>`` may itself be dotted."""
    return re.compile(rf"(?<![\w$.])(?P<recv>{_RECEIVER})\s*\.\s*{_dotted(path)}\s*\(")


def _bare_reference_re(path: str) -> re.Pattern[str]:
    """Match ``.<path>`` in any position -- call or not."""
    return re.compile(rf"\.\s*{_dotted(path)}\b")


class MethodRenameCodemod:
    """Rename a method on a package's client object.

    Handles ``client.old()``, ``this.client.old()`` and arbitrarily dotted
    receivers, and rewrites only the member path -- never the receiver,
    whose name is a local variable choice we have no business changing.
    """

    id = "js.method-rename"
    specificity = 10

    def claims(self, change: BreakingChange) -> bool:
        return rename_paths(change.old_api, change.new_api) is not None

    def apply(self, source: str, change: BreakingChange) -> CodemodResult:
        paths = rename_paths(change.old_api, change.new_api)
        if paths is None:
            return CodemodResult.decline(
                self.id, "old_api/new_api do not parse as a namespace-preserving dotted rename"
            )

        # When the old leaf is a suffix of the new path (`create` ->
        # `chat.completions.create`), no textual scan can distinguish a
        # surviving old reference from the tail of a freshly-written new one:
        # the bare-reference scan matches the new code, and so does the call
        # scan, because receivers may be dotted. A rule that cannot verify
        # its own completeness must not run.
        if f".{paths.old_path}" in f".{paths.new_path}":
            return CodemodResult.decline(
                self.id,
                f"the old path .{paths.old_path} is a suffix of the new path "
                f".{paths.new_path}, so completeness cannot be verified textually",
            )

        call_re = _method_call_re(paths.old_path)
        mask = mask_code(source)
        matches = list(code_only_finditer(call_re, source, mask=mask))
        if not matches:
            return CodemodResult.unchanged(
                self.id, f"no .{paths.old_path}( call sites in real code"
            )

        # Right to left, so earlier match offsets stay valid.
        result = source
        for match in reversed(matches):
            receiver = source[match.start("recv") : match.end("recv")]
            result = f"{result[: match.start()]}{receiver}.{paths.new_path}({result[match.end() :]}"

        residual = count_code_occurrences(_bare_reference_re(paths.old_path), result)
        if residual:
            return CodemodResult.decline(
                self.id,
                f"{residual} reference(s) to .{paths.old_path} remain that are not direct "
                "calls -- aliased, destructured, or passed as a callback -- and a partial "
                "migration is worse than none",
            )

        return CodemodResult.rewrote(
            self.id,
            result,
            f"rewrote {len(matches)} call site(s): .{paths.old_path} -> .{paths.new_path}",
        )


class OpenAIV4MethodCodemod(MethodRenameCodemod):
    """openai-node v3 -> v4 method rename *and* response-shape change.

    v4 did two things at once: it moved methods (``createChatCompletion`` ->
    ``chat.completions.create``) and it stopped wrapping responses in the
    axios ``.data`` envelope. Fixing only the rename produces code that
    compiles and returns ``undefined`` at runtime, which is precisely the
    failure mode a deterministic rule exists to prevent -- so this rule
    handles both or declines.
    """

    id = "openai.v4-method"
    specificity = 50

    def claims(self, change: BreakingChange) -> bool:
        if change.package.strip().lower() != "openai":
            return False
        old = normalize_api(change.old_api)
        return old is not None and super().claims(change)

    def apply(self, source: str, change: BreakingChange) -> CodemodResult:
        renamed = super().apply(source, change)
        if renamed.declined or not renamed.changed or renamed.source is None:
            return renamed

        paths = rename_paths(change.old_api, change.new_api)
        assert paths is not None  # guaranteed by the successful super() call

        unwrapped, inline_count = _unwrap_inline_data(renamed.source, paths.new_path)
        unwrapped, var_count, aliased = _unwrap_variable_data(unwrapped, paths.new_path)
        if aliased:
            return CodemodResult.decline(
                self.id,
                f"the response variable(s) {', '.join(sorted(aliased))} are reassigned after "
                "the call, so which uses of .data belong to this response cannot be "
                "determined textually",
            )

        total = inline_count + var_count
        return CodemodResult.rewrote(
            self.id,
            unwrapped,
            f"{renamed.note}; removed {total} .data access(es) for the v4 response shape",
        )


class OpenAIV4ConstructorCodemod:
    """openai-node v3 -> v4 client construction.

    v3::

        const { Configuration, OpenAIApi } = require("openai");
        const configuration = new Configuration({ apiKey: process.env.KEY });
        const openai = new OpenAIApi(configuration);

    v4::

        const OpenAI = require("openai");
        const openai = new OpenAI({ apiKey: process.env.KEY });

    Three statements collapse to two, which means deleting a line -- so this
    rule refuses unless it can prove the ``Configuration`` instance is used
    nowhere else.
    """

    id = "openai.v4-constructor"
    specificity = 60

    _CJS_IMPORT = re.compile(
        r"""(?P<decl>const|let|var)\s*\{(?P<names>[^}]*)\}\s*=\s*
            require\(\s*['"]openai['"]\s*\)\s*;?""",
        re.VERBOSE,
    )
    _ESM_IMPORT = re.compile(
        r"""import\s*\{(?P<names>[^}]*)\}\s*from\s*['"]openai['"]\s*;?""",
        re.VERBOSE,
    )
    _NEW_CONFIG = re.compile(
        rf"(?P<decl>const|let|var)\s+(?P<name>{_IDENT})\s*=\s*new\s+Configuration\s*\("
    )
    _NEW_CLIENT = re.compile(rf"new\s+OpenAIApi\s*\(\s*(?P<arg>{_IDENT})\s*\)")

    def claims(self, change: BreakingChange) -> bool:
        if change.package.strip().lower() != "openai":
            return False
        haystack = " ".join(
            filter(None, (change.old_api, change.new_api, change.description))
        ).lower()
        return "configuration" in haystack or "openaiapi" in haystack

    def apply(self, source: str, change: BreakingChange) -> CodemodResult:
        mask = mask_code(source)

        import_match = next(code_only_finditer(self._CJS_IMPORT, source, mask=mask), None)
        is_esm = False
        if import_match is None:
            import_match = next(code_only_finditer(self._ESM_IMPORT, source, mask=mask), None)
            is_esm = import_match is not None
        if import_match is None:
            return CodemodResult.unchanged(self.id, "no v3-style openai import in this file")

        names = {
            n.strip()
            for n in source[import_match.start("names") : import_match.end("names")].split(",")
        }
        names.discard("")
        if not {"Configuration", "OpenAIApi"} & names:
            return CodemodResult.unchanged(
                self.id, "the openai import does not bring in Configuration/OpenAIApi"
            )
        extra = names - {"Configuration", "OpenAIApi"}
        if extra:
            return CodemodResult.decline(
                self.id,
                f"the openai import also brings in {', '.join(sorted(extra))}, which v4 may "
                "expose differently -- rewriting the import would risk dropping a binding",
            )

        config_match = next(code_only_finditer(self._NEW_CONFIG, source, mask=mask), None)
        if config_match is None:
            return CodemodResult.decline(
                self.id, "found the v3 import but no `new Configuration(` to fold into the client"
            )
        config_name = source[config_match.start("name") : config_match.end("name")]

        open_paren = config_match.end() - 1
        close_paren = matching_paren(source, open_paren, mask=mask)
        if close_paren < 0:
            return CodemodResult.decline(
                self.id, "the `new Configuration(` argument list is unbalanced"
            )
        config_args = source[open_paren + 1 : close_paren].strip()

        client_match = next(code_only_finditer(self._NEW_CLIENT, source, mask=mask), None)
        if client_match is None:
            return CodemodResult.decline(
                self.id, "found `new Configuration(` but no `new OpenAIApi(` to pass it to"
            )
        if source[client_match.start("arg") : client_match.end("arg")] != config_name:
            return CodemodResult.decline(
                self.id,
                f"`new OpenAIApi(` is not passed {config_name}, so the config-to-client "
                "relationship is not the standard v3 shape",
            )

        # The configuration variable is about to be deleted. If anything else
        # reads it, deleting the declaration leaves a reference to nothing.
        config_uses = count_code_occurrences(
            re.compile(rf"(?<![\w$.]){re.escape(config_name)}(?![\w$])"), source, mask=mask
        )
        if config_uses > 2:  # the declaration itself, plus the OpenAIApi call
            return CodemodResult.decline(
                self.id,
                f"`{config_name}` is referenced {config_uses - 1} time(s) besides the client "
                "constructor, so its declaration cannot be removed safely",
            )

        # Apply right to left so earlier offsets stay valid.
        result = source
        result = f"{result[: client_match.start()]}new OpenAI({config_args}){result[client_match.end() :]}"

        stmt_start = line_start(result, config_match.start())
        stmt_end = close_paren + 1
        while stmt_end < len(result) and result[stmt_end] in " \t":
            stmt_end += 1
        if stmt_end < len(result) and result[stmt_end] == ";":
            stmt_end += 1
        if stmt_end < len(result) and result[stmt_end] == "\n":
            stmt_end += 1
        result = result[:stmt_start] + result[stmt_end:]

        replacement = (
            'import OpenAI from "openai";' if is_esm else 'const OpenAI = require("openai");'
        )
        result = f"{result[: import_match.start()]}{replacement}{result[import_match.end() :]}"

        residual = count_code_occurrences(
            re.compile(r"(?<![\w$.])(?:Configuration|OpenAIApi)(?![\w$])"), result
        )
        if residual:
            return CodemodResult.decline(
                self.id,
                f"{residual} reference(s) to Configuration/OpenAIApi remain after the rewrite, "
                "so this file uses them in a way this rule does not model",
            )

        return CodemodResult.rewrote(
            self.id,
            result,
            "collapsed Configuration + OpenAIApi into a single OpenAI client",
        )


# --- .data unwrapping -----------------------------------------------------


def _unwrap_inline_data(source: str, new_path: str) -> tuple[str, int]:
    """Remove ``.data`` applied directly to a migrated call's result.

    Handles both ``client.chat.completions.create(...).data`` and
    ``(await client.chat.completions.create(...)).data``. The wrapping
    parens are left in place -- they are harmless, and removing them is
    another chance to get bracket matching wrong.
    """
    call_re = _method_call_re(new_path)
    result = source
    removed = 0
    while True:
        mask = mask_code(result)
        target: tuple[int, int] | None = None
        for match in reversed(list(code_only_finditer(call_re, result, mask=mask))):
            close = matching_paren(result, match.end() - 1, mask=mask)
            if close < 0:
                continue
            tail = mask[close + 1 :]
            tail_match = re.match(r"\s*\)?\s*\.\s*data\b", tail)
            if tail_match is not None:
                target = (close + 1, tail_match.end())
                break
        if target is None:
            return result, removed
        tail_start, tail_len = target
        segment = result[tail_start : tail_start + tail_len]
        dot = segment.rindex(".")
        cut_start = tail_start + dot
        cut_end = tail_start + tail_len
        result = result[:cut_start] + result[cut_end:]
        removed += 1


def _unwrap_variable_data(source: str, new_path: str) -> tuple[str, int, set[str]]:
    """Remove ``.data`` from uses of a variable holding a migrated response.

    Returns the rewritten source, the number of accesses removed, and the
    set of variable names that were reassigned after the call. A reassigned
    variable is a decline: once ``response`` can hold something else, no
    textual rule can tell which ``response.data`` belonged to the migrated
    call.
    """
    call_re = _method_call_re(new_path)
    mask = mask_code(source)
    assign_re = re.compile(rf"(?:const|let|var)\s+(?P<name>{_IDENT})\s*=\s*(?:await\s+)?$")
    spans: list[tuple[int, int, str]] = []
    aliased: set[str] = set()

    for match in code_only_finditer(call_re, source, mask=mask):
        head = mask[line_start(source, match.start()) : match.start()]
        assigned = assign_re.search(head)
        if assigned is None:
            continue
        name = assigned.group("name")
        after = mask[match.end() :]
        if re.search(rf"(?<![\w$.]){re.escape(name)}\s*=(?!=)", after):
            aliased.add(name)
            continue
        access_re = re.compile(rf"(?<![\w$.]){re.escape(name)}\s*\.\s*data\b")
        for access in access_re.finditer(after):
            spans.append((match.end() + access.start(), match.end() + access.end(), name))

    if aliased:
        return source, 0, aliased

    result = source
    for start, end, name in sorted(spans, reverse=True):
        result = result[:start] + name + result[end:]
    return result, len(spans), aliased
