"""Load and validate a repo's ``.depfix.yml``.

Fails loudly on malformed input, following the same convention as
:mod:`depfix.providers.loader`: a repo owner who mistypes a field should
get a descriptive error, not a config that silently falls back to
"pipeline disabled" or, worse, "everything allowed".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from depfix.repoconfig.models import (
    SUPPORTED_VERSION,
    PathFilter,
    ProviderFilter,
    RepoConfig,
)

_KNOWN_FIELDS = {
    "version",
    "enabled",
    "base_branch",
    "verify",
    "open_pr",
    "draft_pr",
    "max_changes_per_run",
    "max_repo_mb",
    "providers",
    "paths",
    "ignore",
}
_KNOWN_FILTER_FIELDS = {"include", "exclude"}


class RepoConfigError(ValueError):
    """A repo's ``.depfix.yml`` is missing, malformed, or uses an
    unsupported ``version``."""


def load_repo_config(path: str | Path) -> RepoConfig:
    """Read and parse ``path`` as a ``.depfix.yml``."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"repo config not found: {file_path}")
    return parse_repo_config(file_path.read_text(encoding="utf-8"), where=str(file_path))


def parse_repo_config(text: str, *, where: str = "<string>") -> RepoConfig:
    """Parse already-read ``.depfix.yml`` text. Split out from
    :func:`load_repo_config` so callers that fetch the file over the
    GitHub API (no local path) can validate it too."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RepoConfigError(f"{where}: invalid YAML ({exc})") from exc

    if not isinstance(data, dict):
        raise RepoConfigError(f"{where}: top-level document must be a mapping")

    unknown = set(data) - _KNOWN_FIELDS
    if unknown:
        raise RepoConfigError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise RepoConfigError(f"{where}: 'version' is required and must be an integer")
    if version != SUPPORTED_VERSION:
        raise RepoConfigError(
            f"{where}: unsupported version {version!r} (depfix supports version {SUPPORTED_VERSION})"
        )

    return RepoConfig(
        version=version,
        enabled=_bool(data, "enabled", default=True, where=where),
        base_branch=_optional_str(data.get("base_branch"), f"{where}.base_branch"),
        verify=_bool(data, "verify", default=True, where=where),
        open_pr=_bool(data, "open_pr", default=True, where=where),
        draft_pr=_bool(data, "draft_pr", default=False, where=where),
        max_changes_per_run=_optional_positive_int(
            data.get("max_changes_per_run"), f"{where}.max_changes_per_run"
        ),
        providers=_parse_filter(data.get("providers"), f"{where}.providers", cls=ProviderFilter),
        paths=_parse_filter(data.get("paths"), f"{where}.paths", cls=PathFilter),
        ignore=_str_tuple(data.get("ignore"), f"{where}.ignore"),
        max_repo_mb=_optional_positive_int(data.get("max_repo_mb"), f"{where}.max_repo_mb"),
    )


def _bool(data: dict[str, Any], key: str, *, default: bool, where: str) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise RepoConfigError(f"{where}.{key}: must be a boolean")
    return value


def _optional_str(raw: Any, where: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise RepoConfigError(f"{where}: must be a non-empty string")
    return raw.strip()


def _optional_positive_int(raw: Any, where: str) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise RepoConfigError(f"{where}: must be a positive integer")
    return raw


def _str_tuple(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise RepoConfigError(f"{where}: must be a list of non-empty strings")
    return tuple(raw)


def _parse_filter(raw: Any, where: str, *, cls: type[ProviderFilter] | type[PathFilter]) -> Any:
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise RepoConfigError(f"{where}: must be a mapping")
    unknown = set(raw) - _KNOWN_FILTER_FIELDS
    if unknown:
        raise RepoConfigError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")
    return cls(
        include=_str_tuple(raw.get("include"), f"{where}.include"),
        exclude=_str_tuple(raw.get("exclude"), f"{where}.exclude"),
    )
