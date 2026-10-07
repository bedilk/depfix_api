"""The local learned-migration store: provenance, keys, promotion, I/O safety."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from depfix.catalog import (
    STATUS_CANDIDATE,
    STATUS_CONFIRMED,
    load_learned_migrations,
    persist_learned_migrations,
    promote_learned_migration,
)
from depfix.catalog import learned as learned_module
from depfix.config import get_settings
from depfix.core import shortcuts
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.storage import db as db_module
from depfix.storage.schema import Base, BreakingChangeRow, SpecChangeRow

_SKIPPED = {ChangeKind.DEPENDENCY_VERSION_BUMP, ChangeKind.SECURITY_ADVISORY, ChangeKind.UNKNOWN}


def _change(
    *,
    kind: ChangeKind = ChangeKind.METHOD_RENAMED,
    old_api: str = "openai.createChatCompletion",
    old_version: str = "3.3.0",
    new_version: str = "4.0.0",
    source: ClassificationSource = ClassificationSource.RELEASE_NOTES,
    confidence: float = 0.75,
) -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version=old_version,
        new_version=new_version,
        old_api=old_api,
        new_api="openai.chat.completions.create",
        description="moved",
        migration_guide="use the namespaced method",
        kind=kind,
        source=source,
        provider_id="openai",
        source_url="https://example.test/notes",
        evidence="Removed createChatCompletion",
        confidence=confidence,
        call_site_hints=["openai.createChatCompletion"],
    )


@pytest.mark.parametrize("kind", [k for k in ChangeKind if k not in _SKIPPED])
def test_every_change_kind_round_trips_exactly(tmp_path: Path, kind: ChangeKind) -> None:
    path = tmp_path / "learned.yaml"
    change = _change(kind=kind)

    assert persist_learned_migrations(path, [change]) == 1
    (loaded,) = load_learned_migrations(path)

    assert loaded.change == change  # kind, source, confidence, evidence, source_url intact


@pytest.mark.parametrize("kind", sorted(_SKIPPED))
def test_repo_scoped_kinds_are_not_learned(tmp_path: Path, kind: ChangeKind) -> None:
    path = tmp_path / "learned.yaml"
    change = BreakingChange(
        package="openai",
        old_version="1.0.0",
        new_version="1.1.0",
        old_api="openai@1.0.0",
        new_api="openai@1.1.0",
        description="d",
        migration_guide="",
        kind=kind,
        provider_id="openai",
    )
    assert persist_learned_migrations(path, [change]) == 0
    assert not path.exists()


def test_migrations_in_the_same_major_range_do_not_collide(tmp_path: Path) -> None:
    """4.22->5.0 and 4.30->5.59 used to share one feed ID; the second was dropped."""
    path = tmp_path / "learned.yaml"
    first = _change(old_api="algolia.initIndex", old_version="4.22.0", new_version="5.0.0")
    second = _change(old_api="algolia.index.search", old_version="4.30.0", new_version="5.59.0")

    assert persist_learned_migrations(path, [first, second]) == 2
    assert {m.change.old_api for m in load_learned_migrations(path)} == {
        "algolia.initIndex",
        "algolia.index.search",
    }


def test_same_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    assert persist_learned_migrations(path, [_change()]) == 1
    assert persist_learned_migrations(path, [_change()]) == 0
    assert len(load_learned_migrations(path)) == 1


def test_llm_output_is_a_candidate_and_deterministic_output_is_confirmed(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    persist_learned_migrations(
        path,
        [
            _change(old_api="openai.a"),
            _change(old_api="openai.b", source=ClassificationSource.SPEC_DIFF, confidence=1.0),
        ],
    )
    by_api = {m.change.old_api: m for m in load_learned_migrations(path)}

    assert by_api["openai.a"].status == STATUS_CANDIDATE
    assert by_api["openai.b"].status == STATUS_CONFIRMED
    assert by_api["openai.b"].confirmed_by == "deterministic:spec_diff"


def test_promotion_confirms_a_candidate_and_is_never_undone(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    change = _change()
    persist_learned_migrations(path, [change])

    assert promote_learned_migration(path, change.dedupe_key, "verified:high") is True
    assert promote_learned_migration(path, change.dedupe_key, "merged_pr") is False
    persist_learned_migrations(path, [_change(confidence=0.9)])  # later LLM re-observation

    (loaded,) = load_learned_migrations(path)
    assert loaded.status == STATUS_CONFIRMED
    assert loaded.confirmed_by == "verified:high"
    assert loaded.change.confidence == 0.75  # confirmed payload not overwritten


def test_candidates_can_be_excluded_on_load(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    persist_learned_migrations(path, [_change()])
    assert load_learned_migrations(path, include_candidates=False) == []


def test_read_only_target_degrades_to_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "learned.yaml"
    persist_learned_migrations(path, [_change(old_api="openai.a")])
    before = path.read_text(encoding="utf-8")

    def refuse(*_args: object) -> None:
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(learned_module.os, "replace", refuse)

    assert persist_learned_migrations(path, [_change(old_api="openai.b")]) == 0
    assert path.read_text(encoding="utf-8") == before
    assert not path.with_suffix(".yaml.tmp").exists()


def test_malformed_file_loads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    path.write_text("{not: [valid", encoding="utf-8")
    assert load_learned_migrations(path) == []


# -- DB round trip: a candidate never gains SPEC_DIFF confidence -------------


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


@pytest.fixture
def learned_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "learned.yaml"
    settings = get_settings()
    monkeypatch.setattr(settings, "learned_migrations_file", str(path))
    monkeypatch.setattr(settings, "learned_migrations_include_candidates", True)
    return path


def test_reimported_candidate_keeps_its_original_source_and_confidence(
    in_memory_db: None, learned_path: Path
) -> None:
    persist_learned_migrations(learned_path, [_change()])

    assert shortcuts._import_learned_catalog("openai") == 1
    assert shortcuts._import_learned_catalog("openai") == 0  # idempotent

    with db_module.session_scope() as session:
        (row,) = session.scalars(select(BreakingChangeRow)).all()
        assert row.source == ClassificationSource.RELEASE_NOTES.value
        assert row.confidence == 0.75
        assert row.evidence == "Removed createChatCompletion"
        assert session.scalars(select(SpecChangeRow)).all() == []  # never a spec diff


def test_auto_persist_writes_the_learned_file(
    tmp_path: Path, learned_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shortcuts._auto_persist_catalog("openai", [_change()])

    stored = yaml.safe_load(learned_path.read_text(encoding="utf-8"))
    assert len(stored["migrations"]) == 1
