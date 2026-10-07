"""Command-line entry point for depfix.

Exposed as the console script ``depfix`` after ``pip install -e .``.

Subcommands
-----------

* ``depfix run [--change-file ... | --from-event ID]`` — run the LLM fixer
  on a codebase for one breaking change (default).
* ``depfix watch [--providers-file ...]`` — poll every provider's change
  feeds and persist any new ``ChangeEvent``s.
* ``depfix classify`` — turn persisted, unclassified change events into
  ``BreakingChangeRow``s (deterministic spec-diff, or LLM release-notes).
* ``depfix eval [--no-llm]`` — score the classify/fixer pipeline against the
  YAML corpus in ``evals/corpus``.
* ``depfix scan (--repo OWNER/NAME | --path DIR) --provider ID`` — find SDK
  call sites in a local checkout or a repo cloned via the GitHub App.
* ``depfix repos`` — list repos visible to the configured GitHub App
  installation(s), useful before pointing ``scan`` at one.
* ``depfix init`` — initialize the local database and run the full machine,
  integration, and toolchain preflight diagnostic.
* ``depfix plan [OWNER/NAME ...]`` — run the repository workflow read-only and
  report the verified edits that would be proposed.
* ``depfix apply [OWNER/NAME ...]`` — run the live repository workflow and open
  pull requests for verified edits.

When invoked without a subcommand (e.g. ``depfix --codebase X``), ``run`` is
assumed for backwards compatibility with the Day 1-2 CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.table import Table
from sqlalchemy import or_, select

from depfix import DependencyFixAgent
from depfix.apply import EditVerdict, FileEdit
from depfix.classify import (
    Classifier,
    GeminiCompleter,
    LLMCompleter,
    OllamaCompleter,
    breaking_change_from_row,
    classify_pending,
    discover_usage_candidates,
)
from depfix.classify.llm import BedrockCompleter, CostTrackingCompleter
from depfix.clone import CloneError, CloneService, RepoTooLargeError
from depfix.config import Settings, get_settings
from depfix.core.fix_service import FixServiceResult, run_fix_for_change
from depfix.core.models import AgentResult, BreakingChange, ChangeKind, ClassificationSource
from depfix.core.pipeline import FixPipelineResult
from depfix.core.shortcuts import (
    all_changes_for_provider,
    capture_repository_dependency_drift,
    ensure_changes_detected,
    latest_change_for_provider,
)
from depfix.evals import (
    CorpusError,
    EvalCategory,
    EvalReport,
    EvalRunner,
    compare_reports,
    load_corpus_dir,
    load_report,
    save_report,
)
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
from depfix.obs.cost import CostLedger
from depfix.obs.logging import configure_logging, configure_sentry
from depfix.orchestrator import ChangeOutcome, Orchestrator, OrchestratorOutcome, PlanArtifact
from depfix.providers.loader import ProviderConfigError, load_providers_file
from depfix.providers.models import ProviderSpec
from depfix.repoconfig.models import SUPPORTED_VERSION, RepoConfig
from depfix.scanners import (
    CallSiteKind,
    CallSiteScanner,
    MatchConfidence,
    RepoScanResult,
    ScanChangeAssessment,
    ScanMatchStatus,
    assess_scan_changes,
    build_scan_target,
    record_scan,
    record_scan_assessments,
    scan_checkout,
    scan_path,
    scan_repo,
)
from depfix.severity import Severity as ChangeSeverity
from depfix.sources.factory import SourceDeps
from depfix.sources.models import Severity
from depfix.storage import (
    AttemptStatus,
    BreakingChangeRow,
    get_engine,
    init_schema,
    record_fix_run,
    record_pull_request,
    reset_sqlite_state,
    session_scope,
)
from depfix.watcher.runner import Watcher, WatchOutcome

console = Console()
logger = logging.getLogger(__name__)

_RUN_COMMANDS = {
    "run",
    "watch",
    "classify",
    "eval",
    "scan",
    "repos",
    "init",
    "plan",
    "apply",
    "status",
    "pr",
}
_TOP_LEVEL_FLAGS = {"-h", "--help", "--log-level", "--log-format"}


def _breaking_change_from_json(data: dict) -> BreakingChange:
    payload = dict(data)
    if "kind" in payload:
        payload["kind"] = ChangeKind(payload["kind"])
    if "source" in payload:
        payload["source"] = ClassificationSource(payload["source"])
    if "examples" in payload:
        payload["examples"] = [tuple(e) for e in payload["examples"]]
    return BreakingChange(**payload)


def _load_breaking_change(args: argparse.Namespace) -> BreakingChange:
    """Resolve the ``BreakingChange`` a `run` invocation should fix.

    There is no hardcoded demo fixture any more (see docs/decisions.md) — the
    caller must point at either a hand-written JSON file or a row that
    ``depfix classify`` already produced.
    """
    if args.change_file:
        data = json.loads(Path(args.change_file).read_text(encoding="utf-8"))
        return _breaking_change_from_json(data)
    if args.from_event is not None:
        with session_scope() as session:
            row = session.get(BreakingChangeRow, args.from_event)
            if row is None:
                raise LookupError(f"no breaking_change row with id={args.from_event}")
            return breaking_change_from_row(row)
    raise ValueError(
        "no breaking change specified: pass --change-file <path>.json or "
        "--from-event <id> (run `depfix classify` first to populate rows)"
    )


def _load_optional_breaking_change(args: argparse.Namespace) -> BreakingChange | None:
    """Like ``_load_breaking_change``, but for a `scan` invocation where
    narrowing to one breaking change's symbols is optional -- a plain
    ``depfix scan --provider X`` with neither flag scans for every symbol
    the provider declares."""
    if not args.change_file and args.from_event is None:
        return None
    return _load_breaking_change(args)


def _scan_location(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Resolve ``scan`` input as ``(local_path, remote_repo)``.

    ``--path`` is the documented local-checkout option.  Treat an explicitly
    path-like ``--repo ./dir`` as a local path too, so older examples do not
    accidentally attempt a GitHub clone named ``./dir``.
    """
    repo_is_local_path = args.repo is not None and args.repo.startswith(("./", "../", "/"))
    local_path = args.path or (args.repo if repo_is_local_path else None)
    remote_repo = None if repo_is_local_path else args.repo
    if bool(local_path) == bool(remote_repo):
        raise ValueError("pass exactly one of --repo OWNER/NAME or --path DIR")
    return local_path, remote_repo


def _scan_provider(
    providers: list[ProviderSpec], provider_id: str | None, package: str | None
) -> ProviderSpec | None:
    """Resolve a scan provider by configured id or SDK package name."""
    if provider_id is not None:
        return next((provider for provider in providers if provider.id == provider_id), None)
    assert package is not None
    matches = [
        provider
        for provider in providers
        if provider.id == package or any(sdk.name == package for sdk in provider.sdk_packages)
    ]
    return matches[0] if len(matches) == 1 else None


def _load_pipeline_changes(
    args: argparse.Namespace,
    settings: Settings,
    *,
    limit: int,
    repo_full_names: list[str] | None = None,
) -> list[BreakingChange]:
    """Resolve the breaking changes a `pipeline` run should consider.

    Explicit ``--change-file``/``--from-event`` inputs are never truncated.
    The normal cron path is bounded and newest-first, so an old backlog does
    not delay urgent changes or make every tick re-evaluate all history.
    """
    changes: list[BreakingChange] = []
    for path in args.change_file or []:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        changes.append(_breaking_change_from_json(data))
    for event_id in args.from_event or []:
        with session_scope() as session:
            row = session.get(BreakingChangeRow, event_id)
            if row is None:
                raise LookupError(f"no breaking_change row with id={event_id}")
            changes.append(breaking_change_from_row(row))
    if changes:
        return changes

    with session_scope() as session:
        order = (
            BreakingChangeRow.created_at.desc()
            if settings.pipeline_newest_first
            else BreakingChangeRow.created_at.asc()
        )
        stmt = select(BreakingChangeRow)
        if repo_full_names:
            # Registry drift is repository-scoped: the old side is the
            # version seen in that repository's lockfile. Never let a
            # Documenso Stripe-12 record become Medusa's plan input merely
            # because it was globally newer in SQLite. Provider-wide API
            # migrations remain eligible for every explicitly named repo.
            from depfix.core.models import ChangeKind
            from depfix.storage import ChangeEventRow

            repo_keys = [
                hashlib.sha256(repo.encode("utf-8")).hexdigest()[:12] for repo in repo_full_names
            ]
            repo_drift = or_(
                *[
                    ChangeEventRow.feed_key.like(f"%:repo:{repo_key}:installed:%")
                    for repo_key in repo_keys
                ]
            )
            stmt = stmt.join(ChangeEventRow).where(
                or_(
                    BreakingChangeRow.kind != ChangeKind.DEPENDENCY_VERSION_BUMP.value,
                    repo_drift,
                )
            )
        stmt = stmt.order_by(order)
        # Pre-cap only when no specific repos are named. When repos are named
        # the orchestrator filters by scan-match relevance before capping, so
        # a provider with 477 classified changes doesn't drown the 5 that
        # actually apply to this repo.
        if not repo_full_names:
            stmt = stmt.limit(limit)
        return [breaking_change_from_row(row) for row in session.scalars(stmt)]


def _load_pipeline_repos(args: argparse.Namespace, gh_auth: GitHubAppAuth) -> list[str]:
    """Resolve the repos a `pipeline` run should process.

    ``--repo`` (repeatable) narrows this to an explicit, ad hoc set -- the
    default is every repo visible to every installation of the configured
    GitHub App, which is the normal cron-driven fleet behavior.
    """
    explicit_repos = [*(args.repos or []), *(args.repo or [])]
    if explicit_repos:
        return explicit_repos
    repo_full_names: list[str] = []
    for installation in gh_auth.list_installations():
        repo_full_names.extend(r.full_name for r in gh_auth.list_repositories(installation.id))
    return repo_full_names


def _build_gh_auth(settings) -> GitHubAppAuth | None:
    """Build a ``GitHubAppAuth`` from settings, or ``None`` if the App isn't
    configured -- callers turn that into an actionable CLI error rather than
    letting a blank app_id/private_key fail deep inside a JWT-signing call."""
    if not settings.github_app_id:
        return None
    private_key = settings.github_app_private_key
    if not private_key and settings.github_app_private_key_path:
        private_key = Path(settings.github_app_private_key_path).read_text(encoding="utf-8")
    if not private_key:
        return None
    return GitHubAppAuth(
        app_id=settings.github_app_id, private_key=private_key, api_url=settings.github_api_url
    )


def _resolve_light_model(settings: Settings) -> tuple[str, str]:
    """Return ``(light_model_id, heavy_model_id)`` for the active provider."""
    if settings.llm_provider == "bedrock":
        heavy = settings.bedrock_model
        light = settings.bedrock_light_model or heavy
        return light, heavy
    if settings.llm_provider == "gemini":
        heavy = settings.gemini_model
        light = settings.gemini_light_model or heavy
        return light, heavy
    heavy = settings.ollama_model
    light = settings.ollama_light_model or heavy
    return light, heavy


def _build_completer(settings, *, heavy: bool = False) -> LLMCompleter | None:
    """Build an LLMCompleter.

    By default returns the **light** model (Haiku-class) for cheap tasks
    like classification, scanning, and judging.  Pass ``heavy=True`` to
    get the main model (Sonnet-class) for code-generation-adjacent work
    like characterization.
    """
    light_id, heavy_id = _resolve_light_model(settings)
    model_id = heavy_id if heavy else light_id
    if settings.llm_provider == "bedrock":
        return BedrockCompleter(
            aws_access_key=settings.bedrock_access_key,
            aws_secret_key=settings.bedrock_secret_key,
            aws_region=settings.bedrock_region,
            aws_profile=settings.bedrock_profile,
            model=model_id,
        )
    if settings.llm_provider == "gemini":
        if not settings.google_api_key:
            return None
        return GeminiCompleter(api_key=settings.google_api_key, model=model_id)
    return OllamaCompleter(
        model=model_id,
        base_url=settings.ollama_base_url,
        timeout=settings.ollama_timeout,
    )


def _build_fixer(settings):
    if settings.llm_provider == "bedrock":
        from depfix.fixers.bedrock import BedrockFixGenerator

        return BedrockFixGenerator(
            aws_access_key=settings.bedrock_access_key,
            aws_secret_key=settings.bedrock_secret_key,
            aws_region=settings.bedrock_region,
            aws_profile=settings.bedrock_profile,
            model=settings.bedrock_model,
        )
    if settings.llm_provider == "gemini":
        if not settings.google_api_key:
            return None
        return FixGenerator(api_key=settings.google_api_key, model=settings.gemini_model)
    return OllamaFixGenerator(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        timeout=settings.ollama_timeout,
    )


def _build_scan_coordinator(settings: Settings, ledger: CostLedger) -> object | None:
    """Build a ScanCoordinator sharing the caller's CostLedger.

    The ledger is passed in, never created here — it must be the SAME
    ledger the command's summary reads, or agent spend records to an
    orphan and TTS shows 0 despite real LLM usage.

    Returns None when scan_strategy is "deterministic" OR when no LLM is
    configured. For "agent" the missing LLM is a hard error; for "hybrid"
    it is a graceful fallback to the deterministic result.
    """
    if settings.scan_strategy not in ("agent", "hybrid"):
        return None
    completer = _build_completer(settings)
    if completer is None:
        if settings.scan_strategy == "agent":
            console.print(
                "[red]Error: SCAN_STRATEGY=agent requires an LLM "
                "(set GOOGLE_API_KEY, LLM_PROVIDER=ollama, or configure Bedrock).[/red]"
            )
        return None
    # Import lazily so the module is not loaded when strategy=deterministic.
    from depfix.classify.llm import CostTrackingCompleter
    from depfix.obs.cost import CostStage

    # Wrap the completer so every agent LLM call bills to SCAN_AGENT stage.
    tracking = CostTrackingCompleter(completer, ledger, CostStage.SCAN_AGENT)
    # Return the completer itself as the "coordinator" — scan_checkout
    # receives it as agent_completer and builds a fresh ScanAgent per call.
    return tracking


def _apply_model_overrides(args: argparse.Namespace, settings: Settings) -> None:
    if getattr(args, "fallback_provider", None):
        settings.llm_fallback_provider = args.fallback_provider
    if getattr(args, "fallback_model", None):
        settings.llm_fallback_model = args.fallback_model
    if getattr(args, "no_codemods", False):
        settings.codemods_enabled = False


def _print_header() -> None:
    console.print(
        Panel.fit(
            "[bold blue]depfix[/bold blue]\n"
            "[dim]Automatically fix breaking dependency changes[/dim]",
            border_style="blue",
        )
    )


def _print_breaking_change(change: BreakingChange) -> None:
    table = Table(title="Breaking Change Details", show_header=False)
    table.add_column("Property", style="cyan")
    table.add_column("Value")
    table.add_row("Package", change.package)
    table.add_row("Version", f"{change.old_version} → {change.new_version}")
    table.add_row("Old API", f"[red]{change.old_api}[/red]")
    table.add_row("New API", f"[green]{change.new_api}[/green]")
    table.add_row("Description", change.description)
    console.print(table)
    console.print()


def _print_results(result: AgentResult) -> None:
    table = Table(title="Fix Results")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Files Scanned", str(result.files_scanned))
    table.add_row("Files Affected", str(result.files_affected))
    table.add_row("Total Usages Fixed", str(result.total_usages_fixed))
    table.add_row("Confidence", f"[bold]{result.confidence.value}[/bold]")  # type: ignore[attr-defined]
    rate_color = "green" if result.success_rate == 1.0 else "yellow"
    table.add_row("Success Rate", f"[{rate_color}]{result.success_rate:.0%}[/{rate_color}]")
    table.add_row("Total Cost", f"${result.total_cost:.4f}")
    table.add_row("Total Tokens", str(result.total_tokens))
    table.add_row("Duration", f"{result.duration_ms}ms")
    console.print(table)
    console.print()

    if result.fixes:
        console.print("[bold]File Details:[/bold]")
        for fix in result.fixes:
            status = "✅" if fix.is_successful else "❌"
            color = (
                "green" if fix.confidence >= 0.9 else "yellow" if fix.confidence >= 0.7 else "red"
            )
            console.print(
                f"  {status} [cyan]{fix.filepath}[/cyan] - "
                f"{fix.usages_fixed} usages - "
                f"confidence: [{color}]{fix.confidence:.2f}[/{color}]"
            )
            if not fix.validation.is_valid:
                console.print(f"      [red]Validation error: {fix.validation.error_message}[/red]")
    console.print()


def _print_diffs(result: AgentResult, show_full: bool) -> None:
    console.print("[bold]Generated Diffs:[/bold]\n")
    for fix in result.fixes:
        if not fix.diff:
            continue
        console.print(f"[cyan]{fix.filepath}[/cyan]")
        if show_full:
            console.print(Syntax(fix.diff, "diff", theme="monokai", line_numbers=True))
        else:
            lines = fix.diff.split("\n")
            preview = "\n".join(lines[:30])
            console.print(Syntax(preview, "diff", theme="monokai"))
            if len(lines) > 30:
                console.print(f"[dim]... ({len(lines) - 30} more lines)[/dim]")
        console.print()


_STATUS_STYLE = {
    "changed": "[green]changed[/green]",
    "baseline": "[blue]baseline[/blue]",
    "unchanged": "[dim]unchanged[/dim]",
    "cached": "[cyan]cached[/cyan]",
    "not_found": "[yellow]not found[/yellow]",
    "unsupported": "[yellow]unsupported[/yellow]",
    "failed": "[red]failed[/red]",
    "skipped": "[dim]skipped[/dim]",
}

_SEVERITY_STYLE = {
    Severity.BREAKING: "[red]BREAKING[/red]",
    Severity.POTENTIALLY_BREAKING: "[yellow]potentially breaking[/yellow]",
    Severity.ADDITIVE: "[green]additive[/green]",
    Severity.UNKNOWN: "[dim]unknown[/dim]",
}


def _print_watch_outcome(outcome: WatchOutcome, show_changes: bool) -> None:
    table = Table(title="Change Feeds")
    table.add_column("Provider", style="cyan")
    table.add_column("Feed")
    table.add_column("Status")
    table.add_column("Detail", style="dim")

    for report in outcome.reports:
        table.add_row(
            report.provider_id,
            report.feed_key if report.feed_key != "—" else f"({report.kind})",
            _STATUS_STYLE.get(report.status, report.status),
            report.detail,
        )
    console.print(table)
    console.print(
        f"[dim]feeds={outcome.feeds_polled}  cached={outcome.feeds_cached}  "
        f"events={len(outcome.events)}  breaking={len(outcome.breaking_events)}  "
        f"duplicates={outcome.duplicates}  failed={len(outcome.failures)}[/dim]\n"
    )

    for event in outcome.events:
        console.print(
            Panel.fit(
                f"[bold]{event.title}[/bold]\n"
                f"{_SEVERITY_STYLE.get(event.severity, '?')}  ·  {event.summary}\n"
                f"[dim]{event.source_url or event.feed_key}[/dim]",
                border_style="red" if event.severity is Severity.BREAKING else "yellow",
            )
        )
        if not event.spec_changes:
            continue
        limit = len(event.spec_changes) if show_changes else 12
        for change in event.spec_changes[:limit]:
            console.print(f"  {change.one_line()}")
        remaining = len(event.spec_changes) - limit
        if remaining > 0:
            console.print(f"  [dim]... {remaining} more (use --show-changes)[/dim]")
        console.print()


_CONFIDENCE_STYLE = {
    MatchConfidence.HIGH: "[green]high[/green]",
    MatchConfidence.MEDIUM: "[yellow]medium[/yellow]",
    MatchConfidence.LOW: "[dim]low[/dim]",
}

_SEVERITY_COLOR = {
    ChangeSeverity.CRITICAL: "red bold",
    ChangeSeverity.HIGH: "red",
    ChangeSeverity.MEDIUM: "yellow",
    ChangeSeverity.LOW: "dim",
    ChangeSeverity.NONE: "dim",
}


def _severity_style(severity: ChangeSeverity) -> str:
    return f"[{_SEVERITY_COLOR[severity]}]{severity.value}[/{_SEVERITY_COLOR[severity]}]"


def _print_scan_result(result: RepoScanResult, *, show_low: bool) -> None:
    sites = result.call_sites if show_low else result.actionable_sites

    table = Table(title=f"Call Sites — {result.repo_full_name or '(local)'}")
    table.add_column("File", style="cyan")
    table.add_column("Line", justify="right")
    table.add_column("Kind")
    table.add_column("Confidence")
    table.add_column("Symbol")
    for site in sorted(sites, key=lambda s: (s.filepath, s.line_number)):
        table.add_row(
            site.filepath,
            str(site.line_number),
            site.kind.value,
            _CONFIDENCE_STYLE.get(site.confidence, site.confidence.value),
            site.symbol,
        )
    console.print(table)
    console.print(
        f"[dim]files scanned:[/dim] {result.files_scanned}  "
        f"[dim]call sites:[/dim] {len(result.call_sites)} "
        f"({len(result.actionable_sites)} actionable)  "
        f"[dim]commit:[/dim] {result.commit_sha or '?'}\n"
    )
    if result.severity is not None:
        evidence = result.severity_evidence.value if result.severity_evidence else "unknown"
        console.print(
            f"[dim]severity:[/dim] {_severity_style(result.severity)} [dim](predicted, {evidence})[/dim]"
        )

    if result.manifest_matches:
        console.print("[bold]Manifest matches:[/bold]")
        for match in result.manifest_matches:
            console.print(f"  {match}")
        console.print()

    if result.raw_http_only:
        console.print(
            "[yellow]This repo calls the provider API via raw HTTP without an SDK package "
            "dependency. Depfix can detect these calls but cannot produce a version-drift "
            "plan without a manifest entry. Consider adopting the official SDK for managed "
            "upgrades.[/yellow]\n"
        )

    for err in result.errors:
        console.print(f"  [yellow]{err}[/yellow]")


def _print_scan_assessments(assessments: list[ScanChangeAssessment]) -> None:
    """Show the commit-specific upstream-change decisions from a scan."""
    if not assessments:
        return

    status_style = {
        ScanMatchStatus.ACTIONABLE: "green",
        ScanMatchStatus.CURRENT: "green",
        ScanMatchStatus.NO_CALL_SITES: "yellow",
        ScanMatchStatus.VERSION_NOT_AFFECTED: "dim",
    }
    table = Table(title="Upstream Change Match")
    table.add_column("Old API", style="cyan")
    table.add_column("New API")
    table.add_column("Decision")
    table.add_column("Evidence", style="dim")
    for assessment in assessments:
        style = status_style[assessment.status]
        table.add_row(
            assessment.change.old_api or "(version drift)",
            assessment.change.new_api or "—",
            f"[{style}]{assessment.status.value}[/{style}]",
            assessment.reason,
        )
    console.print(table)
    console.print()


def _print_dependency_drift_summary(
    result: RepoScanResult, assessments: list[ScanChangeAssessment]
) -> None:
    """Print feed-version context without implying an automatic upgrade."""
    from depfix.scanners.drift_summary import summarize_dependency_drift

    rows = summarize_dependency_drift(result, assessments)
    if not rows:
        return
    table = Table(title="Dependency Drift Summary (feed observation)")
    table.add_column("Package", style="cyan")
    table.add_column("Repository version")
    table.add_column("Feed version")
    table.add_column("Decision")
    table.add_column("Risk if ignored", style="dim")
    for row in rows:
        table.add_row(
            row.package,
            row.current_version,
            row.feed_version,
            row.decision,
            row.risk,
        )
    console.print(table)
    console.print(
        "[dim]A feed version is comparison evidence, not automatic permission to "
        "perform a major upgrade.[/dim]\n"
    )


def _write_scan_report(
    *,
    local_path: str | None,
    remote_repo: str | None,
    result: RepoScanResult,
    assessments: list[ScanChangeAssessment],
    settings: Settings,
) -> None:
    """Persist an operator-requested Markdown report without touching main."""
    from depfix.scanners.report import render_scan_report

    content = render_scan_report(result, assessments)
    relpath = ".depfix/scan-report.md"
    if local_path is not None:
        destination = Path(local_path) / relpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        console.print(f"[green]Scan report:[/green] {destination}")
        return

    assert remote_repo is not None
    owner, name = remote_repo.split("/", 1)
    auth = _build_gh_auth(settings)
    if auth is None:
        console.print("[yellow]Could not write scan report: GitHub App is not configured.[/yellow]")
        return
    try:
        installation = auth.resolve_installation_for_repo(owner, name)
        token = auth.installation_token(
            installation.id, repositories=(name,), permissions={"contents": "write"}
        )
        branch = f"{BRANCH_PREFIX}scan-report-{result.commit_sha[:12]}"
        edit = FileEdit(
            relpath=relpath,
            original_content="",
            fixed_content=content,
            diff="",
            verdict=EditVerdict.KEPT,
            derivation_trace="Depfix scan report",
        )
        with BranchWriter(token.token, owner, name) as writer:
            writer.commit_edits(
                branch=branch,
                base_branch=auth.default_ref(owner, name),
                message="chore(depfix): add scan report",
                edits=[edit],
            )
    except GitHubAppError as exc:
        console.print(f"[yellow]Could not write scan report: {exc}[/yellow]")
        return
    finally:
        auth.close()
    console.print(
        f"[green]Scan report branch:[/green] https://github.com/{remote_repo}/tree/{branch}/{relpath}"
    )


def _record_scan_detections(
    settings: Settings,
    *,
    repo_full_name: str,
    provider_id: str,
    result: RepoScanResult,
    assessments: list[ScanChangeAssessment],
) -> None:
    from depfix.catalog import learned_migrations_file
    from depfix.catalog.detections import (
        build_anchor_detection,
        build_detections,
        record_repository_detections,
    )

    path = learned_migrations_file(settings)
    detections = build_detections(result, assessments, provider_id=provider_id)

    # When the agent discovered old-API anchors but no feed-classified breaking
    # change matched (e.g. feeds only carry recent version bumps), persist a
    # synthetic anchor detection so the fix pipeline can still act on them.
    anchors = getattr(result, "discovered_anchors", []) or []
    if anchors and not detections:
        anchor_det = build_anchor_detection(result, provider_id=provider_id)
        if anchor_det:
            detections = [anchor_det]

    try:
        count = record_repository_detections(
            path,
            repo_full_name=repo_full_name,
            provider_id=provider_id,
            detections=detections,
        )
    except (OSError, yaml.YAMLError) as exc:
        console.print(f"[yellow]Could not list detections in {path}: {exc}[/yellow]")
        return
    if count:
        console.print(f"[green]{count} drift detection(s) listed in {path}[/green]")
    anchors_found = len(anchors)
    api_mapping = getattr(result, "api_mapping", []) or []
    if anchors_found:
        console.print(
            f"[green]{anchors_found} old-API anchor(s) discovered by agent "
            f"({len(api_mapping)} old→new mapping(s)); "
            f"listed in {path}[/green]"
        )


def _record_planned_detection(
    settings: Settings, artifact: PlanArtifact, artifact_path: Path
) -> None:
    from depfix.catalog import learned_migrations_file
    from depfix.catalog.detections import (
        STATUS_PLANNED,
        STATUS_REPORT_ONLY,
        mark_detection_planned,
        mark_detection_status,
    )

    path = learned_migrations_file(settings)
    try:
        mark_detection_planned(
            path,
            repo_full_name=artifact.repo_full_name,
            change=artifact.change,
            commit_sha=artifact.base_sha,
            artifact_path=str(artifact_path),
            files=[e.relpath for e in artifact.edits],
        )
        members = [k for k in artifact.member_dedupe_keys if k != artifact.change_dedupe_key]
        if members:
            mark_detection_status(
                path,
                repo_full_name=artifact.repo_full_name,
                dedupe_keys=members,
                status=STATUS_PLANNED if artifact.edits else STATUS_REPORT_ONLY,
                plan_artifact=str(artifact_path),
            )
    except (OSError, yaml.YAMLError) as exc:
        console.print(f"[yellow]Could not record planned PR in {path}: {exc}[/yellow]")


def _record_applied_detections(settings: Settings, outcome: OrchestratorOutcome) -> None:
    from depfix.catalog import learned_migrations_file
    from depfix.catalog.detections import STATUS_PR_OPENED, mark_detection_status

    path = learned_migrations_file(settings)
    for repo in outcome.repos:
        for change in repo.changes:
            if change.pr_url:
                mark_detection_status(
                    path,
                    repo_full_name=repo.repo_full_name,
                    dedupe_keys=change.member_dedupe_keys or (change.dedupe_key,),
                    status=STATUS_PR_OPENED,
                    pr_url=change.pr_url,
                )


def _outcome_text(change: ChangeOutcome) -> str:
    if change.skip_reason is not None:
        if change.skip_detail_text:
            return f"[dim]skip: {change.skip_detail_text}[/dim]"
        return f"[dim]skip: {change.skip_reason.value}[/dim]"
    if change.attempt_status == AttemptStatus.PR_OPENED:
        return "[green]pr_opened[/green]"
    if change.attempt_status == AttemptStatus.FAILED:
        return "[red]failed[/red]"
    if change.attempt_status is not None:
        return change.attempt_status.value
    return "?"


def _print_orchestrator_outcome(outcome: OrchestratorOutcome, *, show_skips: bool = False) -> None:
    """Print actions, not an unbounded matrix of fleet skips."""
    console.print(f"Fleet pipeline run {outcome.run_id}")
    skip_tally: Counter[str] = Counter()
    for repo in outcome.repos:
        if repo.skip_reason is not None:
            skip_tally[repo.skip_reason.value] += 1
            if show_skips:
                console.print(f"  {repo.repo_full_name}: skipped ({repo.skip_reason.value})")
            continue
        if repo.error:
            console.print(f"  [red]{repo.repo_full_name}: ERROR {repo.error}[/red]")
            continue
        acted = [change for change in repo.changes if not change.skipped]
        for change in repo.changes:
            if change.skipped:
                assert change.skip_reason is not None
                skip_tally[change.skip_reason.value] += 1
        if not acted:
            continue
        console.print(f"  {repo.repo_full_name} ({repo.open_pr_count} open depfix PR(s)):")
        for change in acted:
            if change.upgrade is not None:
                plan = change.upgrade
                verb = (
                    "would upgrade"
                    if change.planned
                    and change.attempt_status
                    in {AttemptStatus.PR_OPENED, AttemptStatus.FIXED_NO_PR}
                    else _outcome_text(change)
                )
                console.print(
                    f"    {plan.package} {plan.installed_version or '?'} → {plan.target_version}: "  # type: ignore[attr-defined]
                    f"{verb} [{len(plan.migrations)} migration(s), "  # type: ignore[attr-defined]
                    f"{len(change.member_dedupe_keys)} change(s)]"
                    f"{change.pr_url or (f' -- {change.detail}' if change.detail else '')}"
                )
                continue
            # A dry-run predicts the *actual* terminal outcome. Do not hide
            # "no kept edits" or "no call sites" behind a vague success-like
            # message merely because no state was written.
            verb = (
                "would process"
                if change.planned
                and change.attempt_status
                in {
                    AttemptStatus.PR_OPENED,
                    AttemptStatus.FIXED_NO_PR,
                }
                else _outcome_text(change)
            )
            suffix = change.pr_url or (f" -- {change.detail}" if change.detail else "")
            suppressed = (
                f" [{change.suppressed_call_sites} path-suppressed]"
                if change.suppressed_call_sites
                else ""
            )
            source = f" [dim](scan: {change.scan_source})[/dim]" if change.scan_source else ""
            console.print(
                f"    {change.package} {change.old_api}: {verb}{suppressed}{suffix}{source}"
            )
    console.print(
        f"\n{outcome.prs_opened} PR(s), {outcome.changes_failed} failed change(s), "
        f"${outcome.total_cost_usd:.4f} spent"
    )
    if outcome.merge_rate and outcome.merge_rate["total"]:
        console.print(
            f"historical merge rate: {outcome.merge_rate['merged']}/{outcome.merge_rate['total']} "
            f"({float(outcome.merge_rate['rate'] or 0):.0%})"
        )
    if skip_tally and not show_skips:
        summary = ", ".join(f"{count}x {reason}" for reason, count in skip_tally.most_common())
        console.print(f"skipped: {summary} (--show-skips for detail)")
    if outcome.stopped_early:
        console.print("STOPPED EARLY: budget exhausted -- remaining work resumes next run")


def _cmd_watch(args: argparse.Namespace) -> int:
    settings = get_settings()
    providers_file = args.providers_file or settings.providers_file

    try:
        providers = load_providers_file(providers_file)
    except (FileNotFoundError, ProviderConfigError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 2

    if args.provider:
        wanted = set(args.provider)
        unknown = wanted - {p.id for p in providers}
        if unknown:
            console.print(f"[red]Unknown provider id(s): {', '.join(sorted(unknown))}[/red]")
            return 2
        providers = [p for p in providers if p.id in wanted]

    _print_header()
    feed_count = sum(len(p.feeds) for p in providers)
    console.print(
        f"[dim]Providers:[/dim] {len(providers)}  "
        f"[dim]Feeds:[/dim] {feed_count}  "
        f"[dim]Mode:[/dim] {'dry-run' if args.dry_run else 'persist'}\n"
    )

    try:
        with SourceDeps.from_settings() as deps:
            outcome = Watcher(
                deps=deps,
                dry_run=args.dry_run,
                ttl_seconds=0 if args.refresh else None,
            ).run_once(providers)
    except Exception as e:
        console.print(f"[red]Watch failed: {e}[/red]")
        logger.exception("Watcher crashed")
        return 1

    _print_watch_outcome(outcome, show_changes=args.show_changes)

    # Exit non-zero only if every enabled feed is dark. A cached feed was
    # healthy on its last poll, so it counts as alive.
    return 1 if outcome.all_failed else 0


def _cmd_classify(args: argparse.Namespace) -> int:
    settings = get_settings()
    ledger = CostLedger()

    completer: LLMCompleter | None = None
    if not args.no_llm:
        completer = _build_completer(settings)
        if completer is None:
            console.print(
                "[yellow]No LLM configured (GOOGLE_API_KEY unset for provider=gemini); "
                "only events with spec_changes will classify. Pass --no-llm to silence "
                "this warning.[/yellow]"
            )
        elif completer is not None:
            completer = CostTrackingCompleter(completer, ledger)

    classifier = Classifier(completer, min_confidence=settings.classify_min_confidence)

    _print_header()
    try:
        init_schema()
        with session_scope() as session:
            summary = classify_pending(session, classifier, limit=args.limit)
    except Exception as e:
        console.print(f"[red]Classify failed: {e}[/red]")
        logger.exception("Classifier crashed")
        return 1

    console.print(
        f"[dim]events processed:[/dim] {summary.events_processed}  "
        f"[dim]breaking changes created:[/dim] {summary.changes_created}  "
        f"[dim]unclassifiable:[/dim] {summary.unclassifiable}  "
        f"[dim]errors:[/dim] {len(summary.errors)}\n"
    )
    if summary.created_rows:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for row in summary.created_rows:
            change = breaking_change_from_row(row)
            path = output_dir / f"{row.id}.json"
            path.write_text(
                json.dumps(
                    {
                        "id": row.id,
                        "package": change.package,
                        "old_version": change.old_version,
                        "new_version": change.new_version,
                        "old_api": change.old_api,
                        "new_api": change.new_api,
                        "description": change.description,
                        "migration_guide": change.migration_guide,
                        "kind": change.kind.value,
                        "source": change.source.value,
                        "provider_id": change.provider_id,
                        "source_url": change.source_url,
                        "evidence": change.evidence,
                        "confidence": change.confidence,
                        "call_site_hints": change.call_site_hints,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            console.print(f"  [green]breaking change {row.id}:[/green] {path}")
    for err in summary.errors:
        console.print(f"  [yellow]{err}[/yellow]")
    if ledger.by_stage:
        console.print(f"[dim]classify LLM cost:[/dim] {ledger.summary()}")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    settings = get_settings()

    if getattr(args, "anyversion", False):
        settings.drift_scope = "any"
        import yaml as _yaml

        from depfix.defaults import _path, load_local_defaults

        _local = load_local_defaults()
        _local["drift_scope"] = "any"
        with contextlib.suppress(OSError):
            _path().write_text(_yaml.safe_dump(_local, sort_keys=True), encoding="utf-8")

    ledger = CostLedger()
    _scan_coordinator = _build_scan_coordinator(settings, ledger)
    _scan_strategy = settings.scan_strategy if _scan_coordinator is not None else "deterministic"

    try:
        local_path, remote_repo = _scan_location(args)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 2
    if remote_repo and "/" not in remote_repo:
        console.print("[red]Error: --repo must be OWNER/NAME[/red]")
        return 2

    providers_file = args.providers_file or settings.providers_file
    try:
        providers = load_providers_file(providers_file)
    except (FileNotFoundError, ProviderConfigError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 2

    provider = _scan_provider(providers, args.provider, args.package)
    if provider is None:
        known = ", ".join(sorted(p.id for p in providers))
        selector = args.provider or args.package
        console.print(
            f"[red]Error: unknown provider/package {selector!r} (known providers: {known})[/red]"
        )
        return 2

    try:
        change = _load_optional_breaking_change(args)
    except (FileNotFoundError, ValueError, LookupError, json.JSONDecodeError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 2

    provider_changes: list[BreakingChange] = []
    if change is None and not args.old_api:
        console.print(f"[dim]Detecting changes for {provider.id}...[/dim]")
        try:
            _detect_completer = _build_completer(settings)
            if _detect_completer is not None:
                _detect_completer = CostTrackingCompleter(_detect_completer, ledger)
            created = ensure_changes_detected(
                provider.id,
                providers_file=providers_file,
                completer=_detect_completer,
                min_confidence=settings.classify_min_confidence,
            )
            if created:
                console.print(f"[green]{created} new breaking change(s) classified[/green]")
            provider_changes = all_changes_for_provider(provider.id)
            change, _change_id = latest_change_for_provider(provider.id)
        except Exception as exc:  # detection must not prevent a broad provider scan
            console.print(
                f"[yellow]Change detection failed ({exc}); scanning all provider symbols[/yellow]"
            )
        else:
            if change is not None:
                console.print(
                    f"[dim]Auto-selected:[/dim] {change.old_api} → {change.new_api} "
                    f"({change.old_version} → {change.new_version})"
                )

    # Explicit selection remains deliberately narrow. A normal scan begins
    # broad: it records all provider usage before feed candidates are matched
    # against the repository's actual API symbols.
    explicitly_selected_change = bool(args.change_file or args.from_event is not None)
    scan_change = change if explicitly_selected_change else None
    changes_to_assess = [change] if scan_change is not None else provider_changes
    target = build_scan_target(provider, scan_change, feed_changes=changes_to_assess)  # type: ignore[arg-type]
    if args.old_api:
        if change is not None:
            console.print(
                "[red]Error: --old-api cannot be combined with --change-file or --from-event[/red]"
            )
            return 2
        target = replace(target, symbols=(args.old_api,))
    scanner = CallSiteScanner(
        max_file_bytes=settings.scan_max_file_bytes, max_files=settings.scan_max_files
    )
    judge = None
    if settings.scan_llm_judge_enabled and scan_change is not None:
        # Call-site judging (yes/no decisions on ambiguous call sites) uses the
        # heavy model (Sonnet-class) — the judgment quality materially affects
        # whether a fix is attempted, so accuracy outweighs cost here.
        completer = _build_completer(settings, heavy=True)
        if completer is not None:
            from depfix.obs.cost import CostStage
            from depfix.scanners.judge import LLMCallSiteJudge

            judge = LLMCallSiteJudge(
                CostTrackingCompleter(completer, ledger, CostStage.CALL_SITE_JUDGE),
                max_candidates=settings.scan_llm_judge_max_candidates,
            )

    _print_header()
    light, heavy = _resolve_light_model(settings)
    if light != heavy:
        console.print(f"[dim]Model tiers — scan/classify: {light}  fix/generate: {heavy}[/dim]")
    console.print(
        f"[dim]Looking for {len(target.feed_symbols)} feed-derived symbol(s) "
        "(old APIs + replacements)[/dim]"
        if target.feed_symbols
        else "[dim]No feed change names an API symbol yet; recording SDK usage only[/dim]"
    )

    owner = name = ""
    gh_auth: GitHubAppAuth | None = None
    try:
        _max_data_bytes = int(settings.scan_max_data_mb * 1024 * 1024)
        if local_path:
            repo_full_name = str(Path(local_path).resolve())
            result = scan_path(
                local_path,
                target,
                repo_full_name=repo_full_name,
                scanner=scanner,
                change=scan_change,
                judge=judge,
                prepare_package_manager_runtime=settings.scan_prepare_package_manager,
                package_manager_timeout=settings.package_manager_install_timeout,
                strategy=_scan_strategy,
                provider=provider,
                agent_completer=_scan_coordinator,
                max_data_bytes=_max_data_bytes,
            )
        else:
            assert remote_repo is not None
            owner, name = remote_repo.split("/", 1)
            gh_auth = _build_gh_auth(settings)
            if gh_auth is None:
                console.print(
                    "[red]Error: GitHub App not configured. Set GITHUB_APP_ID and either "
                    "GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH.[/red]"
                )
                return 2
            clone_service = CloneService(
                clone_dir=settings.clone_dir or None,
                timeout=settings.clone_timeout,
                max_repo_mb=settings.clone_max_repo_mb,
            )
            repo_full_name = f"{owner}/{name}"
            # Read per-repo .depfix.yml before cloning so max_repo_mb can
            # override the fleet cap for large repos.
            _per_repo_max_mb: int | None = None
            try:
                from depfix.repoconfig.loader import parse_repo_config

                _config_text = gh_auth.get_file_contents(owner, name, settings.repo_config_filename)
                if _config_text:
                    _per_repo_max_mb = parse_repo_config(_config_text).max_repo_mb
            except Exception:  # nosec B110 — non-critical config lookup; repo still scanned without per-repo override
                pass
            result = scan_repo(
                owner,
                name,
                target,
                gh_auth=gh_auth,
                clone_service=clone_service,
                ref=args.ref,
                scanner=scanner,
                change=scan_change,
                judge=judge,
                prepare_package_manager_runtime=settings.scan_prepare_package_manager,
                package_manager_timeout=settings.package_manager_install_timeout,
                strategy=_scan_strategy,
                provider=provider,
                agent_completer=_scan_coordinator,
                max_data_bytes=_max_data_bytes,
                max_repo_mb=_per_repo_max_mb,
            )
    except RepoTooLargeError as exc:
        console.print(f"[yellow]Info: {exc}[/yellow]")
        return 0
    except (CloneError, GitHubAppError, FileNotFoundError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 1
    finally:
        if gh_auth is not None:
            gh_auth.close()

    if scan_change is not None:
        from depfix.scanners.severity import severity_from_scan

        result.severity, result.severity_evidence = severity_from_scan(
            scan_change, result.call_sites
        )
    if change is None and not args.old_api:
        # The first feed poll after ``init`` is correctly a baseline, but a
        # repository lockfile supplies the missing old side of a version
        # comparison.  Persist that scan-specific drift before assessing call
        # sites, so a fresh SQLite database has useful, non-stale findings.
        package_ecosystems: dict[str, tuple[str, ...]] = {}
        for sdk in provider.sdk_packages:
            package_ecosystems[sdk.name] = (*package_ecosystems.get(sdk.name, ()), sdk.ecosystem)
        github_repos = {
            str(feed.config.get("package") or feed.config.get("gem")): str(
                feed.config["github_repo"]
            )
            for feed in provider.feeds
            if feed.config.get("github_repo")
            and (feed.config.get("package") or feed.config.get("gem"))
        }
        drift_completer = _build_completer(settings)
        if drift_completer is not None:
            drift_completer = CostTrackingCompleter(drift_completer, ledger)
        captured = capture_repository_dependency_drift(
            provider.id,
            repository=repo_full_name,
            dependencies=result.dependencies,
            package_ecosystems=package_ecosystems,
            github_repos=github_repos,
            completer=drift_completer,
            github_api_url=settings.github_api_url,
        )
        if captured:
            console.print(
                f"[green]{captured} repository/feed version drift record(s) classified[/green]"
            )
            provider_changes = all_changes_for_provider(provider.id)
            changes_to_assess = provider_changes
    if scan_change is None and result.actionable_sites:
        completer = _build_completer(settings)
        if completer is not None:
            completer = CostTrackingCompleter(completer, ledger)
            package = provider.sdk_packages[0].name if provider.sdk_packages else provider.id
            with session_scope() as session:
                discovered = discover_usage_candidates(
                    session,
                    provider_id=provider.id,
                    package=package,
                    observed_symbols=[site.symbol for site in result.actionable_sites],
                    completer=completer,
                )
            if discovered:
                provider_changes = all_changes_for_provider(provider.id)
                # ``discover_usage_candidates`` persists before returning,
                # but include its return value too. This keeps the scan
                # decision correct for every storage adapter and avoids
                # making the security decision depend on a read-after-write.
                changes_to_assess = list(
                    {
                        change.dedupe_key: change for change in [*provider_changes, *discovered]
                    }.values()
                )
                console.print(
                    f"[green]{len(discovered)} feed-proven migration candidate(s) "
                    "matched this repository's usage[/green]"
                )
    _print_scan_result(result, show_low=args.show_low)
    preparation = result.package_manager_preparation
    if preparation is not None:
        from depfix.verify.manager import PackageManagerPreparation

        assert isinstance(preparation, PackageManagerPreparation)
        if preparation.ready:
            source = "installed in Depfix cache" if preparation.installed else "already available"
            version = f"@{preparation.version}" if preparation.version else ""
            console.print(
                f"[green]Package manager ready:[/green] "
                f"{preparation.package_manager}{version} ({source})"
            )
        else:
            console.print(f"[yellow]Package manager unavailable:[/yellow] {preparation.detail}")
    assessments = assess_scan_changes(result, changes_to_assess, provider_id=provider.id)  # type: ignore[arg-type]
    _record_scan_detections(
        settings,
        repo_full_name=repo_full_name,
        provider_id=provider.id,
        result=result,
        assessments=assessments,
    )
    _print_scan_assessments(assessments)
    _print_dependency_drift_summary(result, assessments)
    if ledger.by_stage:
        console.print(f"[dim]LLM cost:[/dim] {ledger.summary()}")
    else:
        console.print("[dim]No LLM calls (scan is AST/regex-only).  TTS: 0 tokens[/dim]")
    if args.write_report:
        _write_scan_report(
            local_path=local_path,
            remote_repo=remote_repo,
            result=result,
            assessments=assessments,
            settings=settings,
        )

    if not args.no_save:
        # Repository usage is recorded before plan/apply eligibility is
        # considered. The catalog's managed section preserves every resolved
        # call site, including usages for which no feed has a migration yet.
        from depfix.catalog import record_detected_usage, repository_observations_file

        catalog_path = repository_observations_file(settings)
        if catalog_path is not None:
            recordable_kinds = frozenset(
                {
                    CallSiteKind.METHOD_CALL,
                    CallSiteKind.BARE_SYMBOL,
                    CallSiteKind.SDK_IMPORT,
                    CallSiteKind.CLIENT_CONSTRUCTION,
                    CallSiteKind.WRAPPER_IMPORT,
                }
            )
            provider_packages = {sdk.name for sdk in provider.sdk_packages}
            try:
                learned = record_detected_usage(
                    catalog_path,
                    repo_full_name=repo_full_name,
                    commit_sha=result.commit_sha,
                    provider_id=provider.id,
                    detected_symbols=[
                        site.symbol
                        for site in result.actionable_sites
                        if site.kind in recordable_kinds
                    ],
                    classified_changes=changes_to_assess,  # type: ignore[arg-type]
                    sdk_versions={
                        dependency.package: dependency.resolved_version
                        for dependency in result.dependencies
                        if dependency.package in provider_packages
                        and dependency.resolved_version is not None
                    },
                )
            except (OSError, yaml.YAMLError) as exc:
                console.print(f"[yellow]Could not update migration catalog: {exc}[/yellow]")
            else:
                if learned:
                    console.print(
                        f"[green]{learned} repository API usage record(s) written to "
                        f"{catalog_path}[/green]"
                    )
        init_schema()
        with session_scope() as session:
            scan_row = record_scan(
                session,
                repo_full_name=repo_full_name,
                owner=owner or "local",
                name=name or Path(local_path).name,  # type: ignore[arg-type]
                result=result,
                provider_id=provider.id,
                change=scan_change,
                narrowed_symbols=target.symbols,
            )
            record_scan_assessments(session, scan=scan_row, assessments=assessments)
        console.print(f"[dim]saved scan to database ({repo_full_name})[/dim]")
        if target.symbols:
            console.print(
                "[yellow]Narrowed scan: plan will not reuse it (not a superset). "
                "Run without --change-file/--from-event/--old-api for a reusable scan.[/yellow]"
            )
        elif local_path is not None:
            console.print(
                f"[yellow]Local scan stored under {repo_full_name}; "
                "`depfix plan OWNER/NAME` looks up owner/name. Use --repo to make it "
                "reusable.[/yellow]"
            )
        else:
            console.print(
                f"[dim]Reusable by:[/dim] depfix plan {repo_full_name} --provider {provider.id}"
            )
        console.print()
    else:
        console.print(
            "[yellow]--no-save: nothing persisted, so `depfix plan` will rescan "
            "(and re-pay any scan-agent cost).[/yellow]"
        )

    console.print("[dim]Evaluating depfix's deterministic regression corpus...[/dim]")
    evaluation_args = argparse.Namespace(
        corpus_dir=None,
        category=None,
        no_llm=True,
        fail_under=1.0,
        save=None,
        compare=None,
    )
    return _cmd_eval(evaluation_args)


_VERDICT_STYLE = {
    EditVerdict.APPLIED: "green",
    EditVerdict.KEPT: "green",
    EditVerdict.SUSPECT: "yellow",
    EditVerdict.REVERTED: "red",
    EditVerdict.SKIPPED: "dim",
}


def _print_fix_result(result: FixPipelineResult) -> None:
    table = Table(title="Fix Results")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Files Scanned", str(result.files_scanned))
    table.add_row("Files Affected", str(result.files_affected))
    table.add_row("Total Usages Fixed", str(result.total_usages_fixed))
    table.add_row("Total Cost", f"${result.total_cost:.4f}")
    table.add_row("Total Tokens", str(result.total_tokens))
    table.add_row("Duration", f"{result.duration_ms}ms")

    verification = result.verification
    if verification is not None:
        table.add_row(
            "Verified", "yes" if verification.ran else f"no ({verification.skipped_reason})"
        )
        if verification.ran and verification.baseline is not None:
            table.add_row("New Test Failures", str(verification.new_failure_count))
    console.print(table)
    console.print()

    if result.edits:
        console.print("[bold]File Details:[/bold]")
        for edit in result.edits:
            style = _VERDICT_STYLE[edit.verdict]
            console.print(
                f"  [{style}]{edit.verdict.value:>8}[/{style}] [cyan]{edit.relpath}[/cyan] - "
                f"{edit.usages_fixed} usages - confidence: {edit.confidence:.2f}"
            )
            if edit.error_message:
                console.print(f"      [red]{edit.error_message}[/red]")

    from depfix.core.explain import explain_rejections

    explanations = explain_rejections(result)
    if explanations:
        console.print()
        console.print("[bold yellow]Why some edits weren't kept:[/bold yellow]")
        for explanation in explanations:
            console.print(explanation.render(indent="  "))
    if result.codemod_notes:
        console.print()
        console.print("[bold]Codemod / cross-file notes:[/bold]")
        for note in result.codemod_notes[:20]:
            console.print(f"  - {note}")
        if len(result.codemod_notes) > 20:
            console.print(f"  - _(+{len(result.codemod_notes) - 20} more; see PR body)_")
    console.print()


def _print_fix_diffs(result: FixPipelineResult, show_full: bool) -> None:
    console.print("[bold]Generated Diffs:[/bold]\n")
    for edit in result.edits:
        if not edit.diff:
            continue
        console.print(f"[cyan]{edit.relpath}[/cyan]")
        if show_full:
            console.print(Syntax(edit.diff, "diff", theme="monokai", line_numbers=True))
        else:
            lines = edit.diff.split("\n")
            preview = "\n".join(lines[:30])
            console.print(Syntax(preview, "diff", theme="monokai"))
            if len(lines) > 30:
                console.print(f"[dim]... ({len(lines) - 30} more lines)[/dim]")
        console.print()


def _write_fix_diffs(edits: list[FileEdit], output_dir: str) -> None:
    """Write each on-disk edit's diff as ``output_dir/<flattened-relpath>.patch``
    -- same convention as :meth:`depfix.core.agent.DependencyFixAgent._write_diffs`,
    so ``git apply {output}/*.patch`` works identically for both commands."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    for edit in edits:
        if not edit.diff:
            continue
        safe_name = edit.relpath.replace("/", "_").replace("\\", "_")
        (output_path / f"{safe_name}.patch").write_text(edit.diff)


def _open_pr_for_fix(
    args: argparse.Namespace,
    settings,
    owner: str,
    name: str,
    breaking: BreakingChange,
    result: FixPipelineResult,
    edits: list[FileEdit],
) -> PullRequest | None:
    """Push the shared fix-service's committable edits to a PR."""
    if not args.repo:
        console.print(
            "[yellow]Skipping --open-pr: only supported with --repo, not --path.[/yellow]"
        )
        return None
    if not edits:
        console.print("[yellow]Skipping --open-pr: no committable edits.[/yellow]")
        return None

    pr_gh_auth = _build_gh_auth(settings)
    if pr_gh_auth is None:
        console.print("[red]Error: GitHub App not configured, cannot open a PR.[/red]")
        return None
    try:
        installation = pr_gh_auth.resolve_installation_for_repo(owner, name)
        write_token = pr_gh_auth.installation_token(
            installation.id,
            repositories=(name,),
            permissions={"contents": "write", "pull_requests": "write"},
        )
        base_branch = args.base_branch or pr_gh_auth.default_ref(owner, name)
        head_branch = branch_name_for(breaking)
        with BranchWriter(write_token.token, owner, name) as writer:
            writer.commit_edits(
                branch=head_branch,
                base_branch=base_branch,
                message=f"depfix: migrate {breaking.old_api} -> {breaking.replacement}",
                edits=edits,
            )
        pr = open_pull_request(
            owner=owner,
            repo=name,
            token=write_token.token,
            head_branch=head_branch,
            base_branch=base_branch,
            title=pr_title_for(breaking),
            body=build_pr_body(breaking, result, edits=edits),
            draft=args.draft_pr,
            api_url=settings.github_api_url,
        )
        from depfix.gh import apply_pr_labels

        apply_pr_labels(
            owner=owner,
            repo=name,
            pr_number=pr.number,
            labels=[f"depfix:confidence={result.confidence.value}"],
            token=write_token.token,
            api_url=settings.github_api_url,
        )
        verb = "Reused existing" if pr.already_existed else "Opened"
        console.print(f"[green]{verb} PR: {pr.html_url}[/green]")
        return pr
    except GitHubAppError as exc:
        console.print(f"[red]Error opening PR: {exc}[/red]")
        return None
    finally:
        pr_gh_auth.close()


def _cmd_fix(args: argparse.Namespace) -> int:
    settings = get_settings()
    ledger = CostLedger()
    _apply_model_overrides(args, settings)
    api_key = args.api_key or settings.google_api_key
    model = args.model or settings.gemini_model
    llm_provider = args.llm_provider or settings.llm_provider
    ollama_model = args.ollama_model or settings.ollama_model

    if llm_provider == "gemini" and not api_key:
        console.print("[red]Error: GOOGLE_API_KEY not set. Use --api-key or .env[/red]")
        return 2

    if bool(args.repo) == bool(args.path):
        console.print("[red]Error: pass exactly one of --repo OWNER/NAME or --path DIR[/red]")
        return 2
    if args.repo and "/" not in args.repo:
        console.print("[red]Error: --repo must be OWNER/NAME[/red]")
        return 2

    providers_file = args.providers_file or settings.providers_file
    try:
        providers = load_providers_file(providers_file)
    except (FileNotFoundError, ProviderConfigError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 2

    provider = next((p for p in providers if p.id == args.provider), None)
    if provider is None:
        known = ", ".join(sorted(p.id for p in providers))
        console.print(f"[red]Error: unknown provider id {args.provider!r} (known: {known})[/red]")
        return 2

    try:
        breaking = _load_breaking_change(args)
    except (FileNotFoundError, ValueError, LookupError, json.JSONDecodeError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 2

    fixer: FixGenerator | OllamaFixGenerator | BedrockFixGenerator
    if llm_provider == "bedrock":
        from depfix.fixers.bedrock import BedrockFixGenerator

        fixer = BedrockFixGenerator(
            aws_access_key=settings.bedrock_access_key,
            aws_secret_key=settings.bedrock_secret_key,
            aws_region=settings.bedrock_region,
            aws_profile=settings.bedrock_profile,
            model=settings.bedrock_model,
        )
    elif llm_provider == "gemini":
        fixer = FixGenerator(api_key=api_key, model=model)
    else:
        fixer = OllamaFixGenerator(
            model=ollama_model, base_url=settings.ollama_base_url, timeout=settings.ollama_timeout
        )

    target = build_scan_target(provider, breaking)
    scanner = CallSiteScanner(
        max_file_bytes=settings.scan_max_file_bytes, max_files=settings.scan_max_files
    )

    _print_header()
    _print_breaking_change(breaking)
    if llm_provider == "bedrock":
        light, heavy = _resolve_light_model(settings)
        if light != heavy:
            console.print(
                f"[dim]Using provider: bedrock  heavy: {heavy}  light: {light}  "
                f"region: {settings.bedrock_region}[/dim]\n"
            )
        else:
            console.print(
                f"[dim]Using provider: bedrock  model: {heavy}  "
                f"region: {settings.bedrock_region}[/dim]\n"
            )
    elif llm_provider == "ollama":
        console.print(f"[dim]Using provider: ollama  model: {ollama_model}[/dim]\n")
    else:
        console.print(f"[dim]Using provider: gemini  model: {model}[/dim]\n")

    clone_service = CloneService(
        clone_dir=settings.clone_dir or None,
        timeout=settings.clone_timeout,
        max_repo_mb=settings.clone_max_repo_mb,
    )

    owner = name = ""
    repo_full_name = ""
    gh_auth: GitHubAppAuth | None = None
    checkout = None
    result: FixPipelineResult | None = None
    service_result: FixServiceResult | None = None
    try:
        if args.path:
            owner, name = "local", Path(args.path).name
            repo_full_name = str(Path(args.path).resolve())
            checkout = clone_service.copy_local(args.path)
        else:
            owner, name = args.repo.split("/", 1)
            gh_auth = _build_gh_auth(settings)
            if gh_auth is None:
                console.print(
                    "[red]Error: GitHub App not configured. Set GITHUB_APP_ID and either "
                    "GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH.[/red]"
                )
                return 2
            installation = gh_auth.resolve_installation_for_repo(owner, name)
            token = gh_auth.installation_token(installation.id, repositories=(name,))
            resolved_ref = args.ref or gh_auth.default_ref(owner, name)
            checkout = clone_service.clone(owner, name, ref=resolved_ref, token=token.token)
            repo_full_name = f"{owner}/{name}"

        scan_result = scan_checkout(
            checkout, target, repo_full_name=repo_full_name, scanner=scanner
        )
        if (
            not scan_result.actionable_sites
            and breaking.kind is not ChangeKind.DEPENDENCY_VERSION_BUMP
        ):
            console.print("[yellow]No actionable call sites found for this change.[/yellow]")
            return 0

        max_retries = (
            0
            if args.no_retry
            else (args.max_retries if args.max_retries is not None else settings.retry_max_rounds)
        )
        repo_config = RepoConfig(version=SUPPORTED_VERSION, verify=not args.no_verify)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Fixing and verifying...", total=None)
            service_result = run_fix_for_change(
                checkout=checkout,
                scan_result=scan_result,
                change=breaking,
                settings=settings,
                repo_config=repo_config,
                fixer=fixer,
                max_retries=max_retries,
                ledger=ledger,
            )
            result = service_result.pipeline_result
            progress.update(task, completed=True)
    except RepoTooLargeError as exc:
        console.print(f"[yellow]Info: {exc}[/yellow]")
        return 0
    except (CloneError, GitHubAppError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 1
    except Exception as e:  # surfaced to the user, then re-logged with a traceback
        console.print(f"[red]Fix pipeline failed: {e}[/red]")
        logger.exception("Fix pipeline crashed")
        return 1
    finally:
        if gh_auth is not None:
            gh_auth.close()
        if checkout is not None:
            clone_service.cleanup(checkout)

    assert result is not None
    assert service_result is not None
    console.print()
    _print_fix_result(result)
    if ledger.by_stage:
        console.print(f"[dim]LLM spend this run:[/dim] {ledger.summary()}")
    if args.show_diffs:
        _print_fix_diffs(result, show_full=True)

    output = args.output or settings.output_dir
    if result.applied_edits:
        _write_fix_diffs(result.applied_edits, output)
        console.print(
            f"[green]Diffs written to: {output}[/green]\n"
            f"[dim]Apply with: git apply {output}/*.patch[/dim]"
        )

    pr_result: PullRequest | None = None
    if args.open_pr:
        pr_result = _open_pr_for_fix(
            args, settings, owner, name, breaking, result, service_result.committable
        )

    if not args.no_save:
        init_schema()
        with session_scope() as session:
            run = record_fix_run(
                session,
                repo_full_name=repo_full_name,
                owner=owner,
                name=name,
                result=result,
                escalated=service_result.escalated,
                cost_ledger=ledger,
            )
            if pr_result is not None:
                record_pull_request(session, run, pr_result)
        console.print(f"[dim]saved fix run to database ({repo_full_name})[/dim]")

    if result.reverted_edits or result.suspect_edits:
        return 1
    return 0


def _cmd_repos(args: argparse.Namespace) -> int:
    settings = get_settings()
    gh_auth = _build_gh_auth(settings)
    if gh_auth is None:
        console.print(
            "[red]Error: GitHub App not configured. Set GITHUB_APP_ID and either "
            "GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH.[/red]"
        )
        return 2

    _print_header()
    try:
        installations = gh_auth.list_installations()
        if args.installation is not None:
            installations = [i for i in installations if i.id == args.installation]
            if not installations:
                console.print(f"[red]Error: no installation with id={args.installation}[/red]")
                return 2

        table = Table(title="GitHub App Installations & Repos")
        table.add_column("Installation", style="cyan")
        table.add_column("Account Type")
        table.add_column("Repo")
        table.add_column("Default Branch")
        table.add_column("Private", justify="center")

        for installation in installations:
            repos = gh_auth.list_repositories(installation.id)
            if not repos:
                table.add_row(
                    installation.account_login,
                    installation.account_type,
                    "[dim](none)[/dim]",
                    "",
                    "",
                )
                continue
            for repo in repos:
                table.add_row(
                    installation.account_login,
                    installation.account_type,
                    repo.full_name,
                    repo.default_branch,
                    "yes" if repo.private else "no",
                )
        console.print(table)
    except GitHubAppError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 1
    finally:
        gh_auth.close()
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Initialize local state, then run the complete preflight diagnostic."""
    from depfix.doctor import CheckStatus, run_checks, worst_status

    settings = get_settings()
    if args.providers_file:
        settings.providers_file = args.providers_file
    _print_header()

    console.print("[dim]Initializing database...[/dim]")
    try:
        if not args.keep_state and reset_sqlite_state():
            console.print("  [green]✓[/green] cleared local SQLite state")
        init_schema()
        console.print(f"  [green]✓[/green] database ready ({get_engine().dialect.name})")
    except Exception as exc:
        console.print(f"  [red]✗[/red] database failed: {exc}")
        return 1

    checks = run_checks(settings, skip_network=args.offline)
    styles = {
        CheckStatus.OK: "[green]OK[/green]",
        CheckStatus.WARN: "[yellow]WARN[/yellow]",
        CheckStatus.FAIL: "[red]FAIL[/red]",
    }
    for check in checks:
        console.print(f"{styles[check.status]:<18} {check.name:<24} {check.detail}")
        if check.hint and check.status is not CheckStatus.OK:
            console.print(f"  [dim]{check.hint}[/dim]")

    if worst_status(checks) is not CheckStatus.FAIL:
        console.print("\n[bold green]depfix is ready.[/bold green]")
        console.print("[dim]Next:[/dim] depfix scan --repo OWNER/NAME --provider ID")
        return 0
    console.print("\n[yellow]Some checks failed. Fix the issues above and re-run.[/yellow]")
    return 1


def _cmd_pipeline(args: argparse.Namespace) -> int:
    # Terraform-style aliases intentionally share the one audited
    # orchestrator implementation.  A plan is never allowed to persist a
    # ledger record, branch, or PR; apply is the normal live path.
    if args.command == "plan":
        args.dry_run = True
        args.no_pr = True
        args.force = True
    settings = get_settings()
    from depfix.defaults import fill_pipeline_context, load_local_defaults

    fill_pipeline_context(args, load_local_defaults())
    _apply_model_overrides(args, settings)

    if getattr(args, "no_upgrade_plans", False):
        settings.upgrade_plans_enabled = False

    if getattr(args, "reuse_scan", None):
        settings.plan_reuse_scan = args.reuse_scan

    # A repository named directly on the command line is an explicit opt-in.
    # The repo-config gate remains for unattended, fleet-wide discovery.
    has_explicit_repos = bool(getattr(args, "repos", None) or getattr(args, "repo", None))
    if has_explicit_repos:
        settings.pipeline_require_config_file = False

    api_key = args.api_key or settings.google_api_key
    model = args.model or settings.gemini_model
    llm_provider = args.llm_provider or settings.llm_provider
    ollama_model = args.ollama_model or settings.ollama_model

    if llm_provider == "gemini" and not api_key:
        console.print("[red]Error: GOOGLE_API_KEY not set. Use --api-key or .env[/red]")
        return 2
    if llm_provider == "bedrock":
        light, heavy = _resolve_light_model(settings)
        if light != heavy:
            console.print(
                f"[dim]Using provider: bedrock  heavy: {heavy}  light: {light}  "
                f"region: {settings.bedrock_region}[/dim]"
            )
        else:
            console.print(
                f"[dim]Using provider: bedrock  model: {heavy}  "
                f"region: {settings.bedrock_region}[/dim]"
            )

    providers_file = args.providers_file or settings.providers_file
    try:
        providers = load_providers_file(providers_file)
    except (FileNotFoundError, ProviderConfigError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 2

    gh_auth = _build_gh_auth(settings)
    if gh_auth is None:
        console.print(
            "[red]Error: GitHub App not configured. Set GITHUB_APP_ID and either "
            "GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH.[/red]"
        )
        return 2

    init_schema()
    try:
        repo_full_names = _load_pipeline_repos(args, gh_auth)
    except GitHubAppError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        gh_auth.close()
        return 1
    if not repo_full_names:
        console.print("[yellow]No repos to process.[/yellow]")
        gh_auth.close()
        return 0

    try:
        changes = _load_pipeline_changes(
            args,
            settings,
            limit=(
                args.limit if args.limit is not None else settings.pipeline_default_change_limit
            ),
            repo_full_names=repo_full_names if (args.repos or args.repo) else None,
        )
    except (FileNotFoundError, LookupError, json.JSONDecodeError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        gh_auth.close()
        return 2
    if args.provider:
        wanted = set(args.provider)
        changes = [c for c in changes if c.provider_id in wanted]
    if not changes and args.provider and not args.plan_file:
        for provider_id in args.provider:
            console.print(f"[dim]No classified changes for {provider_id}; detecting...[/dim]")
            try:
                created = ensure_changes_detected(provider_id, providers_file=providers_file)
                if created:
                    console.print(
                        f"[green]{created} new breaking change(s) classified for {provider_id}[/green]"
                    )
                provider_changes = all_changes_for_provider(provider_id)
            except Exception as exc:
                console.print(f"[yellow]Change detection for {provider_id} failed: {exc}[/yellow]")
                continue
            if provider_changes:
                changes.extend(provider_changes)
                console.print(
                    f"[dim]Auto-selected {len(provider_changes)} change(s) for {provider_id}:[/dim]"
                )
                for change in provider_changes[:5]:
                    console.print(f"[dim]  {change.old_api} → {change.new_api}[/dim]")
                if len(provider_changes) > 5:
                    console.print(f"[dim]  ... and {len(provider_changes) - 5} more[/dim]")
    # `apply <repo>` may obtain its one change from the repo's canonical
    # plan artifact, so do not reject an empty database/change-file result
    # until plan discovery has had a chance to run.
    if not changes and not args.plan_file and args.command != "apply":
        console.print("[yellow]No breaking changes to process.[/yellow]")
        gh_auth.close()
        return 0
    plan_artifacts: dict[tuple[str, str], PlanArtifact] = {}
    resolved_artifact: PlanArtifact | None = None

    if args.plan_file:
        try:
            resolved_artifact = PlanArtifact.read(args.plan_file)
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            console.print(f"[red]Error loading plan artifact: {exc}[/red]")
            gh_auth.close()
            return 2
    elif (
        args.command == "apply"
        and len(repo_full_names) == 1
        and not (args.from_event or args.change_file)
    ):
        discovered = PlanArtifact.plans_for_repo(repo_full_names[0], directory=settings.plan_dir)
        if not discovered:
            console.print(
                f"[red]No plan artifact found for {repo_full_names[0]}.[/red]\n"
                f"[dim]Run first:[/dim] "
                f"[bold]depfix plan {repo_full_names[0]} --from-event <ID>[/bold]"
            )
            gh_auth.close()
            return 2
        report_only = [a for a in discovered if not a.edits and not a.is_upgrade]
        discovered = [a for a in discovered if a.edits or a.is_upgrade]
        if report_only:
            console.print(
                f"[dim]{len(report_only)} report-only drift plan(s) skipped (no rewritable "
                f"manifest); see {settings.learned_migrations_file}[/dim]"
            )
        if not discovered:
            console.print("[yellow]No plan with committable edits to apply.[/yellow]")
            gh_auth.close()
            return 0
        for artifact in discovered:
            plan_artifacts[(artifact.repo_full_name, artifact.change_dedupe_key)] = artifact
        changes = [artifact.change for artifact in discovered]
        console.print(
            f"[dim]Applying {len(discovered)} reviewed plan(s) for {repo_full_names[0]}[/dim]"
        )

    if resolved_artifact is not None:
        if args.command != "apply":
            console.print("[red]Error: --plan-file is only valid with `depfix apply`.[/red]")
            gh_auth.close()
            return 2
        if resolved_artifact.repo_full_name not in repo_full_names:
            console.print(
                f"[red]Error: plan artifact is for "
                f"{resolved_artifact.repo_full_name}, which is not included in "
                "this apply command.[/red]"
            )
            gh_auth.close()
            return 2
        conflicting = [c for c in changes if c.dedupe_key != resolved_artifact.change_dedupe_key]
        if conflicting and (args.from_event or args.change_file):
            console.print(
                "[yellow]Note: ignoring --from-event/--change-file; the plan "
                f"artifact applies change {resolved_artifact.change_dedupe_key[:12]}.[/yellow]"
            )
        changes = [resolved_artifact.change]
        plan_artifacts[(resolved_artifact.repo_full_name, resolved_artifact.change_dedupe_key)] = (
            resolved_artifact
        )

    if not changes:
        console.print("[yellow]No breaking changes to process.[/yellow]")
        gh_auth.close()
        return 0

    def fixer_factory() -> FixGenerator | OllamaFixGenerator | BedrockFixGenerator:
        if llm_provider == "gemini":
            return FixGenerator(api_key=api_key, model=model)
        if llm_provider == "bedrock":
            return BedrockFixGenerator(
                aws_access_key=settings.bedrock_access_key,
                aws_secret_key=settings.bedrock_secret_key,
                aws_region=settings.bedrock_region,
                aws_profile=settings.bedrock_profile,
                model=settings.bedrock_model,
            )
        return OllamaFixGenerator(
            model=ollama_model, base_url=settings.ollama_base_url, timeout=settings.ollama_timeout
        )

    clone_service = CloneService(
        clone_dir=settings.clone_dir or None,
        timeout=settings.clone_timeout,
        max_repo_mb=settings.clone_max_repo_mb,
    )

    mode = (
        "plan"
        if args.command == "plan"
        else (
            "no-pr"
            if args.no_pr
            else "force"
            if args.force
            else "apply"
            if args.command == "apply"
            else "live"
        )
    )
    _print_header()
    console.print(
        f"[dim]Repos:[/dim] {len(repo_full_names)}  [dim]Changes:[/dim] {len(changes)}  "
        f"[dim]Mode:[/dim] {mode}\n"
    )

    _pipeline_ledger = CostLedger()
    _pipeline_coordinator = _build_scan_coordinator(settings, _pipeline_ledger)

    _raw_mins: float | None = (
        args.max_duration
        if args.max_duration is not None
        else settings.pipeline_max_duration_minutes
    )
    orchestrator = Orchestrator(
        settings=settings,
        gh_auth=gh_auth,
        clone_service=clone_service,
        providers=providers,
        fixer_factory=fixer_factory,
        force=args.force,
        dry_run=args.dry_run,
        no_pr=args.no_pr,
        use_lock=not args.no_lock and not args.dry_run and settings.pipeline_require_lock,
        max_duration_seconds=_raw_mins * 60.0 if _raw_mins is not None else None,
        max_cost_usd=args.max_cost,
        plan_artifacts=plan_artifacts,
        completer=_build_completer(settings),
        agent_coordinator=_pipeline_coordinator,  # NEW — shared with ledger
        ledger=_pipeline_ledger,  # NEW — shared ledger
    )

    try:
        outcome = orchestrator.run(repo_full_names, changes)
    except Exception as e:
        console.print(f"[red]Pipeline failed: {e}[/red]")
        logger.exception("Orchestrator crashed")
        return 1
    finally:
        gh_auth.close()

    _print_orchestrator_outcome(outcome, show_skips=args.show_skips)
    if args.command == "plan":
        # Write the canonical per-repo artifact so ``apply <repo>`` can use
        # the latest reviewed plan without a copied filename. Keep optional
        # run-stamped copies for CI/audit workflows.
        legacy_dir = Path(args.plan_output_dir)
        for repo in outcome.repos:
            for change in repo.changes:  # type: ignore[assignment]
                if change.plan_artifact is None:  # type: ignore[attr-defined]
                    continue
                canonical = change.plan_artifact.write_for_repo(settings.plan_dir)  # type: ignore[attr-defined]
                archived = change.plan_artifact.write_to_archive(settings.plan_dir)  # type: ignore[attr-defined]
                _record_planned_detection(settings, change.plan_artifact, canonical)  # type: ignore[attr-defined]
                if change.plan_artifact.is_upgrade:  # type: ignore[attr-defined]
                    console.print(
                        f"[green]Upgrade plan:[/green] {canonical}  "
                        f"[dim]{change.plan_artifact.upgrade_package} "  # type: ignore[attr-defined]
                        f"{change.plan_artifact.upgrade_from or '?'} → "  # type: ignore[attr-defined]
                        f"{change.plan_artifact.upgrade_to}[/dim]"  # type: ignore[attr-defined]
                    )
                elif change.plan_artifact.is_drift:  # type: ignore[attr-defined]
                    art = change.plan_artifact  # type: ignore[attr-defined]
                    color = "red" if art.drift_risk == "multi-major" else "yellow"
                    label = "PR planned" if art.edits else "report-only, no PR"
                    console.print(
                        f"[{color}]Version drift ({art.drift_risk}, {label}):[/{color}] "
                        f"{canonical}  [dim]{art.change.package} "
                        f"{art.change.old_version} → {art.change.new_version}[/dim]"
                    )
                else:
                    console.print(f"[green]Plan artifact:[/green] {canonical}")
                console.print(f"[dim]Archived to:[/dim] {archived}")
                if args.plan_output_dir != Path(settings.plan_dir).as_posix():
                    stamped = legacy_dir / (
                        f"{outcome.run_id}-{change.plan_artifact.change_dedupe_key[:12]}.json"  # type: ignore[attr-defined]
                    )
                    change.plan_artifact.write(stamped)  # type: ignore[attr-defined]
                console.print(
                    f"[dim]Apply with:[/dim] [bold]{change.plan_artifact.apply_command()}[/bold]"  # type: ignore[attr-defined]
                )
        rollup = outcome.rollup()
        if rollup.prs_planned:
            acted = [c for r in outcome.repos for c in r.changes if c.plan_artifact is not None]
            if acted and all(c.kind == ChangeKind.DEPENDENCY_VERSION_BUMP.value for c in acted):
                cost_note = " [dim](manifest-only dependency drift; no model was asked to rewrite code)[/dim]"
            elif rollup.total_cost_usd == 0.0:
                cost_note = (
                    " [dim](no billed model cost -- a local model, a deterministic codemod, "
                    "or a replayed plan)[/dim]"
                )
            else:
                cost_note = ""
            console.print(
                f"\n[bold]{rollup.prs_planned} PR(s) planned[/bold]  [dim]cost:[/dim] ${rollup.total_cost_usd:.4f}{cost_note}"
            )
            for severity, count in sorted(
                rollup.by_severity.items(), key=lambda item: item[0].rank, reverse=True
            ):
                console.print(f"  {_severity_style(severity)}: {count}")
        else:
            console.print("\n[dim]No PRs planned this run.[/dim]")

    if outcome.cost_ledger is not None and outcome.cost_ledger.by_stage:
        console.print(f"[bold]LLM spend this run:[/bold] {outcome.cost_ledger.summary()}")
    elif outcome.cost_ledger is not None:
        console.print("[dim]No LLM calls this run.  TTS: 0 tokens[/dim]")
    if outcome.stopped_early:
        console.print("[yellow]Run stopped early: budget (time or cost) exhausted.[/yellow]")
    if not outcome.locked:
        console.print("[yellow]Another orchestrator run already holds the lock; skipped.[/yellow]")

    if args.command == "apply":
        _record_applied_detections(settings, outcome)

    if not outcome.failures and outcome.locked and getattr(args, "_depfix_has_target", False):
        from depfix.defaults import save_context

        save_context(
            repos=repo_full_names,
            provider=args.provider,
            from_event=args.from_event,
            change_file=args.change_file,
        )

    return 1 if outcome.failures else 0


def _print_eval_report(report: EvalReport) -> None:
    table = Table(title="Eval Results")
    table.add_column("Category", style="cyan")
    table.add_column("Cases", justify="right")
    table.add_column("Passed", justify="right")
    table.add_column("Skipped", justify="right")
    table.add_column("Unverified", justify="right")
    table.add_column("Cost", justify="right")

    for category in EvalCategory:
        cases = report.by_category(category)
        if not cases:
            continue
        scored = [c for c in cases if not c.skipped]
        passed = sum(1 for c in scored if c.passed)
        skipped = sum(1 for c in cases if c.skipped)
        unverified = sum(1 for c in cases if not c.verified)
        category_cost = sum(c.cost for c in cases)
        table.add_row(
            category.value,
            str(len(cases)),
            f"{passed}/{len(scored)}",
            str(skipped),
            str(unverified),
            f"${category_cost:.4f}" if category_cost > 0 else "-",
        )
    console.print(table)

    for case in report.scored_cases:
        status = "[green]PASS[/green]" if case.passed else "[red]FAIL[/red]"
        cost_str = f"  ${case.cost:.4f}" if case.cost > 0 else ""
        console.print(f"  {status} {case.case_id}{cost_str}: {case.detail}")

    console.print(
        f"\n[dim]{report.summary_line}[/dim]  [dim]cost:[/dim] ${report.total_cost:.4f}\n"
    )
    skipped_categories = sorted({case.category.value for case in report.skipped_cases})
    if skipped_categories:
        console.print(
            f"[yellow]Not measured this run: {', '.join(skipped_categories)} "
            "(no LLM configured). The pass rate does not measure fix quality.[/yellow]"
        )


def _cmd_eval(args: argparse.Namespace) -> int:
    settings = get_settings()
    corpus_dir = Path(args.corpus_dir or settings.eval_corpus_dir)

    try:
        cases = load_corpus_dir(corpus_dir)
    except CorpusError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 2

    if args.category:
        wanted = set(args.category)
        cases = [c for c in cases if c.category.value in wanted]

    use_llm = not args.no_llm
    completer: LLMCompleter | None = None
    fixer = None
    if use_llm:
        completer = _build_completer(settings)
        fixer = _build_fixer(settings)
        if completer is None:
            console.print(
                "[yellow]No LLM configured (GOOGLE_API_KEY unset); release_notes and "
                "fix_generation cases will be skipped.[/yellow]"
            )
            use_llm = False

    _print_header()
    console.print(f"[dim]Corpus:[/dim] {corpus_dir}  [dim]Cases:[/dim] {len(cases)}\n")

    runner = EvalRunner(
        completer=completer, fixer=fixer, default_min_recall=settings.scan_min_recall
    )
    report = runner.run(cases, use_llm=use_llm)

    _print_eval_report(report)

    exit_code = 0
    if report.pass_rate < args.fail_under:
        console.print(
            f"[red]pass rate {report.pass_rate:.0%} is below "
            f"--fail-under {args.fail_under:.0%}[/red]"
        )
        exit_code = 1

    if args.compare:
        baseline = load_report(Path(args.compare))
        regressions = compare_reports(baseline, report)
        if regressions:
            console.print(f"[red]{len(regressions)} regression(s) vs baseline:[/red]")
            for r in regressions:
                console.print(f"  [red]{r.case_id}[/red] was passing, now failing")
            exit_code = 1
        else:
            console.print("[green]No regressions vs baseline.[/green]")

    if args.save:
        save_report(report, Path(args.save))
        console.print(f"[dim]saved report to {args.save}[/dim]")

    return exit_code


def _add_command_logging_arguments(parser: argparse.ArgumentParser) -> None:
    """Allow logging options on either side of a subcommand.

    ``argparse`` treats options registered on the root parser as preceding its
    subcommand.  Re-registering these options on every command preserves the
    conventional ``depfix watch --log-format json`` form as well.
    """
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--log-format",
        choices=["text", "json"],
        default=argparse.SUPPRESS,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="depfix",
        description="Fix breaking dependency changes with AI",
    )
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=None)
    p.add_argument("--log-format", choices=["text", "json"], default=None)
    sub = p.add_subparsers(dest="command")

    # -- run --------------------------------------------------------------------
    run_p = sub.add_parser("run", help="Run the LLM fixer on a codebase")
    run_p.add_argument("--codebase", "-c", type=str, default=None, help="Path to codebase to fix")
    run_p.add_argument("--output", "-o", type=str, default=None, help="Where to write .patch diffs")
    run_p.add_argument("--show-diffs", action="store_true", help="Print full diffs")
    run_p.add_argument(
        "--change-file",
        type=str,
        default=None,
        help="Path to a JSON file with a single BreakingChange (see core.models.BreakingChange)",
    )
    run_p.add_argument(
        "--from-event",
        type=int,
        default=None,
        metavar="ID",
        help="breaking_change row id, as produced by `depfix classify`",
    )
    run_p.add_argument(
        "--api-key", type=str, default=None, help="Google API key (or set GOOGLE_API_KEY)"
    )
    run_p.add_argument("--model", type=str, default=None, help="Gemini model name (overrides env)")
    run_p.add_argument(
        "--provider",
        type=str,
        choices=["gemini", "ollama", "bedrock"],
        default=None,
        help="LLM backend (default: LLM_PROVIDER env, else gemini)",
    )
    run_p.add_argument(
        "--ollama-model",
        type=str,
        default=None,
        help="Ollama model tag when --provider=ollama (default: qwen2.5-coder:7b)",
    )
    _add_command_logging_arguments(run_p)

    # -- watch ------------------------------------------------------------------
    watch_p = sub.add_parser(
        "watch", help="Poll every provider's change feeds for breaking changes"
    )
    watch_p.add_argument(
        "--providers-file",
        type=str,
        default=None,
        help="Path to providers.yaml (default: PROVIDERS_FILE env)",
    )
    watch_p.add_argument(
        "--provider",
        action="append",
        default=None,
        metavar="ID",
        help="Only watch this provider id. Repeatable.",
    )
    watch_p.add_argument(
        "--once", action="store_true", help="Single pass and exit (the only mode)."
    )
    watch_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Poll but persist nothing. Every feed reports as a baseline.",
    )
    watch_p.add_argument(
        "--show-changes",
        action="store_true",
        help="Print the full structural change list for each event",
    )
    watch_p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore FEED_POLL_TTL_SECONDS and poll every feed now.",
    )
    _add_command_logging_arguments(watch_p)

    # -- classify -----------------------------------------------------------------
    classify_p = sub.add_parser(
        "classify", help="Classify persisted change events into breaking changes"
    )
    classify_p.add_argument(
        "--limit", type=int, default=None, help="Classify at most this many events"
    )
    classify_p.add_argument(
        "--output-dir",
        default="output/changes",
        help="Write one generated JSON artifact per created breaking change",
    )
    classify_p.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip the release-notes LLM path; only spec-diff events classify",
    )
    _add_command_logging_arguments(classify_p)

    # -- eval ---------------------------------------------------------------------
    eval_p = sub.add_parser("eval", help="Score the classify/fixer pipeline against the corpus")
    eval_p.add_argument(
        "--corpus-dir",
        type=str,
        default=None,
        help="Directory of *.yaml corpus files (default: EVAL_CORPUS_DIR env)",
    )
    eval_p.add_argument(
        "--category",
        action="append",
        choices=[c.value for c in EvalCategory],
        default=None,
        help="Only run this category. Repeatable.",
    )
    eval_p.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip cases that require an LLM (marks them skipped, not failed)",
    )
    eval_p.add_argument(
        "--fail-under",
        type=float,
        default=1.0,
        help="Exit non-zero if the scored pass rate falls below this (default: 1.0)",
    )
    eval_p.add_argument(
        "--save", type=str, default=None, metavar="PATH", help="Write the JSON report to PATH"
    )
    eval_p.add_argument(
        "--compare",
        type=str,
        default=None,
        metavar="PATH",
        help="Compare against a previously-saved report; exit non-zero on any regression",
    )
    _add_command_logging_arguments(eval_p)

    status_p = sub.add_parser("status", help="Show the orchestrator attempt ledger")
    status_p.add_argument("--repo", action="append", default=None, metavar="OWNER/NAME")
    status_p.add_argument("--status", default=None, help="Filter by attempt status")
    status_p.add_argument("--limit", type=int, default=50)
    status_p.add_argument("-v", "--verbose", action="store_true")
    status_p.add_argument(
        "--reconcile", action="store_true", help="Cross-reference open depfix PRs on GitHub"
    )
    _add_command_logging_arguments(status_p)

    # -- scan -------------------------------------------------------------------
    scan_p = sub.add_parser("scan", help="Find SDK call sites in a local checkout or a cloned repo")
    scan_p.add_argument(
        "--repo",
        type=str,
        default=None,
        metavar="OWNER/NAME",
        help="Remote repo to clone via the GitHub App and scan",
    )
    scan_p.add_argument(
        "--path", type=str, default=None, help="Local directory to scan instead of cloning"
    )
    scan_p.add_argument(
        "--ref",
        type=str,
        default=None,
        help="Branch/tag/sha to clone (only with --repo; default: the repo's default branch)",
    )
    scan_target_p = scan_p.add_mutually_exclusive_group(required=True)
    scan_target_p.add_argument(
        "--provider", type=str, help="Provider id from providers.yaml to scan for"
    )
    scan_target_p.add_argument(
        "--package",
        type=str,
        help="SDK package name; compatibility alias that resolves its configured provider",
    )
    scan_p.add_argument(
        "--providers-file",
        type=str,
        default=None,
        help="Path to providers.yaml (default: PROVIDERS_FILE env)",
    )
    scan_p.add_argument(
        "--change-file",
        type=str,
        default=None,
        help="Narrow the scan to one BreakingChange's symbols (see `run --change-file`)",
    )
    scan_p.add_argument(
        "--from-event",
        type=int,
        default=None,
        metavar="ID",
        help="Narrow the scan to a breaking_change row's symbols",
    )
    scan_p.add_argument(
        "--old-api",
        type=str,
        default=None,
        help="Narrow the scan to an older API prefix (for example openai.Completion)",
    )
    scan_p.add_argument(
        "--no-save", action="store_true", help="Don't persist this scan to the database"
    )
    scan_p.add_argument(
        "--anyversion",
        action="store_true",
        help="Produce a PR artifact for a version drift of ANY distance "
        "(overrides DRIFT_SCOPE for this run and persists to .depfix-local.yml).",
    )
    scan_p.add_argument(
        "--show-low",
        action="store_true",
        help="Also print LOW-confidence (report-only, never auto-fixed) call sites",
    )
    scan_p.add_argument(
        "--write-report",
        action="store_true",
        help="Write .depfix/scan-report.md locally, or to a dedicated depfix branch for --repo",
    )
    _add_command_logging_arguments(scan_p)

    # -- repos --------------------------------------------------------------------
    repos_p = sub.add_parser(
        "repos", help="List repos visible to the configured GitHub App installation(s)"
    )
    repos_p.add_argument(
        "--installation",
        type=int,
        default=None,
        metavar="ID",
        help="Only list repos for this installation id",
    )
    _add_command_logging_arguments(repos_p)

    # -- pr ---------------------------------------------------------------------
    pr_p = sub.add_parser("pr", help="Pull request operations")
    pr_sub = pr_p.add_subparsers(dest="pr_command")
    pr_list_p = pr_sub.add_parser("list", help="List open depfix pull requests")
    pr_list_p.add_argument(
        "--repo",
        type=str,
        required=True,
        metavar="OWNER/NAME",
        help="Repository to list PRs for",
    )
    _add_command_logging_arguments(pr_list_p)

    # Shared workflow arguments. This parser is intentionally not registered
    # as a command: plan/apply are the public seam for all edits and PRs.
    workflow_p = argparse.ArgumentParser(add_help=False)
    workflow_p.add_argument(
        "repos",
        nargs="*",
        help="OWNER/NAME; omit to use every repository visible to the GitHub App",
    )
    workflow_p.add_argument(
        "--repo",
        type=str,
        action="append",
        default=None,
        metavar="OWNER/NAME",
        help="Repo to process (repeatable). Default: every repo visible to the "
        "configured GitHub App installation(s)",
    )
    workflow_p.add_argument(
        "--provider",
        type=str,
        action="append",
        default=None,
        help="Restrict to breaking changes from this provider id (repeatable)",
    )
    workflow_p.add_argument(
        "--providers-file",
        type=str,
        default=None,
        help="Path to providers.yaml (default: PROVIDERS_FILE env)",
    )
    workflow_p.add_argument(
        "--change-file",
        type=str,
        action="append",
        default=None,
        help="Path to a JSON file with a single BreakingChange (repeatable). Default: "
        "every classified breaking_change row in the database",
    )
    workflow_p.add_argument(
        "--from-event",
        type=int,
        action="append",
        default=None,
        metavar="ID",
        help="breaking_change row id to include (repeatable)",
    )
    workflow_p.add_argument(
        "--plan-file",
        default=None,
        metavar="PATH",
        help="For apply: consume this immutable plan artifact instead of calling the LLM again.",
    )
    workflow_p.add_argument(
        "--plan-output-dir",
        default="output/plans",
        help="For plan: directory for verified immutable plan artifacts.",
    )
    workflow_p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of breaking_change rows loaded from the database "
        "(only applies when neither --change-file nor --from-event is given)",
    )
    workflow_p.add_argument(
        "--api-key", type=str, default=None, help="Google API key (or set GOOGLE_API_KEY)"
    )
    workflow_p.add_argument(
        "--model", type=str, default=None, help="Gemini model name (overrides env)"
    )
    workflow_p.add_argument(
        "--llm-provider",
        type=str,
        choices=["gemini", "ollama", "bedrock"],
        default=None,
        help="LLM backend (default: LLM_PROVIDER env, else gemini)",
    )
    workflow_p.add_argument(
        "--ollama-model",
        type=str,
        default=None,
        help="Ollama model tag when --llm-provider=ollama (default: qwen2.5-coder:7b)",
    )
    workflow_p.add_argument(
        "--force",
        action="store_true",
        help="Bypass bookkeeping skips (already-terminal, max attempts, cooldown, max "
        "open PRs, max changes per run) -- a repo's own .depfix.yml is never bypassed",
    )
    workflow_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the real scan -> fix -> verify pipeline but write nothing: no ledger "
        "row, no branch commit, no PR. Reported outcomes are predictions (planned=True), "
        "the first rung of the onboarding rehearsal ladder before --no-pr and live.",
    )
    workflow_p.add_argument(
        "--no-pr",
        action="store_true",
        help="Fix and verify for real (real ledger writes) but never open a PR, "
        "regardless of what a repo's own .depfix.yml says -- the middle rung of the "
        "onboarding rehearsal ladder, between --dry-run and live.",
    )
    workflow_p.add_argument(
        "--max-duration",
        type=float,
        default=None,
        metavar="MINUTES",
        help="Stop cleanly between repos after this many minutes (default: "
        "PIPELINE_MAX_DURATION_MINUTES).",
    )
    workflow_p.add_argument(
        "--max-cost",
        type=float,
        default=None,
        metavar="USD",
        help="Stop between changes once LLM spend reaches this USD amount (default: "
        "PIPELINE_MAX_COST_USD_PER_RUN).",
    )
    workflow_p.add_argument(
        "--no-lock",
        action="store_true",
        help="Proceed without the advisory lock; intended only for deliberate parallel runs.",
    )
    workflow_p.add_argument("--no-codemods", action="store_true")
    workflow_p.add_argument(
        "--no-upgrade-plans",
        action="store_true",
        help="Disable combined upgrade PRs; emit one PR per classified change "
        "instead of bundling version bump + API migrations.",
    )
    workflow_p.add_argument("--container-image", default=None)
    workflow_p.add_argument("--container-runtime", choices=["docker", "podman"], default=None)
    workflow_p.add_argument(
        "--fallback-provider", choices=["gemini", "ollama", "bedrock"], default=None
    )
    workflow_p.add_argument("--fallback-model", default=None)
    workflow_p.add_argument(
        "--show-skips",
        action="store_true",
        help="Print a row for every skipped repo and change too, not just the ones "
        "actually processed.",
    )
    workflow_p.add_argument(
        "--reuse-scan",
        choices=["auto", "require", "never"],
        default=None,
        help="Where call sites come from. 'require' refuses to rescan, so a plan "
        "can only reflect a `depfix scan` you reviewed.",
    )
    _add_command_logging_arguments(workflow_p)

    # -- Terraform-style workflow ---------------------------------------------
    # Keep plan and apply on one shared argument interface so their safety,
    # budget, and provider behaviour cannot quietly drift.
    sub.add_parser(
        "plan",
        parents=[workflow_p],
        add_help=False,
        help="Read-only scan -> fix -> verify preview; never writes DB state, branches, or PRs",
    )
    sub.add_parser(
        "apply",
        parents=[workflow_p],
        add_help=False,
        help="Live scan -> fix -> verify -> PR run for opted-in repositories",
    )

    init_p = sub.add_parser(
        "init", help="Initialize the local database and run preflight diagnostics"
    )
    init_p.add_argument("--providers-file", default=None, help="Path to providers.yaml")
    init_p.add_argument(
        "--offline", action="store_true", help="Skip GitHub App and LLM network checks"
    )
    init_p.add_argument(
        "--keep-state",
        action="store_true",
        help="Keep existing local SQLite history instead of starting a fresh local run",
    )
    _add_command_logging_arguments(init_p)

    return p


def _cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    codebase = args.codebase or settings.codebase_path
    output = args.output or settings.output_dir
    api_key = args.api_key or settings.google_api_key
    model = args.model or settings.gemini_model
    provider = args.provider or settings.llm_provider
    ollama_model = args.ollama_model or settings.ollama_model

    if provider == "gemini" and not api_key:
        console.print("[red]Error: GOOGLE_API_KEY not set. Use --api-key or .env[/red]")
        return 2
    if not Path(codebase).exists():
        console.print(f"[red]Error: codebase not found: {codebase}[/red]")
        return 2

    try:
        breaking = _load_breaking_change(args)
    except (FileNotFoundError, ValueError, LookupError, json.JSONDecodeError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 2

    _print_header()
    _print_breaking_change(breaking)

    if provider == "ollama":
        console.print(f"[dim]Using provider: ollama  model: {ollama_model}[/dim]\n")
    else:
        console.print(f"[dim]Using provider: gemini  model: {model}[/dim]\n")

    agent = DependencyFixAgent(
        google_api_key=api_key,
        model=model,
        provider=provider,
        ollama_model=ollama_model,
        ollama_base_url=settings.ollama_base_url,
        ollama_timeout=settings.ollama_timeout,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning and fixing...", total=None)
        try:
            result = agent.fix_breaking_change(
                breaking_change=breaking,
                codebase_path=codebase,
                output_dir=output,
            )
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            logger.exception("Agent failed")
            return 1
        progress.update(task, completed=True)

    console.print()
    _print_results(result)
    if args.show_diffs:
        _print_diffs(result, show_full=True)
    if result.fixes:
        console.print(
            f"[green]✅ Diffs written to: {output}[/green]\n"
            f"[dim]Apply with: git apply {output}/*.patch[/dim]"
        )
    return 0 if result.success_rate == 1.0 else 1


def _cmd_status(args: argparse.Namespace) -> int:
    """Show the ledger and optionally cross-reference open GitHub PRs."""
    from depfix.storage.schema import ChangeAttemptRow, RepoRow

    init_schema()
    settings = get_settings()
    reconciled_by_repo: dict[str, dict[str, str]] = {}
    if args.reconcile:
        gh_auth = _build_gh_auth(settings)
        if gh_auth is None:
            console.print("[yellow]--reconcile skipped: GitHub App not configured.[/yellow]")
        else:
            try:
                reconciled_by_repo = _reconcile_open_prs(gh_auth, args.repo)
            except GitHubAppError as exc:
                console.print(f"[yellow]--reconcile failed: {exc}[/yellow]")
            finally:
                gh_auth.close()

    with session_scope() as session:
        repos = args.repo or [row.id for row in session.scalars(select(RepoRow)).all()]
        if not repos:
            console.print("[dim]No repos have been processed yet.[/dim]")
            return 0
        table = Table(title="Change Attempt Ledger")
        table.add_column("Repo", style="cyan")
        table.add_column("Dedupe key", style="dim")
        table.add_column("Ledger status")
        if args.reconcile:
            table.add_column("GitHub")
        table.add_column("Attempts", justify="right")
        table.add_column("Updated", style="dim")
        drift_count = 0
        shown = 0
        for repo_id in repos:
            github_state = reconciled_by_repo.get(repo_id, {})
            rows = session.scalars(
                select(ChangeAttemptRow)
                .where(ChangeAttemptRow.repo_id == repo_id)
                .order_by(ChangeAttemptRow.updated_at.desc())
            )
            for row in rows:
                if args.status and row.status != args.status:
                    continue
                cells = [row.repo_id, row.dedupe_key[:12], _status_cell(row.status)]
                if args.reconcile:
                    github_cell, drifted = _github_cell(
                        row.status, github_state.get(row.dedupe_key[:12])
                    )
                    cells.append(github_cell)
                    drift_count += int(drifted)
                cells += [
                    str(row.attempts_used),
                    row.updated_at.isoformat(" ", "seconds") if row.updated_at else "-",
                ]
                table.add_row(*cells)
                shown += 1
                if shown >= args.limit:
                    break
            if shown >= args.limit:
                break
        console.print(table if shown else "[dim]No matching ledger entries.[/dim]")
        if args.reconcile and drift_count:
            console.print(
                f"\n[yellow]{drift_count} drift(s) detected. Re-run "
                "`depfix pipeline --force <repo>` on those changes to resync.[/yellow]"
            )
        if args.verbose:
            errors = session.scalars(
                select(ChangeAttemptRow)
                .where(ChangeAttemptRow.last_error != "")
                .order_by(ChangeAttemptRow.updated_at.desc())
                .limit(args.limit)
            ).all()
            if errors:
                console.print("\n[bold]Recent failure details:[/bold]")
                for row in errors:
                    console.print(
                        f"\n[cyan]{row.repo_id}[/cyan] · [dim]{row.dedupe_key[:12]}[/dim]"
                    )
                    console.print(f"  {row.last_error}")
        _print_merge_rate(session, args.repo)
    return 0


_STATUS_STYLE_MAP = {
    "pr_opened": "green",
    "fixed_no_pr": "cyan",
    "no_call_sites": "dim",
    "version_not_affected": "dim",
    "report_only": "yellow",
    "no_kept_edits": "yellow",
    "failed": "red",
    "ignored": "dim",
}


def _status_cell(status: str) -> str:
    return f"[{_STATUS_STYLE_MAP.get(status, 'white')}]{status}[/{_STATUS_STYLE_MAP.get(status, 'white')}]"


def _reconcile_open_prs(gh_auth, repo_filter: list[str] | None) -> dict[str, dict[str, str]]:
    """Persist observed merged/closed PR outcomes, then read back which of
    depfix's PRs are still open for ``depfix status``'s display.

    The actual GitHub-state reconciliation (list live PRs, blame closed
    rows against ``get_pull_request_state``) is
    :func:`depfix.orchestrator.reconcile.reconcile_open_prs` -- the same
    function the fleet orchestrator runs before a pass -- so the two entry
    points can't drift on what "reconciled" means. After that pass, any
    row still ``state == "open"`` in the ledger reflects the live PR list
    it was just checked against, so the display data comes straight from
    the DB rather than a second round of GitHub API calls.
    """
    from depfix.orchestrator.reconcile import reconcile_open_prs
    from depfix.storage.schema import FixRunRow, PullRequestRow

    result: dict[str, dict[str, str]] = {}
    with session_scope() as session:
        repos = repo_filter or list(session.scalars(select(FixRunRow.repo_id).distinct()).all())
        reconcile_open_prs(session, gh_auth, repos)

        rows = session.scalars(
            select(PullRequestRow)
            .join(PullRequestRow.fix_run)
            .where(FixRunRow.repo_id.in_(repos), PullRequestRow.state == "open")
        ).all()
        for row in rows:
            result.setdefault(row.fix_run.repo_id, {})[row.fix_run.dedupe_key[:12]] = "open"
    return result


def _print_merge_rate(session, repo_filter: list[str] | None) -> None:
    from depfix.storage.schema import FixRunRow, PullRequestRow

    statement = (
        select(PullRequestRow).join(PullRequestRow.fix_run).where(PullRequestRow.state != "open")
    )
    if repo_filter:
        statement = statement.where(FixRunRow.repo_id.in_(repo_filter))
    rows = session.scalars(statement).all()
    if not rows:
        return
    merged = sum(row.merged for row in rows)
    total = len(rows)
    rate = merged / total
    color = "green" if rate >= 0.7 else "yellow" if rate >= 0.3 else "red"
    console.print(
        f"\n[bold]Merge rate:[/bold] [{color}]{rate:.0%}[/{color}] "
        f"([dim]{merged} merged / {total} resolved; {total - merged} closed unmerged[/dim])"
    )


def _github_cell(ledger_status: str, github_state: str | None) -> tuple[str, bool]:
    if ledger_status == "pr_opened":
        return (
            ("[green]open[/green]", False)
            if github_state == "open"
            else ("[yellow]closed/merged[/yellow]", True)
        )
    if github_state == "open":
        return "[red]open (stale)[/red]", True
    return "[dim]—[/dim]", False


def _cmd_pr_list(args: argparse.Namespace) -> int:
    """List open pull requests created by depfix for one repository."""
    settings = get_settings()
    gh_auth = _build_gh_auth(settings)
    if gh_auth is None:
        console.print(
            "[red]Error: GitHub App not configured. Set GITHUB_APP_ID and either "
            "GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH.[/red]"
        )
        return 2

    owner, name = args.repo.split("/", 1)
    try:
        prs = gh_auth.list_open_pull_requests(owner, name)
    except GitHubAppError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 1
    finally:
        gh_auth.close()

    depfix_prs = [pr for pr in prs if pr.head_branch.startswith(BRANCH_PREFIX)]
    other_count = len(prs) - len(depfix_prs)
    if not depfix_prs:
        console.print(f"No open depfix PRs for {args.repo}")
    else:
        table = Table(title=f"Open depfix PRs — {args.repo}")
        table.add_column("#", style="cyan", justify="right")
        table.add_column("Branch")
        table.add_column("Base")
        table.add_column("URL")
        for pr in depfix_prs:
            table.add_row(str(pr.number), pr.head_branch, pr.base_branch, pr.html_url)
        console.print(table)
    if other_count:
        console.print(f"[dim]({other_count} other open PR(s) not opened by depfix)[/dim]")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # Backwards compatibility: `depfix --codebase X` (no subcommand) → `run`.
    # But keep `-h` / `--help` at the top level so users discover subcommands.
    # Only a leading option can be the historical no-subcommand form
    # (``depfix --codebase …``). A leading word must be a real command so a
    # removed command is reported as such instead of being misread as `run`.
    needs_default = bool(raw) and raw[0].startswith("-") and raw[0] not in _TOP_LEVEL_FLAGS
    if needs_default:
        raw = ["run", *raw]

    args = build_parser().parse_args(raw)

    settings = get_settings()
    from depfix.defaults import apply_defaults_to_settings, load_local_defaults

    apply_defaults_to_settings(settings, load_local_defaults())
    log_level = getattr(args, "log_level", None) or settings.log_level
    # Human-readable in a developer's terminal, JSON lines everywhere else
    # (staging/production/test) where a log aggregator, not a human, is the
    # reader. See depfix.obs.logging for the JSON shape.
    log_format = getattr(args, "log_format", None) or settings.log_format
    configure_logging(log_level, json_output=log_format == "json")
    configure_sentry(settings.sentry_dsn, environment=settings.environment)

    if args.command == "watch":
        return _cmd_watch(args)
    if args.command == "classify":
        return _cmd_classify(args)
    if args.command == "eval":
        return _cmd_eval(args)
    if args.command == "scan":
        return _cmd_scan(args)
    if args.command == "repos":
        return _cmd_repos(args)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "pr":
        if args.pr_command == "list":
            return _cmd_pr_list(args)
        console.print("[red]Error: pass `depfix pr list --repo OWNER/NAME`.[/red]")
        return 2
    if args.command in {"plan", "apply"}:
        return _cmd_pipeline(args)
    return _cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
