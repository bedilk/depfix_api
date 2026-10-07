"""Integration test for :class:`depfix.core.pipeline.FixPipeline` wired up
to a real :class:`depfix.verify.verifier.Verifier`.

Unlike ``test_end_to_end.py`` (real Gemini API) this never calls an LLM: the
fixer is a small in-process fake that deterministically rewrites the one
call site in ``tests/fixtures/openai_v3_verifiable/src/chat.js``. What *is*
real here is everything downstream of fix generation -- ``WorkspaceEditor``
writing to disk, ``npm install``/``node --test`` actually running inside a
throwaway copy of the fixture, and ``Verifier`` diffing baseline vs
after-fix test identities to settle each edit's verdict. That's the
behavior this test exists to confirm; faking the fixer just keeps it fast,
offline, and independent of any provider's API key.

Skipped unless both ``node`` and ``npm`` are on PATH. Deliberately not
marked ``pytest.mark.integration`` -- that marker is reserved elsewhere in
this suite for tests that hit a real, paid LLM API; this one doesn't.
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
from depfix.validators.javascript import FixValidator
from depfix.verify.verifier import Verifier

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("npm") is None,
    reason="node/npm not on PATH -- required to actually run the fixture's test suite.",
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "openai_v3_verifiable"


def _checkout(tmp_path: Path) -> Path:
    dest = tmp_path / "checkout"
    shutil.copytree(FIXTURE, dest)
    return dest


def _scan_result(root: Path) -> RepoScanResult:
    """One HIGH-confidence call site for ``src/chat.js``'s
    ``openai.createModeration`` line -- built from the fixture's own
    content rather than a hardcoded line number, so this doesn't silently
    drift out of sync if the fixture file is ever reformatted."""
    relpath = "src/chat.js"
    lines = (root / relpath).read_text(encoding="utf-8").splitlines()
    line_number, line_content = next(
        (i, line) for i, line in enumerate(lines, start=1) if "createModeration" in line
    )
    site = CallSite(
        filepath=relpath,
        line_number=line_number,
        column=line_content.index("openai.createModeration"),
        line_content=line_content,
        kind=CallSiteKind.METHOD_CALL,
        confidence=MatchConfidence.HIGH,
        symbol="openai.createModeration",
        provider_id="openai",
    )
    return RepoScanResult(
        repo_full_name="acme/widgets", commit_sha="deadbeef", call_sites=[site], files_scanned=1
    )


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
    """Duck-typed stand-in for ``FixGenerator``/``OllamaFixGenerator`` --
    ``FixPipeline`` only ever calls ``generate_fix`` and reads
    ``total_cost``/``total_tokens``, never ``isinstance``-checks it."""

    def __init__(self, rewrite) -> None:
        self._rewrite = rewrite
        self.total_cost = 0.0
        self.total_tokens = 0

    def generate_fix(self, file_usage, breaking_change):
        return self._rewrite(file_usage.file_content), 0.95, None


def _correct_rewrite(content: str) -> str:
    """A real, working v3->v4 migration of the one call site."""
    return content.replace(
        "openai.createModeration({ input })", "openai.moderations.create({ input })"
    ).replace("response.data.results[0]", "response.results[0]")


def _broken_rewrite(content: str) -> str:
    """An invalid migration -- calls a method the shim doesn't implement,
    so the fixture's test suite fails after the fix is applied."""
    return content.replace(
        "openai.createModeration({ input })", "openai.moderations.createBogus({ input })"
    )


def _run_pipeline(tmp_path: Path, rewrite) -> tuple[Path, object]:
    root = _checkout(tmp_path)
    scan_result = _scan_result(root)
    editor = WorkspaceEditor(Checkout(path=root, is_temporary=False))
    verifier = Verifier(editor, install_timeout=60.0, test_timeout=60.0)
    pipeline = FixPipeline(_FakeFixer(rewrite), validator=FixValidator(), verifier=verifier)
    result = pipeline.run(scan_result, _breaking_change(), editor)
    return root, result


def test_correct_fix_is_kept_after_real_verification(tmp_path: Path) -> None:
    root, result = _run_pipeline(tmp_path, _correct_rewrite)

    assert result.files_affected == 1
    assert len(result.edits) == 1
    edit = result.edits[0]
    assert edit.relpath == "src/chat.js"
    assert edit.verdict == EditVerdict.KEPT

    assert result.verification is not None
    assert result.verification.ran is True
    assert result.verification.new_failure_count == 0

    on_disk = (root / "src" / "chat.js").read_text(encoding="utf-8")
    assert "openai.moderations.create({ input })" in on_disk
    assert result.total_usages_fixed == 1


def test_broken_fix_is_reverted_after_real_verification(tmp_path: Path) -> None:
    original = (FIXTURE / "src" / "chat.js").read_text(encoding="utf-8")
    root, result = _run_pipeline(tmp_path, _broken_rewrite)

    assert len(result.edits) == 1
    edit = result.edits[0]
    assert edit.verdict == EditVerdict.REVERTED

    assert result.verification is not None
    assert result.verification.ran is True
    assert result.verification.new_failure_count == 1

    on_disk = (root / "src" / "chat.js").read_text(encoding="utf-8")
    assert on_disk == original
    assert result.total_usages_fixed == 0
