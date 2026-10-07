"""``tsc --noEmit`` as a second verification oracle.

The test oracle (:mod:`depfix.verify.verifier`) is the strongest signal we
have and also the one most repos can't give us: no ``test`` script, or a
suite that never touches the migrated call site. That left every such edit
``SUSPECT``, i.e. uncommittable, i.e. depfix did nothing for the majority
of the fleet.

A TypeScript repo, though, already ships an oracle that knows the SDK's
*new* type surface: the SDK's own ``.d.ts`` files. A v3->v4 openai
migration that got the request shape or the response unwrapping wrong
almost always fails ``tsc --noEmit``. It costs one compile, needs no test
authoring, and -- critically -- it is not the model grading its own
homework: the constraint comes from the vendor's published types, not from
anything depfix or an LLM wrote.

The comparison discipline is copied deliberately from the test oracle:
baseline first, fixed second, diffed by **diagnostic identity**
(``relpath(line,col): TSxxxx``) and never by error *count*. A repo sitting
on 40 pre-existing type errors must not make every fix look broken, and a
fix that trades one error for a different one must not look clean.

Never raises: every failure to find/run/parse ``tsc`` folds into the
returned :class:`TypecheckResult`, same contract as
:func:`depfix.verify.runner.run_tests`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES, run_sandboxed

logger = logging.getLogger(__name__)

#: ``tsc``'s non-pretty diagnostic line, e.g.
#: ``src/chat.ts(12,7): error TS2551: Property 'createModeration' does not ...``
_DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+),(?P<column>\d+)\):\s+error\s+(?P<code>TS\d+):\s*(?P<message>.*)$"
)

#: Diagnostics with no file position at all (bad ``tsconfig``, missing
#: ``--lib``, ...). Recorded so a *configuration* failure isn't silently
#: read as "the fix broke something".
_GLOBAL_DIAGNOSTIC_RE = re.compile(r"^error\s+(?P<code>TS\d+):\s*(?P<message>.*)$")

_TS_DEPENDENCY_NAMES = ("typescript",)


@dataclass(frozen=True)
class Diagnostic:
    """One ``tsc`` error, identified well enough to match across two runs."""

    relpath: str
    line: int
    column: int
    code: str
    message: str

    @property
    def identity(self) -> str:
        """Stable key for baseline-vs-after diffing.

        Includes position because the same error code on two different
        lines is two different problems, and excludes the message text
        because ``tsc`` wording varies with ``--pretty``/locale while the
        code does not.
        """
        return f"{self.relpath}({self.line},{self.column}): {self.code}"

    def one_line(self) -> str:
        return f"{self.relpath}:{self.line}:{self.column} {self.code}: {self.message}"


@dataclass
class TypecheckResult:
    """Outcome of one ``tsc --noEmit`` run over a whole checkout."""

    ran: bool
    skipped_reason: str = ""
    exit_code: int = 0
    duration_ms: float = 0.0
    diagnostics: tuple[Diagnostic, ...] = ()
    global_errors: tuple[str, ...] = ()
    timed_out: bool = False
    output_tail: str = ""

    @property
    def usable(self) -> bool:
        """True if this run can be *compared* against another one.

        A timeout, or a compiler that failed before it could emit
        positioned diagnostics (bad config, missing types package),
        produces output that isn't a statement about the code -- diffing
        against it would manufacture phantom regressions.
        """
        if not self.ran or self.timed_out:
            return False
        return not (self.global_errors and not self.diagnostics)

    @property
    def clean(self) -> bool:
        return self.usable and self.exit_code == 0 and not self.diagnostics

    @property
    def identities(self) -> frozenset[str]:
        return frozenset(d.identity for d in self.diagnostics)


def has_typescript(root: str | Path) -> bool:
    """True if ``root`` looks like a project ``tsc`` can meaningfully check.

    Requires a ``tsconfig.json``: without one, ``tsc`` has no file list,
    no ``lib``, and no module resolution mode, and its output says more
    about the missing config than about the code.
    """
    return (Path(root) / "tsconfig.json").is_file()


def find_tsc(root: str | Path) -> str | None:
    """Locate the ``tsc`` binary for ``root``, preferring the repo's own
    installed copy over anything on ``PATH``.

    Version matters: checking a repo pinned to TypeScript 4.x with a
    globally-installed 5.x produces diagnostics the repo's own CI would
    never see, which is exactly the kind of false regression that would
    make depfix revert a correct fix.
    """
    root = Path(root)
    local = root / "node_modules" / ".bin" / ("tsc.cmd" if os.name == "nt" else "tsc")
    if local.is_file():
        return str(local)
    return shutil.which("tsc")


def declares_typescript(root: str | Path) -> bool:
    """True if ``package.json`` declares TypeScript as a dependency -- used
    only to produce a better skip message ("typescript declared but not
    installed" vs "not a TypeScript project")."""
    try:
        pkg = json.loads((Path(root) / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    return any(name in deps for name in _TS_DEPENDENCY_NAMES)


def run_typecheck(
    root: str | Path,
    *,
    timeout: float,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> TypecheckResult:
    """Run ``tsc --noEmit`` over ``root`` and parse its diagnostics."""
    root = Path(root)

    if not has_typescript(root):
        return TypecheckResult(
            ran=False,
            skipped_reason=(
                "typescript is declared but no tsconfig.json was found"
                if declares_typescript(root)
                else "not a TypeScript project (no tsconfig.json)"
            ),
        )

    tsc = find_tsc(root)
    if tsc is None:
        return TypecheckResult(
            ran=False,
            skipped_reason="tsc not found (install the repo's dependencies, or `npm i -D typescript`)",
        )

    result = run_sandboxed(
        [tsc, "--noEmit", "--pretty", "false"],
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )
    if result.error:
        return TypecheckResult(ran=False, skipped_reason=result.error)
    if result.timed_out:
        return TypecheckResult(
            ran=True,
            timed_out=True,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            output_tail=(result.stdout[-2000:] + result.stderr[-2000:]),
        )

    diagnostics, global_errors = _parse_diagnostics(result.stdout, result.stderr)
    return TypecheckResult(
        ran=True,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        diagnostics=diagnostics,
        global_errors=global_errors,
        output_tail=(result.stdout[-2000:] + result.stderr[-2000:]),
    )


def _parse_diagnostics(stdout: str, stderr: str) -> tuple[tuple[Diagnostic, ...], tuple[str, ...]]:
    diagnostics: list[Diagnostic] = []
    global_errors: list[str] = []
    for line in (stdout + "\n" + stderr).splitlines():
        line = line.rstrip()
        match = _DIAGNOSTIC_RE.match(line)
        if match:
            diagnostics.append(
                Diagnostic(
                    relpath=Path(match.group("file")).as_posix(),
                    line=int(match.group("line")),
                    column=int(match.group("column")),
                    code=match.group("code"),
                    message=match.group("message").strip(),
                )
            )
            continue
        global_match = _GLOBAL_DIAGNOSTIC_RE.match(line)
        if global_match:
            global_errors.append(f"{global_match.group('code')}: {global_match.group('message')}")
    return tuple(diagnostics), tuple(global_errors)


def new_diagnostics(
    baseline: TypecheckResult, after_fix: TypecheckResult
) -> tuple[Diagnostic, ...]:
    """Diagnostics present after the fix that weren't there before, matched
    by :attr:`Diagnostic.identity` -- never by count."""
    added = after_fix.identities - baseline.identities
    return tuple(d for d in after_fix.diagnostics if d.identity in added)
