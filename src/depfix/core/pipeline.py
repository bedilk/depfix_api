"""Week 4 fix pipeline: wires the Week 3 scanner's output to the
apply/verify subsystems.

Where :class:`depfix.core.agent.DependencyFixAgent` (Week 1/2) scans a
codebase with a regex pattern and generates fixes in memory,
:class:`FixPipeline` starts from a :class:`~depfix.scanners.models.RepoScanResult`
(the binding-aware call-site scanner's output), writes each candidate fix
in place via :class:`~depfix.apply.workspace.WorkspaceEditor` -- so it's a
real, revertible edit on disk, not just a diff -- and optionally confirms
it against the repo's own test suite via :class:`~depfix.verify.verifier.Verifier`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace

from depfix.agent.fix_agent import FixAgent
from depfix.agent.tools import FixToolset
from depfix.apply.models import EditOrigin, EditVerdict, FileEdit
from depfix.apply.workspace import WorkspaceEditor
from depfix.codemods import CodemodRegistry
from depfix.core.models import BreakingChange, FileUsage
from depfix.fixers.gemini import FixGenerator
from depfix.fixers.ollama import OllamaFixGenerator
from depfix.redaction import RedactionError
from depfix.retry.loop import RetryLoop
from depfix.scanners.models import RepoScanResult
from depfix.validators.javascript import FixValidator
from depfix.verify.confidence import ConfidenceTier
from depfix.verify.verifier import VerificationReport, Verifier

logger = logging.getLogger(__name__)


@dataclass
class FixPipelineResult:
    """Everything produced by one :meth:`FixPipeline.run` call."""

    repo_full_name: str
    breaking_change: BreakingChange
    files_scanned: int
    files_affected: int
    edits: tuple[FileEdit, ...]
    total_usages_fixed: int
    total_cost: float
    total_tokens: int
    duration_ms: int
    verification: VerificationReport | None = None
    codemod_notes: tuple[str, ...] = ()

    @property
    def confidence(self) -> ConfidenceTier:
        if self.verification is not None:
            return self.verification.tier
        if any(edit.verdict in (EditVerdict.APPLIED, EditVerdict.KEPT) for edit in self.edits):
            return ConfidenceTier.LOW
        return ConfidenceTier.NONE

    @property
    def confidence_reason(self) -> str:
        if self.verification is not None and self.verification.tier_reason:
            return self.verification.tier_reason
        if self.verification is None:
            return "verification was disabled; only a syntax check ran"
        return ""

    @property
    def applied_edits(self) -> list[FileEdit]:
        """Edits whose fixed content is (as far as we know) on disk right
        now -- APPLIED, KEPT, or SUSPECT. See :attr:`FileEdit.is_on_disk`."""
        return [e for e in self.edits if e.is_on_disk]

    @property
    def reverted_edits(self) -> list[FileEdit]:
        return [e for e in self.edits if e.verdict == EditVerdict.REVERTED]

    @property
    def suspect_edits(self) -> list[FileEdit]:
        return [e for e in self.edits if e.verdict == EditVerdict.SUSPECT]

    @property
    def codemod_edits(self) -> list[FileEdit]:
        return [edit for edit in self.edits if edit.origin is EditOrigin.CODEMOD]

    @property
    def llm_edits(self) -> list[FileEdit]:
        return [edit for edit in self.edits if edit.origin is EditOrigin.LLM]


class FixPipeline:
    """Generates and applies fixes for every actionable call site in a
    :class:`RepoScanResult`, against a checkout already opened by ``editor``.

    Not safe to share across threads or reuse across unrelated scans/repos
    for the same reason :class:`WorkspaceEditor` isn't: both track
    per-instance state (a fixer's running cost/token totals, an editor's
    original-bytes cache) scoped to one run.
    """

    def __init__(
        self,
        fixer: FixGenerator | OllamaFixGenerator | FixAgent,
        *,
        validator: FixValidator | None = None,
        verifier: Verifier | None = None,
        retry_loop: RetryLoop | None = None,
        toolset: FixToolset | None = None,
        codemods: CodemodRegistry | None = None,
    ) -> None:
        self._fixer = fixer
        self._validator = validator or FixValidator()
        self._verifier = verifier
        self._retry_loop = retry_loop
        self._toolset = toolset
        self._codemods = codemods
        self._codemod_notes: list[str] = []

    def run(
        self,
        scan_result: RepoScanResult,
        breaking_change: BreakingChange,
        editor: WorkspaceEditor,
    ) -> FixPipelineResult:
        """Fix every actionable call site ``scan_result`` found, write each
        fix via ``editor``, and -- if this pipeline was built with a
        ``verifier`` -- run the repo's own test suite to settle each edit's
        final verdict.
        """
        start = time.time()
        file_usages = scan_result.to_file_usages(editor.checkout.path)

        self._codemod_notes = []
        batch_edits: dict[str, FileEdit] = {}
        if self._codemods is not None and file_usages:
            sources = {usage.filepath: usage.file_content for usage in file_usages}
            batch_results = self._codemods.apply_batch(sources, breaking_change)
            for usage in file_usages:
                result = batch_results.get(usage.filepath)
                if result is None or result.source is None:
                    continue
                validation = self._validator.validate(
                    usage.file_content, result.source, usage.filepath
                )
                if not validation.is_valid:
                    self._codemod_notes.append(
                        f"{usage.filepath}: batch codemod invalid; fell back to model"
                    )
                    continue
                edit = editor.write_fix(
                    usage.filepath,
                    result.source,
                    confidence=1.0,
                    usages_fixed=usage.usage_count,
                    validation=validation,
                    origin=EditOrigin.CODEMOD,
                    codemod_id=result.codemod_id,
                )
                batch_edits[usage.filepath] = replace(
                    edit,
                    derivation_trace=(
                        f"Deterministic codemod `{result.codemod_id}` rewrote "
                        f"{usage.usage_count} site(s). {result.note}"
                    ),
                )
                self._codemod_notes.append(f"{usage.filepath}: {result.note}")
        edits = [
            batch_edits.get(usage.filepath) or self._fix_one(usage, breaking_change, editor)
            for usage in file_usages
        ]

        # A migration changes both code and the SDK version. The verifier's
        # install step must see the bumped manifest, otherwise migrated code
        # is evaluated against the old SDK. This temporary bump is not a
        # verification edit: fix_service creates the committable manifest
        # edit after source edits are verified.
        pre_verify_bumped: list[str] = []
        if self._verifier is not None and any(edit.is_on_disk for edit in edits):
            from depfix.codemods.lockfile import build_manifest_bumps

            for bump in build_manifest_bumps(editor.checkout.path, breaking_change):
                bump_edit = editor.write_fix(bump.manifest_relpath, bump.fixed_content)
                if bump_edit.changed:
                    pre_verify_bumped.append(bump.manifest_relpath)
                    self._codemod_notes.append(f"pre-verify bump: {bump.note}")

        verification: VerificationReport | None = None
        if self._verifier is not None:
            verification = self._verifier.verify(edits, change=breaking_change)
            edits = list(verification.edits)

            if self._retry_loop is not None:
                file_usages_by_relpath = {fu.filepath: fu for fu in file_usages}
                edits, verification, rounds_used = self._retry_loop.run(
                    edits,
                    verification,
                    file_usages_by_relpath,
                    breaking_change,
                    editor,
                    toolset=self._toolset,
                )
                if rounds_used:
                    logger.info(
                        "retry loop used %d round(s) for %s",
                        rounds_used,
                        scan_result.repo_full_name,
                    )

        # Preserve the checkout's original manifest. fix_service will create
        # the reviewed, committable manifest edit only after kept source edits
        # establish that the migration is viable.
        for relpath in pre_verify_bumped:
            editor.revert(relpath)

        total_usages_fixed = sum(e.usages_fixed for e in edits if e.is_on_disk)

        cross_file_warnings: tuple[str, ...] = ()
        if self._codemods is not None:
            cross_file_warnings = tuple(
                self._codemods.audit_cross_file(
                    breaking_change,
                    editor.checkout.path,
                    {e.relpath for e in edits if e.is_on_disk},
                )
            )

        return FixPipelineResult(
            repo_full_name=scan_result.repo_full_name,
            breaking_change=breaking_change,
            files_scanned=scan_result.files_scanned,
            files_affected=len(file_usages),
            edits=tuple(edits),
            total_usages_fixed=total_usages_fixed,
            total_cost=self._fixer.total_cost,
            total_tokens=self._fixer.total_tokens,
            duration_ms=int((time.time() - start) * 1000),
            verification=verification,
            codemod_notes=(*self._codemod_notes, *cross_file_warnings),
        )

    def _fix_one(
        self, file_usage: FileUsage, breaking_change: BreakingChange, editor: WorkspaceEditor
    ) -> FileEdit:
        """Try a deterministic codemod before requesting an LLM fix.

        A generation failure (LLM error, exhausted retries) produces a
        SKIPPED edit rather than propagating -- one bad file must not abort
        fixing the rest of the batch. A *validation* failure, by contrast,
        still gets written to disk as SUSPECT: see
        :class:`depfix.apply.models.EditVerdict`'s docstring for why an
        unconfirmed fix is kept and flagged rather than silently dropped.
        """
        if self._codemods is not None:
            result = self._codemods.apply(
                file_usage.filepath, file_usage.file_content, breaking_change
            )
            if result is not None and result.changed and result.source is not None:
                validation = self._validator.validate(
                    file_usage.file_content, result.source, file_usage.filepath
                )
                if validation.is_valid:
                    self._codemod_notes.append(f"{file_usage.filepath}: {result.note}")
                    edit = editor.write_fix(
                        file_usage.filepath,
                        result.source,
                        confidence=1.0,
                        usages_fixed=file_usage.usage_count,
                        validation=validation,
                        origin=EditOrigin.CODEMOD,
                        codemod_id=result.codemod_id,
                    )
                    return replace(
                        edit,
                        derivation_trace=(
                            f"Deterministic codemod `{result.codemod_id}` rewrote "
                            f"{file_usage.usage_count} site(s). {result.note}"
                        ),
                    )
                self._codemod_notes.append(
                    f"{file_usage.filepath}: {result.codemod_id} produced invalid syntax; fell back to the model"
                )
            elif result is not None and result.declined:
                self._codemod_notes.append(
                    f"{file_usage.filepath}: {result.codemod_id} declined ({result.declined_reason}); fell back to the model"
                )
        try:
            kwargs = {"toolset": self._toolset} if isinstance(self._fixer, FixAgent) else {}
            fixed_code, confidence, _llm_call = self._fixer.generate_fix(
                file_usage,
                breaking_change,
                **kwargs,  # type: ignore[arg-type]
            )
        except RedactionError as exc:
            # The model dropped or invented a secret placeholder. Writing this
            # output would delete (or fabricate) a credential. Not retried:
            # a second sample of the same prompt is no safer.
            logger.error("redaction guard rejected output for %s: %s", file_usage.filepath, exc)
            return FileEdit(
                relpath=file_usage.filepath,
                original_content=file_usage.file_content,
                fixed_content=file_usage.file_content,
                diff="",
                verdict=EditVerdict.SKIPPED,
                error_message=f"redacted secret was not preserved: {exc}",
                derivation_trace=f"Redaction guard refused the model output: {exc}",
            )
        except Exception as exc:
            logger.error("fix generation failed for %s: %s", file_usage.filepath, exc)
            return FileEdit(
                relpath=file_usage.filepath,
                original_content=file_usage.file_content,
                fixed_content=file_usage.file_content,
                diff="",
                verdict=EditVerdict.SKIPPED,
                error_message=f"fix generation failed: {exc}",
                derivation_trace=f"LLM generation failed: {exc}",
            )

        validation = self._validator.validate(
            file_usage.file_content, fixed_code, file_usage.filepath
        )
        usages_fixed = file_usage.usage_count if validation.is_valid else 0

        edit = editor.write_fix(
            file_usage.filepath,
            fixed_code,
            confidence=confidence,
            usages_fixed=usages_fixed,
            validation=validation,
            origin=EditOrigin.LLM,
        )
        model = getattr(self._fixer, "model", "unknown")
        return replace(
            edit,
            derivation_trace=(
                f"LLM ({model}) generated a migration for {file_usage.usage_count} usage(s) of "
                f"`{breaking_change.old_api}` → `{breaking_change.new_api}`; confidence {confidence:.2f}."
            ),
        )
