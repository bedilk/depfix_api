"""Unit tests for :mod:`depfix.verify.verifier`.

``has_test_script``, ``install_dependencies``, ``run_tests``, and
``attribute_failures`` are monkeypatched throughout -- these tests are
about the Verifier's control flow and verdict-settling logic, not about
actually installing/running a real JS test suite (see
``tests/integration/test_fix_pipeline.py`` for that).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.apply.models import EditVerdict, FileEdit
from depfix.apply.workspace import WorkspaceEditor
from depfix.clone.service import Checkout
from depfix.verify.attribution import AttributionResult
from depfix.verify.manager import InstallResult
from depfix.verify.models import TestCase, TestFramework, TestRunResult, TestStatus
from depfix.verify.sandbox import SandboxResult
from depfix.verify.verifier import VerificationReport, Verifier


def _editor(tmp_path: Path) -> WorkspaceEditor:
    return WorkspaceEditor(Checkout(path=tmp_path, is_temporary=False))


def _edit(
    relpath: str, *, verdict: EditVerdict = EditVerdict.APPLIED, fixed: str = "new\n"
) -> FileEdit:
    return FileEdit(
        relpath=relpath, original_content="old\n", fixed_content=fixed, diff="", verdict=verdict
    )


def _apply(
    editor: WorkspaceEditor, relpath: str, *, original: str = "old\n", fixed: str = "new\n"
) -> FileEdit:
    (editor.checkout.path / relpath).write_text(original, encoding="utf-8")
    return editor.write_fix(relpath, fixed)


def _ok_install(package_manager: str = "npm") -> InstallResult:
    sandbox = SandboxResult(
        argv=(package_manager, "install"), exit_code=0, stdout="", stderr="", duration_ms=1.0
    )
    return InstallResult(package_manager=package_manager, sandbox=sandbox)


def _run(framework: TestFramework, cases: list[TestCase], **kwargs) -> TestRunResult:
    return TestRunResult(framework=framework, cases=cases, **kwargs)


# -- verify(): early-exit branches -----------------------------------------------


def test_verify_skips_when_no_edits_are_on_disk(tmp_path: Path) -> None:
    editor = _editor(tmp_path)
    verifier = Verifier(editor)
    edits = [_edit("a.js", verdict=EditVerdict.SKIPPED)]

    report = verifier.verify(edits)

    assert report.ran is False
    assert report.skipped_reason == "no on-disk edits to verify"
    assert report.edits == tuple(edits)


def test_verify_marks_suspect_when_no_test_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": False
    )
    editor = _editor(tmp_path)
    verifier = Verifier(editor)
    edits = [_edit("a.js")]

    report = verifier.verify(edits)

    assert report.ran is False
    assert report.skipped_reason == "repo has no test script"
    assert report.edits[0].verdict == EditVerdict.SUSPECT


def test_verify_marks_suspect_when_install_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": True
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.install_dependencies",
        lambda root, **kwargs: InstallResult(
            package_manager="npm", skipped_reason="npm not found on PATH"
        ),
    )
    editor = _editor(tmp_path)
    verifier = Verifier(editor)
    edits = [_edit("a.js")]

    report = verifier.verify(edits)

    assert report.ran is False
    assert report.skipped_reason == "npm not found on PATH"
    assert report.install is not None
    assert report.edits[0].verdict == EditVerdict.SUSPECT


def test_verify_does_not_touch_edits_not_on_disk_in_early_exit_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": False
    )
    editor = _editor(tmp_path)
    verifier = Verifier(editor)
    on_disk = _edit("a.js")
    skipped = _edit("b.js", verdict=EditVerdict.SKIPPED)

    report = verifier.verify([on_disk, skipped])

    assert report.edits[0].verdict == EditVerdict.SUSPECT
    assert report.edits[1].verdict == EditVerdict.SKIPPED


# -- verify(): baseline/after-fix run outcomes ------------------------------------


def test_verify_marks_suspect_when_a_run_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Content must actually `require()` cleanly -- otherwise the smoke-check
    # fallback (a real, deliberate oracle; see depfix.verify.smoke) would
    # correctly REVERT this edit for failing to load, which is a different,
    # more confident outcome than the "nothing could decide" SUSPECT this
    # test means to exercise.
    (tmp_path / "a.js").write_text("module.exports = 1;\n", encoding="utf-8")
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": True
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.install_dependencies", lambda root, **kwargs: _ok_install()
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.run_tests",
        lambda root, **kwargs: _run(TestFramework.NODE_TEST, [], timed_out=True),
    )
    editor = _editor(tmp_path)
    verifier = Verifier(editor)
    edits = [_edit("a.js", fixed="module.exports = 2;\n")]

    report = verifier.verify(edits)

    assert report.ran is True
    assert report.skipped_reason == "baseline or after-fix run crashed"
    assert report.edits[0].verdict == EditVerdict.SUSPECT


def test_verify_marks_suspect_when_either_run_used_fallback_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # See the loadability note in test_verify_marks_suspect_when_a_run_crashed.
    (tmp_path / "a.js").write_text("module.exports = 1;\n", encoding="utf-8")
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": True
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.install_dependencies", lambda root, **kwargs: _ok_install()
    )

    calls = {"n": 0}

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        used_fallback = calls["n"] == 1  # baseline degraded, after-fix clean
        return _run(TestFramework.NODE_TEST, [], used_fallback_parser=used_fallback)

    monkeypatch.setattr("depfix.verify.verifier.run_tests", fake_run_tests)
    editor = _editor(tmp_path)
    verifier = Verifier(editor)
    edits = [_edit("a.js", fixed="module.exports = 2;\n")]

    report = verifier.verify(edits)

    assert report.ran is True
    assert report.skipped_reason == ""
    assert report.edits[0].verdict == EditVerdict.SUSPECT


def test_verify_keeps_edit_when_no_new_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = _editor(tmp_path)
    edit = _apply(editor, "a.js")
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": True
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.install_dependencies", lambda root, **kwargs: _ok_install()
    )

    same_case = TestCase(name="still passes", file="a.test.js", status=TestStatus.PASSED)
    monkeypatch.setattr(
        "depfix.verify.verifier.run_tests",
        lambda root, **kwargs: _run(TestFramework.NODE_TEST, [same_case]),
    )
    verifier = Verifier(editor)

    report = verifier.verify([edit])

    assert report.ran is True
    assert report.edits[0].verdict == EditVerdict.KEPT
    assert (tmp_path / "a.js").read_text(encoding="utf-8") == "new\n"


def test_verify_no_new_failures_branch_also_settles_edits_not_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real quirk of the current implementation: the "no new failures"
    branch calls ``_settle`` unconditionally over *all* edits, not just
    the on-disk ones -- unlike the crash/no-test-script/install-failure
    branches, which use ``_mark_suspect`` and leave off-disk edits alone."""
    editor = _editor(tmp_path)
    on_disk_edit = _apply(editor, "a.js")
    never_written = _edit("b.js", verdict=EditVerdict.SKIPPED)
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": True
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.install_dependencies", lambda root, **kwargs: _ok_install()
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.run_tests",
        lambda root, **kwargs: _run(TestFramework.NODE_TEST, []),
    )
    verifier = Verifier(editor)

    report = verifier.verify([on_disk_edit, never_written])

    assert report.edits[0].verdict == EditVerdict.KEPT
    assert report.edits[1].verdict == EditVerdict.KEPT


# -- verify(): attribution branch --------------------------------------------------


def test_verify_reverts_attributed_edit_and_keeps_unrelated_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = _editor(tmp_path)
    a_edit = _apply(editor, "a.js", original="a-old\n", fixed="a-new\n")
    b_edit = _apply(editor, "b.js", original="b-old\n", fixed="b-new\n")
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": True
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.install_dependencies", lambda root, **kwargs: _ok_install()
    )

    failing_case = TestCase(name="broken", file="a.test.js", status=TestStatus.FAILED)
    calls = {"n": 0}

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        cases = [] if calls["n"] == 1 else [failing_case]
        return _run(TestFramework.NODE_TEST, cases)

    monkeypatch.setattr("depfix.verify.verifier.run_tests", fake_run_tests)
    monkeypatch.setattr(
        "depfix.verify.verifier.attribute_failures",
        lambda new_failures, **kwargs: AttributionResult(by_file={"a.js": new_failures}),
    )
    verifier = Verifier(editor)

    report = verifier.verify([a_edit, b_edit])

    settled = {e.relpath: e for e in report.edits}
    assert settled["a.js"].verdict == EditVerdict.REVERTED
    assert settled["b.js"].verdict == EditVerdict.KEPT
    assert (tmp_path / "a.js").read_text(encoding="utf-8") == "a-old\n"


def test_verify_marks_unattributed_ambiguity_as_suspect_for_all_on_disk_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = _editor(tmp_path)
    a_edit = _apply(editor, "a.js")
    b_edit = _apply(editor, "b.js")
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": True
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.install_dependencies", lambda root, **kwargs: _ok_install()
    )

    failing_case = TestCase(
        name="mystery failure", file="unknown.test.js", status=TestStatus.FAILED
    )
    calls = {"n": 0}

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        cases = [] if calls["n"] == 1 else [failing_case]
        return _run(TestFramework.NODE_TEST, cases)

    monkeypatch.setattr("depfix.verify.verifier.run_tests", fake_run_tests)
    monkeypatch.setattr(
        "depfix.verify.verifier.attribute_failures",
        lambda new_failures, **kwargs: AttributionResult(unattributed=new_failures),
    )
    verifier = Verifier(editor)

    report = verifier.verify([a_edit, b_edit])

    assert all(e.verdict == EditVerdict.SUSPECT for e in report.edits)


def test_verify_attribution_branch_passes_through_edits_not_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = _editor(tmp_path)
    a_edit = _apply(editor, "a.js")
    never_written = _edit("b.js", verdict=EditVerdict.SKIPPED)
    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": True
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.install_dependencies", lambda root, **kwargs: _ok_install()
    )

    failing_case = TestCase(name="broken", file="a.test.js", status=TestStatus.FAILED)
    calls = {"n": 0}

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        cases = [] if calls["n"] == 1 else [failing_case]
        return _run(TestFramework.NODE_TEST, cases)

    monkeypatch.setattr("depfix.verify.verifier.run_tests", fake_run_tests)
    monkeypatch.setattr(
        "depfix.verify.verifier.attribute_failures",
        lambda new_failures, **kwargs: AttributionResult(by_file={"a.js": new_failures}),
    )
    verifier = Verifier(editor)

    report = verifier.verify([a_edit, never_written])

    settled = {e.relpath: e for e in report.edits}
    assert settled["a.js"].verdict == EditVerdict.REVERTED
    assert settled["b.js"].verdict == EditVerdict.SKIPPED


# -- VerificationReport.new_failure_count -----------------------------------------


def test_new_failure_count_is_zero_when_baseline_or_after_fix_missing() -> None:
    report = VerificationReport(ran=False)
    assert report.new_failure_count == 0


def test_new_failure_count_counts_identity_diff() -> None:
    baseline = _run(TestFramework.NODE_TEST, [TestCase(name="a", status=TestStatus.FAILED)])
    after_fix = _run(
        TestFramework.NODE_TEST,
        [
            TestCase(name="a", status=TestStatus.FAILED),
            TestCase(name="b", status=TestStatus.FAILED),
        ],
    )
    report = VerificationReport(ran=True, baseline=baseline, after_fix=after_fix)

    assert report.new_failure_count == 1
