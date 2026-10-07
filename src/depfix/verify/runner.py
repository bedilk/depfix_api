"""Detects a checkout's test framework and runs its test script under a
JSON-ish reporter, so :mod:`depfix.verify.parsers` has something structured
to read.

Framework detection is deliberately layered (declared deps first, script
text second) because the dependency that's actually installed is a more
reliable signal than what a maintainer happened to name the npm script --
plenty of repos keep a script named ``"test": "mocha"`` around after
migrating to vitest, or vice versa.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from depfix.verify.manager import detect_package_manager, prepare_package_manager
from depfix.verify.models import TestFramework, TestRunResult
from depfix.verify.parsers import parse_output
from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES, run_sandboxed

logger = logging.getLogger(__name__)

#: npm's placeholder for a package with no real test script -- running it
#: always exits non-zero and would otherwise look like "every test failed".
_NO_TEST_SPECIFIED = "no test specified"


def _read_package_json(root: Path) -> dict:
    try:
        return json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def has_test_script(root: str | Path, ecosystem: str = "javascript") -> bool:
    """True if ``root`` has a runnable test setup for its ecosystem --
    a real npm ``test`` script (not the "no test specified" placeholder),
    or a pytest configuration/convention for python, or RSpec/minitest for ruby."""
    if ecosystem == "python":
        from depfix.ecosystems.python_runtime import has_pytest_setup

        return has_pytest_setup(root)
    if ecosystem == "go":
        from depfix.ecosystems.go_runtime import has_test_setup

        return has_test_setup(root)
    if ecosystem == "ruby":
        from depfix.ecosystems.ruby_runtime import has_test_setup

        return has_test_setup(root)
    pkg = _read_package_json(Path(root))
    script = pkg.get("scripts", {}).get("test", "")
    return bool(script) and _NO_TEST_SPECIFIED not in script


def detect_framework(root: str | Path) -> TestFramework:
    """Detect the test framework from declared dependencies first, falling
    back to the ``test`` script's own text."""
    root = Path(root)
    pkg = _read_package_json(root)
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    script = pkg.get("scripts", {}).get("test", "")

    if "vitest" in deps or "vitest" in script:
        return TestFramework.VITEST
    if "jest" in deps or "jest" in script:
        return TestFramework.JEST
    if "mocha" in deps or "mocha" in script:
        return TestFramework.MOCHA
    if "--test" in script or "node:test" in script:
        return TestFramework.NODE_TEST
    if script.strip().startswith("bun test"):
        return TestFramework.NODE_TEST
    return TestFramework.UNKNOWN


def _reporter_args(framework: TestFramework, out_path: Path) -> list[str]:
    if framework == TestFramework.JEST:
        return ["--json", f"--outputFile={out_path}"]
    if framework == TestFramework.VITEST:
        return ["--watch=false", "--reporter=json", f"--outputFile={out_path}"]
    if framework == TestFramework.MOCHA:
        return ["--reporter", "json"]
    if framework == TestFramework.NODE_TEST:
        # Node's default reporter is version-dependent (recent Node releases
        # emit a human-readable spec format). Request TAP explicitly because
        # it gives the verifier stable per-test identities.
        return ["--test-reporter=tap"]
    return []


def _script_already_configures_reporter(root: Path, framework: TestFramework) -> bool:
    """True when the repo's own ``test`` script already pins the reporter we
    would append. Appending it again used to be harmless; Node 26 rejects a
    duplicated ``--test-reporter`` outright (``ERR_INVALID_ARG_VALUE``), which
    turned every ``node:test`` repo with an explicit reporter into a crashed,
    unverifiable run."""
    script = _read_package_json(root).get("scripts", {}).get("test", "")
    if framework == TestFramework.NODE_TEST:
        return "--test-reporter" in script
    if framework == TestFramework.JEST:
        return "--json" in script
    if framework == TestFramework.VITEST:
        return "--reporter" in script
    if framework == TestFramework.MOCHA:
        return "--reporter" in script
    return False


def _test_argv(
    package_manager: str,
    framework: TestFramework,
    out_path: Path,
    *,
    executable: str | None = None,
    skip_reporter_args: bool = False,
) -> list[str]:
    reporter = [] if skip_reporter_args else _reporter_args(framework, out_path)
    command = executable or package_manager
    if package_manager == "bun":
        return [command, "run", "test", "--", *reporter]
    if package_manager == "pnpm":
        return [command, "run", "test", "--", *reporter]
    return [command, "test", "--", *reporter]


def run_tests(
    root: str | Path,
    *,
    timeout: float,
    package_manager: str | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    test_cwd: str | Path | None = None,
    ecosystem: str = "javascript",
    coverage_dir: Path | None = None,
    contract_log_path: Path | None = None,
    contract_shim_path: Path | None = None,
    selective_test_files: list[Path] | None = None,
) -> TestRunResult:
    """Run ``root``'s ``test`` script under a JSON-ish reporter and parse
    the result. Never raises -- any failure to detect/run/parse is folded
    into the returned :class:`TestRunResult` (``parse_error``,
    ``timed_out``, ``used_fallback_parser``) so callers always get a value
    to compare against a baseline, rather than having to handle exceptions
    from a subprocess they don't control.

    ``ecosystem="python"`` runs pytest with a JUnit XML reporter instead of
    an npm test script; everything downstream (identity-diffing, fallback
    degradation) is framework-agnostic and unchanged.
    """
    root = Path(root)
    effective_cwd = Path(test_cwd) if test_cwd else root

    if ecosystem == "go":
        return _run_go_tests(effective_cwd, timeout=timeout, max_output_bytes=max_output_bytes)
    if ecosystem == "ruby":
        return _run_ruby_tests(effective_cwd, timeout=timeout, max_output_bytes=max_output_bytes)
    if ecosystem == "python":
        return _run_pytest(effective_cwd, timeout=timeout, max_output_bytes=max_output_bytes)

    manager = package_manager or detect_package_manager(effective_cwd)
    framework = detect_framework(effective_cwd)
    prepared = prepare_package_manager(
        effective_cwd, timeout=min(timeout, 90.0), package_manager=manager
    )
    if not prepared.ready:
        return TestRunResult(
            framework=framework,
            parse_error=prepared.detail,
            used_fallback_parser=True,
        )

    with tempfile.TemporaryDirectory(prefix="depfix-verify-") as tmp:
        out_path = Path(tmp) / "results.json"
        argv = _test_argv(
            manager,
            framework,
            out_path,
            executable=str(prepared.executable) if prepared.installed else None,
            skip_reporter_args=_script_already_configures_reporter(effective_cwd, framework),
        )
        logger.info("running tests: %s (framework=%s)", " ".join(argv), framework.value)

        # CI=1 stops Jest/Vitest/Mocha from defaulting to interactive watch
        # mode when stdin isn't a TTY, which would otherwise hang until the
        # timeout on every single run.
        extra_env = {"CI": "1"}
        if coverage_dir is not None:
            extra_env["NODE_V8_COVERAGE"] = str(coverage_dir)
        if contract_shim_path is not None:
            # Prepend our shim to NODE_OPTIONS so it loads before the test suite
            existing = extra_env.get("NODE_OPTIONS", "")
            extra_env["NODE_OPTIONS"] = f"--require {contract_shim_path} {existing}".strip()

        result = run_sandboxed(
            argv,
            cwd=effective_cwd,
            timeout=timeout,
            extra_env=extra_env,
            max_output_bytes=max_output_bytes,
        )

        if result.timed_out:
            return TestRunResult(
                framework=framework,
                command=result.argv,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                timed_out=True,
                raw_output_tail=result.stdout[-2000:] + result.stderr[-2000:],
            )

        raw = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
        stdout = raw if raw else result.stdout
        cases, parse_error, used_fallback = parse_output(framework, stdout, result.stderr)

    return TestRunResult(
        framework=framework,
        command=result.argv,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        cases=cases,
        parse_error=parse_error,
        used_fallback_parser=used_fallback,
        raw_output_tail=(result.stdout[-2000:] + result.stderr[-2000:]),
    )


def _run_pytest(cwd: Path, *, timeout: float, max_output_bytes: int) -> TestRunResult:
    """Run pytest with ``--junit-xml`` and parse the report.

    The venv interpreter is preferred when the checkout's ``.depfix-venv``
    exists (i.e. after :func:`depfix.verify.manager.install_dependencies`
    ran for python), so tests see the dependencies that were installed for
    them rather than whatever the operator's system python happens to have.
    """
    from depfix.ecosystems.python_runtime import test_argv
    from depfix.ecosystems.specs import PYTHON

    with tempfile.TemporaryDirectory(prefix="depfix-verify-") as tmp:
        out_path = Path(tmp) / "results.xml"
        argv = test_argv(cwd, out_path)
        if argv is None:
            return TestRunResult(
                framework=TestFramework.PYTEST,
                parse_error="python3 not found on PATH",
                used_fallback_parser=True,
            )

        result = run_sandboxed(
            argv,
            cwd=cwd,
            timeout=timeout,
            extra_env={"CI": "1"},
            max_output_bytes=max_output_bytes,
            extra_allowlist=PYTHON.env_allowlist_extra,
        )

        if result.timed_out:
            return TestRunResult(
                framework=TestFramework.PYTEST,
                command=result.argv,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                timed_out=True,
                raw_output_tail=result.stdout[-2000:] + result.stderr[-2000:],
            )

        raw = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
        report_input = raw if raw else result.stdout
        cases, parse_error, used_fallback = parse_output(
            TestFramework.PYTEST, report_input, result.stderr
        )

    return TestRunResult(
        framework=TestFramework.PYTEST,
        command=result.argv,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        cases=cases,
        parse_error=parse_error,
        used_fallback_parser=used_fallback,
        raw_output_tail=(result.stdout[-2000:] + result.stderr[-2000:]),
    )


def _run_go_tests(cwd: Path, *, timeout: float, max_output_bytes: int) -> TestRunResult:
    """Run ``go test -json -count=1 ./...`` and parse its event stream.

    ``-count=1`` is not optional: Go's test cache would otherwise serve the
    after-fix run from the baseline run's results, producing a pass for code
    that was never executed.
    """
    from depfix.ecosystems.go_runtime import test_argv
    from depfix.ecosystems.specs import GO

    argv = test_argv(cwd)
    if argv is None:
        return TestRunResult(
            framework=TestFramework.GO_TEST,
            parse_error="go not on PATH, or no go.mod in this checkout",
            used_fallback_parser=True,
        )

    result = run_sandboxed(
        argv,
        cwd=cwd,
        timeout=timeout,
        extra_env={"CI": "1"},
        max_output_bytes=max_output_bytes,
        extra_allowlist=GO.env_allowlist_extra,
    )
    if result.timed_out:
        return TestRunResult(
            framework=TestFramework.GO_TEST,
            command=result.argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=True,
            raw_output_tail=result.stdout[-2000:] + result.stderr[-2000:],
        )

    cases, parse_error, used_fallback = parse_output(
        TestFramework.GO_TEST, result.stdout, result.stderr
    )
    return TestRunResult(
        framework=TestFramework.GO_TEST,
        command=result.argv,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        cases=cases,
        parse_error=parse_error,
        used_fallback_parser=used_fallback,
        raw_output_tail=(result.stdout[-2000:] + result.stderr[-2000:]),
    )


def _run_ruby_tests(cwd: Path, *, timeout: float, max_output_bytes: int) -> TestRunResult:
    """Run RSpec or minitest via bundler and parse the output.

    RSpec emits JSON to stdout when ``--format json`` is passed. Minitest
    is asked for JUnit XML via minitest-reporters env vars when available;
    the plain ``rake test`` fallback still yields aggregate counts the
    fallback parser can read.
    """
    from depfix.ecosystems.ruby_runtime import minitest_junit_env, test_argv
    from depfix.ecosystems.specs import RUBY

    with tempfile.TemporaryDirectory(prefix="depfix-verify-") as tmp:
        junit = Path(tmp) / "results.xml"
        argv = test_argv(cwd, junit)
        if argv is None:
            return TestRunResult(
                framework=TestFramework.RSPEC,
                parse_error="no bundler or no rspec/minitest setup",
                used_fallback_parser=True,
            )
        is_rspec = "rspec" in argv
        extra_env = {"CI": "1"}
        if not is_rspec:
            extra_env.update(minitest_junit_env(junit))
        result = run_sandboxed(
            argv,
            cwd=cwd,
            timeout=timeout,
            extra_env=extra_env,
            max_output_bytes=max_output_bytes,
            extra_allowlist=RUBY.env_allowlist_extra,
        )
        if result.timed_out:
            return TestRunResult(
                framework=TestFramework.RSPEC,
                command=result.argv,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                timed_out=True,
                raw_output_tail=result.stdout[-2000:] + result.stderr[-2000:],
            )
        # RSpec JSON is on stdout; minitest-reporters JUnit is in the file.
        framework = TestFramework.RSPEC if is_rspec else TestFramework.MINITEST
        report_input = result.stdout
        if not is_rspec and junit.is_file():
            report_input = junit.read_text(encoding="utf-8")
        cases, parse_error, used_fallback = parse_output(framework, report_input, result.stderr)
    return TestRunResult(
        framework=framework,
        command=result.argv,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        cases=cases,
        parse_error=parse_error,
        used_fallback_parser=used_fallback,
        raw_output_tail=(result.stdout[-2000:] + result.stderr[-2000:]),
    )
