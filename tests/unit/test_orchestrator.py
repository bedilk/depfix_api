"""Unit tests for the Week 6 fleet orchestrator (``orchestrator/runner.py``).

``GitHubAppAuth`` and ``CloneService`` are replaced with small duck-typed
fakes (no live network, no real git clone) -- same style as
``test_watcher.py``'s scripted ``ChangeSource``. What *is* real here is
everything downstream of "got a checkout": the actual ``CallSiteScanner``
finds the real call site in ``tests/fixtures/openai_v3_project``, the real
``WorkspaceEditor``/``FixPipeline`` write the fix to a throwaway copy of it,
and ``BranchWriter``/``open_pull_request`` make real HTTP calls that respx
intercepts (same style as ``test_gh_branch.py``/``test_gh_pr.py``).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from depfix.clone import Checkout, CloneService
from depfix.config import Settings
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.gh import GitHubAppError, Installation, InstallationToken, branch_name_for
from depfix.orchestrator import Orchestrator, SkipReason
from depfix.orchestrator.plan import PlanArtifact, plan_path_for
from depfix.providers.models import ProviderSpec, SdkPackage
from depfix.scanners.models import CallSite, CallSiteKind, MatchConfidence, RepoScanResult
from depfix.storage import AttemptStatus, Base, ChangeAttemptRow, RepoRow
from depfix.storage import db as db_module

FIXTURE = Path(__file__).parent.parent / "fixtures" / "openai_v3_project"
BASE = "https://api.github.com"


def test_plan_artifact_paths_are_unique_per_repo_change(tmp_path: Path) -> None:
    change_a = _change(old_api="openai.createModeration()")
    change_b = _change(old_api="openai.createChatCompletion()")
    result_a = PlanArtifact("acme/widgets", "sha", change_a, ())
    result_b = PlanArtifact("acme/widgets", "sha", change_b, ())

    result_a.write_for_repo(tmp_path)
    result_b.write_for_repo(tmp_path)

    assert plan_path_for("acme/widgets", change_a.dedupe_key, tmp_path) != plan_path_for(
        "acme/widgets", change_b.dedupe_key, tmp_path
    )
    assert {
        plan.change_dedupe_key for plan in PlanArtifact.plans_for_repo("acme/widgets", tmp_path)
    } == {
        change_a.dedupe_key,
        change_b.dedupe_key,
    }


# -- shared fixtures / fakes -----------------------------------------------------


@pytest.fixture
def in_memory_db(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_session_factory", factory)


class _FakeGhAuth:
    """Duck-typed stand-in for ``GitHubAppAuth`` -- the orchestrator only
    ever calls these six methods."""

    def __init__(
        self,
        *,
        config_text: str | None,
        installation_id: int = 1,
        default_branch: str = "main",
        ref_sha: str = "sha-1",
        open_prs: list[Any] | None = None,
        no_installation: bool = False,
    ) -> None:
        self._config_text = config_text
        self._installation_id = installation_id
        self._default_branch = default_branch
        self._ref_sha = ref_sha
        self._open_prs = open_prs or []
        self._no_installation = no_installation
        self.open_pr_list_calls = 0

    def resolve_installation_for_repo(self, owner: str, name: str) -> Installation:
        if self._no_installation:
            raise GitHubAppError("no installation for this repo")
        return Installation(
            id=self._installation_id,
            account_login=owner,
            account_type="Organization",
            repository_selection="selected",
        )

    def get_file_contents(
        self, owner: str, name: str, path: str, *, ref: str | None = None
    ) -> str | None:
        return self._config_text

    def default_ref(self, owner: str, name: str) -> str:
        return self._default_branch

    def ref_sha(self, owner: str, name: str, ref: str) -> str:
        return self._ref_sha

    def list_open_pull_requests(
        self, owner: str, name: str, *, head: str | None = None, base: str | None = None
    ) -> list[Any]:
        self.open_pr_list_calls += 1
        return self._open_prs

    def installation_token(
        self,
        installation_id: int,
        *,
        repositories: tuple[str, ...] = (),
        permissions: dict[str, str] | None = None,
    ) -> InstallationToken:
        return InstallationToken(
            token="test-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            installation_id=installation_id,
            repositories=repositories,
        )


class _FlakyGhAuth(_FakeGhAuth):
    """Raises an un-caught ``RuntimeError`` for one specific repo name, to
    exercise the orchestrator's outer failure-isolation boundary."""

    def __init__(self, *, breaks_on: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._breaks_on = breaks_on

    def resolve_installation_for_repo(self, owner: str, name: str) -> Installation:
        if name == self._breaks_on:
            raise RuntimeError("boom")
        return super().resolve_installation_for_repo(owner, name)


class _FakeCloneService:
    """Duck-typed stand-in for ``CloneService`` -- copies a local fixture
    directory instead of doing a real git clone over the network. Wraps a
    real ``CloneService`` for ``copy_local``/``cleanup`` so the actual
    file-copy/cleanup logic under test is real, not faked."""

    def __init__(self, source: Path) -> None:
        self._source = source
        self._real = CloneService()
        self.clone_calls: list[tuple[str, str]] = []

    def clone(
        self,
        owner: str,
        repo: str,
        *,
        ref: str | None = None,
        token: str | None = None,
        max_repo_mb: int | None = None,
    ) -> Checkout:
        self.clone_calls.append((owner, repo))
        return self._real.copy_local(self._source)

    def cleanup(self, checkout: Checkout) -> None:
        self._real.cleanup(checkout)


class _FakeFixer:
    """Duck-typed stand-in for ``FixGenerator``/``OllamaFixGenerator``: a
    deterministic, real v3->v4 rewrite of the fixture's one call site."""

    def __init__(self) -> None:
        self.total_cost = 0.0
        self.total_tokens = 0

    def generate_fix(
        self, file_usage: Any, breaking_change: BreakingChange, *, feedback: str | None = None
    ) -> tuple[str, float, None]:
        fixed = file_usage.file_content.replace(
            "openai.createModeration", "openai.moderations.create"
        )
        # Real fixers expose `total_cost` as a running sum over their own
        # `llm_calls` (see FixGenerator.total_cost) -- mirror that here so
        # FixPipelineResult.total_cost (read from `self._fixer.total_cost`,
        # not from generate_fix's per-call return value) actually accrues.
        self.total_cost += 0.3
        return fixed, 0.9, None


def _provider() -> ProviderSpec:
    return ProviderSpec(id="openai", name="OpenAI", sdk_packages=(SdkPackage(name="openai"),))


def _change(old_api: str = "openai.createModeration()") -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="3.x",
        new_version="4.x",
        old_api=old_api,
        new_api="openai.moderations.create()",
        description="moderations moved under a namespace",
        migration_guide="use openai.moderations.create",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
        provider_id="openai",
    )


def _settings(**overrides: object) -> Settings:
    # These tests exercise the fake LLM path; dedicated codemod tests cover
    # deterministic rewrites independently.
    defaults: dict[str, object] = {
        "verify_enabled": False,
        "retry_max_rounds": 0,
        "codemods_enabled": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _orchestrator(
    gh_auth: Any, clone_service: Any, *, force: bool = False, **settings_overrides: object
) -> Orchestrator:
    return Orchestrator(
        settings=_settings(**settings_overrides),
        gh_auth=gh_auth,
        clone_service=clone_service,
        providers=[_provider()],
        fixer_factory=_FakeFixer,
        force=force,
    )


def _mock_pr_endpoints(respx_mock: respx.Router, *, owner: str, repo: str, pr_number: int) -> None:
    respx_mock.get(f"/repos/{owner}/{repo}/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "base-sha"}})
    )
    respx_mock.get(f"/repos/{owner}/{repo}/git/commits/base-sha").mock(
        return_value=httpx.Response(200, json={"tree": {"sha": "base-tree-sha"}})
    )
    respx_mock.post(f"/repos/{owner}/{repo}/git/blobs").mock(
        return_value=httpx.Response(200, json={"sha": "blob-sha"})
    )
    respx_mock.post(f"/repos/{owner}/{repo}/git/trees").mock(
        return_value=httpx.Response(200, json={"sha": "tree-sha"})
    )
    respx_mock.post(f"/repos/{owner}/{repo}/git/commits").mock(
        return_value=httpx.Response(200, json={"sha": "commit-sha"})
    )
    respx_mock.post(f"/repos/{owner}/{repo}/git/refs").mock(
        return_value=httpx.Response(201, json={"ref": "refs/heads/depfix/x"})
    )
    respx_mock.post(f"/repos/{owner}/{repo}/pulls").mock(
        return_value=httpx.Response(
            201,
            json={
                "number": pr_number,
                "html_url": f"https://github.example/{owner}/{repo}/pull/{pr_number}",
                "head": {"ref": "depfix/x"},
                "base": {"ref": "main"},
            },
        )
    )
    respx_mock.post(f"/repos/{owner}/{repo}/issues/{pr_number}/labels").mock(
        return_value=httpx.Response(200, json={"labels": []})
    )


# -- whole-repo skip / error paths -----------------------------------------------


def test_no_installation_is_skipped(in_memory_db: Any) -> None:
    gh_auth = _FakeGhAuth(config_text=None, no_installation=True)
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = _orchestrator(gh_auth, clone_service)

    outcome = orchestrator.run(["acme/widgets"], [_change()])

    (repo,) = outcome.repos
    assert repo.skip_reason == SkipReason.NO_INSTALLATION
    assert clone_service.clone_calls == []


def test_missing_repo_config_is_skipped_when_required(in_memory_db: Any) -> None:
    gh_auth = _FakeGhAuth(config_text=None)
    orchestrator = _orchestrator(gh_auth, _FakeCloneService(FIXTURE))

    outcome = orchestrator.run(["acme/widgets"], [_change()])

    (repo,) = outcome.repos
    assert repo.skip_reason == SkipReason.NO_REPO_CONFIG


def test_invalid_repo_config_is_skipped(in_memory_db: Any) -> None:
    gh_auth = _FakeGhAuth(config_text="version: 1\nunknown_field: true\n")
    orchestrator = _orchestrator(gh_auth, _FakeCloneService(FIXTURE))

    outcome = orchestrator.run(["acme/widgets"], [_change()])

    (repo,) = outcome.repos
    assert repo.skip_reason == SkipReason.INVALID_REPO_CONFIG
    assert "unknown_field" in repo.error


def test_one_repo_failure_does_not_abort_the_batch(in_memory_db: Any) -> None:
    gh_auth = _FlakyGhAuth(breaks_on="other", config_text=None)
    orchestrator = _orchestrator(gh_auth, _FakeCloneService(FIXTURE))

    outcome = orchestrator.run(["acme/widgets", "acme/other"], [_change()])

    by_name = {r.repo_full_name: r for r in outcome.repos}
    assert by_name["acme/widgets"].skip_reason == SkipReason.NO_REPO_CONFIG
    assert by_name["acme/other"].error == "boom"


# -- per-change skip paths --------------------------------------------------------


def test_tenant_ignore_short_circuits_before_any_clone(in_memory_db: Any) -> None:
    change = _change()
    config_text = f'version: 1\nignore:\n  - "{change.dedupe_key}"\n'
    gh_auth = _FakeGhAuth(config_text=config_text)
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = _orchestrator(gh_auth, clone_service)

    outcome = orchestrator.run(["acme/widgets"], [change])

    (repo,) = outcome.repos
    (change_outcome,) = repo.changes
    assert change_outcome.skip_reason == SkipReason.TENANT_IGNORED
    assert clone_service.clone_calls == []


def test_already_terminal_attempt_is_skipped_without_reprocessing(in_memory_db: Any) -> None:
    change = _change()
    with db_module.session_scope() as session:
        session.add(RepoRow(id="acme/widgets", owner="acme", name="widgets"))
        session.add(
            ChangeAttemptRow(
                repo_id="acme/widgets",
                dedupe_key=change.dedupe_key,
                status=AttemptStatus.PR_OPENED.value,
                attempts_used=1,
            )
        )

    gh_auth = _FakeGhAuth(config_text="version: 1\n")
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = _orchestrator(gh_auth, clone_service)

    outcome = orchestrator.run(["acme/widgets"], [change])

    (repo,) = outcome.repos
    (change_outcome,) = repo.changes
    assert change_outcome.skip_reason == SkipReason.ALREADY_TERMINAL
    assert clone_service.clone_calls == []


def test_no_call_sites_records_ledger_without_pr(in_memory_db: Any) -> None:
    change = _change(old_api="openai.totallyMissingMethod()")
    gh_auth = _FakeGhAuth(config_text="version: 1\nverify: false\n")
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = _orchestrator(gh_auth, clone_service)

    outcome = orchestrator.run(["acme/widgets"], [change])

    (repo,) = outcome.repos
    (change_outcome,) = repo.changes
    assert change_outcome.attempt_status == AttemptStatus.NO_CALL_SITES

    with db_module.session_scope() as session:
        row = session.scalar(
            select(ChangeAttemptRow).where(ChangeAttemptRow.dedupe_key == change.dedupe_key)
        )
        assert row is not None
        assert row.status == AttemptStatus.NO_CALL_SITES.value
        assert row.attempts_used == 0


def test_unrelated_provider_sites_do_not_trigger_a_fix(
    in_memory_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider anchors alone must not make an unrelated migration fixable."""
    change = _change(old_api="openai.responses.create()")
    gh_auth = _FakeGhAuth(config_text="version: 1\nverify: false\n")
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = _orchestrator(gh_auth, clone_service)

    # Reproduces Medusa's failure mode: a broad provider observation exists,
    # but it is not the API named by this migration.
    import depfix.orchestrator.runner as runner_module

    monkeypatch.setattr(
        runner_module,
        "scan_checkout",
        lambda *_args, **_kwargs: RepoScanResult(
            repo_full_name="acme/widgets",
            commit_sha="sha-1",
            call_sites=[
                CallSite(
                    filepath="src/config.js",
                    line_number=1,
                    column=0,
                    line_content="apiVersion: '1'",
                    kind=CallSiteKind.API_VERSION_PIN,
                    confidence=MatchConfidence.MEDIUM,
                    symbol="openai.<api_version_pin>",
                    provider_id="openai",
                )
            ],
        ),
    )

    outcome = orchestrator.run(["acme/widgets"], [change])

    (change_outcome,) = outcome.repos[0].changes
    assert change_outcome.attempt_status == AttemptStatus.NO_CALL_SITES
    assert "no call sites match" in change_outcome.detail


# -- full happy path: real scan + real fix + real (respx-mocked) PR -------------


@respx.mock(base_url=BASE)
def test_full_run_opens_pr_and_persists_ledger(respx_mock: respx.Router, in_memory_db: Any) -> None:
    change = _change()
    gh_auth = _FakeGhAuth(config_text="version: 1\nverify: false\n")
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = _orchestrator(gh_auth, clone_service)
    _mock_pr_endpoints(respx_mock, owner="acme", repo="widgets", pr_number=7)

    outcome = orchestrator.run(["acme/widgets"], [change])

    (repo,) = outcome.repos
    assert repo.error == ""
    (change_outcome,) = repo.changes
    assert change_outcome.attempt_status == AttemptStatus.PR_OPENED
    assert change_outcome.pr_url == "https://github.example/acme/widgets/pull/7"
    # Two clones: one pre-scan fallback (no stored depfix scan in test DB),
    # one for the fix. In production, a prior `depfix scan` avoids the extra.
    assert clone_service.clone_calls == [("acme", "widgets"), ("acme", "widgets")]

    with db_module.session_scope() as session:
        row = session.scalar(
            select(ChangeAttemptRow).where(ChangeAttemptRow.dedupe_key == change.dedupe_key)
        )
        assert row is not None
        assert row.status == AttemptStatus.PR_OPENED.value
        assert row.fix_run_id is not None


@respx.mock(base_url=BASE)
def test_second_run_does_not_reopen_a_pr(respx_mock: respx.Router, in_memory_db: Any) -> None:
    change = _change()
    gh_auth = _FakeGhAuth(config_text="version: 1\nverify: false\n")
    clone_service = _FakeCloneService(FIXTURE)
    _mock_pr_endpoints(respx_mock, owner="acme", repo="widgets", pr_number=7)

    orchestrator = _orchestrator(gh_auth, clone_service)
    first = orchestrator.run(["acme/widgets"], [change])
    assert first.repos[0].changes[0].attempt_status == AttemptStatus.PR_OPENED
    # Two clones on first run: pre-scan fallback + fix clone.
    assert clone_service.clone_calls == [("acme", "widgets"), ("acme", "widgets")]

    second = orchestrator.run(["acme/widgets"], [change])

    (change_outcome,) = second.repos[0].changes
    assert change_outcome.skip_reason == SkipReason.ALREADY_TERMINAL
    # No additional clone on second run -- policy gate fires before pre-scan.
    assert clone_service.clone_calls == [("acme", "widgets"), ("acme", "widgets")]


def test_branch_name_for_is_used_as_pr_head(in_memory_db: Any) -> None:
    """Sanity check that the real ``branch_name_for`` helper (not a
    hardcoded string) drives the PR head -- see ``_mock_pr_endpoints``,
    which doesn't assert on the literal branch name."""
    change = _change()
    assert branch_name_for(change).startswith("depfix/")


# -- Week 7 hardening: disabled providers, paths filter, dry-run, lock, budget --


def test_disabled_provider_is_treated_as_unknown(in_memory_db: Any) -> None:
    """A ``ProviderSpec(enabled=False)`` must be filtered out of
    ``known_provider_ids`` at construction time, not just skipped later --
    it should behave exactly as if depfix never registered this provider
    at all."""
    change = _change()
    gh_auth = _FakeGhAuth(config_text="version: 1\n")
    clone_service = _FakeCloneService(FIXTURE)
    disabled_provider = ProviderSpec(
        id="openai", name="OpenAI", sdk_packages=(SdkPackage(name="openai"),), enabled=False
    )
    orchestrator = Orchestrator(
        settings=_settings(),
        gh_auth=gh_auth,
        clone_service=clone_service,
        providers=[disabled_provider],
        fixer_factory=_FakeFixer,
    )

    outcome = orchestrator.run(["acme/widgets"], [change])

    (repo,) = outcome.repos
    (change_outcome,) = repo.changes
    assert change_outcome.skip_reason == SkipReason.UNKNOWN_PROVIDER
    assert clone_service.clone_calls == []


def test_paths_filter_excludes_before_the_fix_pipeline_runs(in_memory_db: Any) -> None:
    """A repo-scoped ``paths.exclude`` that covers the only actionable call
    site must suppress it *before* any fix is generated -- surfaced as
    ``suppressed_call_sites``, distinct from "no call sites found at
    all"."""
    change = _change()
    gh_auth = _FakeGhAuth(
        config_text="version: 1\nverify: false\npaths:\n  exclude:\n    - 'src/chat.js'\n"
    )
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = _orchestrator(gh_auth, clone_service)

    outcome = orchestrator.run(["acme/widgets"], [change])

    (repo,) = outcome.repos
    (change_outcome,) = repo.changes
    assert change_outcome.attempt_status == AttemptStatus.NO_CALL_SITES
    # Broad feed-driven scan finds all actionable sites in the excluded file,
    # not just the one being planned for. Both createModeration and
    # createChatCompletion are in src/chat.js.
    assert change_outcome.suppressed_call_sites >= 1
    assert "suppressed" in change_outcome.detail


@respx.mock(base_url=BASE)
def test_dry_run_makes_no_db_writes_or_pr(respx_mock: respx.Router, in_memory_db: Any) -> None:
    """Under ``dry_run``, the real scan/fix pipeline still runs (so the
    prediction is accurate) but no ledger row, branch, or PR is created."""
    change = _change()
    gh_auth = _FakeGhAuth(config_text="version: 1\nverify: false\n")
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = Orchestrator(
        settings=_settings(),
        gh_auth=gh_auth,
        clone_service=clone_service,
        providers=[_provider()],
        fixer_factory=_FakeFixer,
        dry_run=True,
    )
    # No PR endpoints mocked at all -- if the orchestrator tried to hit
    # them under dry-run, respx would raise on the unmocked call.

    outcome = orchestrator.run(["acme/widgets"], [change])

    (repo,) = outcome.repos
    (change_outcome,) = repo.changes
    assert change_outcome.attempt_status == AttemptStatus.PR_OPENED
    assert change_outcome.planned is True
    assert gh_auth.open_pr_list_calls == 0

    with db_module.session_scope() as session:
        row = session.scalar(
            select(ChangeAttemptRow).where(ChangeAttemptRow.dedupe_key == change.dedupe_key)
        )
        assert row is None


def test_lock_not_acquired_skips_the_whole_run(
    in_memory_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If another orchestrator process already holds the advisory lock,
    this run must do nothing at all, not even attempt the first repo."""
    import depfix.orchestrator.runner as runner_module

    @contextlib.contextmanager
    def _never_acquired(engine: Any, key: int = 0) -> Any:
        yield False

    monkeypatch.setattr(runner_module, "advisory_lock", _never_acquired)

    gh_auth = _FakeGhAuth(config_text="version: 1\n")
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = _orchestrator(gh_auth, clone_service)

    outcome = orchestrator.run(["acme/widgets"], [_change()])

    assert outcome.locked is False
    assert outcome.repos == []
    assert clone_service.clone_calls == []


def test_max_duration_stops_the_run_early(in_memory_db: Any) -> None:
    """An already-expired budget must stop the run before it starts the
    first repo, and report ``stopped_early``."""
    gh_auth = _FakeGhAuth(config_text="version: 1\n")
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = Orchestrator(
        settings=_settings(),
        gh_auth=gh_auth,
        clone_service=clone_service,
        providers=[_provider()],
        fixer_factory=_FakeFixer,
        max_duration_seconds=0.0,
    )

    outcome = orchestrator.run(["acme/widgets", "acme/other"], [_change()])

    assert outcome.stopped_early is True
    assert outcome.repos == []
    assert clone_service.clone_calls == []


@respx.mock(base_url=BASE)
def test_cost_ceiling_stops_the_run_early(respx_mock: respx.Router, in_memory_db: Any) -> None:
    """``pipeline_max_cost_usd_per_run`` is the budget's other ceiling --
    once the first repo's real ``_FakeFixer`` spend (0.3, accrued onto
    ``total_cost`` by ``_FakeFixer.generate_fix``) crosses it, the second
    repo must never be attempted."""
    gh_auth = _FakeGhAuth(config_text="version: 1\nverify: false\n")
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = _orchestrator(gh_auth, clone_service, pipeline_max_cost_usd_per_run=0.2)
    _mock_pr_endpoints(respx_mock, owner="acme", repo="widgets", pr_number=7)

    outcome = orchestrator.run(["acme/widgets", "acme/other"], [_change()])

    by_name = {r.repo_full_name: r for r in outcome.repos}
    assert "acme/widgets" in by_name
    assert "acme/other" not in by_name
    assert outcome.stopped_early is True
    # Two clones for acme/widgets: pre-scan fallback + fix clone.
    # acme/other is never reached (budget exhausted after widgets).
    assert clone_service.clone_calls == [("acme", "widgets"), ("acme", "widgets")]


@respx.mock(base_url=BASE)
def test_no_pr_forces_fixed_no_pr_regardless_of_repo_config(
    respx_mock: respx.Router, in_memory_db: Any
) -> None:
    """``no_pr=True`` must override this repo's own ``.depfix.yml`` (which
    defaults ``open_pr`` to ``True``) for the whole run -- no branch, no PR,
    but still a real ledger write (unlike ``dry_run``)."""
    change = _change()
    gh_auth = _FakeGhAuth(config_text="version: 1\nverify: false\n")
    clone_service = _FakeCloneService(FIXTURE)
    orchestrator = Orchestrator(
        settings=_settings(),
        gh_auth=gh_auth,
        clone_service=clone_service,
        providers=[_provider()],
        fixer_factory=_FakeFixer,
        no_pr=True,
    )
    # No PR endpoints mocked -- if the orchestrator tried to open one
    # despite no_pr, respx would raise on the unmocked call.

    outcome = orchestrator.run(["acme/widgets"], [change])

    (repo,) = outcome.repos
    (change_outcome,) = repo.changes
    assert change_outcome.attempt_status == AttemptStatus.FIXED_NO_PR
    assert change_outcome.pr_url == ""
    assert gh_auth.open_pr_list_calls == 0

    with db_module.session_scope() as session:
        row = session.scalar(
            select(ChangeAttemptRow).where(ChangeAttemptRow.dedupe_key == change.dedupe_key)
        )
        assert row is not None
        assert row.status == AttemptStatus.FIXED_NO_PR.value
