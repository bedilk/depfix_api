"""Keep the local PR ledger aligned with GitHub before a fleet pass."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from depfix.gh.app import GitHubAppAuth, GitHubAppError
from depfix.gh.branch import BRANCH_PREFIX
from depfix.storage.schema import FixRunRow, PullRequestRow

logger = logging.getLogger(__name__)


def reconcile_open_prs(session: Session, gh_auth: GitHubAppAuth, repo_full_names: list[str]) -> int:
    """Persist closed/merged states for depfix PRs; return changed rows.

    Reconciliation is best-effort: an unavailable repository must not stop
    unrelated repositories from being processed in the same fleet pass.
    """
    drift = 0
    for repo_full_name in repo_full_names:
        owner, sep, name = repo_full_name.partition("/")
        if not sep or not name:
            continue
        try:
            live = gh_auth.list_open_pull_requests(owner, name)
            live_numbers = {pr.number for pr in live if pr.head_branch.startswith(BRANCH_PREFIX)}
            rows = session.scalars(
                select(PullRequestRow)
                .join(PullRequestRow.fix_run)
                .where(FixRunRow.repo_id == repo_full_name, PullRequestRow.state == "open")
            ).all()
            for row in rows:
                row.last_reconciled_at = datetime.now(UTC)
                if row.number in live_numbers:
                    continue
                state = gh_auth.get_pull_request_state(owner, name, row.number)
                row.merged = bool(state.get("merged"))
                row.state = "merged" if row.merged else str(state.get("state") or "closed")
                row.merged_at = _parse_dt(state.get("merged_at"))
                row.closed_at = _parse_dt(state.get("closed_at"))
                if row.merged:
                    _promote_merged(row.fix_run.dedupe_key)
                drift += 1
        except GitHubAppError as exc:
            logger.debug("PR reconciliation skipped for %s: %s", repo_full_name, exc)
        except Exception:
            logger.debug("PR reconciliation failed for %s", repo_full_name, exc_info=True)
    return drift


def compute_merge_rate(
    session: Session, repo_ids: list[str] | None = None
) -> dict[str, int | float | None]:
    """Return resolved PR merge statistics, optionally for selected repos."""
    stmt = select(PullRequestRow).join(PullRequestRow.fix_run).where(PullRequestRow.state != "open")
    if repo_ids:
        stmt = stmt.where(FixRunRow.repo_id.in_(repo_ids))
    rows = session.scalars(stmt).all()
    merged = sum(bool(row.merged) for row in rows)
    total = len(rows)
    return {
        "merged": merged,
        "closed_unmerged": total - merged,
        "total": total,
        "rate": merged / total if total else None,
    }


def _promote_merged(dedupe_key: str) -> None:
    """A merged PR is the strongest evidence a learned migration is real."""
    from depfix.catalog import learned_migrations_file, promote_learned_migration

    try:
        promote_learned_migration(learned_migrations_file(), dedupe_key, "merged_pr")
    except Exception:
        logger.debug("learned-catalog promotion on merge failed", exc_info=True)


def _parse_dt(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (TypeError, ValueError):
        return None
