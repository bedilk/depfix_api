"""Regression coverage for automatic historical migration discovery."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from depfix.core import shortcuts
from depfix.scanners.models import DeclaredDependency
from depfix.storage import db as db_module
from depfix.storage.schema import Base, BreakingChangeRow, ChangeEventRow, FeedState, Provider


@pytest.fixture
def in_memory_db(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(
        db_module, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )


class _NoopDeps:
    """Catalog fixture feeds do not use HTTP or registry clients."""

    def __enter__(self) -> _NoopDeps:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def test_catalog_does_not_create_a_migration_when_upstream_has_no_event(
    in_memory_db: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repository usage, not the bundled catalog, drives plan eligibility."""
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        """providers:
  - id: openai
    sdk_packages: [{name: openai, ecosystem: npm}]
    feeds:
      - kind: fixture_change
        id: already-baselined-upstream
        old_token: "3.2.1"
        new_token: "4.0.1"
        spec_changes:
          - kind: operation_id_changed
            severity: breaking
            subject: openai.createChatCompletion
            after: openai.chat.completions.create
""",
        encoding="utf-8",
    )
    with db_module.session_scope() as session:
        session.add(Provider(id="openai", name="OpenAI"))
        session.add(
            FeedState(
                provider_id="openai",
                feed_key="fixture_change:already-baselined-upstream",
                kind="fixture_change",
                last_token="4.0.1",
            )
        )
    monkeypatch.setattr(shortcuts.SourceDeps, "from_settings", lambda: _NoopDeps())
    monkeypatch.setattr(
        shortcuts, "learned_migrations_file", lambda *_a, **_kw: str(tmp_path / "no-such.yaml")
    )
    created = shortcuts.ensure_changes_detected("openai", providers_file=str(providers_file))
    change, change_id = shortcuts.latest_change_for_provider("openai")

    assert created == 0
    assert change_id is None
    assert change is None
    assert shortcuts.all_changes_for_provider("openai") == []


def test_scan_bootstrap_joins_fresh_feed_baseline_to_lockfile_version(
    in_memory_db: None,
) -> None:
    """A fresh DB can report repository drift without manufacturing an API migration."""
    with db_module.session_scope() as session:
        session.add(Provider(id="openai", name="OpenAI"))
        session.add(
            FeedState(
                provider_id="openai",
                feed_key="npm:openai:latest",
                kind="npm_dist_tag",
                last_token="7.20.0",
            )
        )

    created = shortcuts.capture_repository_dependency_drift(
        "openai",
        repository="bedilk/example",
        dependencies=[
            DeclaredDependency(
                package="openai",
                manifest_path="package-lock.json",
                declared_range="^5.8.2",
                resolved_version="5.8.2",
                source="lockfile",
            )
        ],
        package_ecosystems={"openai": "npm"},
    )

    assert created == 1
    with db_module.session_scope() as session:
        event = session.scalar(select(ChangeEventRow))
        change = session.scalar(select(BreakingChangeRow))
        assert event is not None
        assert event.old_token == "5.8.2"
        assert event.new_token == "7.20.0"
        assert change is not None
        assert change.kind == "dependency_version_bump"
        assert change.old_api == "openai@5.8.2"
        assert change.new_api == "openai@7.20.0"


def test_scan_bootstrap_is_idempotent_for_same_repository_version(
    in_memory_db: None,
) -> None:
    with db_module.session_scope() as session:
        session.add(Provider(id="openai", name="OpenAI"))
        session.add(
            FeedState(
                provider_id="openai",
                feed_key="npm:openai:latest",
                kind="npm_dist_tag",
                last_token="7.20.0",
            )
        )
    dependency = DeclaredDependency("openai", "package-lock.json", "^7.19.0", "7.19.0", "lockfile")
    assert (
        shortcuts.capture_repository_dependency_drift(
            "openai",
            repository="bedilk/example",
            dependencies=[dependency],
            package_ecosystems={"openai": "npm"},
        )
        == 1
    )
    assert (
        shortcuts.capture_repository_dependency_drift(
            "openai",
            repository="bedilk/example",
            dependencies=[dependency],
            package_ecosystems={"openai": "npm"},
        )
        == 0
    )


def test_repository_drifts_with_different_installed_versions_stay_distinct(
    in_memory_db: None,
) -> None:
    """A Stripe-12 scan must not hide a later Stripe-19 repository drift."""
    with db_module.session_scope() as session:
        session.add(Provider(id="stripe", name="Stripe"))
        session.add(
            FeedState(
                provider_id="stripe",
                feed_key="npm:stripe:latest",
                kind="npm_dist_tag",
                last_token="22.6.2",
            )
        )

    package_ecosystems = {"stripe": "npm"}
    assert (
        shortcuts.capture_repository_dependency_drift(
            "stripe",
            repository="bedilk/documenso",
            dependencies=[
                DeclaredDependency("stripe", "package.json", "12.18.0", "12.18.0", "lockfile")
            ],
            package_ecosystems=package_ecosystems,
        )
        == 1
    )
    assert (
        shortcuts.capture_repository_dependency_drift(
            "stripe",
            repository="bedilk/medusa",
            dependencies=[
                DeclaredDependency("stripe", "package.json", "19.1.0", "19.1.0", "lockfile")
            ],
            package_ecosystems=package_ecosystems,
        )
        == 1
    )

    with db_module.session_scope() as session:
        changes = session.scalars(
            select(BreakingChangeRow).order_by(BreakingChangeRow.old_version)
        ).all()
    assert [(change.old_version, change.new_version) for change in changes] == [
        ("12.18.0", "22.6.2"),
        ("19.1.0", "22.6.2"),
    ]


def test_scan_bootstrap_accepts_an_exact_manifest_pin_without_lockfile(
    in_memory_db: None,
) -> None:
    with db_module.session_scope() as session:
        session.add(Provider(id="mongodb", name="MongoDB"))
        session.add(
            FeedState(
                provider_id="mongodb",
                feed_key="npm:mongodb:latest",
                kind="npm_dist_tag",
                last_token="7.6.0",
            )
        )
    dependency = DeclaredDependency("mongodb", "package.json", "6.16.0", None, "range")
    assert (
        shortcuts.capture_repository_dependency_drift(
            "mongodb",
            repository="bedilk/example",
            dependencies=[dependency],
            package_ecosystems={"mongodb": "npm"},
        )
        == 1
    )


def test_scan_bootstrap_uses_a_major_bounded_declared_range_without_lockfile(
    in_memory_db: None,
) -> None:
    """A caret range below a newer major is enough evidence of repository drift.

    The exact installed patch is unknown, but ``^12.18.0`` cannot resolve the
    feed's v20 release.  Persisting this review-only drift lets a following
    plan inspect it rather than silently stopping on a fresh database.
    """
    with db_module.session_scope() as session:
        session.add(Provider(id="stripe", name="Stripe"))
        session.add(
            FeedState(
                provider_id="stripe",
                feed_key="npm:stripe:latest",
                kind="npm_dist_tag",
                last_token="20.4.0",
            )
        )

    created = shortcuts.capture_repository_dependency_drift(
        "stripe",
        repository="bedilk/example",
        dependencies=[DeclaredDependency("stripe", "package.json", "^12.18.0", None, "range")],
        package_ecosystems={"stripe": "npm"},
    )

    assert created == 1
    with db_module.session_scope() as session:
        change = session.scalar(select(BreakingChangeRow))
        assert change is not None
        assert change.package == "stripe"
        assert change.old_version == "12.18.0"
        assert change.new_version == "20.4.0"


def test_scan_bootstrap_uses_the_manifest_ecosystem_for_duplicate_package_names(
    in_memory_db: None,
) -> None:
    """``stripe`` exists on npm and PyPI; pyproject.toml must choose PyPI."""
    with db_module.session_scope() as session:
        session.add(Provider(id="stripe", name="Stripe"))
        session.add_all(
            [
                FeedState(
                    provider_id="stripe",
                    feed_key="npm:stripe:latest",
                    kind="npm_dist_tag",
                    last_token="20.4.0",
                ),
                FeedState(
                    provider_id="stripe",
                    feed_key="pypi:stripe",
                    kind="pypi_dist_tag",
                    last_token="15.6.1",
                ),
            ]
        )

    created = shortcuts.capture_repository_dependency_drift(
        "stripe",
        repository="bedilk/example",
        dependencies=[DeclaredDependency("stripe", "pyproject.toml", "^12.18.0", None, "range")],
        package_ecosystems={"stripe": ("npm", "pypi")},
    )

    assert created == 1
    with db_module.session_scope() as session:
        event = session.scalar(select(ChangeEventRow))
        change = session.scalar(select(BreakingChangeRow))
        assert event is not None and event.feed_key.startswith("pypi:stripe:")
        assert change is not None and change.package == "stripe"
