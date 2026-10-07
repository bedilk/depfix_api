"""Security-advisory feed: OSV / GitHub Advisory Database.

Every other feed in this package answers "did a new version appear". This one
answers a different, higher-urgency question: "is the version this repository
already uses known to be unsafe, and what fixes it". That distinction is why
it is its own SourceKind and carries an AdvisoryRecord rather than a
SpecChange -- an advisory is not a structural API delta, it is a statement
about a version *range*.

Uses OSV (https://osv.dev) as the default source: a free, public, no-auth
API that aggregates the GitHub Advisory Database, PyPA, RustSec, and others,
keyed by ecosystem + package + version. The feed is deliberately
version-scoped: it is built per (provider SDK package, currently-observed
version), so it only ever reports advisories that actually apply to the
version in play, not the package's entire history.

Never raises for expected failures (network, 404, malformed payload) -- it
returns a FeedPoll with ``error``/``not_found`` set, same contract as every
other ChangeSource.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import httpx

from depfix.sources.base import ChangeSource
from depfix.sources.models import (
    AdvisoryRecord,
    ChangeEvent,
    FeedPoll,
    FeedStateSnapshot,
    Severity,
    SourceKind,
)

logger = logging.getLogger(__name__)

DEFAULT_OSV_URL = "https://api.osv.dev"

#: OSV ecosystem names differ from providers.yaml's registry aliases.
_ECOSYSTEM_TO_OSV = {
    "npm": "npm",
    "pypi": "PyPI",
    "pip": "PyPI",
    "cargo": "crates.io",
    "crates": "crates.io",
    "go": "Go",
    "gomod": "Go",
    "rubygems": "RubyGems",
    "gem": "RubyGems",
    "composer": "Packagist",
    "packagist": "Packagist",
    "maven": "Maven",
    "gradle": "Maven",
    "nuget": "NuGet",
}

#: OSV database_specific severity strings -> our Severity vocabulary.
_OSV_SEVERITY = {
    "CRITICAL": Severity.BREAKING,
    "HIGH": Severity.BREAKING,
    "MODERATE": Severity.POTENTIALLY_BREAKING,
    "MEDIUM": Severity.POTENTIALLY_BREAKING,
    "LOW": Severity.POTENTIALLY_BREAKING,
}


class SecurityAdvisorySource(ChangeSource):
    kind: ClassVar[SourceKind] = SourceKind.SECURITY_ADVISORY

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any],
        http: httpx.Client,
        *,
        osv_url: str = DEFAULT_OSV_URL,
    ) -> None:
        super().__init__(provider_id, config)
        self.package: str = self._require("package")
        self.ecosystem: str = str(config.get("ecosystem") or "npm")
        # The version to check against. In production this is filled in per
        # repository scan (the lockfile-resolved version); a feed with no
        # version can still poll for "latest known advisory" as a baseline.
        self.version: str | None = config.get("version")
        self._http = http
        self._osv_url = osv_url.rstrip("/")

    @property
    def feed_key(self) -> str:
        # Version-scoped: two repos on different versions of the same package
        # must not share advisory state.
        version_suffix = f":{self.version}" if self.version else ""
        return f"security_advisory:{self.ecosystem}:{self.package}{version_suffix}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        osv_ecosystem = _ECOSYSTEM_TO_OSV.get(self.ecosystem.lower())
        if osv_ecosystem is None:
            return FeedPoll.missing(f"OSV has no ecosystem mapping for {self.ecosystem!r}")

        query: dict[str, Any] = {"package": {"name": self.package, "ecosystem": osv_ecosystem}}
        if self.version:
            query["version"] = self.version

        try:
            response = self._http.post(f"{self._osv_url}/v1/query", json=query)
        except httpx.HTTPError as exc:
            return FeedPoll.failure(f"OSV query failed: {exc}")

        if response.status_code >= 400:
            return FeedPoll.failure(f"OSV returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError:
            return FeedPoll.failure("OSV returned invalid JSON")

        vulns = payload.get("vulns") or []
        if not vulns:
            # No known advisory for this package/version. Record a baseline
            # so a later poll that *does* find one reads as a real change.
            return FeedPoll.baseline(state.last_token or "clean")

        # The newest advisory id is the cursor. If it hasn't moved, nothing
        # new was disclosed since the last poll.
        newest_id = str(vulns[0].get("id") or "")
        token = newest_id or "advisory"
        if token == state.last_token:
            return FeedPoll.unchanged(token=token)

        events: list[ChangeEvent] = []
        for raw in vulns:
            advisory = self._to_advisory(raw)
            if advisory is None:
                continue
            events.append(self._to_event(state.last_token, advisory))

        if not events:
            return FeedPoll.baseline(token)

        return FeedPoll(changed=True, new_token=token, events=events)

    # -- parsing ---------------------------------------------------------------

    def _to_advisory(self, raw: dict[str, Any]) -> AdvisoryRecord | None:
        advisory_id = str(raw.get("id") or "")
        if not advisory_id:
            return None

        fixed_version, vulnerable_range = self._range_and_fix(raw)
        severity = self._severity(raw)
        references = tuple(
            str(r.get("url"))
            for r in (raw.get("references") or [])
            if isinstance(r, dict) and r.get("url")
        )
        return AdvisoryRecord(
            advisory_id=advisory_id,
            package=self.package,
            ecosystem=self.ecosystem,
            summary=str(raw.get("summary") or raw.get("details") or advisory_id)[:500],
            severity=severity,
            vulnerable_range=vulnerable_range,
            fixed_version=fixed_version,
            references=references[:10],
            affected_symbols=self._affected_symbols(raw),
        )

    def _range_and_fix(self, raw: dict[str, Any]) -> tuple[str | None, str]:
        """Pull the fixed version and a human-readable vulnerable range out
        of OSV's ``affected[].ranges`` structure. OSV ranges are events:
        ``introduced`` / ``fixed`` pairs. We take the first fix we find."""
        fixed_version: str | None = None
        introduced: str | None = None
        for affected in raw.get("affected") or []:
            for rng in affected.get("ranges") or []:
                for event in rng.get("events") or []:
                    if "introduced" in event and introduced is None:
                        introduced = str(event["introduced"])
                    if "fixed" in event and fixed_version is None:
                        fixed_version = str(event["fixed"])
        low = introduced or "0"
        high = f" <{fixed_version}" if fixed_version else ""
        return fixed_version, f">={low}{high}"

    def _severity(self, raw: dict[str, Any]) -> str:
        db = raw.get("database_specific") or {}
        label = str(db.get("severity") or "").upper()
        return label.lower() if label else "moderate"

    def _affected_symbols(self, raw: dict[str, Any]) -> tuple[str, ...]:
        """Best-effort: some advisories list affected functions under
        ``affected[].ecosystem_specific.affected_functions`` (OSV) or in an
        imports/symbols block. Most don't -- an empty tuple is the norm and
        just means reachability can't be narrowed by symbol."""
        symbols: list[str] = []
        for affected in raw.get("affected") or []:
            eco = affected.get("ecosystem_specific") or {}
            for symbol in eco.get("affected_functions") or []:
                if isinstance(symbol, str):
                    symbols.append(symbol)
        return tuple(dict.fromkeys(symbols))

    def _to_event(self, old_token: str | None, advisory: AdvisoryRecord) -> ChangeEvent:
        mapped = _OSV_SEVERITY.get(advisory.severity.upper(), Severity.POTENTIALLY_BREAKING)
        fix_note = (
            f"Fixed in {advisory.fixed_version}."
            if advisory.has_fix
            else "No fixed version published yet."
        )
        return ChangeEvent(
            provider_id=self.provider_id,
            feed_key=self.feed_key,
            source_kind=self.kind,
            source_url=advisory.references[0] if advisory.references else None,
            old_token=old_token,
            new_token=advisory.advisory_id,
            title=f"{advisory.advisory_id}: {advisory.package} ({advisory.severity})",
            summary=f"{advisory.summary} {fix_note}",
            severity=mapped,
            advisory=advisory,
            raw={"advisory_id": advisory.advisory_id, "version": self.version},
        )
