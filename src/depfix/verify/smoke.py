"""Smoke-check oracles.

Two distinct things live here:

1. ``run_smoke_check`` (original) — per-file module-load check for
   CommonJS/ESM/Python/Ruby. Used by the Verifier as a last-resort oracle
   when neither tests nor typecheck are available.

2. ``SmokeStage`` (new) — whole-app boot smoke: tries to start the app's
   declared or auto-detected entrypoint and confirms it stays alive for
   ``smoke_window_seconds``. Always attempted (controlled by
   ``smoke_always_run``); skips with a reason rather than silently doing
   nothing when no entrypoint can be found.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from depfix.verify.models import StageResult, StageStatus
from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES, run_sandboxed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmokeResult:
    ran: bool
    passed: bool
    error: str = ""
    skipped_reason: str = ""


def run_smoke_check(
    root: Path,
    relpath: str,
    *,
    timeout: float = 30,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> SmokeResult:
    suffix = Path(relpath).suffix
    if suffix == ".rb":
        return _run_ruby_smoke(root, relpath, timeout=timeout, max_output_bytes=max_output_bytes)
    if suffix == ".py":
        return _run_python_smoke(root, relpath, timeout=timeout, max_output_bytes=max_output_bytes)
    if suffix == ".go":
        return _run_go_smoke(root, relpath, timeout=timeout, max_output_bytes=max_output_bytes)
    if suffix not in {".js", ".cjs", ".mjs"}:
        return SmokeResult(
            False, False, skipped_reason="smoke check supports .js/.cjs/.mjs/.py/.rb/.go only"
        )
    if shutil.which("node") is None:
        return SmokeResult(False, False, skipped_reason="node not on PATH")
    subject = (root / relpath).resolve()
    if not subject.is_file():
        return SmokeResult(False, False, skipped_reason=f"{relpath} not found")
    if suffix == ".mjs":
        script = (
            f"import({json.dumps(str(subject))}).then(() => process.exit(0))"
            ".catch(e => { console.error(e.message); process.exit(1); })"
        )
    else:
        script = f"try {{ require({json.dumps(str(subject))}); }} catch(e) {{ console.error(e.message); process.exit(1); }}"
    result = run_sandboxed(
        ["node", "-e", script], cwd=root, timeout=timeout, max_output_bytes=max_output_bytes
    )
    if result.timed_out:
        return SmokeResult(True, False, error="smoke check timed out")
    return SmokeResult(
        True, result.exit_code == 0, error=(result.stderr or result.stdout).strip()[:500]
    )


def _run_python_smoke(
    root: Path, relpath: str, *, timeout: float, max_output_bytes: int
) -> SmokeResult:
    """``py_compile`` the edited file -- syntax-level only, deliberately.

    The JS smoke check actually *loads* the module; importing an edited
    Python module would execute its top-level code against whatever the
    venv happens to contain, which is a heavier, less predictable oracle.
    Compilation catches the "LLM produced non-parsing code" class of
    failure, which is what this last-resort check exists for.
    """
    from depfix.ecosystems.python_runtime import smoke_argv
    from depfix.ecosystems.specs import PYTHON

    argv = smoke_argv(root, relpath)
    if argv is None:
        return SmokeResult(False, False, skipped_reason="python3 not on PATH")
    if not (root / relpath).is_file():
        return SmokeResult(False, False, skipped_reason=f"{relpath} not found")
    result = run_sandboxed(
        argv,
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        extra_allowlist=PYTHON.env_allowlist_extra,
    )
    if result.timed_out:
        return SmokeResult(True, False, error="smoke check timed out")
    return SmokeResult(
        True, result.exit_code == 0, error=(result.stderr or result.stdout).strip()[:500]
    )


def _run_ruby_smoke(
    root: Path, relpath: str, *, timeout: float, max_output_bytes: int
) -> SmokeResult:
    """`ruby -c` -- syntax only, deliberately. Loading the file would run its
    top-level code against whatever gems the bundle happens to contain."""
    from depfix.ecosystems.ruby_runtime import smoke_argv
    from depfix.ecosystems.specs import RUBY

    argv = smoke_argv(root, relpath)
    if argv is None:
        return SmokeResult(False, False, skipped_reason="ruby not on PATH")
    if not (root / relpath).is_file():
        return SmokeResult(False, False, skipped_reason=f"{relpath} not found")
    result = run_sandboxed(
        argv,
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        extra_allowlist=RUBY.env_allowlist_extra,
    )
    if result.timed_out:
        return SmokeResult(True, False, error="smoke check timed out")
    return SmokeResult(
        True, result.exit_code == 0, error=(result.stderr or result.stdout).strip()[:500]
    )


def _run_go_smoke(
    root: Path, relpath: str, *, timeout: float, max_output_bytes: int
) -> SmokeResult:
    """``gofmt -e -l`` -- parse only, deliberately.

    The JS smoke check loads the module; compiling a Go package would pull in
    the whole module graph and turn a last-resort oracle into the most
    expensive one. This catches the "the model produced code that does not
    parse" class of failure.
    """
    from depfix.ecosystems.go_runtime import smoke_argv
    from depfix.ecosystems.specs import GO

    argv = smoke_argv(root, relpath)
    if argv is None:
        return SmokeResult(False, False, skipped_reason="gofmt not on PATH")
    if not (root / relpath).is_file():
        return SmokeResult(False, False, skipped_reason=f"{relpath} not found")
    result = run_sandboxed(
        argv,
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        extra_allowlist=GO.env_allowlist_extra,
    )
    if result.timed_out:
        return SmokeResult(True, False, error="smoke check timed out")
    return SmokeResult(
        True, result.exit_code == 0, error=(result.stderr or result.stdout).strip()[:500]
    )


# ---------------------------------------------------------------------------
# SmokeStage — whole-app boot check (always-run, auto-detects entrypoint)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SmokeCommand:
    argv: list[str]
    cwd: Path
    source: str
    env: dict[str, str] | None = None


@dataclass
class BootOutcome:
    passed: bool
    summary: str
    log_path: Path | None = None


class SmokeStage:
    """Boot the app briefly; confirm it doesn't crash.

    Attempts on every fix (smoke_always_run=True). Auto-detects entrypoint
    from package.json, pyproject, __main__.py, Go, Dockerfile, Rails, or
    operator probes. Returns SKIPPED(no_entrypoint) when nothing runnable
    is found — promoted to FAILED only with smoke_strict=True.
    """

    def __init__(self, settings: object) -> None:
        self._settings = settings

    def run(
        self,
        *,
        checkout_path: Path,
        declared_entrypoint: SmokeCommand | None = None,
    ) -> StageResult:
        start = time.monotonic()

        if not getattr(self._settings, "smoke_always_run", True) and declared_entrypoint is None:
            return StageResult(
                stage="smoke",
                status=StageStatus.DISABLED,
                detail="smoke_always_run=False and no declared entrypoint",
            )

        candidates: list[SmokeCommand] = []
        if declared_entrypoint is not None:
            candidates.append(declared_entrypoint)
        candidates.extend(self._detect_entrypoints(checkout_path))
        candidates.extend(self._probe_entrypoints(checkout_path))

        if not candidates:
            return self._skip(
                "no_entrypoint", "no runnable entrypoint auto-detected or configured", start
            )

        errors: list[str] = []
        for cmd in candidates:
            outcome = self._boot(cmd)
            if outcome.passed:
                return StageResult(
                    stage="smoke",
                    status=StageStatus.PASSED,
                    detail=f"{cmd.source}: {outcome.summary}",
                    duration_seconds=time.monotonic() - start,
                    artifacts=[str(outcome.log_path)] if outcome.log_path else [],
                )
            errors.append(f"{cmd.source}: {outcome.summary}")

        return StageResult(
            stage="smoke",
            status=StageStatus.FAILED,
            detail=" | ".join(errors[:3]),
            duration_seconds=time.monotonic() - start,
        )

    # -- entrypoint detection --------------------------------------------------

    def _detect_entrypoints(self, root: Path) -> list[SmokeCommand]:
        out: list[SmokeCommand] = []

        # 1. package.json scripts
        pkg = root / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                scripts = data.get("scripts", {})
                for script in ("start", "serve", "dev"):
                    if script in scripts:
                        out.append(
                            SmokeCommand(
                                argv=["npm", "run", script, "--if-present"],
                                cwd=root,
                                source=f"package.json:scripts.{script}",
                            )
                        )
                        break
                if "main" in data and (root / data["main"]).exists():
                    out.append(
                        SmokeCommand(
                            argv=["node", data["main"]],
                            cwd=root,
                            source="package.json:main",
                        )
                    )
            except (json.JSONDecodeError, OSError):
                pass

        # 2. Python entrypoints
        pyproj = root / "pyproject.toml"
        if pyproj.exists():
            try:
                import tomllib

                data = tomllib.loads(pyproj.read_text(encoding="utf-8"))
                scripts = data.get("project", {}).get("scripts", {})
                for name in list(scripts)[:1]:
                    out.append(
                        SmokeCommand(
                            argv=[name, "--help"],
                            cwd=root,
                            source=f"pyproject:scripts.{name}",
                        )
                    )
            except Exception:  # nosec B110 — best-effort metric recording; failure is non-fatal
                pass
        for candidate in ("manage.py", "app.py", "main.py", "server.py"):
            if (root / candidate).exists():
                out.append(
                    SmokeCommand(
                        argv=["python", candidate, "--help"],
                        cwd=root,
                        source=f"convention:{candidate}",
                    )
                )
                break
        for main_py in list(root.glob("src/*/__main__.py"))[:1]:
            pkg_name = main_py.parent.name
            out.append(
                SmokeCommand(
                    argv=["python", "-m", pkg_name, "--help"],
                    cwd=root,
                    source=f"__main__:{pkg_name}",
                )
            )

        # 3. Go
        for main_go in list(root.glob("cmd/*/main.go"))[:1]:
            out.append(
                SmokeCommand(
                    argv=["go", "run", "./" + str(main_go.parent.relative_to(root))],
                    cwd=root,
                    source=f"go:{main_go.parent.name}",
                )
            )
        if (root / "main.go").exists() and not list(root.glob("cmd/*/main.go")):
            out.append(SmokeCommand(argv=["go", "run", "."], cwd=root, source="go:main.go"))

        # 4. Dockerfile CMD (exec-form only)
        dockerfile = root / "Dockerfile"
        if dockerfile.exists():
            cmd = self._parse_dockerfile_cmd(dockerfile)
            if cmd:
                out.append(SmokeCommand(argv=cmd, cwd=root, source="dockerfile:CMD"))

        # 5. Rails
        if (root / "bin" / "rails").exists():
            out.append(
                SmokeCommand(
                    argv=["ruby", "bin/rails", "runner", "puts :ok"],
                    cwd=root,
                    source="rails:runner",
                )
            )
        return out

    def _probe_entrypoints(self, root: Path) -> list[SmokeCommand]:
        probes = getattr(self._settings, "smoke_extra_entrypoint_probes", [])
        return [SmokeCommand(argv=shlex.split(p), cwd=root, source=f"probe:{p}") for p in probes]

    @staticmethod
    def _parse_dockerfile_cmd(path: Path) -> list[str] | None:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("CMD ") and "[" in s:
                    inner = s[s.index("[") :]
                    return json.loads(inner)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return None

    def _boot(self, cmd: SmokeCommand) -> BootOutcome:
        window = getattr(self._settings, "smoke_window_seconds", 8.0)
        log_path = cmd.cwd / f".depfix-smoke-{int(time.time())}.log"
        try:
            with log_path.open("w") as log:
                proc = subprocess.Popen(
                    cmd.argv,
                    cwd=cmd.cwd,
                    env=cmd.env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except FileNotFoundError as exc:
            return BootOutcome(
                passed=False, summary=f"command not found: {getattr(exc, 'filename', exc)}"
            )
        except OSError as exc:
            return BootOutcome(passed=False, summary=f"exec failed: {exc}")
        try:
            exit_code = proc.wait(timeout=window)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            return BootOutcome(passed=True, summary=f"alive after {window}s", log_path=log_path)
        if exit_code == 0:
            return BootOutcome(passed=True, summary="exited 0 within window", log_path=log_path)
        return BootOutcome(
            passed=False, summary=f"exited {exit_code} within {window}s", log_path=log_path
        )

    def _skip(self, reason: str, detail: str, start: float) -> StageResult:
        strict = getattr(self._settings, "smoke_strict", False)
        status = StageStatus.FAILED if strict else StageStatus.SKIPPED
        return StageResult(
            stage="smoke",
            status=status,
            skip_reason=reason if status == StageStatus.SKIPPED else None,
            detail=(detail if status == StageStatus.SKIPPED else f"smoke_strict=True and {detail}"),
            duration_seconds=time.monotonic() - start,
        )
