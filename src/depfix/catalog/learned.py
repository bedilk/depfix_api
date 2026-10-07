"""Persist repository-observed, feed-compared migration records.

``scan`` records what a repository calls and the resolved version of its SDK.
Feed-proven changes then mark each observation ``actionable`` when either the
SDK is behind the feed version or the observed API is documented as replaced;
otherwise it is ``non_actionable``. Provider/feed configuration above the
managed section remains review-owned and is never rewritten.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from depfix.core.models import BreakingChange
from depfix.core.serde import breaking_change_from_dict, breaking_change_to_dict
from depfix.sources.semver import parse_semver

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass

_MANAGED_MARKER = "# Depfix-managed repository migration observations.\n"
_MANAGED_KEY = "repository_migrations"


def record_detected_usage(
    path: str | Path,
    *,
    repo_full_name: str,
    commit_sha: str,
    provider_id: str,
    detected_symbols: list[str],
    classified_changes: list[BreakingChange],
    sdk_versions: Mapping[str, str] | None = None,
) -> int:
    """Record the latest resolved API usage snapshot for one repository.

    Every resolved call-site symbol becomes one record. Any version distance
    counts as behind, but this is an observation decision only: plan/apply
    still require a classified, feed-proven change before an edit is possible.
    Entries for one ``(repository, provider)`` are replaced as a unit so the
    managed catalog reflects one repository commit exactly.
    """
    symbols = sorted(
        {symbol for symbol in detected_symbols if symbol and not _is_generic_scanner_symbol(symbol)}
    )
    sdk_package, sdk_version = _sdk_metadata(sdk_versions or {})
    latest_version = _feed_latest_version(classified_changes, sdk_package)
    version_behind = _is_behind(sdk_version, latest_version)
    entries = [
        _detected_entry(
            symbol,
            repo_full_name=repo_full_name,
            commit_sha=commit_sha,
            provider_id=provider_id,
            changes=classified_changes,
            sdk_package=sdk_package,
            sdk_version=sdk_version,
            latest_version=latest_version,
            version_behind=version_behind,
        )
        for symbol in symbols
    ]
    catalog_path = Path(path)
    original = catalog_path.read_text(encoding="utf-8") if catalog_path.is_file() else ""
    prefix, marker, managed = original.partition(_MANAGED_MARKER)
    loaded_entries = _load_entries(managed) if marker else []
    existing = _normalize_existing_entries(loaded_entries)
    normalized_legacy_entries = existing != loaded_entries
    remaining = [
        entry
        for entry in existing
        if not (
            entry.get("provider_id") == provider_id and entry.get("repository") == repo_full_name
        )
    ]
    if not symbols and len(remaining) == len(existing) and not normalized_legacy_entries:
        return 0
    updated_entries = [*remaining, *entries]
    if updated_entries:
        document = yaml.safe_dump(
            {_MANAGED_KEY: updated_entries}, sort_keys=False, allow_unicode=True
        )
        head = f"{prefix.rstrip()}\n\n" if prefix.strip() else ""
        updated = f"{head}{_MANAGED_MARKER}{document}"
    else:
        updated = f"{prefix.rstrip()}\n" if prefix.strip() else ""
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = catalog_path.with_suffix(f"{catalog_path.suffix}.tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, catalog_path)
    return len(entries)


def _detected_entry(
    symbol: str,
    *,
    repo_full_name: str,
    commit_sha: str,
    provider_id: str,
    changes: list[BreakingChange],
    sdk_package: str,
    sdk_version: str,
    latest_version: str,
    version_behind: bool,
) -> dict[str, str]:
    matched = _match_symbol(symbol, changes, provider_id)
    uses_old_api = matched is not None and matched[1] == "old"
    change = matched[0] if matched is not None else None
    base = {
        "provider_id": provider_id,
        "repository": repo_full_name,
        "commit_sha": commit_sha,
        "current_api": symbol,
        "sdk_package": sdk_package,
        "current_version": sdk_version,
        "latest_version": latest_version,
        "status": "actionable" if version_behind or uses_old_api else "non_actionable",
        "reason": _reason(version_behind, uses_old_api, sdk_version, latest_version),
    }
    if change is None:
        return {
            **base,
            "old_api": "",
            "new_api": "",
            "source_url": "",
            "evidence": "",
        }
    return {
        **base,
        "old_api": change.old_api,
        "new_api": change.new_api,
        "source_url": change.source_url or "",
        "evidence": change.evidence or "",
    }


def _sdk_metadata(sdk_versions: Mapping[str, str]) -> tuple[str, str]:
    """Select the first resolved provider SDK from caller-filtered versions."""
    for package, version in sdk_versions.items():
        if version:
            return package, version
    return "", ""


def _feed_latest_version(changes: list[BreakingChange], package: str) -> str:
    """Return the highest feed-observed semver for the scanned SDK package."""
    latest = ""
    for change in changes:
        if package and change.package and change.package != package:
            continue
        if not change.new_version:
            continue
        if not latest or _is_behind(latest, change.new_version):
            latest = change.new_version
    return latest


def _is_behind(installed: str, latest: str) -> bool:
    installed_version, latest_feed_version = parse_semver(installed), parse_semver(latest)
    return bool(
        installed_version is not None
        and latest_feed_version is not None
        and installed_version < latest_feed_version
    )


def _reason(version_behind: bool, uses_old_api: bool, installed: str, latest: str) -> str:
    if version_behind and uses_old_api:
        return f"uses an old API and SDK is behind ({installed} < {latest})"
    if uses_old_api:
        return "uses an old API the feed reports as replaced"
    if version_behind:
        return f"installed SDK {installed} is behind the feed's latest {latest}"
    return "SDK version and call sites match the feed's current state"


def _match_symbol(
    symbol: str, changes: list[BreakingChange], provider_id: str
) -> tuple[BreakingChange, str] | None:
    """Return the documented migration and whether *symbol* is old or new."""
    from depfix.scanners.repo import symbols_for_api, symbols_for_change

    for change in changes:
        if change.provider_id and change.provider_id != provider_id:
            continue
        old_symbols = symbols_for_change(provider_id, change)
        if _matches_any(symbol, old_symbols):
            return change, "old"
        if change.new_api and _matches_any(symbol, symbols_for_api(provider_id, change.new_api)):
            return change, "new"
    return None


def _matches_any(symbol: str, candidates: tuple[str, ...]) -> bool:
    return any(
        symbol == candidate or symbol.startswith(f"{candidate}.") for candidate in candidates
    )


def _is_generic_scanner_symbol(symbol: str) -> bool:
    return symbol.endswith((".<anchor>", ".<api_version_pin>", ".<raw_http>"))


def _normalize_existing_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Conservatively migrate observations from the immediately prior schema."""
    normalized: list[dict[str, str]] = []
    for entry in entries:
        if "current_version" in entry:
            normalized.append(entry)
            continue
        normalized.append(
            {
                **entry,
                "current_version": str(entry.get("sdk_version") or ""),
                "latest_version": "",
                "status": "non_actionable",
                "reason": "rescan required to compare this usage with current feeds",
            }
        )
    return normalized


def _load_entries(text: str) -> list[dict[str, str]]:
    data = yaml.safe_load(text) or {}
    raw_entries = data.get(_MANAGED_KEY, []) if isinstance(data, dict) else []
    return [entry for entry in raw_entries if isinstance(entry, dict)]


# ---------------------------------------------------------------------------
# Learned migrations: a local store that keeps provenance
# ---------------------------------------------------------------------------
#
# Each entry is the classifier's own BreakingChange, stored unchanged and
# keyed by its dedupe_key. It is never turned into a fixture_change or
# spec_changes entry. That conversion is what laundered LLM output into
# SPEC_DIFF / confidence 1.0 and silently dropped param/field kinds.

LEARNED_FORMAT_VERSION = 1
STATUS_CANDIDATE = "candidate"
STATUS_CONFIRMED = "confirmed"

#: Repository- or version-scoped facts, not reusable migrations.
_SKIP_KINDS = frozenset({"dependency_version_bump", "security_advisory", "unknown"})
#: Sources whose output is deterministic evidence, confirmed on arrival.
_DETERMINISTIC_SOURCES = frozenset({"spec_diff", "registry", "security", "manual"})

_LEARNED_HEADER = (
    "# Depfix-managed learned migrations. Local state -- do not commit.\n"
    "# migrations: reusable changes. 'candidate' = unverified (e.g. LLM-derived),\n"
    "#   'confirmed' = deterministic or verified evidence. Re-imported into the DB.\n"
    "# detections: per-repository drift found by scan/plan (version drift and API\n"
    "#   call-site drift), with the plan artifact / PR each one led to. Never re-imported.\n"
)


@dataclass(frozen=True)
class LearnedMigration:
    change: BreakingChange
    status: str
    confirmed_by: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status == STATUS_CONFIRMED


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _empty_document() -> dict:
    return {"version": LEARNED_FORMAT_VERSION, "migrations": {}, "detections": {}}


def _read_document(path: Path) -> dict:
    """The whole store, normalized. Never raises."""
    if not path.is_file():
        return _empty_document()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("could not read learned migrations at %s: %s", path, exc)
        return _empty_document()
    if not isinstance(data, dict):
        return _empty_document()
    migrations = data.get("migrations")
    detections = data.get("detections")
    return {
        "version": LEARNED_FORMAT_VERSION,
        "migrations": {
            k: v for k, v in migrations.items() if isinstance(k, str) and isinstance(v, dict)
        }
        if isinstance(migrations, dict)
        else {},
        "detections": {
            k: [e for e in v if isinstance(e, dict)]
            for k, v in detections.items()
            if isinstance(k, str) and isinstance(v, list)
        }
        if isinstance(detections, dict)
        else {},
    }


def _write_document(path: Path, document: dict) -> bool:
    """Atomically write the store. Returns False (never raises) on I/O failure."""
    payload = {
        "version": LEARNED_FORMAT_VERSION,
        "migrations": dict(sorted(document.get("migrations", {}).items())),
        "detections": dict(sorted(document.get("detections", {}).items())),
    }
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            _LEARNED_HEADER + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not write learned migrations to %s: %s", path, exc)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    return True


def _read_learned(path: Path) -> dict[str, dict]:
    return _read_document(path)["migrations"]


def _write_learned(path: Path, entries: dict[str, dict]) -> bool:
    """Replace only the migrations section; detections are preserved."""
    document = _read_document(path)
    document["migrations"] = entries
    return _write_document(path, document)


def persist_learned_migrations(path: str | Path, changes: Iterable[BreakingChange]) -> int:
    """Record classified migrations. Returns how many entries were added or updated.

    Deterministic sources arrive ``confirmed``; everything else (release notes,
    LLM synthesis) arrives as a ``candidate``. A confirmed entry is never
    downgraded; a candidate is replaced by a deterministic or higher-confidence
    observation of the same change.
    """
    path = Path(path)
    entries = _read_learned(path)
    now = _utcnow_iso()
    touched = 0
    for change in changes:
        if change.kind.value in _SKIP_KINDS or not change.old_api:
            continue
        key = change.dedupe_key
        deterministic = change.source.value in _DETERMINISTIC_SOURCES
        payload = breaking_change_to_dict(change)
        existing = entries.get(key)
        if existing is None:
            entries[key] = {
                "status": STATUS_CONFIRMED if deterministic else STATUS_CANDIDATE,
                "confirmed_by": f"deterministic:{change.source.value}" if deterministic else "",
                "first_seen": now,
                "confirmed_at": now if deterministic else None,
                "change": payload,
            }
            touched += 1
            continue
        if existing.get("status") == STATUS_CONFIRMED:
            continue
        if deterministic:
            existing.update(
                status=STATUS_CONFIRMED,
                confirmed_by=f"deterministic:{change.source.value}",
                confirmed_at=now,
                change=payload,
            )
            touched += 1
        elif change.confidence > float((existing.get("change") or {}).get("confidence") or 0.0):
            existing["change"] = payload
            touched += 1
    if touched and not _write_learned(path, entries):
        return 0
    return touched


def load_learned_migrations(
    path: str | Path, *, provider_id: str | None = None, include_candidates: bool = True
) -> list[LearnedMigration]:
    """Read learned entries back as BreakingChange objects, provenance intact."""
    out: list[LearnedMigration] = []
    for key, entry in _read_learned(Path(path)).items():
        status = str(entry.get("status") or STATUS_CANDIDATE)
        if status != STATUS_CONFIRMED and not include_candidates:
            continue
        try:
            change = breaking_change_from_dict(entry.get("change") or {})
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("skipping malformed learned migration %s: %s", key[:12], exc)
            continue
        if provider_id is not None and change.provider_id != provider_id:
            continue
        out.append(LearnedMigration(change, status, str(entry.get("confirmed_by") or "")))
    return out


def promote_learned_migration(path: str | Path, dedupe_key: str, confirmed_by: str) -> bool:
    """Mark a candidate confirmed after real evidence (verified fix, merged PR).

    Returns True only when an entry changed state. Never raises on I/O.
    """
    path = Path(path)
    entries = _read_learned(path)
    entry = entries.get(dedupe_key)
    if entry is None or entry.get("status") == STATUS_CONFIRMED:
        return False
    entry.update(status=STATUS_CONFIRMED, confirmed_by=confirmed_by, confirmed_at=_utcnow_iso())
    return _write_learned(path, entries)
