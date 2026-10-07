"""Unit tests for Python lockfile refresh selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.ecosystems import python_lockfile
from depfix.verify.sandbox import SandboxResult


def test_no_supported_lockfile_reports_reason(tmp_path: Path) -> None:
    ok, reason = python_lockfile.try_refresh_python_lockfile(tmp_path)
    assert not ok
    assert "no supported Python lockfile" in reason


def test_missing_tool_and_successful_relock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "poetry.lock").write_text("")
    monkeypatch.setattr(python_lockfile.shutil, "which", lambda _: None)
    ok, reason = python_lockfile.try_refresh_python_lockfile(tmp_path)
    assert not ok
    assert "poetry not on PATH" in reason

    monkeypatch.setattr(python_lockfile.shutil, "which", lambda _: "/usr/bin/poetry")
    monkeypatch.setattr(
        python_lockfile,
        "run_sandboxed",
        lambda argv, **kw: SandboxResult(tuple(argv), 0, "", "", 1.0),
    )
    ok, message = python_lockfile.try_refresh_python_lockfile(tmp_path)
    assert ok
    assert "poetry.lock refreshed" in message


def test_successful_pipenv_relock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "Pipfile").write_text("[packages]\nopenai = '*'\n")
    (tmp_path / "Pipfile.lock").write_text("{}")
    captured: list[tuple[str, ...]] = []
    monkeypatch.setattr(python_lockfile.shutil, "which", lambda _: "/usr/bin/pipenv")

    def run(argv, **kwargs):
        captured.append(tuple(argv))
        return SandboxResult(tuple(argv), 0, "", "", 1.0)

    monkeypatch.setattr(python_lockfile, "run_sandboxed", run)
    ok, message = python_lockfile.try_refresh_python_lockfile(tmp_path)

    assert ok
    assert "Pipfile.lock refreshed" in message
    assert captured == [("pipenv", "lock")]


def test_pip_tools_project_is_deferred_not_recompiled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "requirements.in").write_text("openai\n")
    (tmp_path / "requirements.txt").write_text("openai==1.4.3\n")
    monkeypatch.setattr(python_lockfile.shutil, "which", lambda _: "/usr/bin/pip-compile")

    def must_not_run(*args, **kwargs):
        raise AssertionError("pip-compile must not run for a pip-tools project")

    monkeypatch.setattr(python_lockfile, "run_sandboxed", must_not_run)
    ok, reason = python_lockfile.try_refresh_python_lockfile(tmp_path)

    assert not ok
    assert "pip-tools project detected" in reason
    assert "requirements.in" in reason
    assert (tmp_path / "requirements.txt").read_text() == "openai==1.4.3\n"


def test_bare_requirements_txt_is_not_a_pip_tools_lock(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("openai==1.0.0\n")

    ok, reason = python_lockfile.try_refresh_python_lockfile(tmp_path)

    assert not ok
    assert "no supported Python lockfile" in reason
    assert python_lockfile.python_lockfile_paths(tmp_path) == []


def test_pip_tools_requirements_txt_is_never_listed_as_a_lock(tmp_path: Path) -> None:
    (tmp_path / "requirements.in").write_text("openai\n")
    (tmp_path / "requirements.txt").write_text("openai==1.0.0\n")

    assert python_lockfile.python_lockfile_paths(tmp_path) == []


def test_uses_pip_tools_detects_requirements_in(tmp_path: Path) -> None:
    assert python_lockfile.uses_pip_tools(tmp_path) is False
    (tmp_path / "requirements.in").write_text("openai\n")
    assert python_lockfile.uses_pip_tools(tmp_path) is True
