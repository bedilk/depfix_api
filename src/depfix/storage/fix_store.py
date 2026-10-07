"""Persistence for Week 4 fix runs.

Follows the same convention as :mod:`depfix.scanners.store`: plain
functions that take an already-open :class:`~sqlalchemy.orm.Session` and
do their work inline -- callers wrap with ``depfix.storage.session_scope()``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from depfix.core.pipeline import FixPipelineResult
from depfix.gh.models import PullRequest
from depfix.obs.cost import CostLedger
from depfix.storage.schema import FileFixRow, FixRunRow, PullRequestRow, TestRunRow
from depfix.verify.models import TestRunResult, TestStatus


def record_fix_run(
    session: Session,
    *,
    repo_full_name: str,
    owner: str,
    name: str,
    result: FixPipelineResult,
    repo_scan_id: int | None = None,
    installation_id: int | None = None,
    default_branch: str | None = None,
    escalated: bool = False,
    cost_ledger: CostLedger | None = None,
) -> FixRunRow:
    """Persist one :class:`FixPipelineResult`, creating the parent ``repo``
    row if this is the first time it's been touched by any pipeline (scan
    or fix).

    Every edit is stored, not just the ones that ended up on disk -- a
    SKIPPED/REVERTED edit is exactly the "why didn't this get fixed"
    information a human reviewing a fix run needs.
    """
    # Deferred: storage/__init__ -> fix_store -> scanners.store ->
    # storage.schema -> storage/__init__ is a real import cycle at module
    # load time. Importing here (call time, not import time) breaks it.
    from depfix.scanners.store import upsert_repo

    repo = upsert_repo(
        session,
        full_name=repo_full_name,
        owner=owner,
        name=name,
        installation_id=installation_id,
        default_branch=default_branch,
    )

    change = result.breaking_change
    run = FixRunRow(
        repo_id=repo.id,
        repo_scan_id=repo_scan_id,
        dedupe_key=change.dedupe_key,
        package=change.package,
        old_api=change.old_api,
        new_api=change.new_api,
        kind=change.kind.value,
        files_scanned=result.files_scanned,
        files_affected=result.files_affected,
        total_usages_fixed=result.total_usages_fixed,
        total_cost=result.total_cost,
        cost_classify=cost_ledger.by_stage.get("classify", 0.0) if cost_ledger else 0.0,
        cost_characterization=cost_ledger.by_stage.get("characterization", 0.0)
        if cost_ledger
        else 0.0,
        cost_call_site_judge=cost_ledger.by_stage.get("call_site_judge", 0.0)
        if cost_ledger
        else 0.0,
        total_tokens=result.total_tokens,
        duration_ms=result.duration_ms,
        verified=result.verification is not None and result.verification.ran,
        verification_skipped_reason=(
            result.verification.skipped_reason if result.verification is not None else ""
        ),
        confidence_tier=result.confidence.value,
        typechecked=result.verification is not None and result.verification.typechecked,
        escalated=escalated,
    )
    run.file_fixes = [
        FileFixRow(
            relpath=edit.relpath,
            verdict=edit.verdict.value,
            confidence=edit.confidence,
            usages_fixed=edit.usages_fixed,
            diff=edit.diff,
            error_message=edit.error_message,
            origin=edit.origin.value,
            codemod_id=edit.codemod_id,
        )
        for edit in result.edits
    ]
    if result.verification is not None:
        if result.verification.baseline is not None:
            run.test_runs.append(_test_run_row("baseline", result.verification.baseline))
        if result.verification.after_fix is not None:
            run.test_runs.append(_test_run_row("after_fix", result.verification.after_fix))

    repo.fix_runs.append(run)
    return run


def _test_run_row(phase: str, test_run: TestRunResult) -> TestRunRow:
    passed = sum(1 for c in test_run.cases if c.status == TestStatus.PASSED)
    skipped = sum(1 for c in test_run.cases if c.status == TestStatus.SKIPPED)
    return TestRunRow(
        phase=phase,
        framework=test_run.framework.value,
        exit_code=test_run.exit_code,
        duration_ms=test_run.duration_ms,
        passed_count=passed,
        failed_count=len(test_run.failed_cases),
        skipped_count=skipped,
        failed_identities=sorted(test_run.failed_identities),
        used_fallback_parser=test_run.used_fallback_parser,
        timed_out=test_run.timed_out,
        parse_error=test_run.parse_error,
    )


def record_pull_request(session: Session, fix_run: FixRunRow, pr: PullRequest) -> PullRequestRow:
    """Persist the PR opened (or reused) for ``fix_run``. At most one per
    fix run -- see ``PullRequestRow``'s unique FK."""
    row = PullRequestRow(
        fix_run_id=fix_run.id,
        number=pr.number,
        html_url=pr.html_url,
        head_branch=pr.head_branch,
        base_branch=pr.base_branch,
        already_existed=pr.already_existed,
    )
    session.add(row)
    fix_run.pull_request = row
    return row


def latest_fix_run(session: Session, repo_full_name: str) -> FixRunRow | None:
    """The most recent fix run for ``repo_full_name``, or ``None`` if it has
    never had one."""
    stmt = (
        select(FixRunRow)
        .where(FixRunRow.repo_id == repo_full_name)
        .order_by(FixRunRow.started_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()
