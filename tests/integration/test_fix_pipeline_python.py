"""Integration test for the Python arm of :class:`FixPipeline` + ``Verifier``.

The exact counterpart of ``test_fix_pipeline.py``: the fixer is a fake that
deterministically rewrites the one call site in
``tests/fixtures/python_v1_verifiable/src/app.py``; everything downstream is
real -- ``WorkspaceEditor`` writes, ``install_dependencies`` creates a
throwaway ``.depfix-venv`` and pip-installs pytest into it, ``run_tests``
runs the fixture's actual pytest suite with a JUnit reporter, and the
Verifier diffs baseline vs after-fix test identities.

Needs python3 on PATH and network access for one small pip install of
pytest into the fixture's venv (the npm analogue already hits the registry
the same way).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from depfix.apply.models import EditVerdict
from depfix.apply.workspace import WorkspaceEditor
from depfix.clone.service import Checkout
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.core.pipeline import FixPipeline
from depfix.scanners.models import CallSite, CallSiteKind, MatchConfidence, RepoScanResult
from depfix.validators.python import PythonFixValidator
from depfix.verify.verifier import Verifier

pytestmark = pytest.mark.skipif(
    shutil.which("python3") is None and shutil.which("python") is None,
    reason="python3 not on PATH -- required to run the fixture's pytest suite.",
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "python_v1_verifiable"


def _checkout(tmp_path: Path) -> Path:
    dest = tmp_path / "checkout"
    shutil.copytree(FIXTURE, dest)
    return dest


def _scan_result(root: Path) -> RepoScanResult:
    relpath = "src/app.py"
    lines = (root / relpath).read_text(encoding="utf-8").splitlines()
    line_number, line_content = next(
        (i, line) for i, line in enumerate(lines, start=1) if "create_moderation" in line
    )
    site = CallSite(
        filepath=relpath,
        line_number=line_number,
        column=line_content.index("client.create_moderation"),
        line_content=line_content,
        kind=CallSiteKind.METHOD_CALL,
        confidence=MatchConfidence.HIGH,
        symbol="client.create_moderation",
        provider_id="openai",
    )
    return RepoScanResult(
        repo_full_name="acme/pywidgets", commit_sha="deadbeef", call_sites=[site], files_scanned=1
    )


def _breaking_change() -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="0.x",
        new_version="1.x",
        old_api="client.create_moderation",
        new_api="client.moderations.create",
        description="moderation moved under a namespace; result shape flattened",
        migration_guide="use client.moderations.create(text); read result['flagged'] directly",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
    )


class _FakeFixer:
    def __init__(self, rewrite) -> None:
        self._rewrite = rewrite
        self.total_cost = 0.0
        self.total_tokens = 0

    def generate_fix(self, file_usage, breaking_change):
        return self._rewrite(file_usage.file_content), 0.95, None


def _correct_rewrite(content: str) -> str:
    return content.replace(
        'response = client.create_moderation(text)\n    return response["results"][0]["flagged"]',
        'response = client.moderations.create(text)\n    return response["flagged"]',
    )


def _broken_rewrite(content: str) -> str:
    return content.replace(
        "client.create_moderation(text)", "client.moderations.create_bogus(text)"
    )


def _run_pipeline(tmp_path: Path, rewrite) -> tuple[Path, object]:
    root = _checkout(tmp_path)
    scan_result = _scan_result(root)
    editor = WorkspaceEditor(Checkout(path=root, is_temporary=False))
    verifier = Verifier(editor, install_timeout=180.0, test_timeout=120.0, ecosystem="python")
    pipeline = FixPipeline(_FakeFixer(rewrite), validator=PythonFixValidator(), verifier=verifier)
    result = pipeline.run(scan_result, _breaking_change(), editor)
    return root, result


def test_correct_python_fix_is_kept_after_real_verification(tmp_path: Path) -> None:
    root, result = _run_pipeline(tmp_path, _correct_rewrite)

    assert result.files_affected == 1
    assert len(result.edits) == 1
    edit = result.edits[0]
    assert edit.relpath == "src/app.py"
    assert edit.verdict == EditVerdict.KEPT

    assert result.verification is not None
    assert result.verification.ran is True
    assert result.verification.new_failure_count == 0

    on_disk = (root / "src" / "app.py").read_text(encoding="utf-8")
    assert "client.moderations.create(text)" in on_disk
    assert result.total_usages_fixed == 1


def test_broken_python_fix_is_reverted_after_real_verification(tmp_path: Path) -> None:
    original = (FIXTURE / "src" / "app.py").read_text(encoding="utf-8")
    root, result = _run_pipeline(tmp_path, _broken_rewrite)

    assert len(result.edits) == 1
    edit = result.edits[0]
    assert edit.verdict == EditVerdict.REVERTED

    assert result.verification is not None
    assert result.verification.ran is True
    assert result.verification.new_failure_count >= 1

    on_disk = (root / "src" / "app.py").read_text(encoding="utf-8")
    assert on_disk == original
    assert result.total_usages_fixed == 0
