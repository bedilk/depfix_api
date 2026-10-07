"""Unit tests for ecosystem detection and lookup (``ecosystems/registry.py``).

Detection is marker-file scoring over ``ecosystems/specs.py``; these tests
build one-marker ``tmp_path`` trees per ecosystem so a mis-weighted or
mis-spelled marker in a spec shows up here rather than in a live scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.ecosystems import registry
from depfix.ecosystems.specs import ALL_SPECS

# -- detect ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("package.json", "javascript"),
        ("pyproject.toml", "python"),
        ("pom.xml", "java"),
        ("build.gradle.kts", "kotlin"),
        ("go.mod", "go"),
        ("Cargo.toml", "rust"),
        ("Gemfile", "ruby"),
        ("composer.json", "php"),
        ("Package.swift", "swift"),
    ],
)
def test_detect_identifies_ecosystem_from_its_primary_marker(
    tmp_path: Path, marker: str, expected: str
) -> None:
    (tmp_path / marker).write_text("{}\n", encoding="utf-8")

    assert registry.detect(tmp_path) == expected


def test_detect_identifies_csharp_from_csproj_glob_marker(tmp_path: Path) -> None:
    (tmp_path / "MyApp.csproj").write_text("<Project />", encoding="utf-8")

    assert registry.detect(tmp_path) == "csharp"


def test_detect_returns_unknown_for_empty_directory(tmp_path: Path) -> None:
    assert registry.detect(tmp_path) == "unknown"


def test_detect_prefers_javascript_for_tsconfig_plus_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")

    assert registry.detect(tmp_path) == "javascript"


def test_detect_picks_higher_scoring_ecosystem_in_polyglot_checkout(tmp_path: Path) -> None:
    # python scores 10 (pyproject) + 5 (requirements); javascript only 5 (yarn.lock).
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")

    assert registry.detect(tmp_path) == "python"


def test_detect_scores_kotlin_for_gradle_kts_not_java(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text("", encoding="utf-8")
    (tmp_path / "settings.gradle.kts").write_text("", encoding="utf-8")

    assert registry.detect(tmp_path) == "kotlin"


def test_detect_finds_nested_manifest_in_monorepo_layout(tmp_path: Path) -> None:
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "go.mod").write_text("module x\n", encoding="utf-8")

    assert registry.detect(tmp_path) == "go"


# -- get / all_ids / fixable_ids ---------------------------------------------


def test_all_ids_covers_the_ten_specs_in_declaration_order() -> None:
    assert registry.all_ids() == tuple(spec.id for spec in ALL_SPECS)
    assert len(registry.all_ids()) == 10
    assert registry.all_ids()[0] == "javascript"


def test_fixable_ids_includes_all_fix_supported_ecosystems() -> None:
    assert registry.fixable_ids() == frozenset({"javascript", "python", "ruby", "go"})


def test_get_returns_spec_by_id_and_none_for_unknown() -> None:
    spec = registry.get("python")

    assert spec is not None
    assert spec.display_name == "Python"
    assert registry.get("cobol") is None


# -- ecosystem_for_registry ---------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("npm", "javascript"),
        ("pypi", "python"),
        ("pip", "python"),
        ("maven", "java"),
        ("gradle", "java"),
        ("cargo", "rust"),
        ("nuget", "csharp"),
        ("composer", "php"),
        ("rubygems", "ruby"),
        ("swiftpm", "swift"),
    ],
)
def test_ecosystem_for_registry_maps_alias_to_spec(alias: str, expected: str) -> None:
    spec = registry.ecosystem_for_registry(alias)

    assert spec is not None
    assert spec.id == expected


def test_ecosystem_for_registry_normalizes_case_and_whitespace() -> None:
    spec = registry.ecosystem_for_registry("  PyPI \n")

    assert spec is not None
    assert spec.id == "python"


def test_ecosystem_for_registry_returns_none_for_unknown_registry() -> None:
    assert registry.ecosystem_for_registry("cpan") is None


# -- spec invariants ----------------------------------------------------------


def test_every_registry_alias_is_unique_across_specs() -> None:
    aliases = [alias for spec in ALL_SPECS for alias in spec.registry_aliases]

    assert len(aliases) == len(set(aliases))


def test_all_skip_dirs_merges_common_skip_dirs_into_spec_skip_dirs() -> None:
    csharp = registry.get("csharp")

    assert csharp is not None
    assert {"bin", "obj"} <= csharp.all_skip_dirs()
    assert {"node_modules", ".git", ".venv"} <= csharp.all_skip_dirs()
