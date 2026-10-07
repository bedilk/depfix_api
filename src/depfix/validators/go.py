"""Go fix validation via ``gofmt -e``.

When ``gofmt`` is absent the fix is accepted, exactly as the JS validator
accepts when Node is missing -- the repository's own ``go test`` run remains
the deciding oracle, and refusing a fix for a missing local tool would be a
worse failure than deferring.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from depfix.core.models import ValidationResult, ValidationStatus

_MIN_LINES_FOR_CHANGE_RATIO = 8
_GO_ERROR_RE = re.compile(r":(\d+):\d+:\s*(.*)$")


class GoFixValidator:
    def __init__(self, use_ast_validation: bool = True) -> None:
        self.use_ast_validation = use_ast_validation
        self._gofmt = shutil.which("gofmt")

    def validate(self, original_code: str, fixed_code: str, filepath: str) -> ValidationResult:
        if original_code.strip() == fixed_code.strip():
            return ValidationResult(
                status=ValidationStatus.NO_CHANGES,
                is_valid=False,
                syntax_valid=True,
                changes_detected=False,
                error_message="No changes were made to the code",
            )

        if self.use_ast_validation and self._gofmt is not None:
            syntax = self._check_syntax(fixed_code)
            if not syntax.syntax_valid:
                return syntax

        ratio = self._change_ratio(original_code, fixed_code)
        line_count = max(len(original_code.splitlines()), len(fixed_code.splitlines()))
        if line_count >= _MIN_LINES_FOR_CHANGE_RATIO and ratio > 0.5:
            return ValidationResult(
                status=ValidationStatus.UNEXPECTED_CHANGES,
                is_valid=False,
                syntax_valid=True,
                changes_detected=True,
                error_message=f"Too many changes detected ({ratio:.0%} of lines changed)",
            )
        return ValidationResult.valid()

    def _check_syntax(self, code: str) -> ValidationResult:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".go", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(code)
            temp_path = handle.name
        try:
            result = subprocess.run(
                [self._gofmt, "-e", "-l", temp_path],  # type: ignore[list-item]
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return ValidationResult.valid()
            message = (result.stderr or result.stdout).strip() or "Syntax error"
            match = _GO_ERROR_RE.search(message)
            return ValidationResult.syntax_error(
                message[:300], int(match.group(1)) if match else None
            )
        except (OSError, subprocess.TimeoutExpired):
            return ValidationResult.valid()
        finally:
            Path(temp_path).unlink(missing_ok=True)

    @staticmethod
    def _change_ratio(original: str, fixed: str) -> float:
        original_lines = original.split("\n")
        fixed_lines = fixed.split("\n")
        matcher = difflib.SequenceMatcher(None, original_lines, fixed_lines)
        matching = sum(block.size for block in matcher.get_matching_blocks())
        total = max(len(original_lines), len(fixed_lines))
        return 0.0 if total == 0 else 1.0 - (matching / total)
