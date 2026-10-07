"""Static analysis beyond TypeScript: mypy for Python, go vet for Go.

TypeScript repos get type-checked via ``tsc --noEmit``, which already lives
in :mod:`depfix.verify.typecheck`. But Python and Go repos deserve the same
fast, local, deterministic oracle. This module provides it:

- **Python**: ``mypy`` (if installed) or ``pyright`` (if mypy is missing).
  Both are strict type checkers; a fix that introduces a type error is
  caught without running any tests.

- **Go**: ``go vet``, which is bundled with the Go toolchain. It detects
  suspicious constructs (unreachable code, mismatched printf args, etc.).

These checks run *before* the test suite, as a MEDIUM-tier gate: cheaper
than tests, more signal than "it parses". A fix that passes static checks
but fails tests is downgraded; a fix that fails static checks is rejected
outright without wasting time on tests.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES, SandboxResult, run_sandboxed

#: ``path:line: error: message`` (mypy/pyright), ``path:line:col: message`` (go vet)
_MYPY_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+)(?::\d+)?:\s*error:\s*(?P<message>.+)$", re.MULTILINE
)
_PYRIGHT_RE = re.compile(
    r"^\s*(?P<file>[^:\n]+):(?P<line>\d+):\d+\s*-\s*error:\s*(?P<message>.+)$", re.MULTILINE
)
_GO_VET_RE = re.compile(
    r"^(?P<file>[^:#\n]+\.go):(?P<line>\d+):(?:\d+:)?\s*(?P<message>.+)$", re.MULTILINE
)


def _relative(root: Path, raw: str) -> str:
    candidate = raw.strip().removeprefix("./")
    path = Path(candidate)
    if path.is_absolute():
        try:
            return path.relative_to(root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def _parse_diagnostics(
    root: Path, output: str, pattern: re.Pattern[str]
) -> tuple[StaticDiagnostic, ...]:
    return tuple(
        StaticDiagnostic(
            relpath=_relative(root, match.group("file")),
            line=int(match.group("line")),
            message=match.group("message").strip(),
        )
        for match in pattern.finditer(output)
    )


logger = logging.getLogger(__name__)


@dataclass
class StaticDiagnostic:
    """One static check diagnostic."""

    relpath: str
    line: int
    message: str

    @property
    def identity(self) -> str:
        return f"{self.relpath}:{self.line}:{self.message}"


@dataclass
class StaticCheckResult:
    """Outcome of running a static checker."""

    ran: bool = False
    passed: bool = False
    skipped_reason: str = ""
    output: str = ""
    checker: str = ""
    diagnostics: tuple[StaticDiagnostic, ...] = ()
    sandbox: SandboxResult | None = None

    @property
    def tool(self) -> str:
        return self.checker

    @property
    def usable(self) -> bool:
        return self.ran and not self.skipped_reason

    @property
    def identities(self) -> frozenset[str]:
        return frozenset(d.identity for d in self.diagnostics)


def detect_ecosystem(root: Path, changed_files: list[Path]) -> str | None:
    """Detect the primary ecosystem from changed files and repo structure.

    Returns 'python', 'go', or None.
    """
    # Check changed file extensions
    exts = {f.suffix for f in changed_files}
    if ".py" in exts:
        return "python"
    if ".go" in exts:
        return "go"

    # Fallback: check for ecosystem markers
    if (root / "setup.py").exists() or (root / "pyproject.toml").exists():
        return "python"
    if (root / "go.mod").exists():
        return "go"

    return None


def run_static_check(
    root: Path,
    changed_files: list[Path] | None = None,
    *,
    ecosystem: str | None = None,
    timeout: float = 300.0,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> StaticCheckResult:
    """Run the appropriate static checker for this repository.

    ``ecosystem`` is authoritative when the caller already knows it (the
    verifier does); ``changed_files`` is the fallback sniff for callers that
    only have a diff. Diagnostics are parsed, not just counted, because the
    verifier diffs them by identity.
    """
    resolved = ecosystem or detect_ecosystem(root, changed_files or [])

    if resolved == "python":
        return _check_python(root, timeout, max_output_bytes)
    if resolved == "go":
        return _check_go(root, timeout, max_output_bytes)
    return StaticCheckResult(
        skipped_reason=f"no static checker configured for {resolved or 'this ecosystem'}"
    )


def _check_python(root: Path, timeout: float, max_output_bytes: int) -> StaticCheckResult:
    """Run mypy or pyright on the repo."""
    # Try mypy first
    if shutil.which("mypy") is not None:
        return _run_mypy(root, timeout, max_output_bytes)
    # Fallback to pyright
    if shutil.which("pyright") is not None:
        return _run_pyright(root, timeout, max_output_bytes)

    return StaticCheckResult(skipped_reason="neither mypy nor pyright found on PATH")


def _run_mypy(root: Path, timeout: float, max_output_bytes: int) -> StaticCheckResult:
    argv = ["mypy", ".", "--no-error-summary", "--show-column-numbers"]
    result = run_sandboxed(argv, cwd=root, timeout=timeout, max_output_bytes=max_output_bytes)
    output = result.stdout + "\n" + result.stderr
    return StaticCheckResult(
        ran=True,
        passed=result.ok,
        checker="mypy",
        output=output,
        diagnostics=_parse_diagnostics(root, output, _MYPY_RE),
        sandbox=result,
    )


def _run_pyright(root: Path, timeout: float, max_output_bytes: int) -> StaticCheckResult:
    argv = ["pyright", "."]
    result = run_sandboxed(argv, cwd=root, timeout=timeout, max_output_bytes=max_output_bytes)
    output = result.stdout + "\n" + result.stderr
    return StaticCheckResult(
        ran=True,
        passed=result.ok,
        checker="pyright",
        output=output,
        diagnostics=_parse_diagnostics(root, output, _PYRIGHT_RE),
        sandbox=result,
    )


def _check_go(root: Path, timeout: float, max_output_bytes: int) -> StaticCheckResult:
    """``go vet ./...`` -- bundled with the toolchain, so always available
    wherever a Go fix could be generated at all."""
    from depfix.ecosystems.specs import GO

    if shutil.which("go") is None:
        return StaticCheckResult(skipped_reason="go not found on PATH")

    argv = ["go", "vet", "./..."]
    result = run_sandboxed(
        argv,
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        extra_allowlist=GO.env_allowlist_extra,
    )
    output = result.stdout + "\n" + result.stderr
    return StaticCheckResult(
        ran=True,
        passed=result.ok,
        checker="go vet",
        output=output,
        diagnostics=_parse_diagnostics(root, output, _GO_VET_RE),
        sandbox=result,
    )


def new_static_diagnostics(
    baseline: StaticCheckResult, after_fix: StaticCheckResult
) -> tuple[StaticDiagnostic, ...]:
    """Diagnostics present after the fix that weren't there before."""
    added = after_fix.identities - baseline.identities
    return tuple(d for d in after_fix.diagnostics if d.identity in added)
