"""Unit tests for the ``ecosystem="python"`` branches of the verify
subsystem -- :func:`depfix.verify.runner.has_test_script`,
:func:`depfix.verify.manager.install_dependencies`,
:func:`depfix.verify.runner.run_tests`, and
:func:`depfix.verify.smoke.run_smoke_check`.

``run_sandboxed`` is monkeypatched in the install/test tests (they are
about argv construction and result threading, not about really creating a
venv or running pytest). The smoke test is the one exception: it shells out
to a real ``python3 -m py_compile``, which is local, cheap, and the actual
oracle under test.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from depfix.verify.manager import install_dependencies
from depfix.verify.models import TestFramework, TestStatus
from depfix.verify.runner import has_test_script, run_tests
from depfix.verify.sandbox import SandboxResult
from depfix.verify.smoke import run_smoke_check


def _sandbox(
    argv: tuple[str, ...] = ("cmd",),
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> SandboxResult:
    return SandboxResult(
        argv=argv,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1.0,
        timed_out=timed_out,
    )


def _recording_sandbox(calls: list[tuple[str, ...]], **result_kwargs):
    def fake_run_sandboxed(argv, **kwargs):
        calls.append(tuple(argv))
        return _sandbox(tuple(argv), **result_kwargs)

    return fake_run_sandboxed


def _make_fake_venv(root: Path) -> Path:
    """Make ``.depfix-venv`` look already-created to ``python_runtime``."""
    venv_python = root / ".depfix-venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    return venv_python


# -- has_test_script(ecosystem="python") --------------------------------------------


def test_has_test_script_true_for_python_with_pytest_ini(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    assert has_test_script(tmp_path, "python") is True


def test_has_test_script_false_for_python_without_any_marker(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    assert has_test_script(tmp_path, "python") is False


def test_has_test_script_false_for_pyproject_that_never_mentions_pytest(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")

    assert has_test_script(tmp_path, "python") is False


def test_has_test_script_true_for_a_conventional_tests_directory(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_billing.py").write_text("def test_x(): pass\n", encoding="utf-8")

    assert has_test_script(tmp_path, "python") is True


# -- install_dependencies(ecosystem="python") ---------------------------------------


def test_python_install_skips_when_there_is_no_manifest(tmp_path: Path) -> None:
    result = install_dependencies(tmp_path, timeout=60.0, ecosystem="python")

    assert result.ok is False
    assert "requirements.txt" in result.skipped_reason


def test_python_install_creates_the_venv_then_pip_installs_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "requirements.txt").write_text("stripe==10.0.0\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr("depfix.verify.manager.run_sandboxed", _recording_sandbox(calls))

    result = install_dependencies(tmp_path, timeout=60.0, ecosystem="python")

    assert result.ok is True
    assert result.package_manager == "pip"
    assert len(calls) == 2
    assert calls[0][1:] == ("-m", "venv", ".depfix-venv")
    assert calls[1][1:] == ("-m", "pip", "install", "--quiet", "-r", "requirements.txt")


def test_python_install_reuses_an_existing_venv_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "requirements.txt").write_text("stripe==10.0.0\n", encoding="utf-8")
    venv_python = _make_fake_venv(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr("depfix.verify.manager.run_sandboxed", _recording_sandbox(calls))

    result = install_dependencies(tmp_path, timeout=60.0, ecosystem="python")

    assert result.ok is True
    assert len(calls) == 1  # no venv creation -- it already exists
    assert calls[0][0] == str(venv_python)
    assert "-r" in calls[0]


def test_python_install_installs_the_project_when_only_pyproject_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    _make_fake_venv(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr("depfix.verify.manager.run_sandboxed", _recording_sandbox(calls))

    result = install_dependencies(tmp_path, timeout=60.0, ecosystem="python")

    assert result.ok is True
    assert calls[0][1:] == ("-m", "pip", "install", "--quiet", ".")


def test_python_install_reports_a_failed_venv_creation_without_running_pip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "requirements.txt").write_text("stripe==10.0.0\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "depfix.verify.manager.run_sandboxed",
        _recording_sandbox(calls, exit_code=1, stderr="no ensurepip"),
    )

    result = install_dependencies(tmp_path, timeout=60.0, ecosystem="python")

    assert result.ok is False
    assert len(calls) == 1
    assert result.sandbox is not None and result.sandbox.exit_code == 1


def test_python_install_reports_missing_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "requirements.txt").write_text("stripe==10.0.0\n", encoding="utf-8")
    monkeypatch.setattr("depfix.ecosystems.python_runtime.system_python", lambda: None)
    monkeypatch.setattr("depfix.verify.manager.run_sandboxed", _recording_sandbox([]))

    result = install_dependencies(tmp_path, timeout=60.0, ecosystem="python")

    assert result.ok is False
    assert result.skipped_reason == "python3 not found on PATH"


# -- run_tests(ecosystem="python") --------------------------------------------------


def test_python_run_tests_invokes_pytest_with_a_junit_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "depfix.verify.runner.run_sandboxed",
        _recording_sandbox(calls, stdout="==== 3 passed in 0.10s ===="),
    )

    result = run_tests(tmp_path, timeout=60.0, ecosystem="python")

    assert result.framework == TestFramework.PYTEST
    assert calls[0][1:4] == ("-m", "pytest", "--quiet")
    assert any(arg.startswith("--junit-xml=") for arg in calls[0])
    assert "--continue-on-collection-errors" in calls[0]


def test_python_run_tests_falls_back_to_aggregate_counts_without_a_junit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sandbox stub writes no report file, so the runner parses stdout --
    # which is not XML, so the JUnit parser raises and the aggregate fallback
    # takes over. Identities are synthetic and the run is flagged degraded.
    monkeypatch.setattr(
        "depfix.verify.runner.run_sandboxed",
        _recording_sandbox([], exit_code=1, stdout="==== 1 failed, 2 passed in 0.10s ===="),
    )

    result = run_tests(tmp_path, timeout=60.0, ecosystem="python")

    assert result.framework == TestFramework.PYTEST
    assert result.used_fallback_parser is True
    assert result.parse_error
    assert len(result.failed_cases) == 1
    assert sum(1 for c in result.cases if c.status == TestStatus.PASSED) == 2
    assert result.crashed is False  # cases were recovered, so this is comparable


def test_python_run_tests_reports_a_timeout_as_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "depfix.verify.runner.run_sandboxed",
        _recording_sandbox([], exit_code=-9, stdout="hung", timed_out=True),
    )

    result = run_tests(tmp_path, timeout=60.0, ecosystem="python")

    assert result.framework == TestFramework.PYTEST
    assert result.timed_out is True
    assert result.crashed is True
    assert result.cases == []


def test_python_run_tests_reports_a_missing_interpreter_without_shelling_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr("depfix.ecosystems.python_runtime.system_python", lambda: None)
    monkeypatch.setattr("depfix.verify.runner.run_sandboxed", _recording_sandbox(calls))

    result = run_tests(tmp_path, timeout=60.0, ecosystem="python")

    assert calls == []
    assert result.framework == TestFramework.PYTEST
    assert result.parse_error == "python3 not found on PATH"
    assert result.used_fallback_parser is True


# -- run_smoke_check on .py ---------------------------------------------------------


def test_python_smoke_check_passes_for_a_parseable_module(tmp_path: Path) -> None:
    if shutil.which("python3") is None and shutil.which("python") is None:
        pytest.skip("no python interpreter on PATH")
    (tmp_path / "mod.py").write_text(
        "VALUE = 1\n\n\ndef f():\n    return VALUE\n", encoding="utf-8"
    )

    result = run_smoke_check(tmp_path, "mod.py", timeout=60)

    assert result.ran is True
    assert result.passed is True


def test_python_smoke_check_fails_for_an_unparseable_module(tmp_path: Path) -> None:
    if shutil.which("python3") is None and shutil.which("python") is None:
        pytest.skip("no python interpreter on PATH")
    (tmp_path / "mod.py").write_text("def f(:\n", encoding="utf-8")

    result = run_smoke_check(tmp_path, "mod.py", timeout=60)

    assert result.ran is True
    assert result.passed is False
    assert result.error


def test_python_smoke_check_skips_a_missing_file(tmp_path: Path) -> None:
    result = run_smoke_check(tmp_path, "nope.py", timeout=60)

    assert result.ran is False
    assert result.skipped_reason == "nope.py not found"


def test_smoke_check_skips_unsupported_suffixes(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("x = 1\n", encoding="utf-8")

    result = run_smoke_check(tmp_path, "config.toml", timeout=60)

    assert result.ran is False
    assert "smoke check supports" in result.skipped_reason
