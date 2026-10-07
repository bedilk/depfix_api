"""Feed-kind → source-class wiring, and the dependency bundle sources need."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from depfix.config import get_settings
from depfix.registry.client import NpmRegistryClient
from depfix.registry.pypi_client import PypiRegistryClient
from depfix.registry.rubygems_client import RubyGemsClient
from depfix.sources.asyncapi_spec import AsyncApiSpecSource
from depfix.sources.base import ChangeSource, UnsupportedSourceKind
from depfix.sources.changelog_file import ChangelogFileSource
from depfix.sources.changelog_html import ChangelogHtmlSource
from depfix.sources.fixture_change import FixtureChangeSource
from depfix.sources.github_release import GitHubReleaseSource
from depfix.sources.http import build_http_client
from depfix.sources.models import SourceKind
from depfix.sources.npm_dist_tag import NpmDistTagSource
from depfix.sources.openapi_spec import OpenApiSpecSource
from depfix.sources.pypi_dist_tag import PypiDistTagSource
from depfix.sources.redis_commands import RedisCommandsSource
from depfix.sources.rss import RssSource
from depfix.sources.rubygems_dist_tag import RubyGemsDistTagSource
from depfix.sources.security_advisory import SecurityAdvisorySource
from depfix.sources.smithy_spec import SmithySpecSource
from depfix.sources.ts_exports import TsExportsSource


class FeedLike(Protocol):
    """Structural type for a feed declaration.

    ``providers.models.FeedSpec`` satisfies this without inheriting from it.
    Declared as a Protocol so that ``sources`` never imports ``providers``:
    the dependency runs providers → sources, one direction only. Importing
    upward created a circular import via ``sources/__init__.py``.
    """

    @property
    def kind(self) -> SourceKind: ...

    @property
    def config(self) -> dict[str, Any]: ...


@dataclass
class SourceDeps:
    """Everything a source might need. Built once per watch run."""

    http: httpx.Client
    npm: NpmRegistryClient
    pypi: PypiRegistryClient
    rubygems: RubyGemsClient
    spec_cache_dir: Path
    max_spec_bytes: int
    github_api_url: str

    @classmethod
    def from_settings(cls) -> SourceDeps:
        settings = get_settings()
        return cls(
            http=build_http_client(),
            npm=NpmRegistryClient(),
            pypi=PypiRegistryClient(),
            rubygems=RubyGemsClient(),
            spec_cache_dir=Path(settings.spec_cache_dir),
            max_spec_bytes=settings.max_spec_bytes,
            github_api_url=settings.github_api_url,
        )

    def close(self) -> None:
        self.http.close()
        self.npm.close()
        self.pypi.close()
        self.rubygems.close()

    def __enter__(self) -> SourceDeps:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def build_source(provider_id: str, feed: FeedLike, deps: SourceDeps) -> ChangeSource:
    if feed.kind is SourceKind.NPM_DIST_TAG:
        return NpmDistTagSource(
            provider_id,
            feed.config,
            deps.npm,
            http=deps.http,
            github_api_url=deps.github_api_url,
        )
    if feed.kind is SourceKind.PYPI_DIST_TAG:
        return PypiDistTagSource(
            provider_id,
            feed.config,
            deps.pypi,
            http=deps.http,
            github_api_url=deps.github_api_url,
        )
    if feed.kind is SourceKind.GITHUB_RELEASE:
        return GitHubReleaseSource(provider_id, feed.config, deps.http, deps.github_api_url)
    if feed.kind in (
        SourceKind.OPENAPI_SPEC,
        SourceKind.SMITHY_SPEC,
        SourceKind.ASYNCAPI_SPEC,
        SourceKind.REDIS_COMMANDS,
    ):
        source_cls = {
            SourceKind.OPENAPI_SPEC: OpenApiSpecSource,
            SourceKind.SMITHY_SPEC: SmithySpecSource,
            SourceKind.ASYNCAPI_SPEC: AsyncApiSpecSource,
            SourceKind.REDIS_COMMANDS: RedisCommandsSource,
        }[feed.kind]
        return source_cls(  # type: ignore[abstract]
            provider_id,
            feed.config,
            deps.http,
            deps.spec_cache_dir,
            deps.max_spec_bytes,
        )
    if feed.kind is SourceKind.TS_EXPORTS_DIFF:
        return TsExportsSource(provider_id, feed.config, deps.http, deps.npm)
    if feed.kind is SourceKind.RUBYGEMS_DIST_TAG:
        return RubyGemsDistTagSource(
            provider_id,
            feed.config,
            deps.rubygems,
            http=deps.http,
            github_api_url=deps.github_api_url,
        )
    if feed.kind is SourceKind.SECURITY_ADVISORY:
        return SecurityAdvisorySource(provider_id, feed.config, deps.http)
    if feed.kind is SourceKind.FIXTURE_CHANGE:
        return FixtureChangeSource(provider_id, feed.config)
    if feed.kind is SourceKind.CHANGELOG_HTML:
        return ChangelogHtmlSource(provider_id, feed.config, deps.http)
    if feed.kind is SourceKind.CHANGELOG_FILE:
        return ChangelogFileSource(provider_id, feed.config, deps.http)
    if feed.kind is SourceKind.RSS:
        return RssSource(provider_id, feed.config, deps.http)
    raise UnsupportedSourceKind(f"feed kind '{feed.kind.value}' is not implemented")
