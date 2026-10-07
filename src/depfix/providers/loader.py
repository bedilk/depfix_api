"""Load and validate providers.yaml.

Fails loudly on malformed input. A silently-skipped provider is a silently
missed breaking change, which is the exact failure mode we exist to remove.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from depfix.providers.models import FeedSpec, ProviderSpec, SdkPackage
from depfix.sources.models import SourceKind


class ProviderConfigError(ValueError):
    """providers.yaml is missing or malformed."""


def load_providers_file(path: str | Path) -> list[ProviderSpec]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"providers file not found: {file_path}")

    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or "providers" not in data:
        raise ProviderConfigError(f"{file_path}: expected a top-level 'providers:' key")

    raw_providers = data["providers"]
    if not isinstance(raw_providers, list) or not raw_providers:
        raise ProviderConfigError(f"{file_path}: 'providers' must be a non-empty list")

    providers: list[ProviderSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_providers):
        provider = _parse_provider(entry, f"{file_path}: providers[{index}]")
        if provider.id in seen:
            raise ProviderConfigError(f"{file_path}: duplicate provider id {provider.id!r}")
        seen.add(provider.id)
        providers.append(provider)
    return providers


def _parse_provider(entry: Any, where: str) -> ProviderSpec:
    if not isinstance(entry, dict):
        raise ProviderConfigError(f"{where}: must be a mapping")

    provider_id = entry.get("id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ProviderConfigError(f"{where}: 'id' is required and must be a string")

    raw_feeds = entry.get("feeds") or []
    if not isinstance(raw_feeds, list) or not raw_feeds:
        raise ProviderConfigError(f"{where}: at least one feed is required")

    feeds = tuple(
        _parse_feed(raw_feed, f"{where}.feeds[{i}]") for i, raw_feed in enumerate(raw_feeds)
    )

    if "call_site_anchors" in entry:
        raise ProviderConfigError(
            f"{where}: 'call_site_anchors' is no longer supported. Call sites are "
            "derived from each provider's feeds (the old and replacement APIs of "
            "classified changes). Delete the key."
        )
    return ProviderSpec(
        id=provider_id.strip(),
        name=str(entry.get("name") or provider_id).strip(),
        feeds=feeds,
        sdk_packages=_parse_sdk_packages(entry.get("sdk_packages"), where),
        api_version_scheme=_optional_str(entry.get("api_version_scheme")),
        api_base_urls=_str_tuple(entry.get("api_base_urls"), f"{where}.api_base_urls"),
        enabled=bool(entry.get("enabled", True)),
    )


def _parse_feed(entry: Any, where: str) -> FeedSpec:
    if not isinstance(entry, dict):
        raise ProviderConfigError(f"{where}: must be a mapping")

    raw_kind = entry.get("kind")
    if not isinstance(raw_kind, str):
        raise ProviderConfigError(f"{where}: 'kind' is required")
    try:
        kind = SourceKind(raw_kind)
    except ValueError:
        valid = ", ".join(sorted(k.value for k in SourceKind))
        raise ProviderConfigError(
            f"{where}: unknown feed kind {raw_kind!r} (valid: {valid})"
        ) from None

    config = {k: v for k, v in entry.items() if k != "kind"}
    return FeedSpec(kind=kind, config=config)


def _parse_sdk_packages(raw: Any, where: str) -> tuple[SdkPackage, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProviderConfigError(f"{where}.sdk_packages: must be a list")

    packages: list[SdkPackage] = []
    for i, entry in enumerate(raw):
        if isinstance(entry, str):
            packages.append(SdkPackage(name=entry))
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            packages.append(
                SdkPackage(
                    name=entry["name"],
                    ecosystem=str(entry.get("ecosystem") or "npm"),
                )
            )
        else:
            raise ProviderConfigError(
                f"{where}.sdk_packages[{i}]: expected a string or a mapping with 'name'"
            )
    return tuple(packages)


def _str_tuple(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise ProviderConfigError(f"{where}: must be a list of non-empty strings")
    return tuple(raw)


def _optional_str(raw: Any) -> str | None:
    return raw.strip() if isinstance(raw, str) and raw.strip() else None
