"""
Fix validation module.
"""

import difflib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from depfix.core.models import ValidationResult, ValidationStatus
from depfix.validators.typescript import TypeScriptSyntaxValidator, is_typescript

logger = logging.getLogger(__name__)

# A one-to-five-line snippet has no meaningful line-based change ratio: one
# focused call replacement can necessarily replace its only executable line.
_MIN_LINES_FOR_CHANGE_RATIO = 8


class FixValidator:
    """
    Validates generated code fixes.
    """

    def __init__(
        self,
        use_ast_validation: bool = True,
        checkout_root: str | Path | None = None,
    ):
        """
        Initialize the validator.
        """
        self.use_ast_validation = use_ast_validation
        self._node_path = shutil.which("node")
        self._node_available = self._check_node_available()
        self._checkout_root = checkout_root
        self._ts_validator: TypeScriptSyntaxValidator | None = None

    def _check_node_available(self) -> bool:
        """Check if Node.js is available for syntax validation."""
        if self._node_path is None:
            return False
        try:
            # Full resolved path (not "node") so this isn't a partial-path exec.
            result = subprocess.run([self._node_path, "--version"], capture_output=True, timeout=5)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def validate(self, original_code: str, fixed_code: str, filepath: str) -> ValidationResult:
        """
        Validate a generated fix.
        """
        # Check if there are any changes
        if original_code.strip() == fixed_code.strip():
            return ValidationResult(
                status=ValidationStatus.NO_CHANGES,
                is_valid=False,
                syntax_valid=True,
                changes_detected=False,
                error_message="No changes were made to the code",
            )

        # Validate syntax
        syntax_result = self._validate_syntax(fixed_code, filepath)
        if not syntax_result.syntax_valid:
            return syntax_result

        # Check for unexpected large changes
        change_ratio = self._calculate_change_ratio(original_code, fixed_code)
        line_count = max(len(original_code.splitlines()), len(fixed_code.splitlines()))
        if line_count >= _MIN_LINES_FOR_CHANGE_RATIO and change_ratio > 0.5:
            logger.warning(f"Large change ratio detected: {change_ratio:.2%}")
            return ValidationResult(
                status=ValidationStatus.UNEXPECTED_CHANGES,
                is_valid=False,
                syntax_valid=True,
                changes_detected=True,
                error_message=f"Too many changes detected ({change_ratio:.0%} of lines changed)",
            )
        if line_count < _MIN_LINES_FOR_CHANGE_RATIO and change_ratio > 0.5:
            logger.warning(
                "Large change ratio detected in a %d-line file; accepting it because "
                "line-ratio validation is not meaningful for tiny files",
                line_count,
            )

        return ValidationResult.valid()

    def _validate_syntax(self, code: str, filepath: str) -> ValidationResult:
        """
        Validate JavaScript/TypeScript syntax.
        """
        if is_typescript(filepath):
            if self._ts_validator is None:
                self._ts_validator = TypeScriptSyntaxValidator(self._checkout_root)
            return self._ts_validator.validate_syntax(code, filepath)

        if not self._node_available or self._node_path is None:
            logger.warning("Node.js not available, skipping syntax validation")
            return ValidationResult(
                status=ValidationStatus.VALID,
                is_valid=True,
                syntax_valid=True,
                changes_detected=True,
            )

        # Determine file extension
        ext = Path(filepath).suffix

        # Write code to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=ext, delete=False, encoding="utf-8") as f:
            f.write(code)
            temp_path = f.name

        try:
            # Use Node.js to check syntax. Use %-formatting so the JS
            # curly braces don't collide with Python's f-string / .format()
            # placeholders. %r repr-quotes the path safely.
            check_script = """
            const fs = require('fs');
            const code = fs.readFileSync(%r, 'utf-8');
            try {
                new Function(code);
                process.exit(0);
            } catch (e) {
                console.error(e.message);
                process.exit(1);
            }
            """ % (temp_path,)  # noqa: UP031 -- .format()/f-string would collide with the JS braces above

            result = subprocess.run(
                [self._node_path, "-e", check_script], capture_output=True, text=True, timeout=10
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or "Syntax error"
                # Try to extract line number from error
                line_num = self._extract_line_number(error_msg)

                return ValidationResult.syntax_error(message=error_msg, line=line_num)

            return ValidationResult(
                status=ValidationStatus.VALID,
                is_valid=True,
                syntax_valid=True,
                changes_detected=True,
            )

        except subprocess.TimeoutExpired:
            return ValidationResult.syntax_error("Syntax check timed out")
        finally:
            # Clean up temp file
            Path(temp_path).unlink(missing_ok=True)

    def _extract_line_number(self, error_msg: str) -> int | None:
        """Extract line number from error message."""
        import re

        match = re.search(r"line (\d+)", error_msg, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _calculate_change_ratio(self, original: str, fixed: str) -> float:
        """Calculate the ratio of changed lines."""
        original_lines = original.split("\n")
        fixed_lines = fixed.split("\n")

        matcher = difflib.SequenceMatcher(None, original_lines, fixed_lines)
        matching_blocks = matcher.get_matching_blocks()

        matching_lines = sum(block.size for block in matching_blocks)
        total_lines = max(len(original_lines), len(fixed_lines))

        if total_lines == 0:
            return 0.0

        return 1.0 - (matching_lines / total_lines)


def create_unified_diff(original: str, fixed: str, filepath: str) -> str:
    """
    Create a unified diff between original and fixed code.
    """
    original_lines = original.splitlines(keepends=True)
    fixed_lines = fixed.splitlines(keepends=True)

    # Ensure last line has newline
    if original_lines and not original_lines[-1].endswith("\n"):
        original_lines[-1] += "\n"
    if fixed_lines and not fixed_lines[-1].endswith("\n"):
        fixed_lines[-1] += "\n"

    diff = difflib.unified_diff(
        original_lines, fixed_lines, fromfile=f"a/{filepath}", tofile=f"b/{filepath}", lineterm="\n"
    )

    return "".join(diff)
