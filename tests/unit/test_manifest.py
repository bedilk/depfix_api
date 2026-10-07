"""Unit tests for npm manifest/lockfile scanning."""

from __future__ import annotations

import json
from pathlib import Path

from depfix.scanners.manifest import (
    affects_version,
    find_manifests,
    range_allows_major,
    scan_manifests,
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_find_manifests_skips_node_modules_and_finds_monorepo_workspaces(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {"name": "root"})
    _write_json(tmp_path / "packages" / "api" / "package.json", {"name": "api"})
    _write_json(tmp_path / "node_modules" / "lodash" / "package.json", {"name": "lodash"})

    found = find_manifests(tmp_path)

    rel = {p.relative_to(tmp_path) for p in found}
    assert Path("package.json") in rel
    assert Path("packages/api/package.json") in rel
    assert not any("node_modules" in p.parts for p in rel)


def test_scan_manifests_falls_back_to_declared_range_without_lockfile(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {"dependencies": {"openai": "^3.3.0"}})

    scan = scan_manifests(tmp_path, ["openai"])

    assert len(scan.matches) == 1
    match = scan.matches[0]
    assert match.package == "openai"
    assert match.declared_range == "^3.3.0"
    assert match.resolved_version is None
    assert match.source == "range"


def test_scan_manifests_ignores_untracked_packages(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {"dependencies": {"lodash": "^4.17.21"}})

    scan = scan_manifests(tmp_path, ["openai"])

    assert scan.manifests_found == [str(tmp_path / "package.json")]
    assert scan.matches == []


def test_scan_manifests_resolves_version_from_lockfile_v1(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {"dependencies": {"openai": "^3.0.0"}})
    _write_json(
        tmp_path / "package-lock.json",
        {
            "lockfileVersion": 1,
            "dependencies": {
                "openai": {"version": "3.3.0"},
                "other": {
                    "version": "1.0.0",
                    "dependencies": {"openai": {"version": "3.3.0"}},
                },
            },
        },
    )

    scan = scan_manifests(tmp_path, ["openai"])

    assert len(scan.matches) == 1
    assert scan.matches[0].resolved_version == "3.3.0"
    assert scan.matches[0].source == "lockfile"


def test_scan_manifests_resolves_version_from_lockfile_v2plus(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {"dependencies": {"openai": "^4.0.0"}})
    _write_json(
        tmp_path / "package-lock.json",
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "root"},
                "node_modules/openai": {"version": "4.20.1"},
                "packages/api/node_modules/openai": {"version": "4.0.0"},
            },
        },
    )

    scan = scan_manifests(tmp_path, ["openai"])

    # The shallowest (top-level) install wins when hoisted at more than one depth.
    assert scan.matches[0].resolved_version == "4.20.1"


def test_scan_manifests_resolves_workspace_version_from_pnpm_lockfile(tmp_path: Path) -> None:
    """A workspace package may use ``catalog:`` while pnpm pins the real SDK version."""
    _write_json(tmp_path / "package.json", {"name": "root"})
    _write_json(
        tmp_path / "apps" / "web" / "package.json",
        {"dependencies": {"@prisma/client": "catalog:"}},
    )
    (tmp_path / "pnpm-lock.yaml").write_text(
        """lockfileVersion: '9.0'
packages:
  '@prisma/client@6.14.0':
    resolution: {integrity: sha512-example}
""",
        encoding="utf-8",
    )

    scan = scan_manifests(tmp_path, ["@prisma/client"])

    assert scan.matches[0].resolved_version == "6.14.0"
    assert scan.matches[0].source == "lockfile"


def test_scan_manifests_skips_unparseable_manifest(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not valid json", encoding="utf-8")

    scan = scan_manifests(tmp_path, ["openai"])

    assert scan.manifests_found == []
    assert scan.matches == []


def test_affects_version_true_below_boundary() -> None:
    assert affects_version("3.3.0", "3.3.0", "4.0.0") is True


def test_affects_version_false_at_or_above_boundary() -> None:
    assert affects_version("4.1.0", "3.3.0", "4.0.0") is False


def test_affects_version_unparseable_returns_none() -> None:
    assert affects_version("latest", "3.3.0", "4.0.0") is None
    assert affects_version("4.0.0", "3.3.0", "not-a-version") is None


def test_range_allows_major_caret_and_tilde_pin_major() -> None:
    assert range_allows_major("^3.0.0", 3) is True
    assert range_allows_major("^3.0.0", 4) is False
    assert range_allows_major("~3.2.0", 3) is True


def test_range_allows_major_comparison_operators() -> None:
    assert range_allows_major(">=3.0.0", 4) is True
    assert range_allows_major(">=3.0.0", 2) is False
    assert range_allows_major("<3.0.0", 2) is True
    assert range_allows_major("<3.0.0", 4) is False


def test_range_allows_major_unknowable_specs_return_none() -> None:
    assert range_allows_major("latest", 4) is None
    assert range_allows_major("*", 4) is None
    assert range_allows_major("git+https://example.com/foo.git", 4) is None
    assert range_allows_major("workspace:*", 4) is None


def test_range_allows_major_or_combinator() -> None:
    assert range_allows_major("^3.0.0 || ^4.0.0", 4) is True
    assert range_allows_major("^3.0.0 || ^4.0.0", 5) is False
    assert range_allows_major("^3.0.0 || latest", 5) is None


def test_range_allows_major_hyphen_range() -> None:
    assert range_allows_major("3.0.0 - 4.5.0", 4) is True
    assert range_allows_major("3.0.0 - 4.5.0", 5) is False


def test_range_allows_major_multiple_space_separated_constraints_is_unknown() -> None:
    assert range_allows_major(">=3.0.0 <5.0.0", 4) is None
