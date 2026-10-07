"""Unit tests for the pure fleet-orchestrator policy (``orchestrator/policy.py``).

No DB, no HTTP -- ``decide()`` is deliberately side-effect-free, so every
branch is exercised here directly against plain dataclasses/enum values,
plus a bare (unpersisted) ``ChangeAttemptRow`` where a ledger row is
needed. Check order mirrors the docstring in ``policy.py``: tenant
instructions (never bypassable) -> capability -> bookkeeping
(force-bypassable).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from depfix.config import Settings
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.orchestrator.policy import SkipReason, decide, effective_max_changes_per_run
from depfix.repoconfig.models import PathFilter, ProviderFilter, RepoConfig
from depfix.storage import AttemptStatus, ChangeAttemptRow


def _change(provider_id: str = "openai") -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="3.x",
        new_version="4.x",
        old_api="openai.createModeration()",
        new_api="openai.moderations.create()",
        description="moderations moved under a namespace",
        migration_guide="use openai.moderations.create",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
        provider_id=provider_id,
    )


def _config(**overrides: object) -> RepoConfig:
    defaults: dict[str, object] = {"version": 1}
    defaults.update(overrides)
    return RepoConfig(**defaults)


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)


def _attempt(
    *,
    status: AttemptStatus = AttemptStatus.FAILED,
    attempts_used: int = 1,
    last_checked_ref_sha: str = "",
    updated_at: datetime | None = None,
) -> ChangeAttemptRow:
    row = ChangeAttemptRow(
        repo_id="acme/widgets",
        dedupe_key="dk",
        status=status.value,
        attempts_used=attempts_used,
        last_checked_ref_sha=last_checked_ref_sha,
    )
    # Bypass the column's onupdate/default so tests can pin an exact age
    # for the cooldown check without a real INSERT. Defaults to "long ago"
    # so tests unrelated to cooldown aren't accidentally tripped by it.
    row.updated_at = updated_at if updated_at is not None else datetime.now(UTC) - timedelta(days=1)
    return row


def _decide(
    *,
    repo_config: RepoConfig | None = None,
    change: BreakingChange | None = None,
    known_provider_ids: frozenset[str] = frozenset({"openai"}),
    attempt: ChangeAttemptRow | None = None,
    current_ref_sha: str = "sha-1",
    open_pr_count: int = 0,
    changes_processed_this_run: int = 0,
    settings: Settings | None = None,
    force: bool = False,
) -> SkipReason | None:
    return decide(
        repo_config=repo_config or _config(),
        change=change or _change(),
        known_provider_ids=known_provider_ids,
        attempt=attempt,
        current_ref_sha=current_ref_sha,
        open_pr_count=open_pr_count,
        changes_processed_this_run=changes_processed_this_run,
        settings=settings or _settings(),
        force=force,
    )


# -- go-ahead baseline --------------------------------------------------------


def test_default_config_and_no_prior_attempt_goes_ahead() -> None:
    assert _decide() is None


# -- tenant instructions: never bypassable by force ---------------------------


def test_ignored_dedupe_key_is_skipped() -> None:
    change = _change()
    config = _config(ignore=(change.dedupe_key,))
    assert _decide(repo_config=config, change=change) == SkipReason.TENANT_IGNORED


def test_ignored_provider_glob_is_skipped() -> None:
    config = _config(ignore=("openai*",))
    assert _decide(repo_config=config) == SkipReason.TENANT_IGNORED


def test_ignore_is_not_bypassable_by_force() -> None:
    change = _change()
    config = _config(ignore=(change.dedupe_key,))
    assert _decide(repo_config=config, change=change, force=True) == SkipReason.TENANT_IGNORED


def test_pipeline_disabled_is_skipped() -> None:
    config = _config(enabled=False)
    assert _decide(repo_config=config) == SkipReason.PIPELINE_DISABLED


def test_pipeline_disabled_is_not_bypassable_by_force() -> None:
    config = _config(enabled=False)
    assert _decide(repo_config=config, force=True) == SkipReason.PIPELINE_DISABLED


def test_provider_excluded_is_skipped() -> None:
    config = _config(providers=ProviderFilter(exclude=("openai",)))
    assert _decide(repo_config=config) == SkipReason.PROVIDER_NOT_ALLOWED


def test_provider_not_in_include_list_is_skipped() -> None:
    config = _config(providers=ProviderFilter(include=("anthropic",)))
    assert _decide(repo_config=config) == SkipReason.PROVIDER_NOT_ALLOWED


def test_provider_allowed_by_include_goes_ahead() -> None:
    config = _config(providers=ProviderFilter(include=("openai",)))
    assert _decide(repo_config=config) is None


# -- capability ---------------------------------------------------------------


def test_unknown_provider_is_skipped() -> None:
    assert _decide(known_provider_ids=frozenset({"anthropic"})) == SkipReason.UNKNOWN_PROVIDER


def test_unknown_provider_is_not_bypassable_by_force() -> None:
    assert (
        _decide(known_provider_ids=frozenset({"anthropic"}), force=True)
        == SkipReason.UNKNOWN_PROVIDER
    )


def test_removal_without_replacement_is_skipped_even_when_forced() -> None:
    change = BreakingChange(
        package="openai",
        old_version="7.17.0",
        new_version="7.19.0",
        old_api="openai.responses.connector_id",
        new_api="",
        description="d",
        migration_guide="",
        kind=ChangeKind.FIELD_REMOVED,
        source=ClassificationSource.SPEC_DIFF,
        provider_id="openai",
    )

    assert _decide(change=change, force=True) == SkipReason.NO_REPLACEMENT_API


def test_version_not_affected_is_ref_gated() -> None:
    attempt = _attempt(status=AttemptStatus.VERSION_NOT_AFFECTED, last_checked_ref_sha="sha-1")

    assert _decide(attempt=attempt, current_ref_sha="sha-1") == SkipReason.VERSION_UNCHANGED
    assert _decide(attempt=attempt, current_ref_sha="sha-2") is None


# -- bookkeeping: force-bypassable ---------------------------------------------


def test_terminal_attempt_is_skipped() -> None:
    attempt = _attempt(status=AttemptStatus.PR_OPENED)
    assert _decide(attempt=attempt) == SkipReason.ALREADY_TERMINAL


def test_non_terminal_attempt_goes_ahead() -> None:
    attempt = _attempt(status=AttemptStatus.FAILED, attempts_used=1)
    assert _decide(attempt=attempt) is None


def test_force_bypasses_already_terminal() -> None:
    attempt = _attempt(status=AttemptStatus.PR_OPENED)
    assert _decide(attempt=attempt, force=True) is None


def test_max_attempts_reached_is_skipped() -> None:
    settings = _settings(pipeline_max_attempts=3)
    attempt = _attempt(status=AttemptStatus.FAILED, attempts_used=3)
    assert _decide(attempt=attempt, settings=settings) == SkipReason.MAX_ATTEMPTS_REACHED


def test_max_attempts_not_yet_reached_goes_ahead() -> None:
    settings = _settings(pipeline_max_attempts=3)
    attempt = _attempt(status=AttemptStatus.FAILED, attempts_used=2)
    assert _decide(attempt=attempt, settings=settings) is None


def test_force_bypasses_max_attempts() -> None:
    settings = _settings(pipeline_max_attempts=3)
    attempt = _attempt(status=AttemptStatus.FAILED, attempts_used=5)
    assert _decide(attempt=attempt, settings=settings, force=True) is None


def test_no_call_sites_unchanged_ref_is_skipped() -> None:
    attempt = _attempt(status=AttemptStatus.NO_CALL_SITES, last_checked_ref_sha="sha-1")
    assert _decide(attempt=attempt, current_ref_sha="sha-1") == SkipReason.NO_CALL_SITES_UNCHANGED


def test_no_call_sites_changed_ref_goes_ahead() -> None:
    attempt = _attempt(status=AttemptStatus.NO_CALL_SITES, last_checked_ref_sha="sha-old")
    assert _decide(attempt=attempt, current_ref_sha="sha-new") is None


def test_no_call_sites_with_empty_current_ref_goes_ahead() -> None:
    """An empty ``current_ref_sha`` (e.g. a repo with no commits reachable)
    must never match ``last_checked_ref_sha`` -- that comparison is
    deliberately guarded by ``current_ref_sha and ...`` in ``decide()``."""
    attempt = _attempt(status=AttemptStatus.NO_CALL_SITES, last_checked_ref_sha="")
    assert _decide(attempt=attempt, current_ref_sha="") is None


def test_cooldown_active_is_skipped() -> None:
    settings = _settings(pipeline_retry_cooldown_hours=6.0)
    attempt = _attempt(updated_at=datetime.now(UTC) - timedelta(hours=1))
    assert _decide(attempt=attempt, settings=settings) == SkipReason.COOLDOWN_ACTIVE


def test_cooldown_expired_goes_ahead() -> None:
    settings = _settings(pipeline_retry_cooldown_hours=6.0)
    attempt = _attempt(updated_at=datetime.now(UTC) - timedelta(hours=7))
    assert _decide(attempt=attempt, settings=settings) is None


def test_cooldown_disabled_by_zero_goes_ahead() -> None:
    settings = _settings(pipeline_retry_cooldown_hours=0.0)
    attempt = _attempt(updated_at=datetime.now(UTC))
    assert _decide(attempt=attempt, settings=settings) is None


def test_naive_updated_at_is_treated_as_utc() -> None:
    """SQLite doesn't persist tzinfo -- a naive ``updated_at`` from just now
    must still trip the cooldown, not be treated as an ancient timestamp."""
    settings = _settings(pipeline_retry_cooldown_hours=6.0)
    attempt = _attempt(updated_at=datetime.now(UTC) - timedelta(hours=1))
    attempt.updated_at = attempt.updated_at.replace(tzinfo=None)
    assert _decide(attempt=attempt, settings=settings) == SkipReason.COOLDOWN_ACTIVE


def test_force_bypasses_cooldown() -> None:
    settings = _settings(pipeline_retry_cooldown_hours=6.0)
    attempt = _attempt(updated_at=datetime.now(UTC) - timedelta(hours=1))
    assert _decide(attempt=attempt, settings=settings, force=True) is None


def test_max_open_prs_reached_is_skipped() -> None:
    settings = _settings(pipeline_max_open_prs=2)
    assert _decide(open_pr_count=2, settings=settings) == SkipReason.MAX_OPEN_PRS_REACHED


def test_below_max_open_prs_goes_ahead() -> None:
    settings = _settings(pipeline_max_open_prs=2)
    assert _decide(open_pr_count=1, settings=settings) is None


def test_force_bypasses_max_open_prs() -> None:
    settings = _settings(pipeline_max_open_prs=2)
    assert _decide(open_pr_count=5, settings=settings, force=True) is None


def test_max_changes_per_run_reached_is_skipped() -> None:
    settings = _settings(pipeline_max_changes_per_repo=2)
    assert (
        _decide(changes_processed_this_run=2, settings=settings)
        == SkipReason.MAX_CHANGES_PER_RUN_REACHED
    )


def test_below_max_changes_per_run_goes_ahead() -> None:
    settings = _settings(pipeline_max_changes_per_repo=2)
    assert _decide(changes_processed_this_run=1, settings=settings) is None


def test_force_bypasses_max_changes_per_run() -> None:
    settings = _settings(pipeline_max_changes_per_repo=2)
    assert _decide(changes_processed_this_run=10, settings=settings, force=True) is None


# -- check ordering ------------------------------------------------------------


def test_tenant_ignore_is_checked_before_unknown_provider() -> None:
    """A repo-owner ignore rule wins even when depfix also can't act on the
    provider at all -- tenant instructions are checked first."""
    change = _change(provider_id="mystery")
    config = _config(ignore=(change.dedupe_key,))
    assert (
        _decide(repo_config=config, change=change, known_provider_ids=frozenset())
        == SkipReason.TENANT_IGNORED
    )


def test_no_prior_attempt_skips_all_bookkeeping_checks() -> None:
    """No ledger row at all (first time this change is seen) must not trip
    max-attempts/cooldown/no-call-sites-unchanged -- those only apply once
    there's a prior attempt to compare against."""
    settings = _settings(pipeline_max_attempts=1, pipeline_retry_cooldown_hours=999.0)
    assert _decide(attempt=None, settings=settings) is None


# -- effective_max_changes_per_run --------------------------------------------


def test_effective_max_changes_per_run_defaults_to_settings_cap() -> None:
    settings = _settings(pipeline_max_changes_per_repo=3)
    assert effective_max_changes_per_run(_config(), settings) == 3


def test_effective_max_changes_per_run_can_only_lower_the_cap() -> None:
    settings = _settings(pipeline_max_changes_per_repo=3)
    config = _config(max_changes_per_run=1)
    assert effective_max_changes_per_run(config, settings) == 1


def test_effective_max_changes_per_run_cannot_raise_the_cap() -> None:
    settings = _settings(pipeline_max_changes_per_repo=3)
    config = _config(max_changes_per_run=10)
    assert effective_max_changes_per_run(config, settings) == 3


def test_path_filter_unused_by_decide_does_not_affect_it() -> None:
    """Sanity check: ``paths`` gates which edits get committed later in the
    pipeline, not whether ``decide()`` proceeds -- a fully-excluding path
    filter must not itself cause a skip here."""
    config = _config(paths=PathFilter(exclude=("**",)))
    assert _decide(repo_config=config) is None
