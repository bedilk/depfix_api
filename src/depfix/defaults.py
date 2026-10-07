"""Allowlisted local defaults and remembered interactive CLI context."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)
_FILENAME = ".depfix-local.yml"
_ALLOWED = frozenset(
    {
        "llm_provider",
        # google_api_key excluded: storing API keys in a plaintext YAML file
        # that could be accidentally committed is a credential leak risk.
        "gemini_model",
        "ollama_model",
        # ollama_base_url excluded: can be pointed at a malicious server.
        # providers_file excluded: can point at arbitrary YAML on disk.
        "output_dir",
        "plan_dir",
        "log_level",
        "log_format",
        "drift_scope",
    }
)
# Repository and provider are stable user preferences. Event IDs and local
# change-file paths are not: an ID belongs to one database and a path belongs
# to one checkout. Reusing either can silently override automatic detection.
_CONTEXT = frozenset({"last_repos", "last_provider"})


def _path(search_from: Path | None = None) -> Path:
    return (search_from or Path.cwd()).resolve() / _FILENAME


def load_local_defaults(search_from: Path | None = None) -> dict[str, Any]:
    try:
        data = yaml.safe_load(_path(search_from).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return (
        {
            key: value
            for key, value in data.items()
            if key in _ALLOWED | _CONTEXT and value is not None
        }
        if isinstance(data, dict)
        else {}
    )


def apply_defaults_to_settings(settings: Any, defaults: dict[str, Any]) -> None:
    baseline = type(settings)()
    for key in _ALLOWED:
        if key in defaults and getattr(settings, key, None) == getattr(baseline, key, None):
            setattr(settings, key, defaults[key])


def fill_pipeline_context(args: Any, defaults: dict[str, Any]) -> None:
    """Fill only omitted optional pipeline filters; explicit flags always win."""
    has_target = bool(getattr(args, "repos", None) or getattr(args, "repo", None))
    if not getattr(args, "repos", None) and not getattr(args, "repo", None):
        repos = defaults.get("last_repos")
        if isinstance(repos, list) and all(isinstance(repo, str) for repo in repos):
            args.repos = repos
            has_target = True
    # A fleet-wide run is intentional but must never overwrite the last
    # focused repository context. Otherwise one accidental bare `plan`
    # turns the next bare `apply` into a fleet operation.
    args._depfix_has_target = has_target
    if getattr(args, "provider", None) is None and defaults.get("last_provider"):
        args.provider = [defaults["last_provider"]]


def save_context(
    *,
    repos: list[str],
    provider: list[str] | None,
    from_event: list[int] | None,
    change_file: list[str] | None,
    search_from: Path | None = None,
) -> None:
    """Remember only portable context.

    ``from_event`` and ``change_file`` remain parameters for call-site
    compatibility while older callers are migrated; they are deliberately not
    persisted because they are local to a database or checkout.
    """
    data = load_local_defaults(search_from)
    data["last_repos"] = repos
    if provider:
        data["last_provider"] = provider[0]
    try:
        _path(search_from).write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.debug("could not save local depfix context: %s", exc)
