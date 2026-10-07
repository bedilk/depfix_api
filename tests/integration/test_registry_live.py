"""Live integration test — hits the real npm registry.

Marked ``integration`` so CI skips it (see docs/decisions.md). Run locally with:

    pytest -m integration tests/integration/test_registry_live.py
"""

from __future__ import annotations

import re

import pytest

from depfix.registry.client import NpmRegistryClient

pytestmark = pytest.mark.integration


_SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+].*)?$")


def test_fetch_lodash_latest_matches_semver() -> None:
    with NpmRegistryClient() as c:
        meta = c.get_package_metadata("lodash")
    assert _SEMVER.match(meta.latest), f"unexpected latest: {meta.latest!r}"
    assert len(meta.versions) > 0
