"""Value objects for the Week 3 repo/call-site scanner.

``CallSiteKind``/``MatchConfidence`` are the load-bearing vocabulary: only
HIGH/MEDIUM confidence sites are handed to a fixer via
``RepoScanResult.to_file_usages()`` -- LOW confidence sites are reported
only, never auto-edited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from depfix.core.models import FileUsage, Usage
from depfix.severity import EvidenceKind, Severity

SCANNER_VERSION = "4.1"


class CallSiteKind(StrEnum):
    """What kind of evidence a call site is."""

    SDK_IMPORT = "sdk_import"  # import OpenAI from "openai" / require("openai")
    CLIENT_CONSTRUCTION = "client_construction"  # new OpenAI(...)
    METHOD_CALL = "method_call"  # client.chat.completions.create(...)
    WRAPPER_IMPORT = "wrapper_import"  # local module re-exporting an SDK client/binding
    RAW_HTTP = "raw_http"  # fetch()/axios/etc against a known api_base_url
    API_VERSION_PIN = "api_version_pin"  # apiVersion: "..." / Stripe-Version literal
    ANCHOR = (
        "anchor"  # legacy: no longer produced (call_site_anchors removed); kept for stored rows
    )
    BARE_SYMBOL = "bare_symbol"  # destructured import used bare, e.g. createModeration(...)


class MatchConfidence(StrEnum):
    """How much to trust a given :class:`CallSite`.

    HIGH/MEDIUM are "actionable" -- handed to a fixer. LOW is reported only.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_ACTIONABLE = frozenset({MatchConfidence.HIGH, MatchConfidence.MEDIUM})
_CONFIDENCE_RANK = {MatchConfidence.HIGH: 0, MatchConfidence.MEDIUM: 1, MatchConfidence.LOW: 2}


@dataclass(frozen=True)
class CallSite:
    """One matched location in one file.

    ``filepath`` is repo-relative POSIX (as of ``SCANNER_VERSION`` "4.0";
    earlier versions stored an absolute path) -- matching the convention
    used by :class:`depfix.apply.models.FileEdit` so a call site, the diff
    header it produces, and its persisted DB row all agree on what "this
    file" means without a throwaway-clone-directory prefix baked in.
    """

    filepath: str
    line_number: int
    column: int
    line_content: str
    kind: CallSiteKind
    confidence: MatchConfidence
    symbol: str  # the resolved dotted symbol, e.g. "openai.moderations.create"
    provider_id: str = ""
    evidence: str = ""  # short human-readable "why this matched"
    context_before: tuple[str, ...] = ()
    context_after: tuple[str, ...] = ()

    @property
    def is_actionable(self) -> bool:
        return self.confidence in _ACTIONABLE


def dedupe_call_sites(sites: list[CallSite]) -> list[CallSite]:
    """Collapse call sites at the same ``(filepath, line, column, symbol)``.

    Multiple passes (anchor grep + binding-propagation matching) can flag
    the same physical location for different reasons; keep the
    highest-confidence classification for that location instead of
    reporting it twice.
    """
    best: dict[tuple[str, int, int, str], CallSite] = {}
    for site in sites:
        key = (site.filepath, site.line_number, site.column, site.symbol)
        current = best.get(key)
        if (
            current is None
            or _CONFIDENCE_RANK[site.confidence] < _CONFIDENCE_RANK[current.confidence]
        ):
            best[key] = site
    return sorted(best.values(), key=lambda s: (s.filepath, s.line_number, s.column))


@dataclass(frozen=True)
class ScanTarget:
    """What a scan is looking for.

    ``symbols`` is a *narrowing filter* (empty = report every SDK call).
    ``feed_symbols`` is never a filter: it is the set of old APIs *and* their
    replacements named by classified feed changes, which every scanner must
    actively look for -- the old ones are what plan rewrites, the replacement
    ones are the evidence that a repo is already migrated (CURRENT).
    """

    provider_id: str
    sdk_packages: tuple[str, ...] = ()
    api_base_urls: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()  # narrows matching to these dotted symbols, if non-empty
    feed_symbols: tuple[str, ...] = ()
    #: (package name, registry name) pairs -- preserves each SDK package's
    #: ``ecosystem:`` tag from providers.yaml, which the bare ``sdk_packages``
    #: tuple historically dropped. Empty for legacy callers; scanners then
    #: treat every package as belonging to whatever ecosystem they scan.
    sdk_package_refs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DeclaredDependency:
    """Version evidence for one SDK declaration found during a scan."""

    package: str
    manifest_path: str
    declared_range: str | None
    resolved_version: str | None
    source: str


@dataclass
class RepoScanResult:
    """Everything found in one scan of one checkout."""

    repo_full_name: str
    commit_sha: str
    scanner_version: str = SCANNER_VERSION
    call_sites: list[CallSite] = field(default_factory=list)
    manifest_matches: list[str] = field(default_factory=list)
    dependencies: list[DeclaredDependency] = field(default_factory=list)
    files_scanned: int = 0
    errors: list[str] = field(default_factory=list)
    # Agent-verified old-API call sites (each: symbol, filepath, line_number,
    # line_content). Only populated by the agent scanner path; always empty
    # from the deterministic path. Persisted to learned_migrations.yaml
    # detections so the fix pipeline can generate targeted code changes.
    discovered_anchors: list[dict] = field(default_factory=list)
    # Agent-derived old→new API mappings (each: old_symbol, new_symbol, notes).
    api_mapping: list[dict] = field(default_factory=list)
    #: Repository-specific package-manager preparation performed by the scan.
    #: Kept as ``object`` to avoid coupling the scanner value model to the
    #: verification implementation; the concrete value is a
    #: ``PackageManagerPreparation``.
    package_manager_preparation: object | None = None
    severity: Severity | None = None
    severity_evidence: EvidenceKind | None = None

    @property
    def actionable_sites(self) -> list[CallSite]:
        return [s for s in self.call_sites if s.is_actionable]

    @property
    def raw_http_only(self) -> bool:
        """True when the repo calls a provider's API via raw HTTP but has no
        SDK package dependency. Useful for advising SDK adoption."""
        actionable = self.actionable_sites
        if not actionable:
            return False
        has_raw = any(s.kind == CallSiteKind.RAW_HTTP for s in actionable)
        has_sdk = any(
            s.kind
            in (
                CallSiteKind.SDK_IMPORT,
                CallSiteKind.METHOD_CALL,
                CallSiteKind.BARE_SYMBOL,
                CallSiteKind.CLIENT_CONSTRUCTION,
                CallSiteKind.WRAPPER_IMPORT,
            )
            for s in actionable
        )
        return has_raw and not has_sdk and not self.dependencies

    def to_file_usages(self, root: str | Path) -> list[FileUsage]:
        """Bridge to the Week 1 ``FileUsage`` shape so actionable sites can
        be handed to the existing ``FixGenerator``/``FixValidator``
        pipeline without a second "how a fix gets applied" code path.

        ``root`` is the checkout directory that ``filepath`` (repo-relative
        POSIX, as of ``SCANNER_VERSION`` "4.0") is resolved against -- the
        scanner itself has no notion of a checkout root, only the caller
        (:mod:`depfix.core.pipeline`) does.

        Only actionable (HIGH/MEDIUM) sites are included -- LOW confidence
        is reported to the user, never auto-edited.
        """
        root = Path(root)
        by_file: dict[str, list[CallSite]] = {}
        for site in self.actionable_sites:
            by_file.setdefault(site.filepath, []).append(site)

        file_usages: list[FileUsage] = []
        for filepath, sites in by_file.items():
            try:
                content = (root / filepath).read_text(encoding="utf-8")
            except OSError:
                continue
            usages = [
                Usage(
                    line_number=site.line_number,
                    column=site.column,
                    line_content=site.line_content,
                    context_before=list(site.context_before),
                    context_after=list(site.context_after),
                    match_text=site.symbol,
                )
                for site in sorted(sites, key=lambda s: (s.line_number, s.column))
            ]
            file_usages.append(FileUsage(filepath=filepath, usages=usages, file_content=content))
        return file_usages
