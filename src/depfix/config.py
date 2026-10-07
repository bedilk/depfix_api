"""Application configuration via Pydantic Settings.

All runtime knobs are read from environment variables (loaded from `.env`
when present). CLI flags in :mod:`depfix.cli` override these.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE_DIR = Path(__file__).resolve().parent

#: Hard ceiling on how many files a single repository scan may read.
#:
#: ``scan_max_files`` below is the knob an operator turns; this is the
#: number they cannot turn it past. Enforced twice on purpose: here, so
#: a bad ``SCAN_MAX_FILES`` fails at start-up rather than halfway through
#: a fleet run; and in every scanner via
#: :func:`depfix.scanners.limits.clamp_max_files`, so a programmatic caller
#: (a test, a script, a future adapter) cannot exceed it either.
MAX_SCANNABLE_FILES = 15_000


class Settings(BaseSettings):
    """Runtime settings for depfix."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",  # variables are read verbatim (e.g. GOOGLE_API_KEY)
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM ------------------------------------------------------------------
    llm_provider: Literal["gemini", "ollama", "bedrock"] = Field(
        default="gemini", description="Which LLM backend to use for `depfix run`"
    )
    google_api_key: str = Field(default="", description="Google Generative AI API key")
    gemini_model: str = Field(
        default="gemini-2.5-flash", description="Gemini model ID for heavy tasks"
    )
    gemini_light_model: str = Field(
        default="gemini-2.0-flash-lite",
        description="Gemini model ID for lightweight tasks. Empty means use gemini_model for everything.",
    )

    # --- AWS Bedrock (Claude) -------------------------------------------------
    bedrock_access_key: str = Field(default="", description="AWS Bedrock access key")
    bedrock_secret_key: str = Field(default="", description="AWS Bedrock secret key")
    bedrock_region: str = Field(default="us-east-1", description="AWS region for Bedrock")
    bedrock_profile: str = Field(default="", description="AWS profile name for Bedrock auth")
    bedrock_model: str = Field(
        default="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        description="Bedrock model ID for heavy tasks (fix generation, characterization, retry).",
    )
    bedrock_light_model: str = Field(
        default="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        description="Bedrock model ID for lightweight tasks (classification, scanning, judging). "
        "Set to empty string to use bedrock_model for everything.",
    )
    llm_max_retries: int = Field(default=3, ge=1, le=10)
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    fix_strategy: Literal["single_shot", "agent"] = Field(
        default="single_shot",
        description="Fix-generation strategy. The tool-assisted agent is opt-in.",
    )
    fix_agent_max_steps: int = Field(
        default=6,
        ge=1,
        le=10,
        description="Maximum read-only tool rounds for one agent-generated file edit.",
    )

    # --- Ollama (local LLM) ---------------------------------------------------
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="qwen2.5-coder:7b")
    ollama_light_model: str = Field(
        default="",
        description="Ollama model for lightweight tasks. Empty means use ollama_model for everything.",
    )
    ollama_timeout: float = Field(default=120.0, ge=1.0, le=600.0)

    # --- Scanner defaults -----------------------------------------------------
    codebase_path: str = Field(default="./tests/fixtures/openai_v3_project")
    output_dir: str = Field(default="./output/fixes")
    context_lines: int = Field(default=5, ge=0, le=50)

    # --- Infra -----------------------------------------------------------------
    database_url: str = Field(
        default="sqlite:///depfix.db",
        description="Database URL. Defaults to a local SQLite file for development; "
        "set a postgresql+psycopg:// URL for a production fleet deployment.",
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

    # --- npm registry (used by sources.npm_dist_tag feeds) --------------------
    npm_registry_url: str = Field(default="https://registry.npmjs.org")
    npm_request_timeout: float = Field(default=10.0, ge=1.0, le=60.0)

    # --- PyPI registry (used by sources.pypi_dist_tag feeds) -----------------
    pypi_registry_url: str = Field(default="https://pypi.org")
    pypi_request_timeout: float = Field(default=10.0, ge=1.0, le=60.0)

    # --- RubyGems registry (used by sources.rubygems_dist_tag feeds) --------
    rubygems_registry_url: str = Field(default="https://rubygems.org")
    rubygems_request_timeout: float = Field(default=10.0, ge=1.0, le=60.0)

    # --- Drift policy ---------------------------------------------------------
    drift_max_major_gap: int = Field(
        default=0,
        ge=0,
        description="How many major steps a registry drift may cross and still be treated as "
        "an automatic manifest edit. 0 (default) means any major change is a draft review PR.",
    )
    drift_max_minor_gap: int | None = Field(
        default=None,
        ge=0,
        description="Downgrade a same-major drift to a draft review PR once the minor gap "
        "exceeds this. None (default) keeps every same-major drift automatic.",
    )
    drift_scope: str = Field(
        default="one-major",
        description="Which registry version drifts produce a PR artifact.\n"
        "  one-major: a same-major drift OR a single-major step (N -> N+1) is "
        "actionable; a gap of 2+ majors is report-only.\n"
        "  any-minor: only a same-major drift (any minor/patch distance) is "
        "actionable; ANY major step is report-only.\n"
        "  any: every forward drift is actionable regardless of distance. "
        "Enabled per-run by `depfix scan --anyversion`.",
    )

    # --- change sources (Week 1) ----------------------------------------------
    providers_file: str = Field(
        default="providers.yaml", description="YAML list of providers and their feeds"
    )
    http_timeout: float = Field(default=20.0, ge=1.0, le=300.0)
    github_token: str = Field(
        default="",
        description="GitHub PAT. Unauthenticated is 60 req/hr, which two feeds "
        "exhaust in a day of cron ticks; authenticated is 5000.",
    )
    github_api_url: str = Field(default="https://api.github.com")
    spec_cache_dir: str = Field(
        default="./.depfix-cache/specs",
        description="Where previous OpenAPI documents are cached for diffing.",
    )
    max_spec_bytes: int = Field(
        default=32 * 1024 * 1024, ge=1024, description="Reject specs larger than this."
    )

    # --- classification (Week 2) -----------------------------------------------
    classify_min_confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="LLM-derived changes below this confidence are dropped by NotesClassifier.",
    )

    # --- eval harness (Week 2) --------------------------------------------------
    eval_corpus_dir: str = Field(
        default=str(_PACKAGE_DIR / "evals" / "corpus"),
        description="Directory of eval corpus YAML files.",
    )
    eval_results_dir: str = Field(
        default="./.depfix-cache/eval-results",
        description="Where `depfix eval --save` writes JSON reports.",
    )

    # --- GitHub App (Week 3) ----------------------------------------------------
    # Distinct from `github_token` above: that's a PAT used for polling public
    # feeds; this is a GitHub App used to clone and scan a specific installed
    # repo with a short-lived, least-privilege token minted per scan.
    github_app_id: str = Field(default="", description="GitHub App ID (numeric, as a string).")
    github_app_private_key: str = Field(
        default="",
        description="PEM-encoded RSA private key for the GitHub App, inline. "
        "Mutually exclusive with github_app_private_key_path (this one wins if both are set).",
    )
    github_app_private_key_path: str = Field(
        default="",
        description="Path to a .pem file with the GitHub App's private key, "
        "for environments where an inline multi-line env var is awkward.",
    )

    # --- Repo checkout (Week 3) --------------------------------------------------
    clone_dir: str = Field(
        default="", description="Base directory for repo clones. Empty uses the system temp dir."
    )
    clone_timeout: float = Field(default=120.0, ge=1.0, le=600.0)
    clone_max_repo_mb: int = Field(
        default=500, ge=1, description="Reject a clone once its working tree exceeds this size."
    )

    # --- Repo/call-site scanner (Week 3) -----------------------------------------
    scan_max_file_bytes: int = Field(
        default=2_000_000,
        ge=1024,
        description="Skip individual source files larger than this during a repo scan.",
    )
    scan_max_files: int = Field(
        default=MAX_SCANNABLE_FILES,
        ge=1,
        le=MAX_SCANNABLE_FILES,
        description=(
            "Files scanned per repo, to bound worst-case scan time. Cannot exceed "
            f"MAX_SCANNABLE_FILES ({MAX_SCANNABLE_FILES}); a larger value is a "
            "configuration error, not a silently-clamped request."
        ),
    )
    scan_max_data_mb: float = Field(
        default=50.0,
        ge=0.1,
        description=(
            "Hard cap on total data read across all scanned files per repo (in MB). "
            "The scanner stops accepting new files once cumulative bytes read reaches this limit."
        ),
    )
    scan_min_recall: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Recall floor for the call_sites eval category. Per docs/plan.md, "
        "falling below this is the trigger to bring in tree-sitter instead of regex.",
    )
    scan_type_resolve: bool = Field(
        default=True,
        description="Use the repo's own type checker (tsc for TS, pyright for Python) to "
        "find SDK call sites with declaration evidence. Falls back silently when "
        "the type checker is unavailable.",
    )
    scan_align_symbols: bool = Field(
        default=True,
        description="When changes remain unmatched after deterministic scanning, run one "
        "cheap LLM call per (provider, commit) to align documented API names "
        "with observed symbols. Skipped when nothing is ambiguous.",
    )
    scan_max_cost_usd: float | None = Field(
        default=0.10,
        ge=0.0,
        description="Hard budget for LLM spend within one scan. The scan stops cleanly and "
        "reports partial results when this is exceeded. None means uncapped.",
    )
    scan_judge_model: str = Field(
        default="",
        description="Model ID for the cheap alignment/judge calls during scan. Empty means "
        "use the main model. Set to a flash/haiku model to reduce cost.",
    )
    scan_llm_judge_enabled: bool = Field(default=False)
    scan_llm_judge_max_candidates: int = Field(default=20, ge=1, le=100)
    scan_prepare_package_manager: bool = Field(
        default=True,
        description="During depfix scan, prepare a repository's declared npm/Yarn/pnpm runtime "
        "for a later local verification. Yarn and pnpm are cached under ~/.depfix, never globally.",
    )
    package_manager_install_timeout: float = Field(
        default=90.0,
        ge=1.0,
        le=600.0,
        description="Wall-clock cap for installing a repository-pinned package-manager runtime.",
    )

    # --- Fix generation + verification (Week 4) ----------------------------------
    fix_validate_syntax: bool = Field(
        default=True,
        description="Run FixValidator's syntax/sanity checks before writing an edit to disk.",
    )
    verify_enabled: bool = Field(
        default=True,
        description="Whether `depfix plan`/`apply` runs the repo's own test suite to confirm "
        "each edit. Off by default in CI-less environments where no runtime is available.",
    )
    verify_install_timeout: float = Field(
        default=300.0,
        ge=1.0,
        le=1800.0,
        description="Wall-clock cap for `npm ci`/`yarn install`/`pnpm install` before Verifier "
        "gives up and marks the run's edits SUSPECT rather than hanging indefinitely.",
    )
    verify_test_timeout: float = Field(
        default=300.0,
        ge=1.0,
        le=1800.0,
        description="Wall-clock cap for one baseline or after-fix test run.",
    )
    verify_typecheck_enabled: bool = Field(default=True)
    plan_impact_check_enabled: bool = Field(
        default=True,
        description="During `depfix plan`/--dry-run, before generating any fix: run the "
        "repo's test suite on the current dependency tree, bump the affected dependency "
        "to the post-change version, run the suite again, and report which tests newly "
        "fail. Confirms (or fails to confirm) that the detected breaking change actually "
        "breaks this repo, locally, before an LLM is ever asked for a fix.",
    )
    impact_check_max_seconds: float = Field(
        default=600.0,
        ge=0.0,
        description="Total wall-clock budget for one plan-time impact check.",
    )
    verify_typecheck_timeout: float = Field(default=300.0, ge=1.0, le=1800.0)
    plan_reuse_scan: Literal["auto", "require", "never"] = Field(
        default="auto",
        description="Where plan/apply get call sites. 'auto': reuse a matching "
        "`depfix scan` result, else rescan. 'require': refuse to rescan, so the "
        "artifact can only ever reflect a scan the operator reviewed. 'never': "
        "always rescan (debugging).",
    )
    plan_dir: str = Field(default=".depfix/plans")
    learned_migrations_file: str = Field(
        default=".depfix/learned_migrations.yaml",
        description="Per-repo YAML store of confirmed breaking changes discovered during scanning.",
    )
    learned_migrations_include_candidates: bool = Field(default=False)
    repository_observations_file: str = Field(
        default=".depfix/repository_migrations.yaml",
        description="Where `depfix scan` records resolved repository API usage. "
        "Local state covered by .gitignore — scan results (and local absolute "
        "paths) are never committed to the packaged catalog.",
    )
    apply_min_confidence: Literal["high", "medium", "low", "none"] = Field(default="low")
    draft_below_confidence: Literal["high", "medium", "low", "none"] = Field(default="high")
    codemods_enabled: bool = Field(default=True)
    verify_characterize_enabled: bool = Field(default=False)
    verify_characterize_timeout: int = Field(default=120, ge=1, le=1800)
    llm_fallback_provider: str = Field(default="")
    llm_fallback_model: str = Field(default="")

    # --- Always-run verification stages: characterization + smoke --------
    characterization_always_run: bool = Field(
        default=True,
        description="Run the CHARACTERIZATION stage on every fix, even when "
        "the repo already has tests covering the changed call sites. "
        "Costs LLM tokens per run. Set False to restore the old "
        "'only when coverage gap detected' behavior.",
    )
    characterization_strict: bool = Field(
        default=False,
        description="Promote a SKIPPED characterization result (no changed "
        "call sites to exercise, or LLM unavailable) to FAILED, blocking "
        "the PR. Off by default — pure library repos with no runnable "
        "call sites should not be blocked.",
    )
    smoke_always_run: bool = Field(
        default=True,
        description="Attempt the SMOKE stage on every fix. Depfix "
        "auto-detects a runnable entrypoint from package.json, pyproject, "
        "Dockerfile, Go main, Rails, etc. If nothing runnable is found, "
        "result is SKIPPED(no_entrypoint) — not a failure unless "
        "smoke_strict=True.",
    )
    smoke_strict: bool = Field(
        default=False,
        description="Promote 'no_entrypoint' from SKIPPED to FAILED. "
        "Useful for service repos where a missing entrypoint is itself a "
        "bug. Do NOT enable globally if you scan libraries.",
    )
    smoke_window_seconds: float = Field(
        default=8.0,
        description="How long the smoke run keeps a server process alive "
        "before declaring it healthy.",
    )
    smoke_extra_entrypoint_probes: list[str] = Field(
        default_factory=list,
        description="Additional commands tried as smoke entrypoints when "
        "auto-detection finds nothing. Tried in order; first non-crashing "
        "one wins. Example: ['python -m mypackage', './bin/serve'].",
    )

    # --- Behavioural evidence: coverage + HTTP contract (Week 8) ----------------
    verify_coverage_enabled: bool = Field(
        default=True,
        description="Record V8 coverage during the after-fix test run and check whether the "
        "migrated lines were actually executed. A suite that passes without ever running the "
        "changed line is not HIGH confidence, and this is what detects that.",
    )
    verify_contract_enabled: bool = Field(
        default=False,
        description="Record the SHAPE of outbound HTTP during both test runs (method, host, "
        "normalised path, header/body key names -- never values) and diff them. Answers the "
        "question repo tests usually can't: did the migrated call produce the same request? "
        "Off by default because it preloads a shim into the repo's test process.",
    )
    verify_flake_retries: int = Field(
        default=1,
        ge=0,
        le=5,
        description="Re-run the after-fix suite this many extra times when new failures appear. "
        "Only failures present in EVERY run are blamed on the fix; the rest are reported as "
        "flaky. 0 restores the old single-run behaviour.",
    )
    verify_static_check_enabled: bool = Field(
        default=True,
        description="Use a non-TypeScript static checker (mypy/pyright for Python, `go vet` for "
        "Go) as a MEDIUM-tier oracle when the repo's tests can't decide.",
    )
    verify_static_check_timeout: float = Field(default=300.0, ge=1.0, le=1800.0)
    verify_selective_tests: bool = Field(
        default=False,
        description="Run only the test files that reach a touched file (import graph + filename "
        "convention) instead of the whole suite. Much cheaper per fix, but a regression in an "
        "unrelated test is invisible -- opt in per fleet, never silently.",
    )

    # --- Scan strategy (deterministic / hybrid / agent) --------------------------
    scan_strategy: Literal["deterministic", "agent", "hybrid"] = Field(
        default="hybrid",
        description="Which scanner produces RepoScanResult and ChangeEvent. "
        "'hybrid' (recommended default) = deterministic scanner finds call sites "
        "first (free, O(n)), then an LLM agent augments ONLY where the deterministic "
        "scanner is weak: resolves LOW-confidence ambiguous bindings and finds raw "
        "HTTP calls with no SDK import. Bills the delta to SCAN_AGENT. Costs ~$0 "
        "when deterministic already succeeds cleanly; ~$0.01-0.05 when the agent "
        "actually adds value. Falls back gracefully to the deterministic result if "
        "no LLM is configured. "
        "'deterministic' = regex/AST scanners + HTTP pollers only (fast, free, "
        "zero-cost; excellent at well-formed SDK imports; use for offline runs). "
        "'agent' = pure LLM scan with no deterministic pass; for ecosystems the "
        "regex scanner genuinely cannot parse. Requires an LLM; refuses to fall "
        "back silently.",
    )
    scan_agent_max_steps: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Maximum read-only tool calls per agent invocation.",
    )
    scan_agent_judge_cap: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum LOW-confidence sites the CallSiteJudgeAgent will "
        "review per repo scan in hybrid mode. Sites beyond the cap keep their "
        "original LOW confidence without LLM review.",
    )

    # --- Feed poll TTL ------------------------------------------------------------
    feed_poll_ttl_seconds: int = Field(
        default=3600,
        ge=0,
        description="How long (seconds) a feed poll result is considered fresh. When a feed "
        "was last polled within this window the HTTP call is skipped and the stored "
        "token is reused. 0 disables TTL-based skipping (always poll). Useful for "
        "local dev or tests where you want fresh data every run.",
    )

    # --- Version cooldown gate ----------------------------------------------------
    version_cooldown_hours: float = Field(
        default=72.0,
        ge=0.0,
        description="Minimum age (hours) a newly published version must reach before "
        "depfix migrates a repo onto it -- a version's first hours are when a "
        "compromised release is most likely still live. Distinct from "
        "pipeline_retry_cooldown_hours, which paces depfix's own retries. Security "
        "advisories bypass this gate. 0 disables it.",
    )

    # --- Container sandbox -------------------------------------------------------
    sandbox_container_image: str = Field(
        default="",
        description="When set, every install/test/typecheck subprocess runs inside this container "
        "image instead of on the host. Empty keeps the existing process-level sandbox.",
    )
    sandbox_container_runtime: str = Field(
        default="docker",
        description="Container CLI used for sandbox_container_image (docker, podman, ...).",
    )

    # --- Prompt hygiene -----------------------------------------------------------
    redact_secrets_in_prompts: bool = Field(
        default=True,
        description="Scrub credential-shaped text out of file contents before they are sent to a "
        "language model. Leave this on; the only reason to turn it off is debugging redaction.",
    )
    lockfile_regenerate: bool = Field(default=True)
    verify_ignore_scripts: bool = Field(
        default=True,
        description="Pass --ignore-scripts to the install command so a hostile repo's "
        "package.json can't run arbitrary code (postinstall, etc.) during verification.",
    )
    verify_max_output_bytes: int = Field(
        default=2_000_000,
        ge=1024,
        description="Truncate captured install/test stdout+stderr past this size, so a "
        "runaway or malicious process can't exhaust memory holding its output.",
    )

    # --- Retry loop (Week 5) ----------------------------------------------------
    retry_max_rounds: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Bounded number of times `depfix plan`/`apply` re-attempts a SUSPECT/REVERTED "
        "edit by feeding its concrete failure back to the LLM. 0 disables retries.",
    )

    # --- Fleet orchestrator (Week 6) --------------------------------------------
    repo_config_filename: str = Field(
        default=".depfix.yml",
        description="Per-repo opt-in pipeline config file the orchestrator looks for "
        "at the repo root before touching it at all.",
    )
    pipeline_require_config_file: bool = Field(
        default=True,
        description="Refuse to touch a repo that has no repo_config_filename at all. "
        "The whole point of the fleet orchestrator is that it never acts on a repo "
        "that hasn't explicitly opted in.",
    )
    pipeline_max_open_prs: int = Field(
        default=5,
        ge=1,
        description="Per-repo cap on simultaneously open depfix-authored PRs. Once "
        "reached, the orchestrator skips new changes for that repo until one closes.",
    )
    pipeline_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Per-(repo, change) cap on non-terminal orchestrator attempts "
        "(see AttemptStatus.is_terminal) before it gives up and leaves the change FAILED.",
    )
    pipeline_retry_cooldown_hours: float = Field(
        default=6.0,
        ge=0.0,
        description="Minimum time between two orchestrator attempts at the same "
        "(repo, change), so a flaky failure doesn't get hammered on every cron tick.",
    )
    pipeline_max_changes_per_repo: int = Field(
        default=3,
        ge=1,
        description="Hard cap on how many distinct breaking changes the orchestrator "
        "processes for one repo in a single run, overridable lower (never higher) by "
        "that repo's own .depfix.yml max_changes_per_run.",
    )
    pipeline_max_cost_usd_per_run: float | None = Field(
        default=None,
        ge=0.0,
        description="Wall-clock budget's cost-ceiling counterpart: once this run's "
        "cumulative FixPipelineResult.total_cost crosses this, the orchestrator stops "
        "before the next repo/change the same way an expired --max-duration does. "
        "None (default) means uncapped -- only the --max-duration wall clock applies.",
    )
    pipeline_default_change_limit: int = Field(
        default=50,
        ge=1,
        description="Maximum classified changes considered by a normal fleet run.",
    )
    pipeline_newest_first: bool = Field(
        default=True,
        description="Consider recent breaking changes before historical backlog.",
    )
    pipeline_max_duration_minutes: float | None = Field(
        default=None,
        ge=0.0,
        description="Default fleet-run wall-clock budget in minutes; None is unlimited.",
    )
    pipeline_require_lock: bool = Field(
        default=True,
        description="Require the Postgres advisory lock for live fleet runs.",
    )
    upgrade_plans_enabled: bool = Field(
        default=True,
        description="Compose a registry drift and the API migrations inside its version "
        "window into one verified PR. False restores one PR per classified change.",
    )
    upgrade_max_migrations: int = Field(
        default=8,
        ge=1,
        description="Refuse an upgrade whose window contains more applicable migrations "
        "than this; a very wide window is a review job, not an automated PR.",
    )

    # --- Observability --------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    environment: Literal["development", "staging", "production", "test"] = "development"
    sentry_dsn: str = Field(default="")
    log_format: Literal["text", "json"] = "text"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (env loaded once per process)."""
    return Settings()
