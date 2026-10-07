"""Serializable, immutable output of a verified ``depfix plan`` run.

Two properties make the plan/apply split usable rather than ceremonial:

**Self-describing.** The artifact carries the repo, the exact base commit
it was verified against, *and the full* :class:`~depfix.core.models.BreakingChange`
it migrates. So ``depfix apply`` needs nothing else -- no ``--from-event``,
no ``--provider``, no ``--providers-file``. Re-supplying those was not just
tedious, it was a correctness hazard: an ``--from-event`` that didn't match
the artifact silently applied a reviewed diff under the wrong change's
branch name and PR body.

**Discoverable.** A plan is written to a deterministic path derived from the
repo name (:func:`plan_path_for`), so ``apply`` can find the latest plan for
a repo with no copy-pasted filename. Explicit ``--plan-file`` still works
and is what CI should use.

An artifact is immutable and content-addressed (:attr:`PlanArtifact.digest`),
and the digest goes into the PR body footer -- so a PR can always be traced
back to the exact reviewed plan that produced it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from depfix.core.models import BreakingChange
from depfix.core.pipeline import FixPipelineResult
from depfix.core.serde import breaking_change_from_dict, breaking_change_to_dict
from depfix.scanners.removals import RemovalNotice
from depfix.verify.confidence import ConfidenceTier

#: Bumped from 1 when the embedded ``change`` became mandatory. A v1
#: artifact has no way to tell ``apply`` which change it is for, so it is
#: rejected with an actionable message rather than half-read.
ARTIFACT_VERSION = 5

DEFAULT_PLAN_DIR = ".depfix/plans"
logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def plan_slug(repo_full_name: str) -> str:
    """Filesystem-safe, reversible-by-eye slug for ``owner/name``."""
    return repo_full_name.replace("/", "__")


def plan_path_for(
    repo_full_name: str, change_dedupe_key: str, directory: str | Path = DEFAULT_PLAN_DIR
) -> Path:
    """Return the canonical path for one reviewed ``(repo, change)`` plan."""
    return Path(directory) / f"{plan_slug(repo_full_name)}__{change_dedupe_key[:12]}.json"


def plan_archive_path_for(
    change: BreakingChange, created_at_iso: str = "", directory: str | Path = DEFAULT_PLAN_DIR
) -> Path:
    """Provider/date archive path, kept alongside the canonical per-repo file.

    ``.depfix/plans/archive/<provider>/<YYYY-MM-DD>/<slug>__<change>.json`` --
    a durable, human-browsable record, distinct from the canonical
    per-(repo, change) file that ``apply`` reads.
    """
    date = created_at_iso[:10] if created_at_iso else datetime.now(tz=UTC).date().isoformat()
    provider = change.provider_id or "unknown"
    return Path(directory) / "archive" / provider / date


@dataclass(frozen=True)
class PlannedEdit:
    relpath: str
    original_content: str
    fixed_content: str
    diff: str
    confidence: float
    usages_fixed: int


@dataclass(frozen=True)
class PlanArtifact:
    repo_full_name: str
    base_sha: str
    change: BreakingChange
    edits: tuple[PlannedEdit, ...]
    #: The :class:`~depfix.verify.confidence.ConfidenceTier` the plan was
    #: verified to. ``apply`` re-reports it, and it lands in the PR body --
    #: a plan reviewed as ``low`` must not present itself as anything else.
    confidence: str = ConfidenceTier.NONE.value
    verified_by: str = ""
    impact_summary: str = ""
    created_at: str = field(default_factory=_utcnow_iso)
    removals: tuple = ()
    upgrade_package: str = ""
    upgrade_from: str = ""
    upgrade_to: str = ""
    steps: tuple = ()  # tuple[UpgradeStep, ...]
    member_dedupe_keys: tuple = ()
    notes: tuple = ()
    # "patch-minor", "major", "multi-major", or "" (not a drift artifact)
    drift_risk: str = ""
    scan_source: str = ""

    @property
    def is_upgrade(self) -> bool:
        return bool(self.upgrade_to)

    @property
    def is_drift(self) -> bool:
        return bool(self.drift_risk)

    @property
    def change_dedupe_key(self) -> str:
        return self.change.dedupe_key

    @property
    def payload(self) -> dict:
        return {
            "version": ARTIFACT_VERSION,
            "repo_full_name": self.repo_full_name,
            "base_sha": self.base_sha,
            "change_dedupe_key": self.change_dedupe_key,
            "change": breaking_change_to_dict(self.change),
            "confidence": self.confidence,
            "verified_by": self.verified_by,
            "impact_summary": self.impact_summary,
            "created_at": self.created_at,
            "edits": [e.__dict__ for e in self.edits],
            "removals": [r.to_dict() for r in self.removals],
            "upgrade_package": self.upgrade_package,
            "upgrade_from": self.upgrade_from,
            "upgrade_to": self.upgrade_to,
            "steps": [s.to_dict() for s in self.steps],
            "member_dedupe_keys": list(self.member_dedupe_keys),
            "notes": list(self.notes),
            "drift_risk": self.drift_risk,
            "scan_source": self.scan_source,
        }

    @property
    def digest(self) -> str:
        """``sha256:...`` over the canonical payload, excluding
        ``created_at`` -- two plans with identical content should have
        identical digests regardless of when they were produced, so a
        re-plan that changed nothing doesn't look like a new plan in a PR
        footer."""
        payload = {k: v for k, v in self.payload.items() if k != "created_at"}
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    @classmethod
    def from_result(
        cls,
        repo: str,
        base_sha: str,
        result: FixPipelineResult,
        *,
        impact_summary: str = "",
        edits: list | None = None,
        drift_risk: str = "",
        scan_source: str = "",
    ) -> PlanArtifact:
        """``edits`` should be the fix service's committable set. The legacy
        default (KEPT only) silently dropped SUSPECT/APPLIED drift edits,
        producing an artifact ``apply`` could replay into nothing."""
        source = (
            edits if edits is not None else [e for e in result.edits if e.verdict.value == "kept"]
        )
        return cls(
            repo_full_name=repo,
            base_sha=base_sha,
            change=result.breaking_change,
            edits=tuple(
                PlannedEdit(
                    e.relpath,
                    e.original_content,
                    e.fixed_content,
                    e.diff,
                    e.confidence,
                    e.usages_fixed,
                )
                for e in source
            ),
            confidence=result.confidence.value,
            verified_by=result.confidence_reason,
            impact_summary=impact_summary,
            removals=(),
            notes=tuple(result.codemod_notes),
            drift_risk=drift_risk,
            scan_source=scan_source,
        )

    @classmethod
    def from_drift(
        cls,
        repo: str,
        base_sha: str,
        change: BreakingChange,
        *,
        drift_risk: str,
    ) -> PlanArtifact:
        """Artifact for a version-drift-only change: no code edits, just a record
        that the installed version is behind and how risky that gap is."""
        return cls(
            repo_full_name=repo,
            base_sha=base_sha,
            change=change,
            edits=(),
            confidence="none",
            drift_risk=drift_risk,
        )

    @classmethod
    def from_upgrade(
        cls, repo: str, base_sha: str, result, *, scan_source: str = ""
    ) -> PlanArtifact:
        plan = result.plan
        carrier = plan.drift_change or plan.migrations[0]
        return cls(
            repo_full_name=repo,
            base_sha=base_sha,
            change=carrier,
            edits=tuple(
                PlannedEdit(
                    e.relpath,
                    e.original_content,
                    e.fixed_content,
                    e.diff,
                    e.confidence,
                    e.usages_fixed,
                )
                for e in result.committable
            ),
            confidence=result.confidence.value,
            verified_by=result.confidence_reason,
            removals=tuple(plan.removals),
            upgrade_package=plan.package,
            upgrade_from=plan.installed_version or "",
            upgrade_to=plan.target_version,
            steps=tuple(result.steps),
            member_dedupe_keys=plan.member_dedupe_keys,
            notes=tuple(result.notes),
            scan_source=scan_source,
        )

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.payload, indent=2) + "\n", encoding="utf-8")
        return path

    def write_for_repo(self, directory: str | Path = DEFAULT_PLAN_DIR) -> Path:
        return self.write(plan_path_for(self.repo_full_name, self.change_dedupe_key, directory))

    def write_to_archive(self, directory: str | Path = DEFAULT_PLAN_DIR) -> Path:
        """Write an immutable dated, provider-scoped copy for the record."""
        archive_dir = plan_archive_path_for(self.change, self.created_at, directory)
        path = archive_dir / f"{plan_slug(self.repo_full_name)}__{self.change_dedupe_key[:12]}.json"
        return self.write(path)

    @classmethod
    def plans_for_repo(
        cls, repo_full_name: str, directory: str | Path = DEFAULT_PLAN_DIR
    ) -> list[PlanArtifact]:
        """Return readable plans for a repo, newest first."""
        directory = Path(directory)
        if not directory.is_dir():
            return []
        plans: list[PlanArtifact] = []
        prefix = f"{plan_slug(repo_full_name)}__"
        for path in directory.glob(f"{prefix}*.json"):
            try:
                plans.append(cls.read(path))
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
                logger.warning("skipping unreadable plan artifact at %s", path)
        return sorted(plans, key=lambda plan: plan.created_at, reverse=True)

    @classmethod
    def read(cls, path: str | Path) -> PlanArtifact:
        from depfix.core.upgrade import UpgradeStep

        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        version = raw.get("version")
        if version not in (2, 3, 4, ARTIFACT_VERSION):
            raise ValueError(
                f"unsupported plan artifact version {version!r} (this depfix writes "
                f"version {ARTIFACT_VERSION}); re-run `depfix plan` to regenerate it"
            )
        return cls(
            repo_full_name=raw["repo_full_name"],
            base_sha=raw["base_sha"],
            change=breaking_change_from_dict(raw["change"]),
            edits=tuple(PlannedEdit(**e) for e in raw["edits"]),
            confidence=raw.get("confidence", ConfidenceTier.NONE.value),
            verified_by=raw.get("verified_by", ""),
            impact_summary=raw.get("impact_summary", ""),
            created_at=raw.get("created_at", ""),
            removals=tuple(RemovalNotice.from_dict(r) for r in raw.get("removals", [])),
            steps=tuple(UpgradeStep.from_dict(s) for s in raw.get("steps", [])),
            member_dedupe_keys=tuple(raw.get("member_dedupe_keys", [])),
            notes=tuple(raw.get("notes", [])),
            upgrade_package=raw.get("upgrade_package", ""),
            upgrade_from=raw.get("upgrade_from", ""),
            upgrade_to=raw.get("upgrade_to", ""),
            drift_risk=raw.get("drift_risk", ""),
            scan_source=raw.get("scan_source", ""),
        )

    @classmethod
    def latest_for_repo(
        cls, repo_full_name: str, directory: str | Path = DEFAULT_PLAN_DIR
    ) -> PlanArtifact | None:
        """The plan most recently written for ``repo_full_name``, or ``None``
        if there isn't one. Never raises for a missing file -- "no plan yet"
        is a normal state that ``apply`` reports as an actionable message."""
        plans = cls.plans_for_repo(repo_full_name, directory)
        return plans[0] if plans else None

    def apply_command(self) -> str:
        """The exact next command to run -- printed at the end of ``plan``,
        because the operator should never have to assemble it by hand."""
        return f"depfix apply {self.repo_full_name}"
