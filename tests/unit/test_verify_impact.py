"""Unit tests for :mod:`depfix.verify.impact` (plan-time impact checking).

``has_test_script``, ``install_dependencies``, ``run_tests`` and
``_regenerate_js_lockfile`` are monkeypatched throughout -- these tests are
about the impact check's control flow (which of the three honest outcomes
it settles on) and its revert-the-manifest-afterwards contract, not about
actually installing dependencies or running a real test suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from depfix.clone.service import Checkout
from depfix.core.models import BreakingChange, ChangeKind
from depfix.verify.impact import (
    ImpactReport,
    _python_requirement_bumps,
    run_impact_check,
)
from depfix.verify.manager import InstallResult
from depfix.verify.models import TestCase, TestFramework, TestRunResult, TestStatus
from depfix.verify.sandbox import SandboxResult

IMPACT = "depfix.verify.impact"


def _checkout(tmp_path: Path, *, is_temporary: bool = True) -> Checkout:
    return Checkout(path=tmp_path, is_temporary=is_temporary)


def _change(
    package: str = "openai", old_version: str = "3.0.0", new_version: str = "4.0.0"
) -> BreakingChange:
    return BreakingChange(
        package=package,
        old_version=old_version,
        new_version=new_version,
        old_api="openai.Completion.create",
        new_api="client.completions.create",
        description="the module-level client was replaced by an instance",
        migration_guide="instantiate OpenAI() and call it",
        kind=ChangeKind.MODULE_SHAPE_CHANGED,
    )


def _ok_install(package_manager: str = "npm") -> InstallResult:
    sandbox = SandboxResult(
        argv=(package_manager, "install"), exit_code=0, stdout="", stderr="", duration_ms=1.0
    )
    return InstallResult(package_manager=package_manager, sandbox=sandbox)


def _failed_install(package_manager: str = "npm") -> InstallResult:
    sandbox = SandboxResult(
        argv=(package_manager, "install"),
        exit_code=1,
        stdout="",
        stderr="ERESOLVE could not resolve",
        duration_ms=1.0,
    )
    return InstallResult(package_manager=package_manager, sandbox=sandbox)


def _run(cases: list[TestCase], **kwargs) -> TestRunResult:
    return TestRunResult(framework=TestFramework.NODE_TEST, cases=cases, **kwargs)


def _failed(name: str, file: str = "a.test.js") -> TestCase:
    return TestCase(name=name, file=file, status=TestStatus.FAILED)


def _write_package_json(root: Path, package: str, spec: str) -> str:
    """Write a manifest pinning ``package`` at ``spec`` and return its text."""
    content = (
        json.dumps(
            {"name": "demo", "scripts": {"test": "jest"}, "dependencies": {package: spec}}, indent=2
        )
        + "\n"
    )
    (root / "package.json").write_text(content, encoding="utf-8")
    return content


def _patch_runners(
    monkeypatch: pytest.MonkeyPatch,
    *,
    has_tests: bool = True,
    install=None,
    run=None,
) -> None:
    monkeypatch.setattr(f"{IMPACT}.has_test_script", lambda root, ecosystem="javascript": has_tests)
    monkeypatch.setattr(f"{IMPACT}._regenerate_js_lockfile", lambda *a, **kw: None)
    if install is not None:
        monkeypatch.setattr(f"{IMPACT}.install_dependencies", install)
    if run is not None:
        monkeypatch.setattr(f"{IMPACT}.run_tests", run)


def _check(checkout: Checkout, change: BreakingChange, **kwargs) -> ImpactReport:
    return run_impact_check(checkout, change, install_timeout=60.0, test_timeout=60.0, **kwargs)


# -- refusal / skip branches -------------------------------------------------------


def test_impact_check_refuses_a_non_disposable_checkout(tmp_path: Path) -> None:
    report = _check(_checkout(tmp_path, is_temporary=False), _change())

    assert report.ran is False
    assert report.skipped_reason == "checkout is not disposable"
    assert report.confirmed_breaking is False


def test_impact_check_skips_unsupported_ecosystem(tmp_path: Path) -> None:
    report = _check(_checkout(tmp_path), _change(), ecosystem="rust")

    assert report.ran is False
    assert report.skipped_reason == "no impact runner for rust"


def test_impact_check_skips_when_repo_has_no_test_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runners(monkeypatch, has_tests=False)

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is False
    assert report.skipped_reason == "repo has no test setup"


def test_impact_check_skips_when_no_manifest_pin_can_be_bumped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty checkout: build_manifest_bumps finds no package.json to rewrite.
    _patch_runners(monkeypatch)

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is False
    assert report.skipped_reason == "no manifest pin found to bump to the new version"


def test_impact_check_skips_when_baseline_install_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, "openai", "^3.0.0")
    _patch_runners(
        monkeypatch,
        install=lambda root, **kwargs: InstallResult(
            package_manager="npm", skipped_reason="npm not found on PATH"
        ),
        run=lambda root, **kwargs: _run([]),
    )

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is False
    assert report.skipped_reason == "baseline install failed (npm not found on PATH)"


def test_impact_check_skips_and_leaves_manifest_alone_when_baseline_run_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _write_package_json(tmp_path, "openai", "^3.0.0")
    _patch_runners(
        monkeypatch,
        install=lambda root, **kwargs: _ok_install(),
        run=lambda root, **kwargs: _run([], timed_out=True),
    )

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is False
    assert report.skipped_reason == "baseline test run crashed"
    # The bump never happened, so the manifest is untouched byte for byte.
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == original


# -- javascript: measured outcomes -------------------------------------------------


def test_impact_check_confirms_breaking_and_reverts_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _write_package_json(tmp_path, "openai", "^3.0.0")
    pre_existing = _failed("already broken")
    regression = _failed("newly broken")
    calls = {"n": 0}
    seen_manifest: list[str] = []

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        seen_manifest.append((Path(root) / "package.json").read_text(encoding="utf-8"))
        cases = [pre_existing] if calls["n"] == 1 else [pre_existing, regression]
        return _run(cases)

    _patch_runners(monkeypatch, install=lambda root, **kwargs: _ok_install(), run=fake_run_tests)

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is True
    assert report.baseline_failure_count == 1
    assert report.bumped_failure_count == 2
    assert report.new_failing_tests == (regression.identity,)
    assert report.confirmed_breaking is True
    assert report.degraded is False
    # The bumped run really did see the new pin...
    assert '"openai": "^4.0.0"' in seen_manifest[1]
    # ...and the manifest is restored exactly as found once the check is done.
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == original


def test_impact_check_reports_install_failure_after_bump_and_reverts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _write_package_json(tmp_path, "openai", "^3.0.0")
    calls = {"n": 0}

    def fake_install(root, **kwargs):
        calls["n"] += 1
        return _ok_install() if calls["n"] == 1 else _failed_install()

    _patch_runners(
        monkeypatch,
        install=fake_install,
        run=lambda root, **kwargs: _run([_failed("already broken")]),
    )

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is True
    assert report.install_failed_after_bump is True
    assert report.confirmed_breaking is True
    assert report.baseline_failure_count == 1
    assert report.new_failing_tests == ()
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == original


def test_impact_check_treats_a_crash_only_on_the_new_version_as_breaking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, "openai", "^3.0.0")
    calls = {"n": 0}

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _run([])
        return _run([], timed_out=True)

    _patch_runners(monkeypatch, install=lambda root, **kwargs: _ok_install(), run=fake_run_tests)

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is True
    assert report.new_failing_tests == ("<test run crashed on the new version>",)
    assert report.confirmed_breaking is True
    assert report.install_failed_after_bump is False


def test_impact_check_degraded_parser_with_more_failures_reports_aggregate_increase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, "openai", "^3.0.0")
    calls = {"n": 0}

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        cases = [_failed("<aggregate failed #0>", file="")]
        if calls["n"] == 2:
            cases.append(_failed("<aggregate failed #1>", file=""))
        return _run(cases, used_fallback_parser=True)

    _patch_runners(monkeypatch, install=lambda root, **kwargs: _ok_install(), run=fake_run_tests)

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is True
    assert report.degraded is True
    assert report.new_failing_tests == ("<aggregate failure count increased>",)
    assert report.confirmed_breaking is True
    assert report.baseline_failure_count == 1
    assert report.bumped_failure_count == 2


def test_impact_check_degraded_parser_without_increase_confirms_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, "openai", "^3.0.0")
    calls = {"n": 0}

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        # Only the *baseline* is degraded; that alone is enough to demote the
        # whole comparison to aggregate counts.
        return _run([], used_fallback_parser=calls["n"] == 1)

    _patch_runners(monkeypatch, install=lambda root, **kwargs: _ok_install(), run=fake_run_tests)

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is True
    assert report.degraded is True
    assert report.new_failing_tests == ()
    assert report.confirmed_breaking is False
    assert "weak evidence" in report.summary()


def test_impact_check_clean_pass_is_not_proof_of_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _write_package_json(tmp_path, "openai", "^3.0.0")
    _patch_runners(
        monkeypatch,
        install=lambda root, **kwargs: _ok_install(),
        run=lambda root, **kwargs: _run(
            [TestCase(name="still passes", file="a.test.js", status=TestStatus.PASSED)]
        ),
    )

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is True
    assert report.new_failing_tests == ()
    assert report.confirmed_breaking is False
    assert "NOT proof" in report.summary()
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == original


def test_impact_check_ignores_a_pre_existing_failure_that_also_fails_after_the_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package_json(tmp_path, "openai", "^3.0.0")
    same = _failed("flaky and already red")
    _patch_runners(
        monkeypatch,
        install=lambda root, **kwargs: _ok_install(),
        run=lambda root, **kwargs: _run([same]),
    )

    report = _check(_checkout(tmp_path), _change())

    assert report.ran is True
    assert report.baseline_failure_count == 1
    assert report.bumped_failure_count == 1
    assert report.new_failing_tests == ()
    assert report.confirmed_breaking is False


# -- python ecosystem --------------------------------------------------------------


def test_impact_check_bumps_and_reverts_a_python_requirements_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "stripe==10.0.0\nrequests==2.31.0\n"
    (tmp_path / "requirements.txt").write_text(original, encoding="utf-8")
    regression = _failed("test_charge", file="tests/test_billing.py")
    calls = {"n": 0}
    seen_requirements: list[str] = []

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        seen_requirements.append((Path(root) / "requirements.txt").read_text(encoding="utf-8"))
        return _run([] if calls["n"] == 1 else [regression])

    _patch_runners(
        monkeypatch, install=lambda root, **kwargs: _ok_install("pip"), run=fake_run_tests
    )

    report = _check(
        _checkout(tmp_path),
        _change(package="stripe", old_version="10.0.0", new_version="11.0.0"),
        ecosystem="python",
    )

    assert report.ran is True
    assert report.new_failing_tests == (regression.identity,)
    assert seen_requirements[0] == original
    assert seen_requirements[1] == "stripe==11.0.0\nrequests==2.31.0\n"
    assert (tmp_path / "requirements.txt").read_text(encoding="utf-8") == original


# -- _python_requirement_bumps -----------------------------------------------------


def test_python_requirement_bumps_rewrites_an_exact_pin(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("stripe==10.0.0\n", encoding="utf-8")

    bumps = _python_requirement_bumps(
        tmp_path, _change(package="stripe", old_version="10.0.0", new_version="11.0.0")
    )

    assert bumps == {"requirements.txt": "stripe==11.0.0\n"}


def test_python_requirement_bumps_preserves_extras_and_is_case_insensitive(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text("Stripe[async]>=10\n", encoding="utf-8")

    bumps = _python_requirement_bumps(
        tmp_path, _change(package="stripe", old_version="10.0.0", new_version="11.0.0")
    )

    # The name is spelled back exactly as the file had it, extras included.
    assert bumps == {"requirements.txt": "Stripe[async]==11.0.0\n"}


def test_python_requirement_bumps_leaves_a_file_without_the_package_alone(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")

    bumps = _python_requirement_bumps(
        tmp_path, _change(package="stripe", old_version="10.0.0", new_version="11.0.0")
    )

    assert bumps == {}


def test_python_requirement_bumps_also_covers_requirements_dev(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("stripe==10.0.0\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("stripe~=10.1\n", encoding="utf-8")

    bumps = _python_requirement_bumps(
        tmp_path, _change(package="stripe", old_version="10.0.0", new_version="11.0.0")
    )

    assert bumps == {
        "requirements.txt": "stripe==11.0.0\n",
        "requirements-dev.txt": "stripe==11.0.0\n",
    }


def test_python_requirement_bumps_strips_a_leading_v_from_the_new_version(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text("stripe==10.0.0\n", encoding="utf-8")

    bumps = _python_requirement_bumps(
        tmp_path, _change(package="stripe", old_version="10.0.0", new_version="v11.0.0")
    )

    assert bumps == {"requirements.txt": "stripe==11.0.0\n"}


def test_python_requirement_bumps_preserves_an_environment_marker(tmp_path: Path) -> None:
    """A PEP 508 environment marker scopes *whether* the dependency applies
    at all -- the version rewrite must carry it through verbatim."""
    (tmp_path / "requirements.txt").write_text(
        'stripe==10.0.0 ; python_version >= "3.9"\n', encoding="utf-8"
    )

    bumps = _python_requirement_bumps(
        tmp_path, _change(package="stripe", old_version="10.0.0", new_version="11.0.0")
    )

    assert bumps == {"requirements.txt": 'stripe==11.0.0 ; python_version >= "3.9"\n'}


# -- ImpactReport.summary ----------------------------------------------------------


def test_summary_reports_the_skip_reason_when_the_check_did_not_run() -> None:
    report = ImpactReport(ran=False, skipped_reason="repo has no test setup")

    assert report.summary() == "impact check skipped: repo has no test setup"


def test_summary_calls_out_a_failed_install_after_the_bump() -> None:
    report = ImpactReport(ran=True, install_failed_after_bump=True)

    assert "dependency install FAILED" in report.summary()
    assert "confirmed disruptive" in report.summary()


def test_summary_names_the_newly_failing_tests() -> None:
    report = ImpactReport(ran=True, new_failing_tests=("a.test.js::one", "a.test.js::two"))

    summary = report.summary()

    assert "2 test(s) newly fail" in summary
    assert "a.test.js::one, a.test.js::two" in summary
    assert "more)" not in summary


def test_summary_truncates_a_long_list_of_newly_failing_tests() -> None:
    report = ImpactReport(ran=True, new_failing_tests=tuple(f"t{i}" for i in range(7)))

    summary = report.summary()

    assert "7 test(s) newly fail" in summary
    assert "t0, t1, t2, t3, t4 (+2 more)" in summary
