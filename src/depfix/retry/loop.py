"""Bounded retry loop for :class:`depfix.core.pipeline.FixPipeline`.

When an edit comes back ``SUSPECT`` (syntax invalid, per
:class:`depfix.core.models.ValidationResult`) or ``REVERTED`` (a confirmed
test regression, per :class:`depfix.verify.verifier.VerificationReport`\\
's ``attribution``), :class:`RetryLoop` feeds the concrete failure back to
the fixer and re-attempts, bounded by ``max_rounds``. Edits with no
concrete feedback signal (e.g. ``SUSPECT`` because verification itself was
inconclusive, not because of a syntax error) are left alone -- there's
nothing useful to tell the LLM about them.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Protocol

from depfix.agent.fix_agent import FixAgent
from depfix.agent.tools import FixToolset
from depfix.apply.models import EditVerdict, FileEdit
from depfix.apply.workspace import WorkspaceEditor
from depfix.core.models import BreakingChange, FileUsage, LLMCall
from depfix.validators.javascript import FixValidator
from depfix.verify.verifier import VerificationReport, Verifier

logger = logging.getLogger(__name__)


class _Fixer(Protocol):
    """Duck-typed shape shared by ``FixGenerator``/``OllamaFixGenerator``."""

    def generate_fix(
        self,
        file_usage: FileUsage,
        breaking_change: BreakingChange,
        *,
        feedback: str | None = None,
    ) -> tuple[str, float, LLMCall]: ...


class RetryLoop:
    """Re-attempts ``SUSPECT``/``REVERTED`` edits with concrete feedback,
    up to ``max_rounds`` times.

    Not safe to share across threads or reuse across unrelated fix
    batches -- like :class:`~depfix.verify.verifier.Verifier`, it drives
    ``editor``'s revert/reapply cycle directly.
    """

    def __init__(
        self,
        fixer: _Fixer,
        validator: FixValidator,
        verifier: Verifier,
        *,
        max_rounds: int = 2,
    ) -> None:
        self._fixer = fixer
        self._validator = validator
        self._verifier = verifier
        self._max_rounds = max_rounds

    def run(
        self,
        edits: list[FileEdit],
        verification: VerificationReport,
        file_usages_by_relpath: dict[str, FileUsage],
        breaking_change: BreakingChange,
        editor: WorkspaceEditor,
        *,
        toolset: FixToolset | None = None,
    ) -> tuple[list[FileEdit], VerificationReport, int]:
        """Retry retryable edits, re-verifying the full batch after each
        round. Returns the final edits, the final verification report, and
        the number of rounds actually used (may be less than
        ``max_rounds`` if nothing was retryable, or nothing could be
        retried, in an earlier round).
        """
        current_edits = list(edits)
        current_verification = verification
        rounds_used = 0

        for _ in range(self._max_rounds):
            retryable = self._find_retryable(current_edits, current_verification)
            if not retryable:
                break

            retried_any = False
            for edit in retryable:
                file_usage = file_usages_by_relpath.get(edit.relpath)
                if file_usage is None:
                    continue
                feedback = self._build_feedback(edit, current_verification)
                if not feedback:
                    continue

                try:
                    kwargs = {"toolset": toolset} if isinstance(self._fixer, FixAgent) else {}
                    fixed_code, confidence, _llm_call = self._fixer.generate_fix(
                        file_usage, breaking_change, feedback=feedback, **kwargs
                    )
                except (
                    Exception
                ) as exc:  # a third-party LLM client's failure modes aren't ours to enumerate
                    logger.error("retry fix generation failed for %s: %s", edit.relpath, exc)
                    continue

                validation = self._validator.validate(
                    file_usage.file_content, fixed_code, file_usage.filepath
                )
                usages_fixed = file_usage.usage_count if validation.is_valid else 0
                new_edit = editor.write_fix(
                    edit.relpath,
                    fixed_code,
                    confidence=confidence,
                    usages_fixed=usages_fixed,
                    validation=validation,
                )
                new_edit = replace(
                    new_edit,
                    derivation_trace=(
                        f"Retry round {_ + 1}: LLM regenerated after feedback: {feedback[:200]}"
                    ),
                )
                current_edits = [
                    new_edit if e.relpath == new_edit.relpath else e for e in current_edits
                ]
                retried_any = True

            if not retried_any:
                break

            rounds_used += 1
            current_verification = self._verifier.verify(current_edits)
            current_edits = list(current_verification.edits)

        return current_edits, current_verification, rounds_used

    @staticmethod
    def _find_retryable(edits: list[FileEdit], verification: VerificationReport) -> list[FileEdit]:
        """Edits worth retrying: a ``SUSPECT`` with a concrete syntax error,
        or a ``REVERTED`` with a test failure attributed to that exact file.
        """
        retryable = []
        for edit in edits:
            suspect_with_error = (
                edit.verdict == EditVerdict.SUSPECT
                and edit.validation is not None
                and not edit.validation.is_valid
                and edit.validation.error_message
            )
            reverted_with_attribution = (
                edit.verdict == EditVerdict.REVERTED
                and verification.attribution is not None
                and edit.relpath in verification.attribution.by_file
            )
            if suspect_with_error or reverted_with_attribution:
                retryable.append(edit)
        return retryable

    @staticmethod
    def _build_feedback(edit: FileEdit, verification: VerificationReport) -> str:
        """Concrete, LLM-readable description of why ``edit`` was rejected."""
        if (
            edit.verdict == EditVerdict.SUSPECT
            and edit.validation is not None
            and edit.validation.error_message
        ):
            return f"The generated code had a syntax error: {edit.validation.error_message}"

        if edit.verdict == EditVerdict.REVERTED and verification.attribution is not None:
            cases = verification.attribution.by_file.get(edit.relpath, [])
            if cases:
                lines = [f"- {c.name}: {c.message}" if c.message else f"- {c.name}" for c in cases]
                return "The fix caused these tests to fail:\n" + "\n".join(lines)

        return ""
