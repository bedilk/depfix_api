"""Tests for command-line parsing behaviour."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from depfix import cli
from depfix.cli import _cmd_pipeline, _scan_location, build_parser
from depfix.config import Settings
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.providers.loader import ProviderConfigError
from depfix.scanners.models import (
    CallSite,
    CallSiteKind,
    DeclaredDependency,
    MatchConfidence,
    RepoScanResult,
)


def test_log_format_is_accepted_after_watch_subcommand() -> None:
    args = build_parser().parse_args(["watch", "--once", "--log-format", "json"])

    assert args.command == "watch"
    assert args.once is True
    assert args.log_format == "json"


def test_log_format_is_accepted_before_watch_subcommand() -> None:
    args = build_parser().parse_args(["--log-format", "json", "watch", "--once"])

    assert args.command == "watch"
    assert args.log_format == "json"


def test_scan_accepts_legacy_local_path_package_and_old_api_arguments() -> None:
    args = build_parser().parse_args(
        [
            "scan",
            "--repo",
            "./path/to/checkout",
            "--package",
            "openai",
            "--old-api",
            "openai.Completion",
        ]
    )

    assert _scan_location(args) == ("./path/to/checkout", None)
    assert args.package == "openai"
    assert args.old_api == "openai.Completion"


def test_plan_and_apply_share_the_pipeline_inputs() -> None:
    for command in ("plan", "apply"):
        args = build_parser().parse_args(
            [
                command,
                "acme/widgets",
                "--change-file",
                "change.json",
                "--llm-provider",
                "ollama",
            ]
        )

        assert args.command == command
        assert args.repos == ["acme/widgets"]
        assert args.change_file == ["change.json"]
        assert args.llm_provider == "ollama"


@pytest.mark.parametrize("command", ["doctor", "fix", "pipeline"])
def test_removed_legacy_workflow_commands_are_rejected(command: str, capsys) -> None:
    """Plan/apply are the only public edit-and-PR workflow commands."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main([command])

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_init_accepts_setup_and_offline_diagnostic_options() -> None:
    args = build_parser().parse_args(
        ["init", "--providers-file", "providers.test.yaml", "--offline"]
    )

    assert args.command == "init"
    assert args.providers_file == "providers.test.yaml"
    assert args.offline is True
    assert not hasattr(args, "repos")
    assert not hasattr(args, "provider")


def test_pr_list_accepts_a_repository() -> None:
    args = build_parser().parse_args(["pr", "list", "--repo", "acme/widgets"])

    assert args.command == "pr"
    assert args.pr_command == "list"
    assert args.repo == "acme/widgets"


def test_explicit_pipeline_repo_bypasses_repo_config_file_gate(monkeypatch) -> None:
    """Naming a repo is an explicit opt-in, unlike a fleet-wide run."""
    settings = Settings(llm_provider="ollama")
    args = build_parser().parse_args(["plan", "acme/widgets", "--provider", "openai"])

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "load_providers_file",
        lambda _path: (_ for _ in ()).throw(ProviderConfigError("stop after setup")),
    )

    assert _cmd_pipeline(args) == 2
    assert settings.pipeline_require_config_file is False


def test_scan_with_auto_detected_changes_starts_with_broad_usage_discovery(
    monkeypatch, tmp_path
) -> None:
    """A normal scan finds repository usage before matching feed changes."""
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        """providers:
  - id: openai
    sdk_packages: [{name: openai, ecosystem: npm}]
    feeds:
      - kind: fixture_change
        id: test
        old_token: '3.0.0'
        new_token: '4.0.0'
"""
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    change = BreakingChange(
        package="openai",
        old_version="3.0.0",
        new_version="4.0.0",
        old_api="openai.createChatCompletion",
        new_api="openai.chat.completions.create",
        description="migration",
        migration_guide="",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
    )
    captured = {}
    settings = Settings(providers_file=str(providers_file), llm_provider="ollama")
    args = build_parser().parse_args(
        ["scan", "--path", str(checkout), "--provider", "openai", "--no-save"]
    )

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "ensure_changes_detected", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(cli, "all_changes_for_provider", lambda _provider: [change])
    monkeypatch.setattr(cli, "latest_change_for_provider", lambda _provider: (change, 1))
    monkeypatch.setattr(cli, "_print_header", lambda: None)
    monkeypatch.setattr(cli, "_print_scan_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_cmd_eval", lambda _args: 0)

    def capture_scan(_path, target, **_kwargs):
        captured["symbols"] = target.symbols
        return RepoScanResult(repo_full_name="local", commit_sha="test")

    monkeypatch.setattr(cli, "scan_path", capture_scan)

    assert cli._cmd_scan(args) == 0
    assert captured["symbols"] == ()


def test_scan_evaluates_after_detecting_and_scanning(monkeypatch, tmp_path) -> None:
    """The optional regression corpus is the final scan workflow phase."""
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        """providers:
  - id: openai
    sdk_packages: [{name: openai, ecosystem: npm}]
    feeds:
      - kind: fixture_change
        id: test
        old_token: '3.0.0'
        new_token: '4.0.0'
"""
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    args = build_parser().parse_args(
        ["scan", "--path", str(checkout), "--provider", "openai", "--no-save"]
    )
    settings = Settings(providers_file=str(providers_file), llm_provider="ollama")
    phases: list[str] = []

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "ensure_changes_detected",
        lambda *_args, **_kwargs: phases.append("detect") or 0,
    )
    monkeypatch.setattr(cli, "all_changes_for_provider", lambda _provider: [])
    monkeypatch.setattr(cli, "latest_change_for_provider", lambda _provider: (None, None))
    monkeypatch.setattr(cli, "_print_header", lambda: None)
    monkeypatch.setattr(cli, "_print_scan_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "scan_path",
        lambda *_args, **_kwargs: (
            phases.append("scan") or RepoScanResult(repo_full_name="local", commit_sha="test")
        ),
    )
    monkeypatch.setattr(cli, "_cmd_eval", lambda _args: phases.append("evaluate") or 0)

    assert cli._cmd_scan(args) == 0
    assert phases == ["detect", "scan", "evaluate"]


def test_scan_derives_feed_candidate_only_from_observed_api_usage(monkeypatch, tmp_path) -> None:
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        """providers:
  - id: openai
    sdk_packages: [{name: openai, ecosystem: npm}]
    feeds: [{kind: fixture_change, id: test, old_token: '3.0.0', new_token: '4.0.0'}]
"""
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    candidate = BreakingChange(
        package="openai",
        old_version="3.0.0",
        new_version="4.0.0",
        old_api="openai.createChatCompletion",
        new_api="openai.chat.completions.create",
        description="migration",
        migration_guide="",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.LLM_SYNTHESIS,
        provider_id="openai",
    )
    settings = Settings(providers_file=str(providers_file), llm_provider="ollama")
    args = build_parser().parse_args(
        ["scan", "--path", str(checkout), "--provider", "openai", "--no-save"]
    )
    seen: dict[str, object] = {}

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "ensure_changes_detected", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(cli, "all_changes_for_provider", lambda _provider: [])
    monkeypatch.setattr(cli, "latest_change_for_provider", lambda _provider: (None, None))
    monkeypatch.setattr(cli, "_build_completer", lambda _settings, **_kw: object())
    monkeypatch.setattr(cli, "_print_header", lambda: None)
    monkeypatch.setattr(cli, "_print_scan_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_print_scan_assessments", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_cmd_eval", lambda _args: 0)

    @contextmanager
    def fake_session_scope():
        yield object()

    monkeypatch.setattr(cli, "session_scope", fake_session_scope)

    def discover(_session, **kwargs):
        seen.update(kwargs)
        return [candidate]

    monkeypatch.setattr(cli, "discover_usage_candidates", discover)
    monkeypatch.setattr(
        cli,
        "scan_path",
        lambda *_args, **_kwargs: RepoScanResult(
            repo_full_name="local",
            commit_sha="test",
            call_sites=[
                CallSite(
                    filepath="src/openai.js",
                    line_number=1,
                    column=0,
                    line_content="openai.createChatCompletion({})",
                    kind=CallSiteKind.METHOD_CALL,
                    confidence=MatchConfidence.HIGH,
                    symbol="openai.createChatCompletion",
                    provider_id="openai",
                )
            ],
        ),
    )

    assert cli._cmd_scan(args) == 0
    assert seen["observed_symbols"] == ["openai.createChatCompletion"]


def test_scan_records_resolved_usage_in_the_managed_catalog(monkeypatch, tmp_path) -> None:
    """A persisted scan writes its resolved usage before plan eligibility."""
    import depfix.catalog as catalog

    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        """providers:
  - id: openai
    sdk_packages: [{name: openai, ecosystem: npm}]
    feeds: [{kind: fixture_change, id: test, old_token: '3.0.0', new_token: '4.0.0'}]
"""
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    candidate = BreakingChange(
        package="openai",
        old_version="3.0.0",
        new_version="4.0.0",
        old_api="openai.createChatCompletion",
        new_api="openai.chat.completions.create",
        description="migration",
        migration_guide="",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.LLM_SYNTHESIS,
        provider_id="openai",
        evidence="release note says use openai.chat.completions.create",
    )
    settings = Settings(providers_file=str(providers_file), llm_provider="ollama")
    args = build_parser().parse_args(["scan", "--path", str(checkout), "--provider", "openai"])
    learned: dict[str, object] = {}

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "ensure_changes_detected", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(cli, "all_changes_for_provider", lambda _provider: [])
    monkeypatch.setattr(cli, "latest_change_for_provider", lambda _provider: (None, None))
    monkeypatch.setattr(cli, "_build_completer", lambda _settings, **_kw: object())
    monkeypatch.setattr(cli, "_print_header", lambda: None)
    monkeypatch.setattr(cli, "_print_scan_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_print_scan_assessments", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_cmd_eval", lambda _args: 0)
    monkeypatch.setattr(cli, "init_schema", lambda: None)
    monkeypatch.setattr(cli, "record_scan", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "record_scan_assessments", lambda *_args, **_kwargs: [])

    @contextmanager
    def fake_session_scope():
        yield object()

    monkeypatch.setattr(cli, "session_scope", fake_session_scope)
    monkeypatch.setattr(cli, "discover_usage_candidates", lambda *_args, **_kwargs: [candidate])
    monkeypatch.setattr(
        cli,
        "scan_path",
        lambda *_args, **_kwargs: RepoScanResult(
            repo_full_name="local",
            commit_sha="observed-sha",
            dependencies=[
                DeclaredDependency("openai", "package-lock.json", "^3", "3.3.0", "lockfile"),
                DeclaredDependency("unrelated", "package-lock.json", "^1", "1.0.0", "lockfile"),
            ],
            call_sites=[
                CallSite(
                    filepath="src/openai.js",
                    line_number=1,
                    column=0,
                    line_content="openai.createChatCompletion({})",
                    kind=CallSiteKind.METHOD_CALL,
                    confidence=MatchConfidence.HIGH,
                    symbol="openai.createChatCompletion",
                    provider_id="openai",
                )
            ],
        ),
    )

    def record(path, **kwargs):
        learned["path"] = path
        learned.update(kwargs)
        return 1

    monkeypatch.setattr(catalog, "record_detected_usage", record)

    assert cli._cmd_scan(args) == 0
    assert learned["path"] == ".depfix/repository_migrations.yaml"
    assert learned["commit_sha"] == "observed-sha"
    assert learned["detected_symbols"] == ["openai.createChatCompletion"]
    assert learned["classified_changes"] == [candidate]
    assert learned["sdk_versions"] == {"openai": "3.3.0"}


# -- tiered model routing ------------------------------------------------------


def test_resolve_light_model_returns_separate_ids_for_bedrock() -> None:
    settings = Settings(
        llm_provider="bedrock",
        bedrock_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        bedrock_light_model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    light, heavy = cli._resolve_light_model(settings)
    assert light == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert heavy == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_resolve_light_model_falls_back_to_heavy_when_empty() -> None:
    settings = Settings(
        llm_provider="bedrock",
        bedrock_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        bedrock_light_model="",
    )
    light, heavy = cli._resolve_light_model(settings)
    assert light == heavy


def test_resolve_light_model_works_for_gemini() -> None:
    settings = Settings(
        llm_provider="gemini",
        gemini_model="gemini-2.5-flash",
        gemini_light_model="gemini-2.0-flash-lite",
    )
    light, heavy = cli._resolve_light_model(settings)
    assert light == "gemini-2.0-flash-lite"
    assert heavy == "gemini-2.5-flash"


def test_resolve_light_model_works_for_ollama() -> None:
    settings = Settings(
        llm_provider="ollama",
        ollama_model="qwen2.5-coder:7b",
        ollama_light_model="",
    )
    light, heavy = cli._resolve_light_model(settings)
    assert light == heavy == "qwen2.5-coder:7b"
