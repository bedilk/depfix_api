"""Change sources — the detection layer.

One provider owns N feeds; each feed is a ``ChangeSource`` that turns "did
anything move" into ``ChangeEvent``. npm is one kind among several, not the
trunk.
"""

from depfix.sources.asyncapi_spec import AsyncApiSpecSource
from depfix.sources.base import ChangeSource, UnsupportedSourceKind
from depfix.sources.factory import SourceDeps, build_source
from depfix.sources.fixture_change import FixtureChangeSource
from depfix.sources.github_release import GitHubReleaseSource
from depfix.sources.models import (
    AdvisoryRecord,
    ChangeEvent,
    FeedPoll,
    FeedStateSnapshot,
    Severity,
    SourceKind,
    SpecChange,
    SpecChangeKind,
    max_severity,
)
from depfix.sources.npm_dist_tag import NpmDistTagSource
from depfix.sources.openapi_diff import diff_specs
from depfix.sources.openapi_spec import OpenApiSpecSource
from depfix.sources.pypi_dist_tag import PypiDistTagSource
from depfix.sources.redis_commands import RedisCommandsSource
from depfix.sources.rubygems_dist_tag import RubyGemsDistTagSource
from depfix.sources.security_advisory import SecurityAdvisorySource
from depfix.sources.smithy_spec import SmithySpecSource
from depfix.sources.ts_exports import TsExportsSource

__all__ = [
    "AdvisoryRecord",
    "AsyncApiSpecSource",
    "ChangeEvent",
    "ChangeSource",
    "FeedPoll",
    "FeedStateSnapshot",
    "FixtureChangeSource",
    "GitHubReleaseSource",
    "NpmDistTagSource",
    "OpenApiSpecSource",
    "PypiDistTagSource",
    "RedisCommandsSource",
    "RubyGemsDistTagSource",
    "SecurityAdvisorySource",
    "Severity",
    "SmithySpecSource",
    "SourceDeps",
    "SourceKind",
    "SpecChange",
    "SpecChangeKind",
    "TsExportsSource",
    "UnsupportedSourceKind",
    "build_source",
    "diff_specs",
    "max_severity",
]
