"""Python fix validation -- the ``.py`` counterpart of
:class:`depfix.validators.javascript.FixValidator`.

Same duck-typed surface (``validate(original, fixed, filepath) ->
ValidationResult``) so :class:`depfix.core.pipeline.FixPipeline` and
:class:`depfix.retry.loop.RetryLoop` take either without knowing which.
Syntax checking is in-process ``ast.parse`` -- no subprocess, no Node-style
availability probe, and a real line number on failure for free.
"""

from __future__ import annotations

import ast
import difflib
import logging

from depfix.core.models import ValidationResult, ValidationStatus

logger = logging.getLogger(__name__)

# Mirrors the JS validator's rationale: a tiny snippet has no meaningful
# line-based change ratio (one focused replacement can be 100% of the lines).
_MIN_LINES_FOR_CHANGE_RATIO = 8


class PythonFixValidator:
    """Validates generated Python fixes: syntax via ``ast.parse``, then the
    same too-much-changed heuristic the JS validator applies."""

    def __init__(self, use_ast_validation: bool = True) -> None:
        self.use_ast_validation = use_ast_validation

    def validate(self, original_code: str, fixed_code: str, filepath: str) -> ValidationResult:
        if original_code.strip() == fixed_code.strip():
            return ValidationResult(
                status=ValidationStatus.NO_CHANGES,
                is_valid=False,
                syntax_valid=True,
                changes_detected=False,
                error_message="No changes were made to the code",
            )

        if self.use_ast_validation:
            try:
                ast.parse(fixed_code)
            except SyntaxError as exc:
                return ValidationResult.syntax_error(
                    message=exc.msg or "Syntax error", line=exc.lineno
                )

        change_ratio = self._calculate_change_ratio(original_code, fixed_code)
        line_count = max(len(original_code.splitlines()), len(fixed_code.splitlines()))
        if line_count >= _MIN_LINES_FOR_CHANGE_RATIO and change_ratio > 0.5:
            logger.warning("Large change ratio detected: %.2f%%", change_ratio * 100)
            return ValidationResult(
                status=ValidationStatus.UNEXPECTED_CHANGES,
                is_valid=False,
                syntax_valid=True,
                changes_detected=True,
                error_message=f"Too many changes detected ({change_ratio:.0%} of lines changed)",
            )

        return ValidationResult.valid()

    @staticmethod
    def _calculate_change_ratio(original: str, fixed: str) -> float:
        original_lines = original.split("\n")
        fixed_lines = fixed.split("\n")
        matcher = difflib.SequenceMatcher(None, original_lines, fixed_lines)
        matching_lines = sum(block.size for block in matcher.get_matching_blocks())
        total_lines = max(len(original_lines), len(fixed_lines))
        if total_lines == 0:
            return 0.0
        return 1.0 - (matching_lines / total_lines)
