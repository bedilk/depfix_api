"""Unit tests for :mod:`depfix.verify.runner`.

``run_sandboxed`` is monkeypatched throughout so these tests never shell
out to a real npm/node -- they exercise framework detection, reporter/argv
construction, and how a sandboxed run's output gets threaded into
:func:`depfix.verify.parsers.parse_output`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from depfix.verify.manager import PackageManagerPreparation
from depfix.verify.models import TestFramework, TestStatus
from depfix.verify.runner import (
    _reporter_args,
    _test_argv,
    detect_framework,
    has_test_script,
    run_tests,
)
from depfix.verify.sandbox import SandboxResult


def _write_package_json(root: Path, data: dict) -> None:
    (root / "package.json").write_text(json.dumps(data), encoding="utf-8")


# -- has_test_script --------------------------------------------------------------


def test_has_test_script_true_for_real_script(tmp_path: Path) -> None:
    _write_package_json(tmp_path, {"scripts": {"test": "jest"}})
    assert has_test_script(tmp_path) is True


def test_has_test_script_false_when_no_scripts_key(tmp_path: Path) -> None:
    _write_package_json(tmp_path, {})
    assert has_test_script(tmp_path) is False


def test_has_test_script_false_for_npm_placeholder(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path, {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}
    )
    assert has_test_script(tmp_path) is False


def test_has_test_script_false_when_package_json_missing(tmp_path: Path) -> None:
    assert has_test_script(tmp_path) is False


# -- detect_framework --------------------------------------------------------------


def test_detect_framework_from_vitest_dependency(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path, {"devDependencies": {"vitest": "^1.0.0"}, "scripts": {"test": "vitest run"}}
    )
    assert detect_framework(tmp_path) == TestFramework.VITEST


def test_detect_framework_from_jest_dependency(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path, {"devDependencies": {"jest": "^29.0.0"}, "scripts": {"test": "jest"}}
    )
    assert detect_framework(tmp_path) == TestFramework.JEST


def test_detect_framework_from_mocha_dependency(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path, {"devDependencies": {"mocha": "^10.0.0"}, "scripts": {"test": "mocha"}}
    )
    assert detect_framework(tmp_path) == TestFramework.MOCHA


def test_detect_framework_from_node_test_flag_in_script(tmp_path: Path) -> None:
    _write_package_json(tmp_path, {"scripts": {"test": "node --test"}})
    assert detect_framework(tmp_path) == TestFramework.NODE_TEST


def test_detect_framework_from_node_colon_test_in_script(tmp_path: Path) -> None:
    _write_package_json(tmp_path, {"scripts": {"test": "node --test-reporter node:test"}})
    assert detect_framework(tmp_path) == TestFramework.NODE_TEST


def test_detect_framework_unknown_when_nothing_matches(tmp_path: Path) -> None:
    _write_package_json(tmp_path, {"scripts": {"test": "eslint ."}})
    assert detect_framework(tmp_path) == TestFramework.UNKNOWN


def test_detect_framework_prefers_declared_dependency_over_unrelated_script_text(
    tmp_path: Path,
) -> None:
    """A repo mid-migration can keep a script literally named after the old
    framework -- the *declared dependency* must still win."""
    _write_package_json(
        tmp_path, {"devDependencies": {"jest": "^29.0.0"}, "scripts": {"test": "run-mocha.sh"}}
    )
    assert detect_framework(tmp_path) == TestFramework.JEST


# -- _reporter_args / _test_argv ------------------------------------------------


def test_reporter_args_jest_uses_json_and_output_file(tmp_path: Path) -> None:
    out_path = tmp_path / "results.json"
    args = _reporter_args(TestFramework.JEST, out_path)
    assert args == ["--json", f"--outputFile={out_path}"]


def test_reporter_args_vitest_disables_watch_mode(tmp_path: Path) -> None:
    out_path = tmp_path / "results.json"
    args = _reporter_args(TestFramework.VITEST, out_path)
    assert "--watch=false" in args
    assert f"--outputFile={out_path}" in args


def test_reporter_args_mocha_uses_json_reporter(tmp_path: Path) -> None:
    args = _reporter_args(TestFramework.MOCHA, tmp_path / "results.json")
    assert args == ["--reporter", "json"]


def test_reporter_args_node_test_requests_tap(tmp_path: Path) -> None:
    assert _reporter_args(TestFramework.NODE_TEST, tmp_path / "results.json") == [
        "--test-reporter=tap"
    ]


def test_test_argv_shape(tmp_path: Path) -> None:
    argv = _test_argv("npm", TestFramework.MOCHA, tmp_path / "results.json")
    assert argv == ["npm", "test", "--", "--reporter", "json"]


# -- run_tests --------------------------------------------------------------------


def test_run_tests_reads_reporter_output_file_when_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, {"devDependencies": {"jest": "^29"}, "scripts": {"test": "jest"}})

    def fake_run_sandboxed(argv, *, cwd, timeout, extra_env=None, max_output_bytes=0):
        out_arg = next(a for a in argv if a.startswith("--outputFile="))
        out_path = Path(out_arg.split("=", 1)[1])
        out_path.write_text(
            json.dumps(
                {
                    "testResults": [
                        {
                            "name": "chat.test.js",
                            "assertionResults": [{"fullName": "does a thing", "status": "passed"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return SandboxResult(argv=tuple(argv), exit_code=0, stdout="", stderr="", duration_ms=42.0)

    monkeypatch.setattr("depfix.verify.runner.run_sandboxed", fake_run_sandboxed)

    result = run_tests(tmp_path, timeout=5.0, package_manager="npm")

    assert result.framework == TestFramework.JEST
    assert len(result.cases) == 1
    assert result.cases[0].status == TestStatus.PASSED
    assert result.used_fallback_parser is False
    assert result.duration_ms == 42.0


def test_run_tests_uses_the_isolated_runtime_prepared_by_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, {"scripts": {"test": "node --test"}})
    executable = tmp_path / ".depfix-tools" / "yarn"
    prepared = PackageManagerPreparation(
        package_manager="yarn", executable=executable, version="4.18.0", installed=True
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "depfix.verify.runner.prepare_package_manager", lambda *_args, **_kwargs: prepared
    )

    def fake_run_sandboxed(argv, *, cwd, timeout, extra_env=None, max_output_bytes=0):
        captured["argv"] = argv
        return SandboxResult(argv=tuple(argv), exit_code=0, stdout="", stderr="", duration_ms=1.0)

    monkeypatch.setattr("depfix.verify.runner.run_sandboxed", fake_run_sandboxed)

    run_tests(tmp_path, timeout=5.0, package_manager="yarn")

    assert captured["argv"][0] == str(executable)


def test_run_tests_falls_back_to_stdout_when_no_reporter_file_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, {"scripts": {"test": "node --test"}})
    tap_output = "ok 1 - first test\nnot ok 2 - second test\n"

    def fake_run_sandboxed(argv, *, cwd, timeout, extra_env=None, max_output_bytes=0):
        return SandboxResult(
            argv=tuple(argv), exit_code=1, stdout=tap_output, stderr="", duration_ms=10.0
        )

    monkeypatch.setattr("depfix.verify.runner.run_sandboxed", fake_run_sandboxed)

    result = run_tests(tmp_path, timeout=5.0, package_manager="npm")

    assert result.framework == TestFramework.NODE_TEST
    assert len(result.cases) == 2
    assert result.failed_cases[0].name == "second test"
    assert result.used_fallback_parser is False  # TAP is a structured parser


def test_run_tests_passes_ci_env_to_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, {"scripts": {"test": "node --test"}})
    captured: dict[str, object] = {}

    def fake_run_sandboxed(argv, *, cwd, timeout, extra_env=None, max_output_bytes=0):
        captured["extra_env"] = extra_env
        return SandboxResult(argv=tuple(argv), exit_code=0, stdout="", stderr="", duration_ms=1.0)

    monkeypatch.setattr("depfix.verify.runner.run_sandboxed", fake_run_sandboxed)

    run_tests(tmp_path, timeout=5.0, package_manager="npm")

    assert captured["extra_env"] == {"CI": "1"}


def test_run_tests_returns_timed_out_result_without_reading_reporter_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, {"scripts": {"test": "jest"}})

    def fake_run_sandboxed(argv, *, cwd, timeout, extra_env=None, max_output_bytes=0):
        return SandboxResult(
            argv=tuple(argv),
            exit_code=-1,
            stdout="partial output",
            stderr="stderr tail",
            duration_ms=5000.0,
            timed_out=True,
        )

    monkeypatch.setattr("depfix.verify.runner.run_sandboxed", fake_run_sandboxed)

    result = run_tests(tmp_path, timeout=5.0, package_manager="npm")

    assert result.timed_out is True
    assert result.cases == []
    assert "partial output" in result.raw_output_tail
    assert "stderr tail" in result.raw_output_tail


# -- duplicate-reporter guard ------------------------------------------------


def test_reporter_args_skipped_when_script_already_configures_one(tmp_path: Path) -> None:
    """A repo whose own test script pins ``--test-reporter=tap`` must not get
    the flag appended a second time -- Node 26 rejects the duplicate with
    ERR_INVALID_ARG_VALUE, turning a healthy suite into a crashed run."""
    import json

    from depfix.verify.runner import _script_already_configures_reporter

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test --test-reporter=tap"}}),
        encoding="utf-8",
    )
    assert _script_already_configures_reporter(tmp_path, TestFramework.NODE_TEST)

    argv = _test_argv(
        "npm", TestFramework.NODE_TEST, tmp_path / "out.json", skip_reporter_args=True
    )
    assert argv == ["npm", "test", "--"]


def test_reporter_args_kept_when_script_has_no_reporter(tmp_path: Path) -> None:
    import json

    from depfix.verify.runner import _script_already_configures_reporter

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8"
    )
    assert not _script_already_configures_reporter(tmp_path, TestFramework.NODE_TEST)
