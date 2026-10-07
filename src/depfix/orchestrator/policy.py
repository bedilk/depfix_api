"""Pure policy for the Week 6 fleet orchestrator: decides whether to act on
one ``(repo, breaking change)`` pair, and why not when it shouldn't.

Deliberately side-effect-free -- no DB session, no HTTP client -- every
branch here is covered by ``tests/unit/test_orchestrator_policy.py`` without
touching a database or GitHub. :mod:`depfix.orchestrator.runner` is the
thin, mockable shell that gathers the inputs this module needs and acts on
its verdict.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from depfix.config import Settings
from depfix.core.models import BreakingChange
from depfix.repoconfig.models import RepoConfig
from depfix.storage.schema import AttemptStatus, ChangeAttemptRow


class SkipReason(enum.StrEnum):
    """Why the orchestrator declined to act on a repo, or on one
    ``(repo, change)`` pair. Ordered the same as the checks in
    :func:`decide` / :meth:`~depfix.orchestrator.runner.Orchestrator._run_repo`.
    """

    # -- whole-repo, resolved before any per-change check runs -------------
    NO_INSTALLATION = "no_installation"
    NO_REPO_CONFIG = "no_repo_config"
    INVALID_REPO_CONFIG = "invalid_repo_config"

    # -- tenant instructions: this repo's own .depfix.yml, never bypassable
    #    by --force ---------------------------------------------------------
    TENANT_IGNORED = "tenant_ignored"
    PIPELINE_DISABLED = "pipeline_disabled"
    PROVIDER_NOT_ALLOWED = "provider_not_allowed"
    TRIAGE_DISMISSED = "triage_dismissed"
    TRIAGE_SNOOZED = "triage_snoozed"

    # -- capability: can depfix act on this at all --------------------------
    UNKNOWN_PROVIDER = "unknown_provider"
    NO_REPLACEMENT_API = "no_replacement_api"
    REMOVAL_ALREADY_REPORTED = "removal_already_reported"

    # -- supply-chain safety -----------------------------------------------
    VERSION_TOO_NEW = "version_too_new"

    # -- scan reuse policy: plan/apply require a prior depfix scan ---------
    NO_REUSABLE_SCAN = "no_reusable_scan"

    # -- bookkeeping: --force exists specifically to override these --------
    ALREADY_TERMINAL = "already_terminal"
    MAX_ATTEMPTS_REACHED = "max_attempts_reached"
    COOLDOWN_ACTIVE = "cooldown_active"
    NO_CALL_SITES_UNCHANGED = "no_call_sites_unchanged"
    VERSION_UNCHANGED = "version_unchanged"
    MAX_OPEN_PRS_REACHED = "max_open_prs_reached"
    MAX_CHANGES_PER_RUN_REACHED = "max_changes_per_run_reached"


@dataclass(frozen=True)
class SkipDetail:
    reason: SkipReason
    message: str = ""
    resumes_at: str = ""

    def one_line(self) -> str:
        parts = [self.reason.value]
        if self.message:
            parts.append(self.message)
        if self.resumes_at:
            parts.append(f"resumes at {self.resumes_at}")
        return " — ".join(parts)


_REF_GATED: dict[str, SkipReason] = {
    AttemptStatus.NO_CALL_SITES.value: SkipReason.NO_CALL_SITES_UNCHANGED,
    AttemptStatus.VERSION_NOT_AFFECTED.value: SkipReason.VERSION_UNCHANGED,
    AttemptStatus.REPORT_ONLY.value: SkipReason.REMOVAL_ALREADY_REPORTED,
}


def effective_max_changes_per_run(repo_config: RepoConfig, settings: Settings) -> int:
    """A repo's own ``max_changes_per_run`` can only lower the fleet-wide
    ``pipeline_max_changes_per_repo`` cap, never raise it -- see
    :class:`depfix.config.Settings`'s docstring for that field."""
    if repo_config.max_changes_per_run is None:
        return settings.pipeline_max_changes_per_repo
    return min(repo_config.max_changes_per_run, settings.pipeline_max_changes_per_repo)


def decide(
    *,
    repo_config: RepoConfig,
    change: BreakingChange,
    known_provider_ids: frozenset[str],
    attempt: ChangeAttemptRow | None,
    current_ref_sha: str,
    open_pr_count: int,
    changes_processed_this_run: int,
    settings: Settings,
    force: bool = False,
    target_version_age_hours: float | None = None,
) -> SkipReason | None:
    """``None`` means "go ahead and process this change"; otherwise the
    reason not to.

    Check order is deliberate: tenant instructions (this repo's own
    ``.depfix.yml``) are unconditional and never bypassable by ``force``;
    then a capability check (can depfix even act on this provider at all);
    then bookkeeping (the idempotency ledger, rate limits) which ``force``
    exists specifically to override.
    """
    # -- tenant instructions: never bypassable ------------------------------
    if repo_config.is_ignored(provider_id=change.provider_id, dedupe_key=change.dedupe_key):
        return SkipReason.TENANT_IGNORED
    if not repo_config.enabled:
        return SkipReason.PIPELINE_DISABLED
    if not repo_config.providers.allows(change.provider_id):
        return SkipReason.PROVIDER_NOT_ALLOWED

    from depfix.classify.triage import TriageAction, evaluate_triage

    verdict = evaluate_triage(change, repo_config.triage_rules)
    if verdict is not None:
        if verdict.action is TriageAction.DISMISS:
            return SkipReason.TRIAGE_DISMISSED
        if verdict.action is TriageAction.SNOOZE:
            return SkipReason.TRIAGE_SNOOZED

    # -- capability: can we act on this at all ------------------------------
    from depfix.core.models import ChangeKind, is_method_removal

    if change.provider_id not in known_provider_ids:
        return SkipReason.UNKNOWN_PROVIDER
    # A security advisory with no published fix has an empty new_api by
    # design. It is still the most important thing an operator can hear, so
    # it proceeds to a report-only outcome rather than being filtered out
    # here as "nothing to migrate to".
    # METHOD_REMOVED changes are also exempt: they continue so the orchestrator
    # can REPORT the still-reachable call sites. They are never fixed and never
    # produce a PR (see Orchestrator._report_removal / run_fix_for_change).
    if (
        change.kind not in (ChangeKind.SECURITY_ADVISORY, ChangeKind.METHOD_DEPRECATED)
        and not is_method_removal(change)
        and not (change.new_api or "").strip()
    ):
        return SkipReason.NO_REPLACEMENT_API

    # -- bookkeeping: force-bypassable ---------------------------------------
    if not force:
        if (
            change.kind is not ChangeKind.SECURITY_ADVISORY
            and settings.version_cooldown_hours > 0
            and target_version_age_hours is not None
            and target_version_age_hours < settings.version_cooldown_hours
        ):
            return SkipReason.VERSION_TOO_NEW

        if attempt is not None and AttemptStatus(attempt.status).is_terminal:
            return SkipReason.ALREADY_TERMINAL
        if attempt is not None and attempt.attempts_used >= settings.pipeline_max_attempts:
            return SkipReason.MAX_ATTEMPTS_REACHED
        gated = _REF_GATED.get(attempt.status) if attempt is not None else None
        if (
            gated is not None
            and current_ref_sha
            and attempt.last_checked_ref_sha == current_ref_sha  # type: ignore[union-attr]
        ):
            return gated
        if attempt is not None and _cooldown_active(attempt, settings):
            return SkipReason.COOLDOWN_ACTIVE
        if open_pr_count >= settings.pipeline_max_open_prs:
            return SkipReason.MAX_OPEN_PRS_REACHED
        if changes_processed_this_run >= effective_max_changes_per_run(repo_config, settings):
            return SkipReason.MAX_CHANGES_PER_RUN_REACHED

    return None


def decide_with_detail(**kwargs: object) -> SkipDetail | None:
    """Return the policy decision plus a concise operator-actionable reason."""
    reason = decide(**kwargs)  # type: ignore[arg-type]
    if reason is None:
        return None
    attempt = kwargs.get("attempt")
    settings = kwargs["settings"]
    message = ""
    resumes_at = ""
    if reason is SkipReason.COOLDOWN_ACTIVE and attempt is not None:
        updated_at = attempt.updated_at  # type: ignore[attr-defined]
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        expires = updated_at + timedelta(hours=settings.pipeline_retry_cooldown_hours)  # type: ignore[attr-defined]
        resumes_at = expires.isoformat(timespec="seconds")
        message = f"{max(0, (expires - datetime.now(UTC)).total_seconds() / 3600):.1f}h left"
    elif reason is SkipReason.ALREADY_TERMINAL and attempt is not None:
        message = f"ledger status is {attempt.status}; use --force to retry"  # type: ignore[attr-defined]
    elif reason is SkipReason.MAX_ATTEMPTS_REACHED and attempt is not None:
        message = (
            f"{attempt.attempts_used}/{settings.pipeline_max_attempts} attempts used; use --force"  # type: ignore[attr-defined]
        )
    elif reason is SkipReason.MAX_OPEN_PRS_REACHED:
        message = "merge or close an existing depfix PR, or use --force"
    elif reason is SkipReason.NO_CALL_SITES_UNCHANGED:
        message = "will recheck when HEAD changes"
    elif reason is SkipReason.MAX_CHANGES_PER_RUN_REACHED:
        message = "deferred to the next fleet pass"
    elif reason is SkipReason.TENANT_IGNORED:
        message = "excluded by .depfix.yml"
    elif reason is SkipReason.PIPELINE_DISABLED:
        message = "enabled: false in .depfix.yml"
    elif reason is SkipReason.PROVIDER_NOT_ALLOWED:
        message = "provider excluded by .depfix.yml"
    elif reason is SkipReason.TRIAGE_DISMISSED:
        message = "dismissed by a triage rule in .depfix.yml"
    elif reason is SkipReason.TRIAGE_SNOOZED:
        message = "snoozed by a triage rule in .depfix.yml"
    elif reason is SkipReason.VERSION_TOO_NEW:
        message = (
            f"target version is newer than the {settings.version_cooldown_hours:.0f}h cooldown; "  # type: ignore[attr-defined]
            "will proceed once it ages, or use --force"
        )
    elif reason is SkipReason.UNKNOWN_PROVIDER:
        message = "provider is not configured"
    elif reason is SkipReason.NO_REPLACEMENT_API:
        message = "the API was removed with no replacement; needs a manual migration"
    elif reason is SkipReason.REMOVAL_ALREADY_REPORTED:
        message = "removed API already reported for this commit; will recheck when HEAD changes"
    elif reason is SkipReason.VERSION_UNCHANGED:
        message = "this repo is not on the affected version; will recheck when HEAD changes"
    return SkipDetail(reason, message, resumes_at)


def _cooldown_active(attempt: ChangeAttemptRow, settings: Settings) -> bool:
    """True if ``attempt`` was touched too recently to retry yet -- so a
    flaky failure doesn't get hammered on every cron tick."""
    if settings.pipeline_retry_cooldown_hours <= 0:
        return False
    updated_at = attempt.updated_at
    if updated_at.tzinfo is None:
        # SQLite (the test/dev default DATABASE_URL) doesn't actually
        # persist tzinfo even though the column is DateTime(timezone=True)
        # -- every value this process itself wrote via `_utcnow()` was UTC.
        updated_at = updated_at.replace(tzinfo=UTC)
    elapsed = datetime.now(UTC) - updated_at
    return elapsed < timedelta(hours=settings.pipeline_retry_cooldown_hours)
