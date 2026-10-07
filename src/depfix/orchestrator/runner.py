"""The Week 6/7 fleet orchestrator: unattended scan -> fix -> verify -> PR
across many repos, gated by each repo's own opt-in ``.depfix.yml`` and an
idempotency ledger so cron-driven reruns never reopen a PR or reprocess a
terminal outcome.

Mirrors :mod:`depfix.watcher.runner`'s failure-isolation shape: one bad
repo (or one bad change within a repo) must never cost the rest of the
fleet run its results -- every per-repo and per-change action is wrapped in
its own try/except, producing a failure outcome rather than propagating.
All the actual go/no-go logic lives in the pure :mod:`depfix.orchestrator.policy`;
this module is the thin, mockable shell that gathers the inputs that
``decide()`` needs and acts on its verdict.

Week 7 hardening on top of the Week 6 shape:

- Disabled providers (``ProviderSpec.enabled=False``) are filtered out of
  the known-provider set at construction time, not just skipped later --
  a disabled provider must behave as fully unknown to the orchestrator.
- Each ``(repo, change)`` pair gets its own DB transaction (one
  ``session_scope()`` per change), not one long-lived session for the
  whole repo -- a failure on change 5 can no longer leave change 6's
  session in a half-committed state, and a slow clone/fix/verify/PR round
  trip for one change no longer holds a transaction open for every other
  change behind it.
- A repo's ``.depfix.yml`` ``paths`` filter is applied to the scan result
  *before* the fix pipeline runs, not just to the already-fixed edits
  right before opening a PR -- an out-of-scope file no longer costs an
  LLM call just to have its result discarded.
- A single Postgres advisory lock (see :mod:`depfix.storage.lock`) and an
  optional wall-clock budget (see :mod:`depfix.orchestrator.budget`) keep
  two overlapping cron ticks from racing each other and keep one run from
  running past its window. That same budget also tracks cumulative LLM
  spend (``Settings.pipeline_max_cost_usd_per_run``), so a run can stop
  early on either ceiling, not just the clock.
- A ``no_pr`` override forces every repo's *effective* open-PR decision to
  ``False`` for one run, independent of ``dry_run`` and of what that
  repo's own ``.depfix.yml`` says -- the middle rung of the onboarding
  rehearsal ladder (``--dry-run`` -> ``--no-pr`` -> ``draft_pr: true`` ->
  live).
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from depfix.apply.models import EditVerdict, FileEdit
from depfix.apply.workspace import WorkspaceEditor
from depfix.classify.llm import CostTrackingCompleter, LLMCompleter
from depfix.clone import Checkout, CloneService
from depfix.config import Settings
from depfix.core.fix_service import FixServiceResult, run_fix_for_change
from depfix.core.models import BreakingChange, ChangeKind, is_method_removal
from depfix.core.pipeline import FixPipelineResult
from depfix.core.upgrade import UpgradePlan, build_upgrade_plan
from depfix.core.upgrade_service import UpgradeResult, run_upgrade
from depfix.fixers.bedrock import BedrockFixGenerator
from depfix.fixers.gemini import FixGenerator
from depfix.fixers.ollama import OllamaFixGenerator
from depfix.gh import (
    BRANCH_PREFIX,
    BranchWriter,
    GitHubAppAuth,
    GitHubAppError,
    PullRequest,
    branch_name_for,
    build_pr_body,
    open_pull_request,
    pr_title_for,
)
from depfix.gh.branch import branch_name_for_key
from depfix.gh.pr import build_upgrade_pr_body
from depfix.obs.cost import CostLedger
from depfix.obs.logging import log_context
from depfix.orchestrator.budget import RunBudget
from depfix.orchestrator.models import ChangeOutcome, OrchestratorOutcome, RepoOutcome
from depfix.orchestrator.plan import PlanArtifact
from depfix.orchestrator.policy import SkipReason, decide_with_detail
from depfix.providers.models import ProviderSpec
from depfix.repoconfig.loader import RepoConfigError, parse_repo_config
from depfix.repoconfig.models import SUPPORTED_VERSION, RepoConfig
from depfix.scanners import (
    CallSiteScanner,
    build_scan_target,
    reusable_scan_for_provider,
    scan_checkout,
    upsert_repo,
)
from depfix.scanners.models import RepoScanResult, ScanTarget
from depfix.scanners.removals import collect_removal_notices
from depfix.scanners.store import load_scan_result
from depfix.scanners.strategy import resolve_scan_strategy
from depfix.severity import EvidenceKind, Severity
from depfix.sources.semver import drift_risk
from depfix.storage import (
    AttemptStatus,
    ChangeAttemptRow,
    attempts_for_repo,
    get_engine,
    init_schema,
    record_change_attempt,
    record_fix_run,
    record_pull_request,
    session_scope,
    touch_recheck_on_change,
)
from depfix.storage.attempt_store import worst_attempt_across
from depfix.storage.lock import advisory_lock
from depfix.verify.confidence import ConfidenceTier

logger = logging.getLogger(__name__)

_NO_WORK_STATUSES = frozenset(
    {AttemptStatus.NO_CALL_SITES, AttemptStatus.VERSION_NOT_AFFECTED, AttemptStatus.REPORT_ONLY}
)


def _major_gap(change: BreakingChange) -> int:
    from depfix.sources.semver import parse_semver

    old, new = parse_semver(change.old_version), parse_semver(change.new_version)
    return (new[0] - old[0]) if old and new else 0


def _is_major_dependency_review(change: BreakingChange, settings: Settings | None = None) -> bool:
    """Whether a registry major drift must be opened as a draft review PR."""
    if change.kind is not ChangeKind.DEPENDENCY_VERSION_BUMP:
        return False
    from depfix.codemods.lockfile import dependency_drift_policy

    kwargs: dict = {}
    if settings is not None:
        kwargs["max_major_gap"] = settings.drift_max_major_gap
        kwargs["max_minor_gap"] = settings.drift_max_minor_gap
    return dependency_drift_policy(change, **kwargs) == "review_pr"


class Orchestrator:
    """Runs the full fleet pipeline for a batch of ``(repo, change)`` pairs.

    ``fixer_factory`` builds one fresh fixer per change (a
    :class:`FixGenerator`/:class:`OllamaFixGenerator` instance keeps its own
    ``llm_calls`` list, so reusing one across changes would mix their
    accounting) -- callers own the choice of provider/model via whatever
    closure they pass in.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        gh_auth: GitHubAppAuth,
        clone_service: CloneService,
        providers: list[ProviderSpec],
        fixer_factory: Callable[[], BedrockFixGenerator | FixGenerator | OllamaFixGenerator],
        force: bool = False,
        dry_run: bool = False,
        no_pr: bool = False,
        use_lock: bool = True,
        max_duration_seconds: float | None = None,
        max_cost_usd: float | None = None,
        plan_artifacts: dict[tuple[str, str], PlanArtifact] | None = None,
        completer: LLMCompleter | None = None,
        agent_coordinator: object | None = None,  # NEW
        ledger: CostLedger | None = None,  # NEW — shared with CLI
    ) -> None:
        self._settings = settings
        self._gh_auth = gh_auth
        self._clone_service = clone_service
        # Disabled providers are dropped here, not just skipped later --
        # `known_provider_ids` (what `decide()` treats as "capable of
        # acting on") must never include them.
        self._providers = {p.id: p for p in providers if p.enabled}
        self._known_provider_ids = frozenset(self._providers)
        self._fixer_factory = fixer_factory
        self._force = force
        self._dry_run = dry_run
        self._no_pr = no_pr
        self._use_lock = use_lock
        self._max_duration_seconds = max_duration_seconds
        self._max_cost_usd = max_cost_usd
        self._plan_artifacts = plan_artifacts or {}
        self._completer = completer
        self._agent_coordinator = agent_coordinator  # NEW
        # Use the caller's shared ledger when provided so agent-scan spend,
        # classify spend, and fix-generation spend all land in the same place.
        self._shared_ledger: CostLedger | None = ledger
        self._budget = RunBudget(None)  # replaced at the top of every run()
        # (package, version) -> age in hours. One registry lookup per target
        # version per run, not one per (repo, change) pair.
        self._version_age_cache: dict[tuple[str, str], float | None] = {}
        self._provider_change_cache: dict[str, list[BreakingChange]] = {}
        self._run_changes: list[BreakingChange] = []
        self._covered_this_run: set[tuple[str, str]] = set()
        self._reuse_scan = settings.plan_reuse_scan
        self._scan_cache: dict[tuple[str, str, str], RepoScanResult] = {}
        # One discover_usage_candidates call per (provider, commit, observed-set).
        # Keyed by (provider_id, commit_sha, sorted-observed-symbols) so a
        # narrowed re-scan of the same commit with different --old-api symbols
        # still gets its own call, while the common case (N changes, same repo)
        # costs exactly one LLM call instead of N.
        self._mapper_cache: dict[tuple[str, str, str], list[BreakingChange]] = {}

    def run(self, repo_full_names: list[str], changes: list[BreakingChange]) -> OrchestratorOutcome:
        """Process every ``(repo, change)`` pair for this run.

        One bad repo can't cost the rest of the fleet its results: any
        exception that escapes :meth:`_run_repo` itself (as opposed to one
        already caught and turned into a per-change ``FAILED`` outcome by
        :meth:`_process_change`) is caught here and recorded as that repo's
        ``error``.
        """
        init_schema()
        self._run_changes = changes
        self._provider_change_cache.clear()
        self._covered_this_run.clear()
        self._mapper_cache.clear()
        self._scan_cache.clear()
        # Use the shared ledger from the CLI when provided, so agent-scan
        # spend and fix-generation spend all appear in the same summary.
        self._ledger = self._shared_ledger if self._shared_ledger is not None else CostLedger()
        if self._completer is not None:
            self._completer = CostTrackingCompleter(self._completer, self._ledger)
        outcome = OrchestratorOutcome(run_id=uuid4().hex)
        engine = get_engine()
        lock_ctx = advisory_lock(engine) if self._use_lock else contextlib.nullcontext(True)

        with log_context(run_id=outcome.run_id), lock_ctx as acquired:
            if not acquired:
                outcome.locked = False
                return outcome

            # Same contract as `should_inspect_prs` below: --no-pr (like
            # --dry-run) must never require the GitHub App's Pull requests
            # permission, so auto-reconciliation -- which lists open PRs --
            # is skipped for both, not just for dry runs.
            if not self._dry_run and not self._no_pr:
                try:
                    from depfix.orchestrator.reconcile import compute_merge_rate, reconcile_open_prs

                    with session_scope() as session:
                        drift = reconcile_open_prs(session, self._gh_auth, repo_full_names)
                        outcome.merge_rate = compute_merge_rate(session, repo_full_names)
                    if drift:
                        logger.info("auto-reconciled %d PR outcome(s) before processing", drift)
                except Exception:
                    logger.debug(
                        "auto-reconciliation failed; proceeding with stale ledger", exc_info=True
                    )

            self._budget = RunBudget(
                self._max_duration_seconds,
                (
                    self._settings.pipeline_max_cost_usd_per_run
                    if self._max_cost_usd is None
                    else self._max_cost_usd
                ),
            )
            for repo_full_name in repo_full_names:
                if self._budget.expired:
                    logger.warning("max-duration budget exhausted; stopping run early")
                    outcome.stopped_early = True
                    break
                try:
                    outcome.repos.append(self._run_repo(repo_full_name, changes))
                except Exception as exc:
                    logger.exception("repo %s failed", repo_full_name)
                    outcome.repos.append(RepoOutcome(repo_full_name=repo_full_name, error=str(exc)))
        outcome.cost_ledger = self._ledger
        return outcome

    def _run_repo(self, repo_full_name: str, changes: list[BreakingChange]) -> RepoOutcome:
        with log_context(repo=repo_full_name):
            return self._run_repo_body(repo_full_name, changes)

    def _run_repo_body(self, repo_full_name: str, changes: list[BreakingChange]) -> RepoOutcome:
        owner, _, name = repo_full_name.partition("/")
        if not name:
            return RepoOutcome(
                repo_full_name=repo_full_name, error=f"not OWNER/NAME: {repo_full_name!r}"
            )

        try:
            installation = self._gh_auth.resolve_installation_for_repo(owner, name)
        except GitHubAppError:
            return RepoOutcome(
                repo_full_name=repo_full_name, skip_reason=SkipReason.NO_INSTALLATION
            )

        try:
            config_text = self._gh_auth.get_file_contents(
                owner, name, self._settings.repo_config_filename
            )
        except GitHubAppError as exc:
            return RepoOutcome(repo_full_name=repo_full_name, error=str(exc))

        if config_text is None:
            if self._settings.pipeline_require_config_file:
                return RepoOutcome(
                    repo_full_name=repo_full_name, skip_reason=SkipReason.NO_REPO_CONFIG
                )
            repo_config = RepoConfig(version=SUPPORTED_VERSION)
        else:
            try:
                repo_config = parse_repo_config(
                    config_text,
                    where=f"{repo_full_name}/{self._settings.repo_config_filename}",
                )
            except RepoConfigError as exc:
                return RepoOutcome(
                    repo_full_name=repo_full_name,
                    skip_reason=SkipReason.INVALID_REPO_CONFIG,
                    error=str(exc),
                )

        try:
            base_branch = repo_config.base_branch or self._gh_auth.default_ref(owner, name)
            current_ref_sha = self._gh_auth.ref_sha(owner, name, base_branch)
            # Dry runs and --no-pr runs never create a PR, so they must not
            # require the GitHub App's Pull requests permission merely to
            # inspect the open-PR cap. The same applies to a repository that
            # explicitly opted out of PR creation in .depfix.yml.
            should_inspect_prs = not self._dry_run and not self._no_pr and repo_config.open_pr
            open_prs = (
                self._gh_auth.list_open_pull_requests(owner, name, base=base_branch)
                if should_inspect_prs
                else []
            )
        except GitHubAppError as exc:
            return RepoOutcome(repo_full_name=repo_full_name, error=str(exc))
        logger.info("repo %s: base=%s sha=%s", repo_full_name, base_branch, current_ref_sha[:12])
        open_pr_count = sum(1 for pr in open_prs if pr.head_branch.startswith(BRANCH_PREFIX))

        with session_scope() as session:
            upsert_repo(
                session,
                full_name=repo_full_name,
                owner=owner,
                name=name,
                installation_id=installation.id,
                default_branch=base_branch,
            )
            attempts = attempts_for_repo(session, repo_id=repo_full_name)

        change_outcomes: list[ChangeOutcome] = []
        changes_processed = 0
        stopped_early = False

        ordered = sorted(
            changes, key=lambda c: (0 if c.kind is ChangeKind.DEPENDENCY_VERSION_BUMP else 1,)
        )

        # Pre-scan: populate _scan_cache for every provider before filtering.
        # Only scan providers that have at least one change that isn't already
        # trivially skippable by the policy gate (ignored, already terminal,
        # etc.) — this avoids a clone when all changes would be skipped anyway.
        needs_scan_providers = {
            c.provider_id
            for c in ordered
            if decide_with_detail(
                repo_config=repo_config,
                change=c,
                known_provider_ids=self._known_provider_ids,
                attempt=attempts.get(c.dedupe_key),
                current_ref_sha=current_ref_sha,
                open_pr_count=open_pr_count,
                changes_processed_this_run=changes_processed,
                settings=self._settings,
                force=self._force,
                target_version_age_hours=self._target_version_age_hours(c),
            )
            is None
        }
        if needs_scan_providers:
            with session_scope() as session:
                self._prescan_providers(
                    session,
                    owner=owner,
                    name=name,
                    repo_full_name=repo_full_name,
                    installation_id=installation.id,
                    base_branch=base_branch,
                    current_ref_sha=current_ref_sha,
                    provider_ids=list(needs_scan_providers),
                    repo_config=repo_config,
                )

        # Relevance filter: keep only changes whose old_api matches a call
        # site, then cap to the pipeline limit so the budget still applies.
        # A provider with 477 classified changes can no longer drown the 5
        # that actually apply to this repo.
        ordered, no_call_site_changes = self._match_filter_and_cap(
            ordered,
            repo_full_name=repo_full_name,
            commit_sha=current_ref_sha,
            cap=self._settings.pipeline_default_change_limit,
        )
        if no_call_site_changes:
            logger.info(
                "%s: relevance-filtered %d classified change(s) with no "
                "matching call site (cap now applies to the %d-match set)",
                repo_full_name,
                len(no_call_site_changes),
                len(ordered),
            )

        for change in ordered:
            if self._budget.expired:
                stopped_early = True
                break

            if (repo_full_name, change.dedupe_key) in self._covered_this_run:
                change_outcomes.append(
                    ChangeOutcome(
                        dedupe_key=change.dedupe_key,
                        package=change.package,
                        old_api=change.old_api,
                        kind=change.kind.value,
                        skip_reason=SkipReason.ALREADY_TERMINAL,
                        skip_detail_text="already shipped by an upgrade PR in this run",
                    )
                )
                continue

            logger.info(
                "considering %s: %s %s -> %s",
                change.dedupe_key[:12],
                change.package,
                change.old_api,
                change.replacement,
            )

            attempt = attempts.get(change.dedupe_key)
            skip = decide_with_detail(
                repo_config=repo_config,
                change=change,
                known_provider_ids=self._known_provider_ids,
                attempt=attempt,
                current_ref_sha=current_ref_sha,
                open_pr_count=open_pr_count,
                changes_processed_this_run=changes_processed,
                settings=self._settings,
                force=self._force,
                target_version_age_hours=self._target_version_age_hours(change),
            )
            if skip is not None:
                change_outcomes.append(
                    ChangeOutcome(
                        dedupe_key=change.dedupe_key,
                        package=change.package,
                        old_api=change.old_api,
                        skip_reason=skip.reason,
                        skip_detail_text=skip.one_line(),
                        kind=change.kind.value,
                    )
                )
                continue

            # One transaction per change -- a failure (or crash) while
            # processing this change commits or rolls back only this
            # change's own ledger write, never a prior change's already
            # -committed work in the same repo.
            before_cost = self._ledger.total
            with session_scope() as session:
                change_outcome = self._process_change(
                    session,
                    owner=owner,
                    name=name,
                    repo_full_name=repo_full_name,
                    installation_id=installation.id,
                    change=change,
                    repo_config=repo_config,
                    base_branch=base_branch,
                    current_ref_sha=current_ref_sha,
                    attempt=attempt,
                )
            change_outcomes.append(change_outcome)
            self._covered_this_run.update(
                (repo_full_name, key) for key in change_outcome.member_dedupe_keys
            )
            self._budget.record_cost(self._ledger.total - before_cost)
            if change_outcome.attempt_status not in _NO_WORK_STATUSES:
                changes_processed += 1
            if change_outcome.attempt_status == AttemptStatus.PR_OPENED:
                open_pr_count += 1

        if stopped_early:
            logger.warning(
                "max-duration budget exhausted mid-repo for %s; %d/%d changes considered",
                repo_full_name,
                changes_processed,
                len(ordered),
            )

        # Record pre-filtered NO_CALL_SITES changes cheaply: no clone, no LLM.
        # This keeps the idempotency ledger current so subsequent runs skip them.
        for dropped in no_call_site_changes:
            change_outcomes.append(
                ChangeOutcome(
                    dedupe_key=dropped.dedupe_key,
                    package=dropped.package,
                    old_api=dropped.old_api,
                    kind=dropped.kind.value,
                    attempt_status=AttemptStatus.NO_CALL_SITES,
                    detail=f"no call sites match {dropped.old_api!r} (pre-filtered by scan)",
                    planned=self._dry_run,
                )
            )
            if not self._dry_run:
                with session_scope() as session:
                    touch_recheck_on_change(
                        session,
                        repo_id=repo_full_name,
                        dedupe_key=dropped.dedupe_key,
                        status=AttemptStatus.NO_CALL_SITES,
                        ref_sha=current_ref_sha,
                    )

        return RepoOutcome(
            repo_full_name=repo_full_name, changes=change_outcomes, open_pr_count=open_pr_count
        )

    def _process_change(
        self,
        session: Session,
        *,
        owner: str,
        name: str,
        repo_full_name: str,
        installation_id: int,
        change: BreakingChange,
        repo_config: RepoConfig,
        base_branch: str,
        current_ref_sha: str,
        attempt: ChangeAttemptRow | None,
    ) -> ChangeOutcome:
        """Scan, fix, verify, and (if configured) open a PR for one
        ``(repo, change)`` pair -- isolated so one change's bug can't cost
        the rest of this repo's run its results (see :meth:`_run_repo`).

        Under ``dry_run``, every branch below still runs the real
        scan/fix/verify pipeline (so the reported outcome reflects what
        would really happen) but skips every DB write, branch commit, and
        PR creation -- ``planned=True`` on the returned outcome marks it as
        a prediction, not a completed action.
        """
        # `attempt.attempts_used` counts attempts already recorded for this
        # change; +1 names *this* attempt, so log lines read "attempt 1" on
        # a change's first try, not "attempt 0".
        attempt_number = (attempt.attempts_used if attempt else 0) + 1
        with log_context(dedupe_key=change.dedupe_key, attempt=attempt_number):
            return self._process_change_body(
                session,
                owner=owner,
                name=name,
                repo_full_name=repo_full_name,
                installation_id=installation_id,
                change=change,
                repo_config=repo_config,
                base_branch=base_branch,
                current_ref_sha=current_ref_sha,
            )

    def _resolve_scan(
        self,
        session: Session,
        *,
        checkout: Checkout,
        target: ScanTarget,
        repo_full_name: str,
        provider_id: str,
        commit_sha: str,
    ) -> tuple[RepoScanResult | None, str]:
        """Prefer the output of ``depfix scan``; rescan only if policy allows.

        ``depfix scan`` is the authoritative discovery pass: it is broad (every
        provider symbol, so a superset of any narrowed target), it is the pass
        whose findings were listed in ``learned_migrations.yaml``, and it is the
        one the operator actually read. Re-deriving call sites here means the
        artifact can disagree with the report that justified it.
        """
        cache_key = (repo_full_name, provider_id, commit_sha)
        if cache_key in self._scan_cache:
            return self._scan_cache[cache_key], "reused (this run)"

        if self._reuse_scan != "never":
            row = reusable_scan_for_provider(
                session, repo_full_name, provider_id, commit_sha=commit_sha
            )
            if row is not None:
                result = load_scan_result(row)
                logger.info(
                    "reusing scan %s@%s: %d call site(s), %d dependency declaration(s)",
                    row.repo_id,
                    row.commit_sha[:12],
                    len(result.call_sites),
                    len(result.dependencies),
                )
                self._scan_cache[cache_key] = result
                return result, "reused (depfix scan)"

        if self._reuse_scan == "require":
            return None, "missing"

        logger.info(
            "no reusable scan for %s/%s@%s; scanning now",
            repo_full_name,
            provider_id,
            commit_sha[:12],
        )
        result = scan_checkout(
            checkout,
            target,
            repo_full_name=repo_full_name,
            scanner=CallSiteScanner(
                max_file_bytes=self._settings.scan_max_file_bytes,
                max_files=self._settings.scan_max_files,
            ),
            strategy=resolve_scan_strategy(
                self._settings.scan_strategy,
                has_llm=self._agent_coordinator is not None,
            ),
            provider=self._providers.get(provider_id),
            agent_completer=self._agent_coordinator,
            ledger=self._ledger,
            max_data_bytes=int(self._settings.scan_max_data_mb * 1024 * 1024),
        )
        self._scan_cache[cache_key] = result
        return result, "rescanned"

    def _mapping_candidates_for(
        self,
        session: Session,
        *,
        provider_id: str,
        commit_sha: str,
        package: str,
        observed_symbols: list[str],
    ) -> list[BreakingChange]:
        """Discover feed-proven candidates for this (provider, commit) once.

        A single plan/apply run processes many classified changes against the
        same checkout; each would otherwise re-enter discover_usage_candidates
        and re-ask the LLM about the same observed-symbols set. One call per
        (provider, commit, observed-set) matches how _scan_cache already amortises
        the scan itself.

        Cache lifetime is per Orchestrator.run() call — cleared at the top of
        every run() with the other caches.
        """
        from depfix.classify.usage_candidates import discover_usage_candidates

        key = (provider_id, commit_sha, ",".join(sorted(observed_symbols)))
        cached = self._mapper_cache.get(key)
        if cached is not None:
            return cached
        if self._completer is None:
            self._mapper_cache[key] = []
            return []
        discovered = discover_usage_candidates(
            session,
            provider_id=provider_id,
            package=package,
            observed_symbols=observed_symbols,
            completer=self._completer,
        )
        self._mapper_cache[key] = discovered
        return discovered

    def _prescan_providers(
        self,
        session: Session,
        *,
        owner: str,
        name: str,
        repo_full_name: str,
        installation_id: int,
        base_branch: str,
        current_ref_sha: str,
        provider_ids: list[str],
        repo_config: RepoConfig,
    ) -> None:
        """Populate _scan_cache for every provider before the change loop.

        Loading from the DB (a prior ``depfix scan`` row) is free. When no
        stored scan exists the fallback clones once and scans — one extra
        git-clone but ensures _match_filter_and_cap always has data.
        """
        for provider_id in provider_ids:
            cache_key = (repo_full_name, provider_id, current_ref_sha)
            if cache_key in self._scan_cache:
                continue

            provider = self._providers.get(provider_id)
            if provider is None:
                continue

            # Try the DB-stored scan first (free; requires prior depfix scan).
            if self._reuse_scan != "never":
                row = reusable_scan_for_provider(
                    session, repo_full_name, provider_id, commit_sha=current_ref_sha
                )
                if row is not None:
                    result = load_scan_result(row)
                    logger.info(
                        "pre-scan: loaded stored scan for %s/%s (%d call site(s))",
                        repo_full_name,
                        provider_id,
                        len(result.call_sites),
                    )
                    self._scan_cache[cache_key] = result
                    continue

            if self._reuse_scan == "require":
                continue

            # Fallback: clone and scan now.
            try:
                token = self._gh_auth.installation_token(installation_id, repositories=(name,))
                checkout = self._clone_service.clone(
                    owner,
                    name,
                    ref=base_branch,
                    token=token.token,
                    max_repo_mb=repo_config.max_repo_mb,
                )
            except Exception:
                logger.debug(
                    "pre-scan clone failed for %s/%s; match-filter will "
                    "pass these changes through unfiltered",
                    repo_full_name,
                    provider_id,
                )
                continue

            all_changes = self._provider_changes(provider_id)
            target = build_scan_target(provider, feed_changes=all_changes)
            try:
                result = scan_checkout(
                    checkout,
                    target,
                    repo_full_name=repo_full_name,
                    scanner=CallSiteScanner(
                        max_file_bytes=self._settings.scan_max_file_bytes,
                        max_files=self._settings.scan_max_files,
                    ),
                    strategy=resolve_scan_strategy(
                        self._settings.scan_strategy,
                        has_llm=self._agent_coordinator is not None,
                    ),
                    provider=provider,
                    agent_completer=self._agent_coordinator,
                    ledger=self._ledger,
                    max_data_bytes=int(self._settings.scan_max_data_mb * 1024 * 1024),
                )
                self._scan_cache[cache_key] = result
                logger.info(
                    "pre-scan: scanned %s/%s — %d actionable call site(s)",
                    repo_full_name,
                    provider_id,
                    len(result.actionable_sites),
                )
            except Exception:
                logger.exception("pre-scan failed for %s/%s", repo_full_name, provider_id)

    def _match_filter_and_cap(
        self,
        changes: list[BreakingChange],
        *,
        repo_full_name: str,
        commit_sha: str,
        cap: int,
    ) -> tuple[list[BreakingChange], list[BreakingChange]]:
        """Keep only changes whose old_api matches an observed call site. Cap
        ACTIONABLE survivors at ``cap``. Returns ``(to_process, no_call_sites)``.

        Pure Python — no LLM. Replaces the time-based cap with a relevance-
        based one so a provider with 477 classified changes doesn't drown the
        5 that actually apply to this repo.

        ``no_call_sites`` are changes the scan confirmed have no matching call
        site; the caller records them cheaply (no clone, no LLM) so the
        idempotency ledger knows not to retry them next run.

        Changes whose provider has no cached scan pass through unfiltered —
        the per-change assess_scan_change in _process_change_body handles them.
        """
        from depfix.scanners.matching import ScanMatchStatus, assess_scan_change

        actionable: list[BreakingChange] = []
        no_call_sites: list[BreakingChange] = []
        for change in changes:
            cache_key = (repo_full_name, change.provider_id, commit_sha)
            scan_result = self._scan_cache.get(cache_key)
            if scan_result is None:
                # No pre-scan for this provider — pass through unfiltered.
                actionable.append(change)
                continue
            verdict = assess_scan_change(scan_result, change, provider_id=change.provider_id)
            if verdict.status is ScanMatchStatus.ACTIONABLE:
                actionable.append(change)
            elif verdict.status is ScanMatchStatus.CURRENT:
                # Repo already uses the replacement — not an error, skip silently.
                continue
            elif verdict.status is ScanMatchStatus.VERSION_NOT_AFFECTED:
                # Change doesn't apply to this repo's version — skip silently.
                continue
            else:
                no_call_sites.append(change)
        return actionable[:cap], no_call_sites

    def _process_change_body(
        self,
        session: Session,
        *,
        owner: str,
        name: str,
        repo_full_name: str,
        installation_id: int,
        change: BreakingChange,
        repo_config: RepoConfig,
        base_branch: str,
        current_ref_sha: str,
    ) -> ChangeOutcome:
        outcome = ChangeOutcome(
            dedupe_key=change.dedupe_key,
            package=change.package,
            old_api=change.old_api,
            kind=change.kind.value,
        )
        checkout: Checkout | None = None
        try:
            provider = self._providers[change.provider_id]
            # Broad, feed-driven target. Never narrow a rescan to one change:
            # _scan_cache is keyed by (repo, provider, sha), so a narrowed
            # result served every *other* change for the same provider with
            # false no_call_sites, and narrowing dropped replacement-API sites
            # so CURRENT ("already migrated") was undetectable.
            target = build_scan_target(
                provider, feed_changes=[change, *self._provider_changes(change.provider_id)]
            )
            token = self._gh_auth.installation_token(installation_id, repositories=(name,))
            checkout = self._clone_service.clone(
                owner,
                name,
                ref=base_branch,
                token=token.token,
                max_repo_mb=repo_config.max_repo_mb,
            )

            scan_result, scan_source = self._resolve_scan(
                session,
                checkout=checkout,
                target=target,
                repo_full_name=repo_full_name,
                provider_id=change.provider_id,
                commit_sha=current_ref_sha,
            )
            outcome.scan_source = scan_source
            if scan_result is None:
                outcome.skip_reason = SkipReason.NO_REUSABLE_SCAN
                outcome.skip_detail_text = (
                    f"no broad `depfix scan` for {change.provider_id} at "
                    f"{current_ref_sha[:12]} (PLAN_REUSE_SCAN=require). Run: "
                    f"depfix scan --repo {repo_full_name} --provider {change.provider_id}"
                )
                return outcome

            # Repo-scoped path filter applies *before* any fix is
            # generated -- an out-of-scope file shouldn't cost an LLM call
            # just to have its edit discarded later.
            before_actionable = len(scan_result.actionable_sites)
            scan_result = dataclasses.replace(
                scan_result,
                call_sites=[
                    site
                    for site in scan_result.call_sites
                    if repo_config.paths.allows(site.filepath)
                ],
            )
            suppressed_call_sites = before_actionable - len(scan_result.actionable_sites)
            logger.info(
                "scan: %d file(s), %d actionable call site(s)%s",
                scan_result.files_scanned,
                len(scan_result.actionable_sites),
                f", {suppressed_call_sites} path-suppressed" if suppressed_call_sites else "",
            )

            if is_method_removal(change):
                return self._report_removal(
                    session,
                    outcome,
                    change,
                    scan_result,
                    repo_full_name=repo_full_name,
                    current_ref_sha=current_ref_sha,
                    suppressed_call_sites=suppressed_call_sites,
                )

            ecosystem = self._ecosystem_for_package(change)
            single_artifact = self._plan_artifacts.get((repo_full_name, change.dedupe_key))
            replaying_single = single_artifact is not None and not single_artifact.is_upgrade
            upgrade: UpgradePlan | None = None
            if self._settings.upgrade_plans_enabled and not replaying_single:
                try:
                    upgrade = build_upgrade_plan(
                        scan_result,
                        self._provider_changes(change.provider_id),
                        provider_id=change.provider_id,
                        package=change.package,
                        ecosystem=ecosystem,  # type: ignore[arg-type]
                        target_version=(
                            change.new_version
                            if change.kind is ChangeKind.DEPENDENCY_VERSION_BUMP
                            else None
                        ),
                    )
                except Exception:
                    logger.exception("upgrade plan assembly failed for %s", repo_full_name)
                    upgrade = None

            if upgrade is not None and upgrade.has_source_work:
                # Version bump + API migrations inside its window: one combined PR.
                gate = self._gate_upgrade(session, upgrade, repo_full_name, current_ref_sha)
                if gate is not None:
                    outcome.skip_reason, outcome.skip_detail_text = gate
                    outcome.upgrade, outcome.member_dedupe_keys = (
                        upgrade,
                        upgrade.member_dedupe_keys,
                    )
                    return outcome
                carrier = upgrade.drift_change or (
                    upgrade.migrations[0] if upgrade.migrations else change
                )
                upgrade_artifact = self._plan_artifacts.get(
                    (repo_full_name, upgrade.dedupe_key)
                ) or self._plan_artifacts.get((repo_full_name, carrier.dedupe_key))
                if upgrade_artifact is not None and not upgrade_artifact.is_upgrade:
                    upgrade_artifact = None
                return self._process_upgrade(
                    session,
                    owner=owner,
                    name=name,
                    repo_full_name=repo_full_name,
                    installation_id=installation_id,
                    checkout=checkout,
                    full_scan=scan_result,
                    plan=upgrade,
                    repo_config=repo_config,
                    base_branch=base_branch,
                    current_ref_sha=current_ref_sha,
                    artifact=upgrade_artifact,
                    outcome=outcome,
                )
            if upgrade is not None:
                # Drift with no API migrations in its window falls through to the
                # deterministic manifest-bump path below.
                outcome.removals = upgrade.removals

            from depfix.scanners.matching import ScanMatchStatus, assess_scan_change

            assessment = assess_scan_change(scan_result, change, provider_id=change.provider_id)
            if assessment.status is ScanMatchStatus.VERSION_NOT_AFFECTED:
                logger.info(
                    "%s does not apply to %s: %s",
                    change.dedupe_key[:12],
                    repo_full_name,
                    assessment.reason,
                )
                if not self._dry_run:
                    touch_recheck_on_change(
                        session,
                        repo_id=repo_full_name,
                        dedupe_key=change.dedupe_key,
                        status=AttemptStatus.VERSION_NOT_AFFECTED,
                        ref_sha=current_ref_sha,
                    )
                outcome.attempt_status = AttemptStatus.VERSION_NOT_AFFECTED
                outcome.detail = assessment.reason
                outcome.suppressed_call_sites = suppressed_call_sites
                outcome.planned = self._dry_run
                return outcome

            if assessment.status is ScanMatchStatus.CURRENT:
                logger.info(
                    "%s: repository already uses the replacement API: %s",
                    change.dedupe_key[:12],
                    assessment.reason,
                )
                if not self._dry_run:
                    touch_recheck_on_change(
                        session,
                        repo_id=repo_full_name,
                        dedupe_key=change.dedupe_key,
                        status=AttemptStatus.VERSION_NOT_AFFECTED,
                        ref_sha=current_ref_sha,
                    )
                outcome.attempt_status = AttemptStatus.VERSION_NOT_AFFECTED
                outcome.detail = assessment.reason
                outcome.suppressed_call_sites = suppressed_call_sites
                outcome.planned = self._dry_run
                return outcome

            if (
                assessment.status is ScanMatchStatus.NO_CALL_SITES
                and change.kind is not ChangeKind.DEPENDENCY_VERSION_BUMP
                and scan_result.actionable_sites
                and self._completer is not None
            ):
                provider = self._providers.get(change.provider_id)  # type: ignore[assignment]
                package = (
                    provider.sdk_packages[0].name
                    if provider and provider.sdk_packages
                    else change.package
                )
                observed = [site.symbol for site in scan_result.actionable_sites]
                discovered = self._mapping_candidates_for(
                    session,
                    provider_id=change.provider_id,
                    commit_sha=scan_result.commit_sha,
                    package=package,
                    observed_symbols=observed,
                )
                if discovered:
                    logger.info(
                        "%d feed-proven candidate(s) discovered for %s",
                        len(discovered),
                        repo_full_name,
                    )
                    for disc_change in discovered:
                        reassessment = assess_scan_change(
                            scan_result, disc_change, provider_id=disc_change.provider_id
                        )
                        if reassessment.status is ScanMatchStatus.ACTIONABLE:
                            assessment = reassessment
                            change = disc_change
                            break

            if (
                assessment.status is ScanMatchStatus.NO_CALL_SITES
                and change.kind is not ChangeKind.DEPENDENCY_VERSION_BUMP
            ):
                if not self._dry_run:
                    touch_recheck_on_change(
                        session,
                        repo_id=repo_full_name,
                        dedupe_key=change.dedupe_key,
                        status=AttemptStatus.NO_CALL_SITES,
                        ref_sha=current_ref_sha,
                    )
                outcome.attempt_status = AttemptStatus.NO_CALL_SITES
                outcome.suppressed_call_sites = suppressed_call_sites
                outcome.detail = (
                    f"no call sites match {change.old_api!r}"
                    if scan_result.actionable_sites
                    else "no actionable call sites"
                ) + (f" ({suppressed_call_sites} path-suppressed)" if suppressed_call_sites else "")
                if scan_result.raw_http_only:
                    outcome.detail += (
                        "; this repo calls the provider over raw HTTP with no SDK package "
                        "in its manifest, so there is no version to anchor a plan against"
                    )
                outcome.planned = self._dry_run
                return outcome

            # Filter scan_result to only the sites that matched this change so
            # the fixer does not spend tokens on RAW_HTTP/API_VERSION_PIN sites
            # or judge-promoted anchors that are not part of the actionable set.
            if (
                assessment.status is ScanMatchStatus.ACTIONABLE
                and change.kind is not ChangeKind.DEPENDENCY_VERSION_BUMP
            ):
                scan_result = dataclasses.replace(
                    scan_result,
                    call_sites=[
                        *assessment.matched_sites,
                        *(s for s in scan_result.call_sites if not s.is_actionable),
                    ],
                )

            # Plan-time impact check: before any fix is generated, measure
            # whether the change actually breaks this repo's own tests when
            # the dependency is bumped -- evidence for the plan output, not
            # a gate (mocked suites passing proves nothing; see
            # depfix.verify.impact's docstring).
            if self._dry_run and self._settings.plan_impact_check_enabled:
                from depfix.scanners.repo import detect_repo_ecosystem
                from depfix.verify.impact import run_impact_check

                impact_ecosystem = detect_repo_ecosystem(checkout.path)
                if impact_ecosystem == "typescript":
                    impact_ecosystem = "javascript"
                try:
                    impact = run_impact_check(
                        checkout,
                        change,
                        install_timeout=self._settings.verify_install_timeout,
                        test_timeout=self._settings.verify_test_timeout,
                        ignore_scripts=self._settings.verify_ignore_scripts,
                        max_output_bytes=self._settings.verify_max_output_bytes,
                        ecosystem=impact_ecosystem,
                        max_seconds=self._settings.impact_check_max_seconds or None,
                    )
                    outcome.impact_summary = impact.summary()
                except Exception:
                    logger.exception("impact check failed for %s", change.dedupe_key[:12])
                    outcome.impact_summary = "impact check errored; see logs"

            artifact = self._plan_artifacts.get((repo_full_name, change.dedupe_key))
            plan_digest = ""
            if artifact is not None:
                if artifact.base_sha != current_ref_sha:
                    raise ValueError(
                        "plan artifact is stale: repository HEAD changed since plan; run plan again"
                    )
                plan_digest = artifact.digest
                fix_service_result = self._apply_plan_artifact(
                    checkout, scan_result, change, artifact
                )
            else:
                logger.info("generating and verifying a fix for %s", change.dedupe_key[:12])
                fix_service_result = self._run_fix_pipeline(
                    checkout, scan_result, change, repo_config
                )
            fix_result = fix_service_result.pipeline_result
            outcome.cost_usd = fix_result.total_cost
            outcome.suppressed_call_sites = suppressed_call_sites
            from depfix.verify.severity import severity_from_verification

            outcome.severity, outcome.severity_evidence = severity_from_verification(
                change, fix_result.verification, scan_result.actionable_sites
            )

            committable = fix_service_result.committable
            if self._dry_run:
                if change.kind is ChangeKind.DEPENDENCY_VERSION_BUMP:
                    return self._plan_drift(
                        outcome,
                        repo_full_name,
                        current_ref_sha,
                        fix_result,
                        committable,
                        repo_config,
                    )
                if not committable:
                    from depfix.core.explain import format_rejections

                    outcome.attempt_status = AttemptStatus.NO_KEPT_EDITS
                    outcome.detail = format_rejections(fix_result)
                    outcome.planned = True
                    return outcome
                outcome.attempt_status = (
                    AttemptStatus.PR_OPENED if repo_config.open_pr else AttemptStatus.FIXED_NO_PR
                )
                outcome.plan_artifact = PlanArtifact.from_result(
                    repo_full_name,
                    current_ref_sha,
                    fix_result,
                    edits=committable,
                    impact_summary=outcome.impact_summary,
                    scan_source=outcome.scan_source,
                )
                paths = ", ".join(edit.relpath for edit in committable)
                outcome.detail = f"[dry-run] verified {len(committable)} file(s): {paths}"
                if outcome.impact_summary:
                    outcome.detail = f"{outcome.detail}\n{outcome.impact_summary}"
                outcome.planned = True
                return outcome

            # --no-pr forces this decision to False for the whole run,
            # regardless of what this repo's own .depfix.yml says -- see
            # `self._no_pr`.
            effective_open_pr = repo_config.open_pr and not self._no_pr
            if effective_open_pr:
                if not committable:
                    from depfix.core.explain import format_rejections

                    detail = format_rejections(fix_result)
                    record_change_attempt(
                        session,
                        repo_id=repo_full_name,
                        dedupe_key=change.dedupe_key,
                        status=AttemptStatus.NO_KEPT_EDITS,
                        last_error=detail,
                        ref_sha=current_ref_sha,
                    )
                    outcome.attempt_status = AttemptStatus.NO_KEPT_EDITS
                    outcome.detail = detail
                    return outcome

                pr = self._open_pr(
                    owner=owner,
                    name=name,
                    installation_id=installation_id,
                    base_branch=base_branch,
                    change=change,
                    fix_result=fix_result,
                    edits=committable,
                    drift_review=_is_major_dependency_review(change, self._settings),
                    draft=(
                        repo_config.draft_pr
                        or _is_major_dependency_review(change, self._settings)
                        or fix_service_result.cross_major_drift
                    ),
                    escalated=fix_service_result.escalated,
                    plan_digest=plan_digest,
                )
                # Bound for the rest of this change's log lines -- e.g. the
                # ledger writes below -- now that a PR actually exists to
                # point at.
                with log_context(pr_url=pr.html_url):
                    run_row = record_fix_run(
                        session,
                        repo_full_name=repo_full_name,
                        owner=owner,
                        name=name,
                        result=fix_result,
                        installation_id=installation_id,
                        default_branch=base_branch,
                        escalated=fix_service_result.escalated,
                    )
                    record_pull_request(session, run_row, pr)
                    session.flush()  # assign run_row.id before it's read below
                    record_change_attempt(
                        session,
                        repo_id=repo_full_name,
                        dedupe_key=change.dedupe_key,
                        status=AttemptStatus.PR_OPENED,
                        fix_run_id=run_row.id,
                        ref_sha=current_ref_sha,
                    )
                outcome.attempt_status = AttemptStatus.PR_OPENED
                outcome.pr_url = pr.html_url
                return outcome

            run_row = record_fix_run(
                session,
                repo_full_name=repo_full_name,
                owner=owner,
                name=name,
                result=fix_result,
                installation_id=installation_id,
                default_branch=base_branch,
                escalated=fix_service_result.escalated,
            )
            session.flush()  # assign run_row.id before it's read below
            record_change_attempt(
                session,
                repo_id=repo_full_name,
                dedupe_key=change.dedupe_key,
                status=AttemptStatus.FIXED_NO_PR,
                fix_run_id=run_row.id,
                ref_sha=current_ref_sha,
            )
            outcome.attempt_status = AttemptStatus.FIXED_NO_PR
            return outcome
        except Exception as exc:
            logger.exception("processing %s for %s failed", change.dedupe_key, repo_full_name)
            if not self._dry_run:
                record_change_attempt(
                    session,
                    repo_id=repo_full_name,
                    dedupe_key=change.dedupe_key,
                    status=AttemptStatus.FAILED,
                    last_error=str(exc),
                    ref_sha=current_ref_sha,
                )
            outcome.attempt_status = AttemptStatus.FAILED
            outcome.detail = str(exc)
            outcome.planned = self._dry_run
            return outcome
        finally:
            if checkout is not None:
                self._clone_service.cleanup(checkout)

    def _target_version_age_hours(self, change: BreakingChange) -> float | None:
        """How old the version this change would adopt is, in hours.

        Best-effort by construction: any failure returns ``None``, which the
        policy reads as "old enough". One registry GET per distinct target
        version per run (not per repo).
        """
        if self._settings.version_cooldown_hours <= 0 or not change.new_version:
            return None

        key = (change.package, change.new_version)
        if key in self._version_age_cache:
            return self._version_age_cache[key]

        ecosystem = self._ecosystem_for_package(change)
        age: float | None = None
        if ecosystem is not None:
            import httpx

            from depfix.sources.release_age import version_age_hours, version_published_at

            try:
                with httpx.Client(timeout=self._settings.http_timeout) as http:
                    age = version_age_hours(
                        version_published_at(
                            http,
                            ecosystem=ecosystem,
                            package=change.package,
                            version=change.new_version,
                            npm_registry_url=self._settings.npm_registry_url,
                            pypi_registry_url=self._settings.pypi_registry_url,
                        )
                    )
            except Exception:
                logger.debug("release-age lookup failed for %s", key, exc_info=True)
                age = None

        self._version_age_cache[key] = age
        return age

    def _report_removal(
        self,
        session: Session,
        outcome: ChangeOutcome,
        change: BreakingChange,
        scan_result: RepoScanResult,
        *,
        repo_full_name: str,
        current_ref_sha: str,
        suppressed_call_sites: int,
    ) -> ChangeOutcome:
        """An upstream method removal with no replacement: information only.

        No fix is generated and no branch or PR is created. The reachable call
        sites are reported, and the ledger is rechecked only when HEAD moves.
        """
        notices = collect_removal_notices(
            scan_result,
            [change],
            provider_id=change.provider_id,
            package=change.package or None,
            include_unreachable=True,
        )
        sites = [site for notice in notices for site in notice.sites]
        if not self._dry_run:
            touch_recheck_on_change(
                session,
                repo_id=repo_full_name,
                dedupe_key=change.dedupe_key,
                status=AttemptStatus.REPORT_ONLY,
                ref_sha=current_ref_sha,
            )
        shown = ", ".join(sites[:5]) + (f" (+{len(sites) - 5} more)" if len(sites) > 5 else "")
        outcome.removals = notices
        outcome.suppressed_call_sites = suppressed_call_sites
        outcome.attempt_status = AttemptStatus.REPORT_ONLY
        outcome.planned = self._dry_run
        outcome.detail = (
            f"{change.old_api} was removed in {change.package} {change.new_version or '?'} "
            f"with no replacement; "
            + (
                f"{len(sites)} call site(s) still use it: {shown}"
                if sites
                else "no reachable call site"
            )
            + " -- information only, no PR"
        )
        return outcome

    def _ecosystem_for_package(self, change: BreakingChange) -> str | None:
        """Which registry this change's package lives in, per providers.yaml."""
        provider = self._providers.get(change.provider_id)
        if provider is None:
            return None
        for sdk in provider.sdk_packages:
            if sdk.name == change.package:
                return sdk.ecosystem
        return None

    def _provider_changes(self, provider_id: str) -> list[BreakingChange]:
        """Every classified change for a provider, run-cached."""
        if provider_id not in self._provider_change_cache:
            pool = {c.dedupe_key: c for c in self._run_changes if c.provider_id == provider_id}
            try:
                from depfix.core.shortcuts import all_changes_for_provider

                for change in all_changes_for_provider(provider_id):
                    pool.setdefault(change.dedupe_key, change)
            except Exception:
                logger.debug("could not load changes for %s", provider_id, exc_info=True)
            self._provider_change_cache[provider_id] = list(pool.values())
        return self._provider_change_cache[provider_id]

    def _version_age_hours_for(self, package: str, version: str) -> float | None:
        if self._settings.version_cooldown_hours <= 0 or not version:
            return None
        key = (package, version)
        if key in self._version_age_cache:
            return self._version_age_cache[key]
        ecosystem = next(
            (
                sdk.ecosystem
                for p in self._providers.values()
                for sdk in p.sdk_packages
                if sdk.name == package
            ),
            None,
        )
        age: float | None = None
        if ecosystem is not None:
            import httpx

            from depfix.sources.release_age import version_age_hours, version_published_at

            try:
                with httpx.Client(timeout=self._settings.http_timeout) as http:
                    age = version_age_hours(
                        version_published_at(
                            http,
                            ecosystem=ecosystem,
                            package=package,
                            version=version,
                            npm_registry_url=self._settings.npm_registry_url,
                            pypi_registry_url=self._settings.pypi_registry_url,
                        )
                    )
            except Exception:
                logger.debug("release-age lookup failed for %s", key, exc_info=True)
        self._version_age_cache[key] = age
        return age

    def _gate_upgrade(
        self, session: Session, plan: UpgradePlan, repo_full_name: str, ref_sha: str
    ) -> tuple[SkipReason, str] | None:
        """Checks that only become possible once the upgrade's members are known."""
        if len(plan.migrations) > self._settings.upgrade_max_migrations:
            return (
                SkipReason.MAX_CHANGES_PER_RUN_REACHED,
                f"{len(plan.migrations)} migrations in the "
                f"{plan.installed_version or '?'} → {plan.target_version} window exceeds "
                f"upgrade_max_migrations={self._settings.upgrade_max_migrations}; "
                "this span needs human review, not an automated PR",
            )

        if not self._force:
            attempt = worst_attempt_across(
                session, repo_id=repo_full_name, dedupe_keys=plan.member_dedupe_keys
            )
            if attempt is not None and AttemptStatus(attempt.status).is_terminal:
                return (
                    SkipReason.ALREADY_TERMINAL,
                    f"a member of this upgrade is already {attempt.status}; use --force",
                )
            if (
                attempt is not None
                and attempt.attempts_used >= self._settings.pipeline_max_attempts
            ):
                return (
                    SkipReason.MAX_ATTEMPTS_REACHED,
                    f"{attempt.attempts_used}/{self._settings.pipeline_max_attempts} "
                    "attempts used across this upgrade's members; use --force",
                )

            age = self._version_age_hours_for(plan.package, plan.target_version)
            cooldown = self._settings.version_cooldown_hours
            if cooldown > 0 and age is not None and age < cooldown:
                return (
                    SkipReason.VERSION_TOO_NEW,
                    f"{plan.package}@{plan.target_version} is {age:.0f}h old, under the "
                    f"{cooldown:.0f}h cooldown; will proceed once it ages, or use --force",
                )
        return None

    def _as_pipeline_result(
        self, plan: UpgradePlan, result: UpgradeResult, scan: RepoScanResult
    ) -> FixPipelineResult:
        """Adapt an upgrade to the row shape ``record_fix_run`` already persists."""
        carrier = plan.drift_change or plan.migrations[0]
        return FixPipelineResult(
            repo_full_name=scan.repo_full_name,
            breaking_change=carrier,
            files_scanned=scan.files_scanned,
            files_affected=len({e.relpath for e in result.committable}),
            edits=tuple(result.edits),
            total_usages_fixed=sum(e.usages_fixed for e in result.committable),
            total_cost=result.total_cost,
            total_tokens=result.total_tokens,
            duration_ms=0,
            verification=result.verification,  # type: ignore[arg-type]
            codemod_notes=tuple(result.notes),
        )

    def _upgrade_severity(
        self, plan: UpgradePlan, result: UpgradeResult
    ) -> tuple[Severity, EvidenceKind]:
        """Determine severity for an upgrade outcome."""
        from depfix.verify.verifier import VerificationReport

        if isinstance(result.verification, VerificationReport):
            if result.verification.new_failure_count > 0:
                return Severity.CRITICAL, EvidenceKind.TEST_FAILURE
            if result.verification.typechecked and result.verification.new_diagnostics:
                return Severity.CRITICAL, EvidenceKind.TYPE_ERROR
        if result.committable and plan.migrations:
            return Severity.HIGH, EvidenceKind.STRUCTURAL
        return Severity.MEDIUM, EvidenceKind.STRUCTURAL

    def _open_upgrade_pr(
        self,
        *,
        owner,
        name,
        installation_id,
        base_branch,
        plan,
        result,
        draft,
        plan_digest: str = "",
    ) -> PullRequest:
        write_token = self._gh_auth.installation_token(
            installation_id,
            repositories=(name,),
            permissions={"contents": "write", "pull_requests": "write"},
        )
        head_branch = branch_name_for_key(plan.dedupe_key)
        with BranchWriter(
            write_token.token, owner, name, api_url=self._settings.github_api_url
        ) as writer:
            writer.commit_edits(
                branch=head_branch,
                base_branch=base_branch,
                message=(
                    f"depfix: upgrade {plan.package} "
                    f"{plan.installed_version or '?'} -> {plan.target_version}"
                ),
                edits=result.committable,
            )

        pr = open_pull_request(
            owner=owner,
            repo=name,
            token=write_token.token,
            head_branch=head_branch,
            base_branch=base_branch,
            title=plan.title,
            body=build_upgrade_pr_body(
                PlanArtifact.from_upgrade(f"{owner}/{name}", "", result),
                plan_digest=plan_digest,
            ),
            draft=draft,
            api_url=self._settings.github_api_url,
        )

        from depfix.gh import apply_pr_labels

        labels = ["depfix:upgrade", f"depfix:confidence={result.confidence.value}"]
        if any(n.reachable for n in plan.removals):
            labels.append("depfix:api-removal")
        apply_pr_labels(
            owner=owner,
            repo=name,
            pr_number=pr.number,
            labels=labels,
            token=write_token.token,
            api_url=self._settings.github_api_url,
        )
        return pr

    def _replay_upgrade(self, checkout, scan, plan, artifact) -> UpgradeResult:
        """Write a reviewed artifact's edits back verbatim. No model is called."""
        from depfix.apply.models import EditOrigin

        editor = WorkspaceEditor(checkout)
        edits: list[FileEdit] = []
        for planned in artifact.edits:
            path = checkout.path / planned.relpath
            if path.read_text(encoding="utf-8") != planned.original_content:
                raise ValueError(
                    f"plan artifact is stale: {planned.relpath} no longer matches its "
                    "planned base; run `depfix plan` again"
                )
            edit = editor.write_fix(
                planned.relpath,
                planned.fixed_content,
                confidence=planned.confidence,
                usages_fixed=planned.usages_fixed,
            )
            edits.append(
                dataclasses.replace(
                    edit,
                    verdict=EditVerdict.KEPT,
                    diff=planned.diff,
                    origin=EditOrigin.PLAN,
                    derivation_trace=(
                        f"Replay from reviewed upgrade artifact "
                        f"({artifact.digest[:19]}). No LLM call."
                    ),
                )
            )
        return UpgradeResult(
            plan=plan,
            edits=edits,
            steps=list(artifact.steps),
            committable=edits,
            confidence=ConfidenceTier(artifact.confidence),
            confidence_reason=artifact.verified_by,
            notes=list(artifact.notes),
        )

    def _process_upgrade(
        self,
        session: Session,
        *,
        owner,
        name,
        repo_full_name,
        installation_id,
        checkout,
        full_scan,
        plan,
        repo_config,
        base_branch,
        current_ref_sha,
        artifact,
        outcome,
    ) -> ChangeOutcome:
        outcome.upgrade = plan
        outcome.member_dedupe_keys = plan.member_dedupe_keys
        outcome.removals = plan.removals

        with log_context(upgrade=f"{plan.package}@{plan.installed_version}->{plan.target_version}"):
            logger.info("composing upgrade: %s", plan.title)

            if artifact is not None and artifact.is_upgrade:
                result = self._replay_upgrade(checkout, full_scan, plan, artifact)
                plan_digest = artifact.digest
            else:
                result = run_upgrade(
                    checkout=checkout,
                    scan_result=full_scan,
                    plan=plan,
                    settings=self._settings,
                    repo_config=repo_config,
                    fixer_factory=self._fixer_factory,
                    ledger=self._ledger,
                )
                plan_digest = ""
            outcome.cost_usd = result.total_cost
            outcome.severity, outcome.severity_evidence = self._upgrade_severity(plan, result)

            if not result.committable:
                detail = (
                    result.rejected_reason
                    or "; ".join(result.notes[:3])
                    or ("the upgrade produced no committable edits")
                )
                if not self._dry_run:
                    for key in plan.member_dedupe_keys:
                        record_change_attempt(
                            session,
                            repo_id=repo_full_name,
                            dedupe_key=key,
                            status=AttemptStatus.NO_KEPT_EDITS,
                            last_error=detail,
                            ref_sha=current_ref_sha,
                        )
                outcome.attempt_status = AttemptStatus.NO_KEPT_EDITS
                outcome.detail = detail
                outcome.planned = self._dry_run
                return outcome

            if self._dry_run:
                outcome.plan_artifact = PlanArtifact.from_upgrade(
                    repo_full_name, current_ref_sha, result, scan_source=outcome.scan_source
                )
                outcome.attempt_status = (
                    AttemptStatus.PR_OPENED if repo_config.open_pr else AttemptStatus.FIXED_NO_PR
                )
                outcome.detail = (
                    f"[dry-run] {plan.title}: {len(result.committable)} file(s) across "
                    f"{len(result.steps)} step(s)"
                )
                outcome.planned = True
                return outcome

            pipeline_result = self._as_pipeline_result(plan, result, full_scan)
            effective_open_pr = repo_config.open_pr and not self._no_pr

            if not effective_open_pr:
                run_row = record_fix_run(
                    session,
                    repo_full_name=repo_full_name,
                    owner=owner,
                    name=name,
                    result=pipeline_result,
                    installation_id=installation_id,
                    default_branch=base_branch,
                    cost_ledger=self._ledger,
                )
                session.flush()
                for key in plan.member_dedupe_keys:
                    record_change_attempt(
                        session,
                        repo_id=repo_full_name,
                        dedupe_key=key,
                        status=AttemptStatus.FIXED_NO_PR,
                        fix_run_id=run_row.id,
                        ref_sha=current_ref_sha,
                    )
                outcome.attempt_status = AttemptStatus.FIXED_NO_PR
                outcome.detail = plan.title
                return outcome

            pr = self._open_upgrade_pr(
                owner=owner,
                name=name,
                installation_id=installation_id,
                base_branch=base_branch,
                plan=plan,
                result=result,
                draft=(
                    repo_config.draft_pr
                    or plan.needs_draft
                    or not result.confidence.at_least(  # type: ignore[attr-defined]
                        ConfidenceTier(self._settings.draft_below_confidence)
                    )
                ),
                plan_digest=plan_digest,
            )
            with log_context(pr_url=pr.html_url):
                run_row = record_fix_run(
                    session,
                    repo_full_name=repo_full_name,
                    owner=owner,
                    name=name,
                    result=pipeline_result,
                    installation_id=installation_id,
                    default_branch=base_branch,
                    cost_ledger=self._ledger,
                )
                record_pull_request(session, run_row, pr)
                session.flush()
                for key in plan.member_dedupe_keys:
                    record_change_attempt(
                        session,
                        repo_id=repo_full_name,
                        dedupe_key=key,
                        status=AttemptStatus.PR_OPENED,
                        fix_run_id=run_row.id,
                        ref_sha=current_ref_sha,
                    )
            outcome.attempt_status = AttemptStatus.PR_OPENED
            outcome.pr_url = pr.html_url
            outcome.detail = plan.title
            return outcome

    def _plan_drift(
        self,
        outcome: ChangeOutcome,
        repo_full_name: str,
        ref_sha: str,
        fix_result: FixPipelineResult,
        committable: list[FileEdit],
        repo_config: RepoConfig,
    ) -> ChangeOutcome:
        """Every detected version drift gets an artifact, with or without call sites.

        With a rewritable manifest, the artifact carries the manifest/lockfile
        edits and ``apply`` opens a version-bump PR (old -> new). Without one
        (transitive dependency, unsupported manifest, bump reverted by the
        repo's own tests), the artifact is report-only: it is still written and
        listed, and ``apply`` explains why no PR can be opened.
        """
        change = fix_result.breaking_change
        risk = drift_risk(change.old_version, change.new_version) or "unknown"
        span = f"{change.package} {change.old_version} → {change.new_version}"
        from depfix.codemods.lockfile import dependency_drift_policy

        policy = dependency_drift_policy(
            change,
            max_major_gap=self._settings.drift_max_major_gap,
            max_minor_gap=self._settings.drift_max_minor_gap,
            scope=self._settings.drift_scope,
        )
        if policy == "declined":
            # The scope excludes this drift distance -> no PR artifact.
            outcome.attempt_status = AttemptStatus.NO_KEPT_EDITS
            outcome.detail = (
                f"[dry-run] [drift:{risk}] {span}: excluded by "
                f"drift_scope={self._settings.drift_scope}; no PR artifact"
            )
            return outcome

        outcome.plan_artifact = PlanArtifact.from_result(
            repo_full_name,
            ref_sha,
            fix_result,
            edits=committable,
            drift_risk=risk,
            impact_summary=outcome.impact_summary,
            scan_source=outcome.scan_source,
        )
        outcome.planned = True

        if committable:
            outcome.attempt_status = (
                AttemptStatus.PR_OPENED if repo_config.open_pr else AttemptStatus.FIXED_NO_PR
            )
            paths = ", ".join(e.relpath for e in committable)
            outcome.detail = f"[dry-run] [drift:{risk}] {span}: {paths}"
        else:
            from depfix.core.explain import format_rejections

            outcome.attempt_status = AttemptStatus.NO_KEPT_EDITS
            outcome.detail = (
                f"[dry-run] [drift:{risk}] {span}: report-only -- {format_rejections(fix_result)}"
            )
        if outcome.impact_summary:
            outcome.detail = f"{outcome.detail}\n{outcome.impact_summary}"
        return outcome

    def _apply_plan_artifact(
        self,
        checkout: Checkout,
        scan_result: RepoScanResult,
        change: BreakingChange,
        artifact: PlanArtifact,
    ) -> FixServiceResult:
        editor = WorkspaceEditor(checkout)
        edits: list[FileEdit] = []
        for planned in artifact.edits:
            path = checkout.path / planned.relpath
            actual = path.read_text(encoding="utf-8")
            if actual != planned.original_content:
                raise ValueError(
                    f"plan artifact is stale: {planned.relpath} no longer matches its planned base"
                )
            edit = editor.write_fix(
                planned.relpath,
                planned.fixed_content,
                confidence=planned.confidence,
                usages_fixed=planned.usages_fixed,
            )
            edits.append(
                dataclasses.replace(
                    edit,
                    verdict=EditVerdict.KEPT,
                    diff=planned.diff,
                    derivation_trace=(
                        f"Replay from reviewed plan artifact ({artifact.digest[:19]}). "
                        "No LLM call; content matches the plan exactly."
                    ),
                )
            )
        result = FixPipelineResult(
            repo_full_name=scan_result.repo_full_name,
            breaking_change=change,
            files_scanned=scan_result.files_scanned,
            files_affected=len(edits),
            edits=tuple(edits),
            total_usages_fixed=sum(e.usages_fixed for e in edits),
            total_cost=0.0,
            total_tokens=0,
            duration_ms=0,
            verification=None,
            codemod_notes=tuple(artifact.notes),
        )
        return FixServiceResult(pipeline_result=result, committable=edits, verify_required=True)

    def _run_fix_pipeline(
        self,
        checkout: Checkout,
        scan_result: RepoScanResult,
        change: BreakingChange,
        repo_config: RepoConfig,
    ) -> FixServiceResult:
        fixer = self._fixer_factory()
        return run_fix_for_change(
            checkout=checkout,
            scan_result=scan_result,
            change=change,
            settings=self._settings,
            repo_config=repo_config,
            fixer=fixer,
            ledger=self._ledger,
        )

    def _open_pr(
        self,
        *,
        owner: str,
        name: str,
        installation_id: int,
        base_branch: str,
        change: BreakingChange,
        fix_result: FixPipelineResult,
        edits: list[FileEdit],
        draft: bool,
        escalated: bool = False,
        plan_digest: str = "",
        drift_review: bool = False,
    ) -> PullRequest:
        write_token = self._gh_auth.installation_token(
            installation_id,
            repositories=(name,),
            permissions={"contents": "write", "pull_requests": "write"},
        )
        head_branch = branch_name_for(change)
        with BranchWriter(
            write_token.token, owner, name, api_url=self._settings.github_api_url
        ) as writer:
            writer.commit_edits(
                branch=head_branch,
                base_branch=base_branch,
                message=f"depfix: migrate {change.old_api} -> {change.replacement}",
                edits=edits,
            )
        confidence_floor = ConfidenceTier(self._settings.draft_below_confidence)
        auto_draft = not fix_result.confidence.at_least(confidence_floor)
        pr = open_pull_request(
            owner=owner,
            repo=name,
            token=write_token.token,
            head_branch=head_branch,
            base_branch=base_branch,
            title=pr_title_for(change),
            body=build_pr_body(
                change,
                fix_result,
                edits=edits,
                escalated=escalated,
                plan_digest=plan_digest,
                drift_review=drift_review,
            ),
            draft=draft or auto_draft,
            api_url=self._settings.github_api_url,
        )
        from depfix.gh import apply_pr_labels

        apply_pr_labels(
            owner=owner,
            repo=name,
            pr_number=pr.number,
            labels=[f"depfix:confidence={fix_result.confidence.value}"],
            token=write_token.token,
            api_url=self._settings.github_api_url,
        )
        return pr
