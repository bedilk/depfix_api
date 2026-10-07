"""Compose one upgrade into one verified, committable edit set."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from dataclasses import dataclass

from depfix.apply.models import EditOrigin, EditVerdict, FileEdit
from depfix.apply.workspace import WorkspaceEditor
from depfix.clone import Checkout
from depfix.config import Settings
from depfix.core.upgrade import StepKind, UpgradePlan, UpgradeStep
from depfix.repoconfig.models import RepoConfig
from depfix.scanners.models import RepoScanResult
from depfix.scanners.removals import narrow_to_symbols
from depfix.scanners.repo import symbols_for_change

logger = logging.getLogger(__name__)


@dataclass
class UpgradeResult:
    plan: UpgradePlan
    edits: list[FileEdit]
    steps: list[UpgradeStep]
    committable: list[FileEdit]
    confidence: object  # ConfidenceTier or equivalent
    confidence_reason: str = ""
    verification: object | None = None
    notes: list[str] = dataclasses.field(default_factory=list)
    total_cost: float = 0.0
    total_tokens: int = 0
    rejected_reason: str = ""


def run_upgrade(
    *,
    checkout: Checkout,
    scan_result: RepoScanResult,
    plan: UpgradePlan,
    settings: Settings,
    repo_config: RepoConfig,
    fixer_factory,
    ledger=None,
) -> UpgradeResult:
    from depfix.core.fix_service import build_fix_pipeline
    from depfix.verify.confidence import ConfidenceTier

    editor = WorkspaceEditor(checkout)
    edits: list[FileEdit] = []
    steps: list[UpgradeStep] = []
    notes = list(plan.notes)
    cost = tokens = 0.0

    # 1. Source migrations
    for change in plan.migrations:
        symbols = symbols_for_change(plan.provider_id, change)
        scoped = narrow_to_symbols(scan_result, symbols)
        if not scoped.actionable_sites:
            notes.append(f"{change.old_api}: no actionable site after narrowing")
            continue
        fixer = fixer_factory()
        pipeline = build_fix_pipeline(
            settings=settings,
            repo_config=repo_config,
            fixer=fixer,
            editor=editor,
            max_retries=settings.retry_max_rounds,
            ecosystem=plan.ecosystem,
        )
        # Disable per-migration verification; we verify the whole tree once below
        if hasattr(pipeline, "_verifier"):
            pipeline._verifier = None
        if hasattr(pipeline, "_retry_loop"):
            pipeline._retry_loop = None
        result = pipeline.run(scoped, change, editor)
        cost += result.total_cost
        tokens += result.total_tokens
        if ledger is not None:
            from depfix.obs.cost import CostStage

            ledger.record(
                CostStage.FIX_GENERATION,
                result.total_cost,
                input_tokens=0,
                output_tokens=result.total_tokens,
            )
        notes.extend(result.codemod_notes)
        member = [e for e in result.edits if getattr(e, "is_on_disk", bool(e.fixed_content))]
        edits.extend(member)
        if member:
            origins = {e.origin for e in member}
            codemod_id = next(
                (e.codemod_id for e in member if getattr(e, "codemod_id", None)), None
            )
            steps.append(
                UpgradeStep(
                    kind=StepKind.SOURCE,
                    dedupe_key=change.dedupe_key,
                    summary=f"{change.old_api} → {getattr(change, 'replacement', change.new_api)}",
                    relpaths=tuple(e.relpath for e in member),
                    origin=(
                        f"codemod:{codemod_id}"
                        if origins == {EditOrigin.CODEMOD} and codemod_id
                        else f"llm:{getattr(fixer, 'model', 'unknown')}"
                    ),
                )
            )
        else:
            notes.append(f"{change.old_api}: produced no on-disk edit")

    if plan.has_source_work and not any(s.kind == StepKind.SOURCE for s in steps):
        return UpgradeResult(
            plan,
            [],
            [],
            [],
            ConfidenceTier.NONE,
            notes=notes,
            total_cost=cost,
            total_tokens=int(tokens),
            rejected_reason="no source migration produced an edit",
        )

    # 2. One manifest bump to exact target version
    from depfix.codemods.lockfile import build_upgrade_manifest_bumps

    manifest_plan = build_upgrade_manifest_bumps(checkout.path, plan.package, plan.target_version)
    notes.extend(manifest_plan.declines)

    for bump in manifest_plan.bumps:
        edit = editor.write_fix(
            bump.manifest_relpath,
            bump.fixed_content,
            confidence=1.0,
            origin=EditOrigin.CODEMOD,
            codemod_id="manifest.upgrade",
        )
        if not edit.changed:
            notes.append(f"manifest bump not written: {bump.manifest_relpath}")
            continue
        edits.append(edit)
        steps.append(
            UpgradeStep(
                kind=StepKind.MANIFEST,
                origin="deterministic",
                summary=f"{bump.package} {bump.old_range} → {bump.new_range}",
                relpaths=(bump.manifest_relpath,),
            )
        )

    if not manifest_plan.bumps:
        return UpgradeResult(
            plan,
            [],
            [],
            [],
            ConfidenceTier.NONE,
            notes=notes,
            total_cost=cost,
            total_tokens=int(tokens),
            rejected_reason=(f"no manifest declares {plan.package} in a form depfix can rewrite"),
        )

    # 3. Lockfile refresh once
    if getattr(settings, "lockfile_regenerate", True) and checkout.is_temporary:
        lock_edits, message = _refresh_lockfile(checkout, editor, plan, settings)
        notes.append(message)
        edits.extend(lock_edits)
        if lock_edits:
            steps.append(
                UpgradeStep(
                    kind=StepKind.LOCKFILE,
                    origin="deterministic",
                    summary=message,
                    relpaths=tuple(e.relpath for e in lock_edits),
                )
            )

    # 4. One verification of the whole tree
    verification = None
    confidence, reason = ConfidenceTier.LOW, "only a syntax check ran"
    if repo_config.verify and settings.verify_enabled:
        from depfix.verify import Verifier

        verifier = Verifier(
            editor,
            install_timeout=settings.verify_install_timeout,
            test_timeout=settings.verify_test_timeout,
            ignore_scripts=settings.verify_ignore_scripts,
            max_output_bytes=settings.verify_max_output_bytes,
            typecheck_enabled=settings.verify_typecheck_enabled,
            typecheck_timeout=settings.verify_typecheck_timeout,
            ecosystem=plan.ecosystem,
            coverage_enabled=getattr(settings, "verify_coverage_enabled", False),
            contract_enabled=getattr(settings, "verify_contract_enabled", False),
            flake_retries=getattr(settings, "verify_flake_retries", 1),
            static_check_enabled=getattr(settings, "verify_static_check_enabled", False),
            static_check_timeout=getattr(settings, "verify_static_check_timeout", 30),
            selective_tests=False,
            frozen_install=False,
        )
        carrier = plan.drift_change or plan.migrations[0]
        verification = verifier.verify(edits, change=carrier)
        edits = list(verification.edits)
        confidence, reason = verification.tier, verification.tier_reason

        # 5. All-or-nothing rejection
        rejected = [e for e in edits if e.verdict in (EditVerdict.REVERTED, EditVerdict.SUSPECT)]
        if rejected:
            for edit in edits:
                if getattr(edit, "is_on_disk", False):
                    with contextlib.suppress(Exception):
                        editor.revert(edit.relpath)
            return UpgradeResult(
                plan,
                edits,
                steps,
                [],
                confidence,
                reason,
                verification,
                notes,
                cost,
                int(tokens),
                rejected_reason=(
                    "upgrade rejected as a unit: "
                    + "; ".join(f"{e.relpath} ({e.verdict.value})" for e in rejected[:5])
                ),
            )

    floor = ConfidenceTier(settings.apply_min_confidence)
    if not confidence.at_least(floor):
        return UpgradeResult(
            plan,
            edits,
            steps,
            [],
            confidence,
            reason,
            verification,
            notes,
            cost,
            int(tokens),
            rejected_reason=f"confidence {confidence.value} below "
            f"apply_min_confidence {floor.value}",
        )

    committable = [e for e in edits if e.verdict in (EditVerdict.KEPT, EditVerdict.APPLIED)]
    return UpgradeResult(
        plan, edits, steps, committable, confidence, reason, verification, notes, cost, int(tokens)
    )


def _refresh_lockfile(checkout, editor, plan: UpgradePlan, settings) -> tuple[list, str]:
    """Refresh lockfile after manifest bump. Returns (edits, message)."""
    from depfix.codemods.lockfile import lockfile_paths, try_refresh_javascript_lockfile

    if plan.ecosystem == "javascript":
        ok, message = try_refresh_javascript_lockfile(
            checkout.path,
            timeout=settings.verify_install_timeout,
            max_output_bytes=settings.verify_max_output_bytes,
        )
        if not ok:
            return [], f"lockfile refresh failed: {message}"
        lock_edits = []
        for lp in lockfile_paths(checkout.path):
            relpath = lp.relative_to(checkout.path).as_posix()
            try:
                new_content = lp.read_text(encoding="utf-8")
                lock_edits.append(
                    editor.write_fix(
                        relpath,
                        new_content,
                        confidence=1.0,
                        origin=EditOrigin.CODEMOD,
                        codemod_id="lockfile.upgrade",
                    )
                )
            except OSError:
                pass
        return lock_edits, message
    return [], f"lockfile refresh not implemented for {plan.ecosystem}"
