"""Shared, deterministic shortcuts for the streamlined CLI workflow.

The public CLI can ask for a provider without making callers manually stitch
together ``watch``, ``classify``, and a database lookup. This module keeps
that sequence at one seam so scan and plan select changes consistently.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from sqlalchemy import select

from depfix.catalog import (
    learned_migrations_file,
    load_learned_migrations,
    persist_learned_migrations,
)
from depfix.classify import Classifier, breaking_change_from_row, classify_pending
from depfix.classify.models import ClassifyOutcome
from depfix.classify.store import persist_classification
from depfix.config import get_settings
from depfix.core.models import BreakingChange
from depfix.providers.loader import load_providers_file
from depfix.scanners.manifest import range_allows_major
from depfix.scanners.models import DeclaredDependency
from depfix.sources.factory import SourceDeps
from depfix.sources.http import build_http_client
from depfix.sources.models import ChangeEvent, Severity, SourceKind
from depfix.sources.release_notes import fetch_release_notes_for_tag
from depfix.sources.semver import is_major_bump, parse_semver
from depfix.storage import BreakingChangeRow, ChangeEventRow, FeedState, init_schema, session_scope
from depfix.storage.schema import Provider
from depfix.watcher.runner import Watcher

logger = logging.getLogger(__name__)


def ensure_changes_detected(
    provider_id: str,
    *,
    providers_file: str,
    completer: object | None = None,
    min_confidence: float = 0.5,
) -> int:
    """Persist one provider's newly observed upstream migrations."""
    providers = load_providers_file(providers_file)
    target = [provider for provider in providers if provider.id == provider_id]
    if not target:
        logger.warning("provider %r not found in %s", provider_id, providers_file)
        return 0

    init_schema()
    with SourceDeps.from_settings() as deps:
        outcome = Watcher(deps=deps, dry_run=False).run_once(target)
    logger.info(
        "watch: %d feed(s), %d event(s) for %s",
        outcome.feeds_polled,
        len(outcome.events),
        provider_id,
    )

    with session_scope() as session:
        summary = classify_pending(
            session,
            Classifier(
                completer,  # type: ignore[arg-type]
                min_confidence=min_confidence,
            ),
            provider_id=provider_id,
        )
    logger.info(
        "classify: %d processed, %d breaking change(s) created for %s",
        summary.events_processed,
        summary.changes_created,
        provider_id,
    )

    _auto_persist_catalog(provider_id, [breaking_change_from_row(r) for r in summary.created_rows])
    imported = _import_learned_catalog(provider_id)
    if imported:
        logger.info("learned catalog: re-imported %d migration(s) for %s", imported, provider_id)

    return summary.changes_created + imported


def latest_change_for_provider(provider_id: str) -> tuple[BreakingChange | None, int | None]:
    """Return the newest classified change and row ID for ``provider_id``."""
    init_schema()
    with session_scope() as session:
        row = session.scalar(
            select(BreakingChangeRow)
            .where(BreakingChangeRow.provider_id == provider_id)
            .order_by(BreakingChangeRow.created_at.desc())
            .limit(1)
        )
        if row is None:
            return None, None
        return breaking_change_from_row(row), row.id


def all_changes_for_provider(provider_id: str) -> list[BreakingChange]:
    """Return every classified migration for one provider, newest first."""
    init_schema()
    with session_scope() as session:
        rows = session.scalars(
            select(BreakingChangeRow)
            .where(BreakingChangeRow.provider_id == provider_id)
            .order_by(BreakingChangeRow.created_at.desc())
        ).all()
        return [breaking_change_from_row(row) for row in rows]


def capture_repository_dependency_drift(
    provider_id: str,
    *,
    repository: str,
    dependencies: list[DeclaredDependency],
    package_ecosystems: Mapping[str, str | Sequence[str]],
    github_repos: Mapping[str, str] | None = None,
    completer: object | None = None,
    github_api_url: str = "https://api.github.com",
) -> int:
    """Persist drift observed *for this repository scan*.

    Feed polling deliberately treats its first observation as a baseline: it
    has no previous upstream version to compare.  A repository scan does have
    an independent old side, however -- the SDK version resolved by that
    repository's lockfile.  This function joins those two observations and
    records a normal registry ``ChangeEvent`` only when they differ.

    When ``github_repos`` maps a package name to its GitHub owner/repo (e.g.
    ``{"algoliasearch": "algolia/algoliasearch-client-javascript"}``), release
    notes for major version boundaries are fetched and attached as the event
    ``body``.  This enables the downstream :class:`NotesClassifier` to extract
    real API migrations from prose instead of producing bare version-bump
    records.

    ``completer`` is forwarded to :func:`classify_pending` so that
    body-bearing drift events run the LLM notes path.
    """
    relevant = [
        dependency for dependency in dependencies if dependency.package in package_ecosystems
    ]
    if not relevant:
        return 0

    github_repos = github_repos or {}

    init_schema()
    with session_scope() as session:
        states = {
            state.feed_key: state
            for state in session.scalars(
                select(FeedState).where(FeedState.provider_id == provider_id)
            )
        }
        created = 0
        for dependency in relevant:
            ecosystem = _ecosystem_for_dependency(
                dependency, package_ecosystems[dependency.package]
            )
            if ecosystem is None:
                continue
            feed_key, source_kind, source_url = _registry_feed_details(
                dependency.package, ecosystem
            )
            latest = states.get(feed_key)
            if latest is None or not latest.last_token:
                continue
            installed = _observed_version(dependency, latest.last_token)
            if installed is None:
                continue
            if installed == latest.last_token:
                continue

            body = ""
            body_url: str | None = None
            gh_repo = github_repos.get(dependency.package)
            if gh_repo and is_major_bump(installed, latest.last_token):
                body, body_url = _fetch_drift_release_notes(
                    gh_repo, installed, latest.last_token, github_api_url
                )

            repo_key = hashlib.sha256(repository.encode("utf-8")).hexdigest()[:12]
            event = ChangeEvent(
                provider_id=provider_id,
                feed_key=f"{feed_key}:repo:{repo_key}:installed:{installed}",
                source_kind=source_kind,
                source_url=source_url.format(package=dependency.package, version=latest.last_token),
                old_token=installed,
                new_token=latest.last_token,
                title=f"{dependency.package} {installed} → {latest.last_token}",
                summary=(
                    "Repository lockfile version differs from the latest provider feed snapshot"
                ),
                body=body,
                body_url=body_url,
                severity=(
                    Severity.BREAKING
                    if is_major_bump(installed, latest.last_token)
                    else Severity.UNKNOWN
                ),
            )
            if (
                session.scalar(
                    select(ChangeEventRow.id).where(ChangeEventRow.dedupe_key == event.dedupe_key)
                )
                is not None
            ):
                continue
            session.add(
                ChangeEventRow(
                    provider_id=event.provider_id,
                    feed_key=event.feed_key,
                    source_kind=event.source_kind.value,
                    source_url=event.source_url,
                    old_token=event.old_token,
                    new_token=event.new_token,
                    title=event.title,
                    summary=event.summary,
                    body=event.body,
                    body_url=event.body_url,
                    severity=event.severity.value,
                    dedupe_key=event.dedupe_key,
                    detected_at=event.detected_at,
                )
            )
            created += 1

        if not created:
            return 0
        drift_summary = classify_pending(
            session,
            Classifier(
                completer,  # type: ignore[arg-type]
            ),
            provider_id=provider_id,
        )
        _auto_persist_catalog(
            provider_id,
            [breaking_change_from_row(r) for r in drift_summary.created_rows],
        )
        return created


def _fetch_drift_release_notes(
    github_repo: str,
    old_version: str,
    new_version: str,
    github_api_url: str,
) -> tuple[str, str | None]:
    """Fetch release notes for the major boundary between two versions.

    Tries the new version's tag first, then each major boundary tag between
    old and new (e.g. for 4.22.0 → 5.59.0, tries tags 5.59.0 then 5.0.0).
    Returns ``(body, html_url)`` or ``("", None)`` on failure.
    """
    old_parsed = parse_semver(old_version)
    new_parsed = parse_semver(new_version)
    if old_parsed is None or new_parsed is None:
        return ("", None)

    tags_to_try = [new_version]
    for major in range(new_parsed[0], old_parsed[0], -1):
        tags_to_try.append(f"{major}.0.0")

    try:
        http = build_http_client(timeout=10.0)
    except Exception:
        return ("", None)

    bodies: list[str] = []
    first_url: str | None = None
    try:
        for tag in tags_to_try:
            notes = fetch_release_notes_for_tag(http, github_api_url, github_repo, tag)
            if notes is not None:
                bodies.append(f"## {tag}\n\n{notes.body}")
                if first_url is None:
                    first_url = notes.html_url
    finally:
        http.close()

    return ("\n\n---\n\n".join(bodies), first_url)


def _auto_persist_catalog(provider_id: str, changes: list[BreakingChange]) -> None:
    """Record newly classified migrations in the local learned store.

    Never writes the packaged canonical catalog. Failures are logged, never
    raised, so a read-only or missing directory can't block scan/plan.
    """
    relevant = [c for c in changes if c.provider_id == provider_id]
    if not relevant:
        return
    path = learned_migrations_file()
    try:
        written = persist_learned_migrations(path, relevant)
    except Exception as exc:
        logger.warning("learned catalog write failed for %s: %s", provider_id, exc)
        return
    if written:
        logger.info(
            "learned catalog: recorded %d migration(s) for %s in %s",
            written,
            provider_id,
            path,
        )


def _import_learned_catalog(provider_id: str) -> int:
    """Seed the DB with learned migrations, keeping original source/confidence.

    Each entry gets a synthetic, already-classified parent event of kind
    ``learned_catalog``. It never passes through spec_rules, so a candidate
    LLM guess cannot become SPEC_DIFF / confidence 1.0.
    """
    settings = get_settings()
    learned = load_learned_migrations(
        learned_migrations_file(settings),
        provider_id=provider_id,
        include_candidates=settings.learned_migrations_include_candidates,
    )
    if not learned:
        return 0

    imported = 0
    with session_scope() as session:
        if session.get(Provider, provider_id) is None:
            session.add(Provider(id=provider_id, name=provider_id))
            session.flush()
        keys = [m.change.dedupe_key for m in learned]
        existing = set(
            session.scalars(
                select(BreakingChangeRow.dedupe_key).where(BreakingChangeRow.dedupe_key.in_(keys))
            )
        )
        for migration in learned:
            change = migration.change
            if change.dedupe_key in existing:
                continue
            event_key = hashlib.sha256(f"learned|{change.dedupe_key}".encode()).hexdigest()
            if (
                session.scalar(
                    select(ChangeEventRow.id).where(ChangeEventRow.dedupe_key == event_key)
                )
                is not None
            ):
                continue
            event = ChangeEventRow(
                provider_id=provider_id,
                feed_key=f"learned:{change.dedupe_key[:16]}",
                source_kind=SourceKind.LEARNED_CATALOG.value,
                source_url=change.source_url,
                old_token=change.old_version or None,
                new_token=change.new_version or "unknown",
                title=f"learned: {change.old_api}",
                summary=f"{migration.status} learned migration ({change.source.value})",
                severity=Severity.UNKNOWN.value,
                dedupe_key=event_key,
            )
            session.add(event)
            session.flush()
            persist_classification(session, event, ClassifyOutcome(changes=[change]))
            existing.add(change.dedupe_key)
            imported += 1
    return imported


def _ecosystem_for_dependency(
    dependency: DeclaredDependency,
    configured: str | Sequence[str],
) -> str | None:
    """Choose the registry matching the scanned manifest, never list order.

    A provider can expose identically named npm and PyPI packages (Stripe is
    one example).  The manifest path is the authoritative ecosystem signal;
    choosing the last duplicate package from providers.yaml corrupts the
    resulting drift record and prevents the manifest writer from targeting it.
    """
    choices = (configured,) if isinstance(configured, str) else tuple(configured)
    name = Path(dependency.manifest_path).name
    preferred = (
        "npm"
        if name == "package.json"
        else "pypi"
        if name in {"pyproject.toml", "requirements.txt", "Pipfile"}
        else "rubygems"
        if name in {"Gemfile", "*.gemspec"}
        else None
    )
    if preferred in choices:
        return preferred
    return choices[0] if len(choices) == 1 else None


def _registry_feed_details(package: str, ecosystem: str) -> tuple[str, SourceKind, str]:
    """Return the canonical feed-state key for a supported registry SDK."""
    if ecosystem == "pypi":
        return (
            f"pypi:{package}",
            SourceKind.PYPI_DIST_TAG,
            "https://pypi.org/project/{package}/{version}/",
        )
    if ecosystem in ("rubygems", "gem"):
        return (
            f"rubygems:{package}",
            SourceKind.RUBYGEMS_DIST_TAG,
            "https://rubygems.org/gems/{package}/versions/{version}",
        )
    return (
        f"npm:{package}:latest",
        SourceKind.NPM_DIST_TAG,
        "https://www.npmjs.com/package/{package}/v/{version}",
    )


_EXACT_SEMVER = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def _observed_version(dependency: DeclaredDependency, feed_version: str) -> str | None:
    """Return lockfile evidence, or an exact manifest pin when available.

    An exact pin such as ``6.16.0`` is evidence of the installed side of a
    drift comparison even in a repository without a committed lockfile.
    A caret/tilde range is also sufficient when it *cannot* resolve the
    feed's newer major.  Its lower bound is not claimed to be the installed
    patch; it is durable evidence that the repository is on the old major,
    which is enough to create a human-review drift record after fresh init.
    Ranges that could already resolve the feed major remain unknown.
    """
    if dependency.resolved_version:
        return dependency.resolved_version
    declared = dependency.declared_range or ""
    if _EXACT_SEMVER.fullmatch(declared):
        return declared
    feed = parse_semver(feed_version)
    lower_bound = re.fullmatch(r"[~^]?(v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", declared.strip())
    if feed is None or lower_bound is None:
        return None
    if range_allows_major(declared, feed[0]) is not False:
        return None
    return lower_bound.group(1)
