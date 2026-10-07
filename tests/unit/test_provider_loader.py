"""Unit tests for providers.yaml loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.providers.loader import ProviderConfigError, load_providers_file
from depfix.sources.models import SourceKind


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "providers.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_parses_valid_file(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        providers:
          - id: acme
            name: Acme
            feeds:
              - kind: npm_dist_tag
                package: acme
                dist_tag: latest
        """,
    )
    (provider,) = load_providers_file(path)
    assert provider.id == "acme"
    assert provider.name == "Acme"
    assert provider.enabled is True
    assert len(provider.feeds) == 1
    assert provider.feeds[0].kind is SourceKind.NPM_DIST_TAG
    assert provider.feeds[0].config == {"package": "acme", "dist_tag": "latest"}


def test_unknown_feed_kind_is_rejected_with_valid_list(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        providers:
          - id: acme
            feeds:
              - kind: carrier_pigeon
        """,
    )
    with pytest.raises(ProviderConfigError) as exc_info:
        load_providers_file(path)
    message = str(exc_info.value)
    assert "carrier_pigeon" in message
    for kind in SourceKind:
        assert kind.value in message


def test_declared_but_unimplemented_kind_parses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        providers:
          - id: acme
            feeds:
              - kind: rss
                url: https://example.com/feed.xml
        """,
    )
    (provider,) = load_providers_file(path)
    assert provider.feeds[0].kind is SourceKind.RSS


def test_provider_without_feeds_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        providers:
          - id: acme
        """,
    )
    with pytest.raises(ProviderConfigError, match="at least one feed"):
        load_providers_file(path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        providers:
          - id: acme
            feeds:
              - kind: npm_dist_tag
                package: acme
          - id: acme
            feeds:
              - kind: npm_dist_tag
                package: acme-two
        """,
    )
    with pytest.raises(ProviderConfigError, match="duplicate provider id"):
        load_providers_file(path)


def test_missing_top_level_key(tmp_path: Path) -> None:
    path = _write(tmp_path, "not_providers: []\n")
    with pytest.raises(ProviderConfigError, match="providers"):
        load_providers_file(path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_providers_file(tmp_path / "does-not-exist.yaml")


def test_name_defaults_to_id(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        providers:
          - id: acme
            feeds:
              - kind: npm_dist_tag
                package: acme
        """,
    )
    (provider,) = load_providers_file(path)
    assert provider.name == "acme"


def test_repo_shipped_providers_file_is_valid() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    providers = load_providers_file(repo_root / "providers.yaml")
    ids = {p.id for p in providers}
    assert {
        "openai",
        "stripe",
        "anthropic",
        "supabase",
        "vercel-ai",
        "sentry",
        "prisma",
        "clerk",
        "langchain",
        "aws-s3",
        "firebase",
        "twilio",
        "sendgrid",
        "octokit",
        "slack",
        "discord",
        "mongodb",
        "redis",
        "posthog",
        "algolia",
        "google-cloud",
        "elasticsearch",
        "postgresql",
        "kafka",
        "rabbitmq",
    } <= ids
    assert len([provider for provider in providers if provider.enabled]) == 25


def test_call_site_anchors_are_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        providers:
          - id: acme
            call_site_anchors: ["acme.client.create("]
            feeds:
              - kind: npm_dist_tag
                package: acme
        """,
    )
    with pytest.raises(ProviderConfigError, match="call_site_anchors"):
        load_providers_file(path)
