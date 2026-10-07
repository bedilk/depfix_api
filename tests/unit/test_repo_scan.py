"""Unit tests for repo-scan orchestration (``scanners/repo.py``) and its
persistence layer (``scanners/store.py``).

The persistence tests use an in-memory SQLite database monkeypatched into
``storage.db``, following the same pattern as ``test_watcher.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.providers.models import ProviderSpec, SdkPackage
from depfix.scanners.matching import ScanMatchStatus, assess_scan_change
from depfix.scanners.models import CallSiteKind, MatchConfidence, RepoScanResult, ScanTarget
from depfix.scanners.repo import (
    _current_commit_sha,
    build_scan_target,
    build_scan_target_for_changes,
    scan_path,
)
from depfix.scanners.store import (
    latest_scan,
    record_scan,
    record_scan_assessments,
    upsert_repo,
)
from depfix.storage import db as db_module
from depfix.storage.schema import Base, BreakingChangeRow, ChangeEventRow, Provider
from depfix.verify.manager import PackageManagerPreparation

pytestmark = pytest.mark.skipif(__import__("shutil").which("git") is None, reason="git not on PATH")


# -- build_scan_target ---------------------------------------------------------


def _provider(**overrides: object) -> ProviderSpec:
    defaults: dict[str, object] = {
        "id": "openai",
        "name": "OpenAI",
        "sdk_packages": (SdkPackage(name="openai"),),
        "api_base_urls": ("https://api.openai.com",),
    }
    defaults.update(overrides)
    return ProviderSpec(**defaults)


def test_build_scan_target_with_no_change_scans_every_symbol() -> None:
    target = build_scan_target(_provider())

    assert target.provider_id == "openai"
    assert target.sdk_packages == ("openai",)
    assert target.symbols == ()


def test_build_scan_target_narrows_to_call_site_hints() -> None:
    change = BreakingChange(
        package="openai",
        old_version="3.3.0",
        new_version="4.0.0",
        old_api="openai.createModeration()",
        new_api="openai.moderations.create()",
        description="d",
        migration_guide="m",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
        provider_id="openai",
        call_site_hints=["openai.moderations.legacyCreate"],
    )

    target = build_scan_target(_provider(), change)

    assert "openai.moderations.legacyCreate" in target.symbols
    assert "openai.createModeration" in target.symbols


def test_build_scan_target_ignores_prose_old_api() -> None:
    change = BreakingChange(
        package="openai",
        old_version="3.3.0",
        new_version="4.0.0",
        old_api="the moderation endpoint changed shape",
        new_api="openai.moderations.create()",
        description="d",
        migration_guide="m",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
        provider_id="openai",
    )

    target = build_scan_target(_provider(), change)

    assert target.symbols == ()


def test_build_scan_target_for_changes_combines_every_old_symbol() -> None:
    changes = [
        BreakingChange(
            package="openai",
            old_version="3.3.0",
            new_version="4.0.0",
            old_api="openai.createChatCompletion",
            new_api="openai.chat.completions.create",
            description="d",
            migration_guide="",
            kind=ChangeKind.METHOD_RENAMED,
            source=ClassificationSource.MANUAL,
            provider_id="openai",
        ),
        BreakingChange(
            package="openai",
            old_version="3.3.0",
            new_version="4.0.0",
            old_api="openai.createEmbedding",
            new_api="openai.embeddings.create",
            description="d",
            migration_guide="",
            kind=ChangeKind.METHOD_RENAMED,
            source=ClassificationSource.MANUAL,
            provider_id="openai",
        ),
    ]

    target = build_scan_target_for_changes(_provider(), changes)

    assert target.symbols == ("openai.createChatCompletion", "openai.createEmbedding")


def test_spec_diff_hint_is_qualified_into_the_scanner_namespace() -> None:
    """Provider-less SDK paths from an OpenAPI diff must match scanner output."""
    change = BreakingChange(
        package="openai",
        old_version="7.17.0",
        new_version="7.19.0",
        old_api="POST /v1/responses (connector_id)",
        new_api="",
        description="d",
        migration_guide="",
        kind=ChangeKind.FIELD_REMOVED,
        source=ClassificationSource.SPEC_DIFF,
        provider_id="openai",
        call_site_hints=["responses.create"],
    )

    assert build_scan_target(_provider(), change).symbols == ("openai.responses.create",)


# -- _current_commit_sha ---------------------------------------------------------


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "c"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return sha.stdout.strip()


def test_current_commit_sha_resolves_head_for_a_real_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    expected_sha = _init_git_repo(repo)

    assert _current_commit_sha(repo) == expected_sha


def test_current_commit_sha_is_blank_for_dir_nested_in_unrelated_repo(tmp_path: Path) -> None:
    """Regression: scanning a plain (non-repo) subdirectory that happens to
    be nested inside some unrelated ancestor repo must not report that
    outer repo's HEAD as if it belonged to the scanned directory."""
    _init_git_repo(tmp_path)
    nested = tmp_path / "some" / "nested" / "fixture"
    nested.mkdir(parents=True)

    assert _current_commit_sha(nested) == ""


def test_current_commit_sha_is_blank_outside_any_repo(tmp_path: Path) -> None:
    plain_dir = tmp_path / "no-git-here"
    plain_dir.mkdir()

    assert _current_commit_sha(plain_dir) == ""


# -- scan_path end-to-end --------------------------------------------------------


def test_scan_path_reports_blank_commit_for_non_repo_directory(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text(
        'const OpenAI = require("openai");\n'
        "const client = new OpenAI();\n"
        "client.moderations.create({});\n",
        encoding="utf-8",
    )
    target = ScanTarget(provider_id="openai", sdk_packages=("openai",))

    result = scan_path(tmp_path, target)

    assert result.commit_sha == ""
    method_sites = [s for s in result.call_sites if s.kind == CallSiteKind.METHOD_CALL]
    assert len(method_sites) == 1


def test_scan_prepares_repository_package_manager_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@4.18.0"}', encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    prepared = PackageManagerPreparation(
        package_manager="yarn", executable=tmp_path / "yarn", version="4.18.0", installed=True
    )
    monkeypatch.setattr(
        "depfix.verify.manager.prepare_package_manager", lambda *_args, **_kwargs: prepared
    )

    result = scan_path(
        tmp_path,
        ScanTarget(provider_id="openai", sdk_packages=("openai",)),
        prepare_package_manager_runtime=True,
    )

    assert result.package_manager_preparation == prepared


def test_spec_diff_narrowed_scan_finds_a_real_call_site(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text(
        'const OpenAI = require("openai");\n'
        "const client = new OpenAI();\n"
        "client.responses.create({});\n",
        encoding="utf-8",
    )
    change = BreakingChange(
        package="openai",
        old_version="7.17.0",
        new_version="7.19.0",
        old_api="POST /v1/responses (connector_id)",
        new_api="openai.responses.create",
        description="d",
        migration_guide="",
        kind=ChangeKind.PARAM_REMOVED,
        source=ClassificationSource.SPEC_DIFF,
        provider_id="openai",
        call_site_hints=["responses.create"],
    )

    result = scan_path(tmp_path, build_scan_target(_provider(), change))

    assert [site.symbol for site in result.actionable_sites] == ["openai.responses.create"]


def test_scan_path_reports_real_commit_when_path_is_a_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    expected_sha = _init_git_repo(repo)
    target = ScanTarget(provider_id="openai", sdk_packages=("openai",))

    result = scan_path(repo, target)

    assert result.commit_sha == expected_sha


# -- persistence: store.py -------------------------------------------------------


@pytest.fixture
def db_session(monkeypatch: pytest.MonkeyPatch) -> Session:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_session_factory", factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _sample_result() -> RepoScanResult:
    from depfix.scanners.models import CallSite

    return RepoScanResult(
        repo_full_name="acme/widgets",
        commit_sha="deadbeef",
        files_scanned=3,
        manifest_matches=["openai@4.1.0 (lockfile, package.json)"],
        call_sites=[
            CallSite(
                filepath="src/index.js",
                line_number=3,
                column=0,
                line_content="client.moderations.create({});",
                kind=CallSiteKind.METHOD_CALL,
                confidence=MatchConfidence.HIGH,
                symbol="openai.moderations.create",
                provider_id="openai",
            )
        ],
    )


def test_record_scan_creates_repo_and_persists_call_sites(db_session: Session) -> None:
    record_scan(
        db_session,
        repo_full_name="acme/widgets",
        owner="acme",
        name="widgets",
        result=_sample_result(),
        provider_id="openai",
    )
    db_session.commit()

    scan = latest_scan(db_session, "acme/widgets")

    assert scan is not None
    assert scan.commit_sha == "deadbeef"
    assert scan.files_scanned == 3
    assert len(scan.call_sites) == 1
    assert scan.call_sites[0].symbol == "openai.moderations.create"


def test_record_scan_persists_change_assessment(db_session: Session) -> None:
    change = BreakingChange(
        package="openai",
        old_version="3.3.0",
        new_version="4.0.0",
        old_api="openai.moderations.create",
        new_api="openai.moderations.create",
        description="d",
        migration_guide="",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
        provider_id="openai",
    )
    provider = Provider(id="openai", name="OpenAI")
    event = ChangeEventRow(
        provider_id="openai",
        feed_key="test",
        source_kind="fixture",
        new_token="4.0.0",
        dedupe_key="event-key",
    )
    db_session.add(provider)
    db_session.add(
        BreakingChangeRow(
            change_event=event,
            dedupe_key=change.dedupe_key,
            package=change.package,
            old_version=change.old_version,
            new_version=change.new_version,
            old_api=change.old_api,
            new_api=change.new_api,
            description=change.description,
            migration_guide=change.migration_guide,
            kind=change.kind.value,
            source=change.source.value,
            provider_id=change.provider_id,
        )
    )
    scan = record_scan(
        db_session,
        repo_full_name="acme/widgets",
        owner="acme",
        name="widgets",
        result=_sample_result(),
        provider_id="openai",
    )
    assessment = assess_scan_change(_sample_result(), change, provider_id="openai")

    record_scan_assessments(db_session, scan=scan, assessments=[assessment])
    db_session.commit()

    stored = latest_scan(db_session, "acme/widgets")
    assert stored is not None
    assert len(stored.change_matches) == 1
    assert stored.change_matches[0].status == ScanMatchStatus.ACTIONABLE.value
    assert stored.change_matches[0].matched_symbols == ["openai.moderations.create"]


def test_latest_scan_returns_none_for_unknown_repo(db_session: Session) -> None:
    assert latest_scan(db_session, "acme/never-scanned") is None


def test_latest_scan_returns_the_most_recent_of_several_scans(db_session: Session) -> None:
    first = _sample_result()
    record_scan(
        db_session, repo_full_name="acme/widgets", owner="acme", name="widgets", result=first
    )
    db_session.commit()

    second = _sample_result()
    second.commit_sha = "second-sha"
    record_scan(
        db_session, repo_full_name="acme/widgets", owner="acme", name="widgets", result=second
    )
    db_session.commit()

    scan = latest_scan(db_session, "acme/widgets")

    assert scan is not None
    assert scan.commit_sha == "second-sha"


def test_upsert_repo_does_not_clobber_existing_fields_with_none(db_session: Session) -> None:
    upsert_repo(
        db_session,
        full_name="acme/widgets",
        owner="acme",
        name="widgets",
        installation_id=42,
        default_branch="main",
    )
    db_session.commit()

    repo = upsert_repo(db_session, full_name="acme/widgets", owner="acme", name="widgets")
    db_session.commit()

    assert repo.installation_id == 42
    assert repo.default_branch == "main"
