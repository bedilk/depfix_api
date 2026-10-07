"""Shared assembly and commit policy for dependency fixes.

The interactive CLI and fleet orchestrator must make the same decision about
unverified edits.  Keeping that rule here prevents the two entry points from
quietly drifting apart.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

from depfix.agent import FixAgent, make_fix_strategy
from depfix.agent.tools import FixToolset
from depfix.apply.models import EditOrigin, FileEdit
from depfix.apply.workspace import WorkspaceEditor
from depfix.classify.llm import BedrockCompleter, GeminiCompleter, LLMCompleter, OllamaCompleter
from depfix.clone import Checkout
from depfix.codemods import CodemodRegistry
from depfix.config import Settings
from depfix.core.models import BreakingChange, ChangeKind, is_method_removal
from depfix.core.pipeline import FixPipeline, FixPipelineResult
from depfix.fixers.bedrock import BedrockFixGenerator
from depfix.fixers.gemini import FixGenerator
from depfix.fixers.ollama import OllamaFixGenerator
from depfix.gh.pr import committable_edits
from depfix.obs.cost import CostLedger, CostStage
from depfix.repoconfig.models import RepoConfig
from depfix.retry.loop import RetryLoop
from depfix.scanners import CallSiteScanner
from depfix.scanners.models import RepoScanResult
from depfix.validators import validator_for_ecosystem
from depfix.verify import Characterizer, Verifier
from depfix.verify.confidence import ConfidenceTier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FixServiceResult:
    """A pipeline result plus the edits that may be committed."""

    pipeline_result: FixPipelineResult
    committable: list[FileEdit]
    verify_required: bool
    escalated: bool = False
    cross_major_drift: bool = False


def build_fix_pipeline(
    *,
    settings: Settings,
    repo_config: RepoConfig,
    fixer: BedrockFixGenerator | FixGenerator | OllamaFixGenerator | FixAgent,
    editor: WorkspaceEditor,
    max_retries: int | None = None,
    toolset: FixToolset | None = None,
    codemods: CodemodRegistry | None = None,
    characterizer: Characterizer | None = None,
    ecosystem: str = "javascript",
) -> FixPipeline:
    """Build one fix pipeline under the common verification policy."""
    from depfix.codemods import CodemodRegistry as _Registry

    verify_required = repo_config.verify and settings.verify_enabled
    validator = validator_for_ecosystem(
        ecosystem,
        use_ast_validation=settings.fix_validate_syntax,
        checkout_root=editor.checkout.path,
    )
    effective_codemods = codemods
    if effective_codemods is None and settings.codemods_enabled:
        effective_codemods = _Registry()
    verifier = (
        Verifier(
            editor,
            install_timeout=settings.verify_install_timeout,
            test_timeout=settings.verify_test_timeout,
            ignore_scripts=settings.verify_ignore_scripts,
            max_output_bytes=settings.verify_max_output_bytes,
            typecheck_enabled=settings.verify_typecheck_enabled,
            typecheck_timeout=settings.verify_typecheck_timeout,
            characterizer=characterizer,
            ecosystem=ecosystem,
            coverage_enabled=settings.verify_coverage_enabled,
            contract_enabled=settings.verify_contract_enabled,
            flake_retries=settings.verify_flake_retries,
            static_check_enabled=settings.verify_static_check_enabled,
            static_check_timeout=settings.verify_static_check_timeout,
            selective_tests=settings.verify_selective_tests,
        )
        if verify_required
        else None
    )
    retries = settings.retry_max_rounds if max_retries is None else max_retries
    retry_loop = (
        RetryLoop(fixer, validator, verifier, max_rounds=retries)
        if verifier is not None and retries > 0
        else None
    )
    return FixPipeline(
        fixer,  # type: ignore[arg-type]
        validator=validator,
        verifier=verifier,
        retry_loop=retry_loop,
        toolset=toolset,
        codemods=effective_codemods,
    )


def _build_completer_for_characterize(settings: Settings) -> LLMCompleter | None:
    """Build the fixture-only completer for the characterization oracle.

    Characterization generates test fixtures — a code-generation task —
    so it uses the heavy model (Sonnet-class), not the light one.
    """
    if settings.llm_provider == "bedrock":
        return BedrockCompleter(
            aws_access_key=settings.bedrock_access_key,
            aws_secret_key=settings.bedrock_secret_key,
            aws_region=settings.bedrock_region,
            aws_profile=settings.bedrock_profile,
            model=settings.bedrock_model,
        )
    if settings.llm_provider == "gemini":
        if not settings.google_api_key:
            return None
        return GeminiCompleter(api_key=settings.google_api_key, model=settings.gemini_model)
    return OllamaCompleter(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        timeout=settings.ollama_timeout,
    )


def _build_fallback_fixer(
    settings: Settings,
) -> BedrockFixGenerator | FixGenerator | OllamaFixGenerator | None:
    """Instantiate the configured stronger fallback model, if any."""
    if not settings.llm_fallback_model:
        return None
    provider = (settings.llm_fallback_provider or settings.llm_provider).strip()
    if provider == "bedrock":
        return BedrockFixGenerator(
            aws_access_key=settings.bedrock_access_key,
            aws_secret_key=settings.bedrock_secret_key,
            aws_region=settings.bedrock_region,
            aws_profile=settings.bedrock_profile,
            model=settings.llm_fallback_model,
        )
    if provider == "gemini":
        if not settings.google_api_key:
            return None
        return FixGenerator(api_key=settings.google_api_key, model=settings.llm_fallback_model)
    return OllamaFixGenerator(
        model=settings.llm_fallback_model,
        base_url=settings.ollama_base_url,
        timeout=settings.ollama_timeout,
    )


def run_fix_for_change(
    *,
    checkout: Checkout,
    scan_result: RepoScanResult,
    change: BreakingChange,
    settings: Settings,
    repo_config: RepoConfig,
    fixer: BedrockFixGenerator | FixGenerator | OllamaFixGenerator,
    max_retries: int | None = None,
    ledger: CostLedger | None = None,
) -> FixServiceResult:
    """Run a fix, escalating once when the primary has no committable edit."""
    from depfix.ecosystems import fixable_ids
    from depfix.ecosystems import get as get_ecosystem
    from depfix.scanners.repo import detect_repo_ecosystem

    # Dependency drift is not a source-code migration.  It therefore has no
    # call sites to hand to an LLM: make a deterministic manifest edit, then
    # use the same verification and commit policy as every other fix.
    if change.kind in (ChangeKind.DEPENDENCY_VERSION_BUMP, ChangeKind.SECURITY_ADVISORY):
        if change.kind is ChangeKind.SECURITY_ADVISORY and (
            not change.new_api.strip() or not change.new_version.strip()
        ):
            result = FixPipelineResult(
                repo_full_name=scan_result.repo_full_name,
                breaking_change=change,
                files_scanned=scan_result.files_scanned,
                files_affected=0,
                edits=(),
                total_usages_fixed=0,
                total_cost=0.0,
                total_tokens=0,
                duration_ms=0,
                codemod_notes=("security advisory has no published fix; reported for review",),
            )
            return FixServiceResult(result, [], repo_config.verify and settings.verify_enabled)
        return _run_dependency_drift(checkout, scan_result, change, settings, repo_config)

    if is_method_removal(change):
        result = FixPipelineResult(
            repo_full_name=scan_result.repo_full_name,
            breaking_change=change,
            files_scanned=scan_result.files_scanned,
            files_affected=0,
            edits=(),
            total_usages_fixed=0,
            total_cost=0.0,
            total_tokens=0,
            duration_ms=0,
            codemod_notes=(
                f"{change.old_api} was removed upstream with no replacement; "
                "reported only, no fix generated",
            ),
        )
        return FixServiceResult(result, [], repo_config.verify and settings.verify_enabled)

    ecosystem = detect_repo_ecosystem(checkout.path)
    if ecosystem == "typescript":
        ecosystem = "javascript"
    if ecosystem not in fixable_ids():
        pretty = ecosystem if ecosystem != "unknown" else "unrecognized"
        spec = get_ecosystem(ecosystem)
        supported = ", ".join(sorted(fixable_ids()))
        note = (
            f"skipped: {spec.display_name} repos are scan-only today -- depfix found the "
            f"call sites above but only generates verified fixes for: {supported}"
            if spec is not None
            else f"skipped: repo ecosystem is {pretty}; depfix generates fixes for: {supported}"
        )
        result = FixPipelineResult(
            repo_full_name=scan_result.repo_full_name,
            breaking_change=change,
            files_scanned=scan_result.files_scanned,
            files_affected=0,
            edits=(),
            total_usages_fixed=0,
            total_cost=0.0,
            total_tokens=0,
            duration_ms=0,
            codemod_notes=(note,),
        )
        logger.info("skipping %s: unsupported ecosystem %s", change.dedupe_key[:12], pretty)
        return FixServiceResult(result, [], repo_config.verify and settings.verify_enabled)
    verify_required = repo_config.verify and settings.verify_enabled
    characterizer = None
    # The characterization harness is Node/CommonJS-specific
    # (verify/characterize.py); other ecosystems skip straight to their
    # own smoke checks.
    if settings.verify_characterize_enabled and ecosystem == "javascript":
        completer = _build_completer_for_characterize(settings)
        if completer is not None:
            characterizer = Characterizer(
                completer,
                timeout=float(settings.verify_characterize_timeout),
                max_output_bytes=settings.verify_max_output_bytes,
                ledger=ledger,
            )

    primary = _run_once(
        checkout=checkout,
        scan_result=scan_result,
        change=change,
        settings=settings,
        repo_config=repo_config,
        fixer=fixer,
        max_retries=max_retries,
        characterizer=characterizer,
        ecosystem=ecosystem,
    )
    if ledger is not None:
        ledger.record(CostStage.FIX_GENERATION, primary.pipeline_result.total_cost)
    if primary.committable:
        return primary

    fallback = _build_fallback_fixer(settings)
    if fallback is None:
        return primary

    logger.info(
        "primary model produced no committable edits for %s; escalating to fallback model %s",
        change.dedupe_key[:12],
        settings.llm_fallback_model,
    )
    # Restore the checkout explicitly. A new WorkspaceEditor alone would
    # capture the primary's rejected contents as its "original" state.
    restore_editor = WorkspaceEditor(checkout)
    for edit in primary.pipeline_result.edits:
        if edit.is_on_disk:
            restore_editor.write_fix(edit.relpath, edit.original_content)

    escalated = _run_once(
        checkout=checkout,
        scan_result=scan_result,
        change=change,
        settings=settings,
        repo_config=repo_config,
        fixer=fallback,
        max_retries=max_retries,
        characterizer=characterizer,
        ecosystem=ecosystem,
    )
    if ledger is not None:
        ledger.record(CostStage.RETRY, escalated.pipeline_result.total_cost)
    if escalated.committable:
        return FixServiceResult(
            pipeline_result=escalated.pipeline_result,
            committable=escalated.committable,
            verify_required=verify_required,
            escalated=True,
        )
    return FixServiceResult(
        pipeline_result=primary.pipeline_result,
        committable=[],
        verify_required=verify_required,
        escalated=True,
    )


def _run_dependency_drift(
    checkout: Checkout,
    scan_result: RepoScanResult,
    change: BreakingChange,
    settings: Settings,
    repo_config: RepoConfig,
) -> FixServiceResult:
    """Apply one safe manifest-only dependency update behind the service seam."""
    from depfix.codemods.lockfile import dependency_drift_policy
    from depfix.scanners.repo import detect_repo_ecosystem

    verify_required = repo_config.verify and settings.verify_enabled
    policy = dependency_drift_policy(
        change,
        max_major_gap=settings.drift_max_major_gap,
        max_minor_gap=settings.drift_max_minor_gap,
        scope=settings.drift_scope,
    )
    ecosystem = detect_repo_ecosystem(checkout.path)
    if ecosystem == "typescript":
        ecosystem = "javascript"

    def report_only(note: str) -> FixServiceResult:
        result = FixPipelineResult(
            repo_full_name=scan_result.repo_full_name,
            breaking_change=change,
            files_scanned=scan_result.files_scanned,
            files_affected=0,
            edits=(),
            total_usages_fixed=0,
            total_cost=0.0,
            total_tokens=0,
            duration_ms=0,
            codemod_notes=(note,),
        )
        return FixServiceResult(result, [], verify_required)

    if policy == "declined":
        return report_only(
            "unparseable or non-forward dependency drift cannot be safely rewritten."
        )

    cross_major = False
    if ecosystem == "javascript":
        from depfix.codemods.lockfile import build_dependency_drift_plan

        plan = build_dependency_drift_plan(checkout.path, change, scope=settings.drift_scope)
        cross_major = plan.cross_major
        drift_bumps = [
            (
                bump.manifest_relpath,
                bump.fixed_content,
                bump.note,
                f"Registry drift: {bump.package} {bump.old_range} -> {bump.new_range} "
                f"in {bump.manifest_relpath}.",
            )
            for bump in plan.bumps
        ]
    elif ecosystem == "python":
        from depfix.ecosystems.pypi_manifests import build_pypi_dependency_drift_bumps

        drift_bumps = [
            (
                bump.manifest_relpath,
                bump.fixed_content,
                bump.note,
                f"Registry drift: {bump.package} {bump.old_spec} -> {bump.new_spec} "
                f"in {bump.manifest_relpath}.",
            )
            for bump in build_pypi_dependency_drift_bumps(checkout.path, change)
        ]
    elif ecosystem == "ruby":
        from depfix.ecosystems.ruby_manifests import build_ruby_dependency_drift_bumps

        drift_bumps = [
            (
                bump.manifest_relpath,
                bump.fixed_content,
                bump.note,
                f"Registry drift: {bump.package} {bump.old_spec} -> {bump.new_spec} "
                f"in {bump.manifest_relpath}.",
            )
            for bump in build_ruby_dependency_drift_bumps(checkout.path, change)
        ]
    elif ecosystem == "go":
        from depfix.ecosystems.go_manifests import build_go_dependency_drift_bumps

        drift_bumps = [
            (
                bump.manifest_relpath,
                bump.fixed_content,
                bump.note,
                f"Registry drift: {bump.package} {bump.old_version} -> {bump.new_version} "
                f"in {bump.manifest_relpath}.",
            )
            for bump in build_go_dependency_drift_bumps(checkout.path, change)
        ]
    else:
        from depfix.ecosystems import get as get_ecosystem

        spec = get_ecosystem(ecosystem)
        pretty = (
            spec.display_name
            if spec is not None
            else (ecosystem if ecosystem != "unknown" else "this repository's")
        )
        return report_only(
            f"dependency drift detected for {pretty}; automatic manifest writers are "
            "currently available for npm, PyPI, RubyGems, and Go modules only. This "
            "drift is reported for review until a manifest, lockfile, and verification "
            "adapter exists for this ecosystem."
        )

    editor = WorkspaceEditor(checkout)
    edits: list[FileEdit] = []
    notes: list[str] = plan.declines if ecosystem == "javascript" else []
    if not drift_bumps and not notes:
        notes.append(
            f"{change.package} drift detected via registry feed but no manifest in this "
            f"repo declares it (may be transitive, or {ecosystem} manifest format not yet supported)"
        )
    for relpath, fixed_content, note, trace in drift_bumps:
        edit = editor.write_fix(
            relpath,
            fixed_content,
            confidence=1.0,
            origin=EditOrigin.CODEMOD,
            codemod_id="manifest.dependency-drift",
        )
        if not edit.changed:
            notes.append(f"manifest drift update not written: {relpath}")
            continue
        edits.append(dataclasses.replace(edit, derivation_trace=trace))
        notes.append(note)

    # Dependency declarations are source-of-truth; JavaScript lockfiles are
    # generated output. Refresh every affected workspace lock before
    # verification, so npm/yarn/pnpm/bun frozen installs see a consistent
    # dependency graph. This deliberately includes pnpm catalogs, whose
    # source of truth is ``pnpm-workspace.yaml`` rather than a consumer JSON.
    if (
        ecosystem == "javascript"
        and settings.lockfile_regenerate
        and checkout.is_temporary
        and edits
    ):
        from depfix.codemods.lockfile import lockfile_paths, try_refresh_javascript_lockfile

        originals: dict[str, bytes] = {}
        for lockfile in lockfile_paths(checkout.path):
            try:
                relpath = lockfile.resolve().relative_to(checkout.path.resolve()).as_posix()
                originals[relpath] = lockfile.read_bytes()
            except OSError:
                continue
        ok, message = try_refresh_javascript_lockfile(
            checkout.path,
            timeout=settings.verify_install_timeout,
            max_output_bytes=settings.verify_max_output_bytes,
        )
        notes.append(message)
        if ok:
            try:
                refreshed = {
                    lockfile.resolve().relative_to(checkout.path.resolve()).as_posix(): lockfile
                    for lockfile in lockfile_paths(checkout.path)
                }
            except OSError:
                refreshed = {}
            for relpath, original_lock in originals.items():
                lockfile = refreshed.get(relpath)  # type: ignore[assignment]
                if lockfile is None:
                    continue
                try:
                    refreshed_lock = lockfile.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if refreshed_lock.encode("utf-8") == original_lock:
                    continue
                lockfile.write_bytes(original_lock)
                lock_edit = editor.write_fix(
                    relpath,
                    refreshed_lock,
                    confidence=1.0,
                    origin=EditOrigin.CODEMOD,
                    codemod_id="manifest.lockfile-regen",
                )
                if lock_edit.changed:
                    edits.append(
                        dataclasses.replace(
                            lock_edit,
                            derivation_trace=f"JavaScript lockfile refreshed after dependency drift: {message}",
                        )
                    )

    # Go is the one ecosystem whose generated file must be refreshed *before*
    # verification: without matching go.sum hashes, `go mod download` and
    # `go test` both refuse to build ("missing go.sum entry"), so a correct
    # manifest bump would be verified as a failure. `go mod tidy` is also
    # allowed to rewrite go.mod itself, so whatever actually changed is
    # re-registered with the editor.
    if ecosystem == "go" and settings.lockfile_regenerate and checkout.is_temporary and edits:
        from depfix.ecosystems.go_lockfile import go_generated_paths, try_refresh_go_lockfile

        before: dict[str, str] = {}
        for generated in go_generated_paths(checkout.path):
            try:
                relpath = generated.relative_to(checkout.path).as_posix()
                before[relpath] = generated.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                continue

        ok, message = try_refresh_go_lockfile(
            checkout.path,
            package=change.package,
            timeout=settings.verify_install_timeout,
            max_output_bytes=settings.verify_max_output_bytes,
        )
        notes.append(message)
        if ok:
            for relpath, original_text in before.items():
                path = checkout.path / relpath
                try:
                    refreshed = path.read_text(encoding="utf-8")  # type: ignore[assignment]
                except (OSError, UnicodeDecodeError):
                    continue
                if refreshed == original_text:
                    continue
                path.write_text(original_text, encoding="utf-8")
                refreshed_edit = dataclasses.replace(
                    editor.write_fix(
                        relpath,
                        refreshed,  # type: ignore[arg-type]
                        confidence=1.0,
                        origin=EditOrigin.CODEMOD,
                        codemod_id="manifest.lockfile-regen",
                    ),
                    derivation_trace=f"Go generated file refreshed after drift: {message}.",
                )
                if not refreshed_edit.changed:
                    continue
                existing = next(
                    (i for i, edit in enumerate(edits) if edit.relpath == relpath), None
                )
                if existing is None:
                    edits.append(refreshed_edit)
                else:
                    edits[existing] = refreshed_edit

    verification = None
    if edits and verify_required:
        verifier = Verifier(
            editor,
            install_timeout=settings.verify_install_timeout,
            test_timeout=settings.verify_test_timeout,
            ignore_scripts=settings.verify_ignore_scripts,
            max_output_bytes=settings.verify_max_output_bytes,
            typecheck_enabled=settings.verify_typecheck_enabled,
            typecheck_timeout=settings.verify_typecheck_timeout,
            ecosystem=ecosystem,
            coverage_enabled=settings.verify_coverage_enabled,
            contract_enabled=settings.verify_contract_enabled,
            flake_retries=settings.verify_flake_retries,
            static_check_enabled=settings.verify_static_check_enabled,
            static_check_timeout=settings.verify_static_check_timeout,
            selective_tests=settings.verify_selective_tests,
            frozen_install=False,
        )
        verification = verifier.verify(edits, change=change)
        edits = list(verification.edits)

    # Refresh only after verification has kept the manifest edit. Both uv
    # and Poetry lock without installing the project; a failed refresh stays
    # an explicit note rather than making the manifest change unsafe.
    if (
        ecosystem == "python"
        and settings.lockfile_regenerate
        and checkout.is_temporary
        and any(edit.is_on_disk for edit in edits)
    ):
        from depfix.ecosystems.python_lockfile import (
            python_lockfile_paths,
            try_refresh_python_lockfile,
        )

        ok, message = try_refresh_python_lockfile(
            checkout.path,
            timeout=settings.verify_install_timeout,
        )
        notes.append(message)
        if ok:
            for lockfile in python_lockfile_paths(checkout.path):
                relpath = lockfile.relative_to(checkout.path).as_posix()
                try:
                    content = lockfile.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                lock_edit = editor.write_fix(
                    relpath,
                    content,
                    confidence=1.0,
                    origin=EditOrigin.CODEMOD,
                    codemod_id="manifest.lockfile-regen",
                )
                if not lock_edit.changed:
                    continue
                lock_edit = dataclasses.replace(
                    lock_edit,
                    derivation_trace=f"Lockfile refreshed after manifest drift: {message}.",
                )
                edits.append(lock_edit)

    if (
        ecosystem == "ruby"
        and settings.lockfile_regenerate
        and checkout.is_temporary
        and any(edit.is_on_disk for edit in edits)
    ):
        from depfix.ecosystems.ruby_lockfile import (
            ruby_lockfile_paths,
            try_refresh_ruby_lockfile,
        )

        ok, message = try_refresh_ruby_lockfile(
            checkout.path,
            timeout=settings.verify_install_timeout,
        )
        notes.append(message)
        if ok:
            for lockfile in ruby_lockfile_paths(checkout.path):
                relpath = lockfile.relative_to(checkout.path).as_posix()
                try:
                    content = lockfile.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                lock_edit = editor.write_fix(
                    relpath,
                    content,
                    confidence=1.0,
                    origin=EditOrigin.CODEMOD,
                    codemod_id="manifest.lockfile-regen",
                )
                if not lock_edit.changed:
                    continue
                lock_edit = dataclasses.replace(
                    lock_edit,
                    derivation_trace=f"Gemfile.lock refreshed after manifest drift: {message}.",
                )
                edits.append(lock_edit)

    needs_major_review = policy == "review_pr" or cross_major
    result = FixPipelineResult(
        repo_full_name=scan_result.repo_full_name,
        breaking_change=change,
        files_scanned=scan_result.files_scanned,
        files_affected=len(edits),
        edits=tuple(edits),
        total_usages_fixed=0,
        total_cost=0.0,
        total_tokens=0,
        duration_ms=0,
        verification=verification,
        codemod_notes=(
            *notes,
            *(
                (
                    "MAJOR VERSION REVIEW: this update crosses a major version boundary "
                    "and is opened as a draft PR requiring human approval.",
                )
                if needs_major_review
                else ()
            ),
        ),
    )
    return FixServiceResult(
        result,
        committable_edits(
            result,
            allow_unverified=not verify_required,
            allow_review_suspects=change.kind is ChangeKind.DEPENDENCY_VERSION_BUMP,
        ),
        verify_required,
        cross_major_drift=cross_major,
    )


def _run_once(
    *,
    checkout: Checkout,
    scan_result: RepoScanResult,
    change: BreakingChange,
    settings: Settings,
    repo_config: RepoConfig,
    fixer: BedrockFixGenerator | FixGenerator | OllamaFixGenerator,
    max_retries: int | None,
    characterizer: Characterizer | None,
    ecosystem: str = "javascript",
) -> FixServiceResult:
    """Run one isolated primary or fallback fix attempt."""
    editor = WorkspaceEditor(checkout)
    strategy = make_fix_strategy(settings, fixer)  # type: ignore[arg-type]
    toolset = (
        FixToolset(
            checkout,
            scanner=CallSiteScanner(
                max_file_bytes=settings.scan_max_file_bytes, max_files=settings.scan_max_files
            ),
            max_file_bytes=settings.scan_max_file_bytes,
        )
        if isinstance(strategy, FixAgent)
        else None
    )
    verify_required = repo_config.verify and settings.verify_enabled
    pipeline = build_fix_pipeline(
        settings=settings,
        repo_config=repo_config,
        fixer=strategy,
        editor=editor,
        max_retries=max_retries,
        toolset=toolset,
        characterizer=characterizer,
        ecosystem=ecosystem,
    )
    result = pipeline.run(scan_result, change, editor)
    committable = committable_edits(result, allow_unverified=not verify_required)
    floor = ConfidenceTier(settings.apply_min_confidence)
    if committable and not result.confidence.at_least(floor):
        logger.info(
            "withholding %d edit(s) for %s: confidence %s is below apply_min_confidence %s",
            len(committable),
            change.dedupe_key[:12],
            result.confidence.value,
            floor.value,
        )
        committable = []
    if committable:
        from depfix.apply.models import EditOrigin, EditVerdict
        from depfix.codemods.lockfile import build_manifest_bumps

        bumped_any = False
        for bump in build_manifest_bumps(checkout.path, change):
            edit = editor.write_fix(
                bump.manifest_relpath, bump.fixed_content, confidence=1.0, usages_fixed=0
            )
            if not edit.changed:
                # write_fix declined (e.g. identical content, encoding error,
                # or an I/O failure) -- do not report an untouched/failed
                # manifest as a kept, on-disk fix.
                result = dataclasses.replace(
                    result,
                    codemod_notes=(
                        *result.codemod_notes,
                        f"manifest bump for {bump.manifest_relpath} was not written: "
                        f"{edit.error_message or 'no change on disk'}",
                    ),
                )
                continue
            edit = dataclasses.replace(
                edit,
                verdict=EditVerdict.KEPT,
                origin=EditOrigin.CODEMOD,
                codemod_id="manifest.version-bump",
                derivation_trace=(
                    f"Manifest bump: {bump.package} {bump.old_range} → "
                    f"{bump.new_range} in {bump.manifest_relpath}."
                ),
            )
            committable.append(edit)
            result = dataclasses.replace(
                result,
                edits=(*result.edits, edit),
                codemod_notes=(*result.codemod_notes, bump.note),
            )
            bumped_any = True
        if bumped_any and settings.lockfile_regenerate and checkout.is_temporary:
            from depfix.codemods.lockfile import lockfile_paths, try_regenerate_lockfile

            ok, message = try_regenerate_lockfile(
                checkout.path,
                timeout=settings.verify_install_timeout,
                ignore_scripts=settings.verify_ignore_scripts,
            )
            if ok:
                for lockfile in lockfile_paths(checkout.path):
                    relpath = lockfile.relative_to(checkout.path).as_posix()
                    try:
                        content = lockfile.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        continue  # binary bun.lockb cannot be represented as a FileEdit
                    edit = editor.write_fix(relpath, content, confidence=1.0, usages_fixed=0)
                    if edit.changed:
                        edit = dataclasses.replace(
                            edit,
                            verdict=EditVerdict.KEPT,
                            origin=EditOrigin.CODEMOD,
                            codemod_id="manifest.lockfile-regen",
                            derivation_trace=f"Lockfile regenerated after manifest bump: {message}.",
                        )
                        committable.append(edit)
                        result = dataclasses.replace(
                            result,
                            edits=(*result.edits, edit),
                            codemod_notes=(*result.codemod_notes, message),
                        )
            else:
                result = dataclasses.replace(
                    result,
                    codemod_notes=(
                        *result.codemod_notes,
                        f"lockfile not regenerated: {message}; regenerate it before merging",
                    ),
                )
    return FixServiceResult(
        pipeline_result=result,
        committable=committable,
        verify_required=verify_required,
    )
