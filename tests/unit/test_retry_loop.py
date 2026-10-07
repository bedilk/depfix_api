"""Unit tests for :mod:`depfix.retry.loop`.

Mirrors ``tests/unit/test_verifier.py``'s style: ``has_test_script``,
``install_dependencies``, and ``run_tests`` are monkeypatched so this
doesn't need a real node/npm install to run -- what's under test here is
``RetryLoop``'s control flow (finding retryable edits, feeding concrete
feedback back to the fixer, re-verifying) against the real
``WorkspaceEditor``/``Verifier``/``FixValidator``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.apply.models import EditVerdict
from depfix.apply.workspace import WorkspaceEditor
from depfix.clone.service import Checkout
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource, FileUsage
from depfix.retry.loop import RetryLoop
from depfix.validators.javascript import FixValidator
from depfix.verify.manager import InstallResult
from depfix.verify.models import TestCase, TestFramework, TestRunResult, TestStatus
from depfix.verify.sandbox import SandboxResult
from depfix.verify.verifier import Verifier

_RELPATH = "src/chat.js"
_ORIGINAL = "const openai = require('openai');\nopenai.createModeration({ input });\n"
_BROKEN = "const openai = require('openai');\nopenai.moderations.createBogus({ input });\n"
_CORRECT = "const openai = require('openai');\nopenai.moderations.create({ input });\n"


def _editor(tmp_path: Path) -> WorkspaceEditor:
    return WorkspaceEditor(Checkout(path=tmp_path, is_temporary=False))


def _ok_install(package_manager: str = "npm") -> InstallResult:
    sandbox = SandboxResult(
        argv=(package_manager, "install"), exit_code=0, stdout="", stderr="", duration_ms=1.0
    )
    return InstallResult(package_manager=package_manager, sandbox=sandbox)


def _run(framework: TestFramework, cases: list[TestCase], **kwargs) -> TestRunResult:
    return TestRunResult(framework=framework, cases=cases, **kwargs)


def _breaking_change() -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="3.x",
        new_version="4.x",
        old_api="openai.createModeration",
        new_api="openai.moderations.create",
        description="moderations moved under a namespace",
        migration_guide="use openai.moderations.create",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
    )


class _FakeFixer:
    """First attempt (no feedback) returns a broken rewrite; any retry
    (feedback set) returns the correct one -- deterministically exercises
    ``RetryLoop``'s feedback-driven re-attempt without a real LLM."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def generate_fix(self, file_usage, breaking_change, *, feedback=None):
        self.calls.append(feedback)
        code = _CORRECT if feedback else _BROKEN
        return code, 0.9, None


def test_retry_loop_fixes_a_reverted_edit_on_second_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / _RELPATH).write_text(_ORIGINAL, encoding="utf-8")

    monkeypatch.setattr(
        "depfix.verify.verifier.has_test_script", lambda root, ecosystem="javascript": True
    )
    monkeypatch.setattr(
        "depfix.verify.verifier.install_dependencies", lambda root, **kwargs: _ok_install()
    )

    failing_case = TestCase(name="moderation works", file=_RELPATH, status=TestStatus.FAILED)
    calls = {"n": 0}

    def fake_run_tests(root, **kwargs):
        calls["n"] += 1
        # Odd calls are baselines (clean), even calls are after-fix.
        # Calls 2 and 3 (after-fix + flake retry) fail because the broken
        # code is still on disk; calls 5+ (retry round) succeed because
        # RetryLoop rewrites with the correct code.
        is_after_fix = calls["n"] in (2, 3)
        cases = [failing_case] if is_after_fix else []
        return _run(TestFramework.NODE_TEST, cases)

    monkeypatch.setattr("depfix.verify.verifier.run_tests", fake_run_tests)

    editor = _editor(tmp_path)
    validator = FixValidator()
    verifier = Verifier(editor)
    fixer = _FakeFixer()
    retry_loop = RetryLoop(fixer, validator, verifier, max_rounds=2)

    file_usage = FileUsage(filepath=_RELPATH, usages=[], file_content=_ORIGINAL)
    breaking_change = _breaking_change()

    fixed_code, confidence, _llm_call = fixer.generate_fix(file_usage, breaking_change)
    validation = validator.validate(_ORIGINAL, fixed_code, _RELPATH)
    edit = editor.write_fix(
        _RELPATH, fixed_code, confidence=confidence, usages_fixed=1, validation=validation
    )
    verification = verifier.verify([edit])
    assert verification.edits[0].verdict == EditVerdict.REVERTED

    final_edits, final_verification, rounds_used = retry_loop.run(
        list(verification.edits),
        verification,
        {_RELPATH: file_usage},
        breaking_change,
        editor,
    )

    assert rounds_used == 1
    assert len(final_edits) == 1
    assert final_edits[0].verdict == EditVerdict.KEPT
    assert final_verification.new_failure_count == 0
    assert (tmp_path / _RELPATH).read_text(encoding="utf-8") == _CORRECT
    assert fixer.calls[0] is None
    assert fixer.calls[1] and "moderation works" in fixer.calls[1]


def test_retry_loop_leaves_unretryable_edits_alone(tmp_path: Path) -> None:
    """A ``SUSPECT`` edit with no concrete error message (e.g. verification
    was inconclusive) has nothing useful to feed the LLM -- ``RetryLoop``
    must not call the fixer for it, and ``rounds_used`` stays 0."""
    from depfix.apply.models import FileEdit
    from depfix.verify.verifier import VerificationReport

    editor = _editor(tmp_path)
    fixer = _FakeFixer()
    validator = FixValidator()
    verifier = Verifier(editor)
    retry_loop = RetryLoop(fixer, validator, verifier, max_rounds=2)

    edit = FileEdit(
        relpath=_RELPATH,
        original_content=_ORIGINAL,
        fixed_content=_BROKEN,
        diff="",
        verdict=EditVerdict.SUSPECT,
    )
    verification = VerificationReport(ran=True)

    final_edits, _final_verification, rounds_used = retry_loop.run(
        [edit], verification, {}, _breaking_change(), editor
    )

    assert rounds_used == 0
    assert final_edits == [edit]
    assert fixer.calls == []
