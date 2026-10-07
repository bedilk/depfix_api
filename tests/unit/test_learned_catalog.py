"""Tests for repository-observed, feed-compared migration records."""

from pathlib import Path

import yaml

from depfix.catalog.learned import record_detected_usage
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource


def _change(
    *,
    package: str = "pg",
    new_version: str = "8.0.0",
    old_api: str = "pg.connect",
    new_api: str = "pg.Pool.connect",
) -> BreakingChange:
    return BreakingChange(
        package=package,
        old_version="6.3.0",
        new_version=new_version,
        old_api=old_api,
        new_api=new_api,
        description="singleton removed",
        migration_guide="Use a Pool.",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.RELEASE_NOTES,
        provider_id="postgresql",
        source_url="https://example.test/release-notes",
        evidence="The pg.connect singleton API was removed; use pg.Pool.connect.",
    )


def _catalog(tmp_path: Path) -> Path:
    path = tmp_path / "canonical_migrations.yaml"
    path.write_text(
        "# Reviewed provider configuration.\nproviders: []\n",
        encoding="utf-8",
    )
    return path


def _entries(path: Path) -> list[dict[str, str]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["repository_migrations"]


def test_version_behind_makes_observed_usage_actionable_regardless_of_distance(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    record_detected_usage(
        catalog,
        repo_full_name="bedilk/medusa",
        commit_sha="abc",
        provider_id="postgresql",
        detected_symbols=["postgresql.pool.query"],
        classified_changes=[_change(new_version="8.0.0")],
        sdk_versions={"pg": "6.3.0"},
    )

    (entry,) = _entries(catalog)
    assert entry["status"] == "actionable"
    assert entry["current_version"] == "6.3.0"
    assert entry["latest_version"] == "8.0.0"
    assert "behind" in entry["reason"]


def test_tiny_version_gap_is_actionable(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    record_detected_usage(
        catalog,
        repo_full_name="bedilk/app",
        commit_sha="abc",
        provider_id="postgresql",
        detected_symbols=["postgresql.pool.query"],
        classified_changes=[_change(new_version="6.3.1")],
        sdk_versions={"pg": "6.3.0"},
    )

    assert _entries(catalog)[0]["status"] == "actionable"


def test_current_version_without_the_old_api_is_non_actionable(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    record_detected_usage(
        catalog,
        repo_full_name="bedilk/modern",
        commit_sha="abc",
        provider_id="postgresql",
        detected_symbols=["postgresql.pool.query"],
        classified_changes=[_change(new_version="8.0.0")],
        sdk_versions={"pg": "8.0.0"},
    )

    (entry,) = _entries(catalog)
    assert entry["status"] == "non_actionable"
    assert entry["reason"] == "SDK version and call sites match the feed's current state"


def test_old_api_is_actionable_even_at_the_latest_sdk_version(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    record_detected_usage(
        catalog,
        repo_full_name="bedilk/deprecated",
        commit_sha="abc",
        provider_id="postgresql",
        detected_symbols=["postgresql.pg.connect"],
        classified_changes=[_change(new_version="8.0.0")],
        sdk_versions={"pg": "8.0.0"},
    )

    (entry,) = _entries(catalog)
    assert entry["status"] == "actionable"
    assert entry["new_api"] == "pg.Pool.connect"
    assert "old API" in entry["reason"]


def test_no_feed_evidence_is_non_actionable_but_is_still_recorded(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    record_detected_usage(
        catalog,
        repo_full_name="bedilk/medusa",
        commit_sha="abc",
        provider_id="postgresql",
        detected_symbols=["postgresql.pool.query"],
        classified_changes=[],
        sdk_versions={"pg": "6.3.0"},
    )

    (entry,) = _entries(catalog)
    assert entry["status"] == "non_actionable"
    assert entry["latest_version"] == ""


def test_records_sdk_package_and_resolved_version(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    record_detected_usage(
        catalog,
        repo_full_name="bedilk/app",
        commit_sha="abc",
        provider_id="postgresql",
        detected_symbols=["postgresql.pool.query"],
        classified_changes=[],
        sdk_versions={"pg": "6.3.0"},
    )

    (entry,) = _entries(catalog)
    assert entry["sdk_package"] == "pg"
    assert entry["current_version"] == "6.3.0"


def test_rescan_replaces_one_provider_snapshot_and_clears_stale_usage(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    record_detected_usage(
        catalog,
        repo_full_name="bedilk/app",
        commit_sha="old",
        provider_id="postgresql",
        detected_symbols=["postgresql.pg.connect"],
        classified_changes=[_change()],
        sdk_versions={"pg": "6.3.0"},
    )
    record_detected_usage(
        catalog,
        repo_full_name="bedilk/app",
        commit_sha="new",
        provider_id="postgresql",
        detected_symbols=["postgresql.<anchor>"],
        classified_changes=[_change()],
        sdk_versions={"pg": "8.0.0"},
    )

    assert "repository_migrations" not in yaml.safe_load(catalog.read_text(encoding="utf-8"))


def test_a_write_conservatively_normalizes_previous_statuses(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + "\n# Depfix-managed repository migration observations.\n"
        + "repository_migrations:\n"
        + "- provider_id: kafka\n"
        + "  repository: bedilk/old\n"
        + "  commit_sha: old\n"
        + "  current_api: kafka.Kafka\n"
        + "  sdk_version: 1.0.0\n"
        + "  status: detected\n",
        encoding="utf-8",
    )

    record_detected_usage(
        catalog,
        repo_full_name="bedilk/new",
        commit_sha="new",
        provider_id="postgresql",
        detected_symbols=["postgresql.pool.query"],
        classified_changes=[],
    )

    old_entry = next(entry for entry in _entries(catalog) if entry["repository"] == "bedilk/old")
    assert old_entry["status"] == "non_actionable"
    assert old_entry["current_version"] == "1.0.0"
    assert old_entry["reason"] == "rescan required to compare this usage with current feeds"


def test_empty_scan_also_normalizes_previous_statuses(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + "\n# Depfix-managed repository migration observations.\n"
        + "repository_migrations:\n"
        + "- provider_id: kafka\n"
        + "  repository: bedilk/old\n"
        + "  commit_sha: old\n"
        + "  current_api: kafka.Kafka\n"
        + "  status: current\n",
        encoding="utf-8",
    )

    record_detected_usage(
        catalog,
        repo_full_name="bedilk/empty",
        commit_sha="new",
        provider_id="postgresql",
        detected_symbols=[],
        classified_changes=[],
    )

    (old_entry,) = _entries(catalog)
    assert old_entry["status"] == "non_actionable"
