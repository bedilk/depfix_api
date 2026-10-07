"""Unit tests for a repo's opt-in ``.depfix.yml`` (``depfix.repoconfig``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.repoconfig.loader import RepoConfigError, load_repo_config, parse_repo_config
from depfix.repoconfig.models import PathFilter, ProviderFilter, RepoConfig, glob_match


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".depfix.yml"
    path.write_text(text, encoding="utf-8")
    return path


# -- parse_repo_config: defaults + happy path ---------------------------------


def test_minimal_valid_config_uses_defaults() -> None:
    config = parse_repo_config("version: 1\n")
    assert config == RepoConfig(version=1)
    assert config.enabled is True
    assert config.base_branch is None
    assert config.verify is True
    assert config.open_pr is True
    assert config.draft_pr is False
    assert config.max_changes_per_run is None
    assert config.providers == ProviderFilter()
    assert config.paths == PathFilter()
    assert config.ignore == ()


def test_fully_populated_config_parses_every_field() -> None:
    config = parse_repo_config(
        """
        version: 1
        enabled: false
        base_branch: develop
        verify: false
        open_pr: false
        draft_pr: true
        max_changes_per_run: 2
        providers:
          include: ["openai", "stripe-*"]
          exclude: ["stripe-legacy"]
        paths:
          include: ["src/**"]
          exclude: ["src/vendor/**"]
        ignore:
          - some-dedupe-key
        """
    )
    assert config.enabled is False
    assert config.base_branch == "develop"
    assert config.verify is False
    assert config.open_pr is False
    assert config.draft_pr is True
    assert config.max_changes_per_run == 2
    assert config.providers == ProviderFilter(
        include=("openai", "stripe-*"), exclude=("stripe-legacy",)
    )
    assert config.paths == PathFilter(include=("src/**",), exclude=("src/vendor/**",))
    assert config.ignore == ("some-dedupe-key",)


def test_empty_document_is_rejected_for_missing_version() -> None:
    with pytest.raises(RepoConfigError, match="version"):
        parse_repo_config("")


# -- version validation ---------------------------------------------------------


def test_missing_version_is_rejected() -> None:
    with pytest.raises(RepoConfigError, match="'version' is required"):
        parse_repo_config("enabled: true\n")


def test_non_integer_version_is_rejected() -> None:
    with pytest.raises(RepoConfigError, match="'version' is required"):
        parse_repo_config("version: '1'\n")


def test_boolean_version_is_rejected() -> None:
    """``True``/``False`` are technically ``int`` subclasses in Python --
    the loader must special-case them so ``version: true`` doesn't parse
    as ``version: 1``."""
    with pytest.raises(RepoConfigError, match="'version' is required"):
        parse_repo_config("version: true\n")


def test_unsupported_version_is_rejected() -> None:
    with pytest.raises(RepoConfigError, match="unsupported version"):
        parse_repo_config("version: 2\n")


# -- structural validation -------------------------------------------------------


def test_invalid_yaml_is_rejected() -> None:
    with pytest.raises(RepoConfigError, match="invalid YAML"):
        parse_repo_config("version: 1\n  bad: indent: here\n")


def test_non_mapping_top_level_is_rejected() -> None:
    with pytest.raises(RepoConfigError, match="must be a mapping"):
        parse_repo_config("- version\n- 1\n")


def test_unknown_top_level_field_is_rejected() -> None:
    with pytest.raises(RepoConfigError, match=r"unknown field.*unknown_field"):
        parse_repo_config("version: 1\nunknown_field: true\n")


def test_unknown_filter_field_is_rejected() -> None:
    with pytest.raises(RepoConfigError, match=r"providers.*unknown field.*typo"):
        parse_repo_config("version: 1\nproviders:\n  typo: [x]\n")


# -- per-field type validation ---------------------------------------------------


@pytest.mark.parametrize("field", ["enabled", "verify", "open_pr", "draft_pr"])
def test_non_boolean_bool_field_is_rejected(field: str) -> None:
    with pytest.raises(RepoConfigError, match=f"{field}.*must be a boolean"):
        parse_repo_config(f"version: 1\n{field}: yes-please\n")


def test_empty_base_branch_is_rejected() -> None:
    with pytest.raises(RepoConfigError, match=r"base_branch.*non-empty string"):
        parse_repo_config("version: 1\nbase_branch: ''\n")


def test_non_string_base_branch_is_rejected() -> None:
    with pytest.raises(RepoConfigError, match=r"base_branch.*non-empty string"):
        parse_repo_config("version: 1\nbase_branch: 7\n")


def test_base_branch_is_stripped() -> None:
    config = parse_repo_config("version: 1\nbase_branch: '  main  '\n")
    assert config.base_branch == "main"


@pytest.mark.parametrize("bad_value", ["0", "-1", "1.5", "true"])
def test_non_positive_max_changes_per_run_is_rejected(bad_value: str) -> None:
    with pytest.raises(RepoConfigError, match=r"max_changes_per_run.*positive integer"):
        parse_repo_config(f"version: 1\nmax_changes_per_run: {bad_value}\n")


def test_ignore_must_be_a_list_of_non_empty_strings() -> None:
    with pytest.raises(RepoConfigError, match=r"ignore.*list of non-empty strings"):
        parse_repo_config("version: 1\nignore: not-a-list\n")


def test_ignore_rejects_empty_string_entries() -> None:
    with pytest.raises(RepoConfigError, match=r"ignore.*list of non-empty strings"):
        parse_repo_config("version: 1\nignore:\n  - ''\n")


def test_providers_filter_must_be_a_mapping() -> None:
    with pytest.raises(RepoConfigError, match=r"providers.*must be a mapping"):
        parse_repo_config("version: 1\nproviders: not-a-mapping\n")


# -- load_repo_config (file-based) ------------------------------------------------


def test_load_repo_config_reads_a_real_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 1\nbase_branch: main\n")
    config = load_repo_config(path)
    assert config.base_branch == "main"


def test_load_repo_config_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_repo_config("/nonexistent/.depfix.yml")


def test_load_repo_config_error_message_includes_path(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 2\n")
    with pytest.raises(RepoConfigError, match=str(path)):
        load_repo_config(path)


# -- glob_match: segment-aware globbing -----------------------------------------


@pytest.mark.parametrize(
    ("value", "pattern", "expected"),
    [
        ("openai", "openai", True),
        ("openai-beta", "openai-*", True),
        ("openai/beta", "openai-*", False),  # `*` never crosses a `/`
        ("src/a.js", "src/*", True),
        ("src/nested/a.js", "src/*", False),
        ("src/nested/a.js", "src/**", True),
        ("src", "src/**", False),  # `src/**` matches *under* src/, not the bare segment itself
        ("tests-utils/x.js", "tests/**", False),
        ("tests/x.js", "tests/**", True),
        ("foo", "**/foo", True),
        ("a/b/foo", "**/foo", True),
        ("a.js", "?.js", True),
        ("ab.js", "?.js", False),
    ],
)
def test_glob_match_is_segment_aware(value: str, pattern: str, expected: bool) -> None:
    assert glob_match(value, pattern) is expected


# -- ProviderFilter / PathFilter: exclude wins over include ----------------------


def test_provider_filter_empty_means_no_restriction() -> None:
    assert ProviderFilter().allows("anything") is True


def test_provider_filter_include_restricts() -> None:
    filt = ProviderFilter(include=("openai",))
    assert filt.allows("openai") is True
    assert filt.allows("anthropic") is False


def test_provider_filter_exclude_wins_over_include() -> None:
    filt = ProviderFilter(include=("openai*",), exclude=("openai-legacy",))
    assert filt.allows("openai") is True
    assert filt.allows("openai-legacy") is False


def test_path_filter_exclude_wins_over_include() -> None:
    filt = PathFilter(include=("src/**",), exclude=("src/vendor/**",))
    assert filt.allows("src/app.js") is True
    assert filt.allows("src/vendor/lib.js") is False
    assert filt.allows("docs/readme.md") is False


# -- RepoConfig.is_ignored --------------------------------------------------------


def test_is_ignored_matches_provider_id() -> None:
    config = RepoConfig(version=1, ignore=("openai*",))
    assert config.is_ignored(provider_id="openai", dedupe_key="unrelated") is True


def test_is_ignored_matches_dedupe_key() -> None:
    config = RepoConfig(version=1, ignore=("the-exact-key",))
    assert config.is_ignored(provider_id="openai", dedupe_key="the-exact-key") is True


def test_is_ignored_false_when_nothing_matches() -> None:
    config = RepoConfig(version=1, ignore=("something-else",))
    assert config.is_ignored(provider_id="openai", dedupe_key="the-exact-key") is False


def test_is_ignored_false_by_default() -> None:
    config = RepoConfig(version=1)
    assert config.is_ignored(provider_id="openai", dedupe_key="anything") is False
