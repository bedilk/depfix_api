"""Unit tests for :mod:`depfix.verify.manager` (package-manager detection
and dependency installation).

``run_sandboxed`` is monkeypatched throughout -- these tests are about
argv/detection logic, not about actually invoking npm/yarn/pnpm.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.verify.manager import (
    DEFAULT_PACKAGE_MANAGER,
    InstallResult,
    PackageManagerPreparation,
    _install_argv,
    _which,
    detect_package_manager,
    install_dependencies,
    prepare_package_manager,
)
from depfix.verify.sandbox import SandboxResult

# -- detect_package_manager -----------------------------------------------------


def test_detect_package_manager_defaults_to_npm_with_no_lockfile(tmp_path: Path) -> None:
    assert detect_package_manager(tmp_path) == DEFAULT_PACKAGE_MANAGER == "npm"


def test_detect_package_manager_detects_npm_from_package_lock(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert detect_package_manager(tmp_path) == "npm"


def test_detect_package_manager_detects_npm_from_shrinkwrap(tmp_path: Path) -> None:
    (tmp_path / "npm-shrinkwrap.json").write_text("{}", encoding="utf-8")
    assert detect_package_manager(tmp_path) == "npm"


def test_detect_package_manager_detects_yarn(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    assert detect_package_manager(tmp_path) == "yarn"


def test_detect_package_manager_detects_pnpm(tmp_path: Path) -> None:
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert detect_package_manager(tmp_path) == "pnpm"


def test_prepare_package_manager_installs_repository_pinned_yarn_in_isolated_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan can prepare Yarn Berry without requiring a global Yarn install."""
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@4.18.0"}', encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    cache_dir = tmp_path / "tools"

    def fake_which(executable: str) -> str | None:
        return "/usr/bin/npm" if executable == "npm" else None

    def fake_run_sandboxed(argv, *, cwd, timeout, max_output_bytes, **kwargs):
        assert argv[:4] == ["npm", "install", "--ignore-scripts", "--prefix"]
        prefix = Path(argv[4])
        executable = prefix / "node_modules" / ".bin" / "yarn"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        return SandboxResult(argv=tuple(argv), exit_code=0, stdout="", stderr="", duration_ms=1.0)

    monkeypatch.setattr("depfix.verify.manager._which", fake_which)
    monkeypatch.setattr("depfix.verify.manager.run_sandboxed", fake_run_sandboxed)

    result = prepare_package_manager(tmp_path, cache_dir=cache_dir, timeout=5.0)

    assert result == PackageManagerPreparation(
        package_manager="yarn",
        executable=cache_dir / "yarn" / "4.18.0" / "node_modules" / ".bin" / "yarn",
        version="4.18.0",
        installed=True,
    )


def test_prepare_package_manager_marks_an_existing_cached_runtime_as_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@4.18.0"}', encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    cache_dir = tmp_path / "tools"
    executable = cache_dir / "yarn" / "4.18.0" / "node_modules" / ".bin" / "yarn"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("depfix.verify.manager._which", lambda executable: None)

    result = prepare_package_manager(tmp_path, cache_dir=cache_dir, timeout=5.0)

    assert result.executable == executable
    assert result.installed is True


# -- _install_argv ---------------------------------------------------------------


def test_install_argv_npm_uses_ci_when_lockfile_present(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert _install_argv("npm", tmp_path, ignore_scripts=False) == ["npm", "ci"]


def test_install_argv_npm_uses_install_without_lockfile(tmp_path: Path) -> None:
    assert _install_argv("npm", tmp_path, ignore_scripts=False) == ["npm", "install"]


def test_install_argv_appends_ignore_scripts(tmp_path: Path) -> None:
    assert _install_argv("npm", tmp_path, ignore_scripts=True) == [
        "npm",
        "install",
        "--ignore-scripts",
    ]


def test_install_argv_yarn_adds_frozen_lockfile_only_with_lockfile(tmp_path: Path) -> None:
    assert _install_argv("yarn", tmp_path, ignore_scripts=False) == ["yarn", "install"]

    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    assert _install_argv("yarn", tmp_path, ignore_scripts=False) == [
        "yarn",
        "install",
        "--frozen-lockfile",
    ]


def test_install_argv_yarn_berry_uses_immutable_lockfile(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")

    assert _install_argv("yarn", tmp_path, ignore_scripts=True, version="4.18.0") == [
        "yarn",
        "install",
        "--immutable",
        "--mode=skip-build",
    ]


def test_install_argv_pnpm_adds_frozen_lockfile_only_with_lockfile(tmp_path: Path) -> None:
    assert _install_argv("pnpm", tmp_path, ignore_scripts=False) == ["pnpm", "install"]

    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert _install_argv("pnpm", tmp_path, ignore_scripts=False) == [
        "pnpm",
        "install",
        "--frozen-lockfile",
    ]


# -- install_dependencies ---------------------------------------------------------


def test_install_dependencies_skips_when_no_package_json(tmp_path: Path) -> None:
    result = install_dependencies(tmp_path, timeout=5.0)

    assert result.ok is False
    assert result.package_manager == "none"
    assert result.skipped_reason == "no package.json found"


def test_install_dependencies_skips_when_manager_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("depfix.verify.manager._which", lambda _exe: None)

    result = install_dependencies(tmp_path, timeout=5.0)

    assert result.ok is False
    assert "not found on PATH" in result.skipped_reason


def test_install_dependencies_runs_sandboxed_and_reports_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("depfix.verify.manager._which", lambda _exe: "/usr/bin/npm")

    captured: dict[str, object] = {}

    def fake_run_sandboxed(argv, *, cwd, timeout, max_output_bytes, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return SandboxResult(argv=tuple(argv), exit_code=0, stdout="", stderr="", duration_ms=1.0)

    monkeypatch.setattr("depfix.verify.manager.run_sandboxed", fake_run_sandboxed)

    result = install_dependencies(tmp_path, timeout=5.0)

    assert result.ok is True
    assert result.package_manager == "npm"
    assert captured["argv"] == ["npm", "install", "--ignore-scripts"]
    assert captured["cwd"] == tmp_path


def test_install_dependencies_not_ok_when_sandbox_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("depfix.verify.manager._which", lambda _exe: "/usr/bin/npm")

    def fake_run_sandboxed(argv, *, cwd, timeout, max_output_bytes, **kwargs):
        return SandboxResult(
            argv=tuple(argv), exit_code=1, stdout="", stderr="boom", duration_ms=1.0
        )

    monkeypatch.setattr("depfix.verify.manager.run_sandboxed", fake_run_sandboxed)

    result = install_dependencies(tmp_path, timeout=5.0)

    assert result.ok is False
    assert result.skipped_reason == ""  # not a *skip*, a real failed install


def test_install_dependencies_honors_explicit_package_manager_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")  # would detect npm
    monkeypatch.setattr("depfix.verify.manager._which", lambda _exe: "/usr/bin/yarn")

    captured: dict[str, object] = {}

    def fake_run_sandboxed(argv, *, cwd, timeout, max_output_bytes, **kwargs):
        captured["argv"] = argv
        return SandboxResult(argv=tuple(argv), exit_code=0, stdout="", stderr="", duration_ms=1.0)

    monkeypatch.setattr("depfix.verify.manager.run_sandboxed", fake_run_sandboxed)

    result = install_dependencies(tmp_path, timeout=5.0, package_manager="yarn")

    assert result.package_manager == "yarn"
    assert captured["argv"][0] == "yarn"


def test_which_finds_real_python_executable() -> None:
    # Sanity check that `_which` is a thin, real `shutil.which` wrapper.
    assert _which("this-binary-should-not-exist-anywhere") is None


def test_install_result_ok_is_false_when_skipped_reason_set_even_with_ok_sandbox() -> None:
    sandbox = SandboxResult(argv=("npm",), exit_code=0, stdout="", stderr="", duration_ms=0.0)
    result = InstallResult(package_manager="npm", sandbox=sandbox, skipped_reason="skip anyway")

    assert result.ok is False
