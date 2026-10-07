from __future__ import annotations

from unittest.mock import MagicMock, patch

from depfix.core.models import ValidationStatus
from depfix.validators.javascript import FixValidator
from depfix.validators.typescript import TypeScriptSyntaxValidator, is_typescript


def _process(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_is_typescript_extensions() -> None:
    assert is_typescript("src/app.ts")
    assert is_typescript("src/app.tsx")
    assert is_typescript("src/app.mts")
    assert not is_typescript("src/app.js")


def test_tsc_accepts_valid_typescript() -> None:
    with patch("depfix.validators.typescript.subprocess.run", return_value=_process(0)):
        result = TypeScriptSyntaxValidator().validate_syntax("const n: number = 1;", "app.ts")
    assert result.status is ValidationStatus.VALID


def test_tsc_reports_typescript_error() -> None:
    output = "check.ts(2,5): error TS1005: ';' expected."
    with patch("depfix.validators.typescript._find_tsc", return_value="/usr/bin/tsc"):
        with patch(
            "depfix.validators.typescript.subprocess.run",
            return_value=_process(1, stdout=output),
        ):
            result = TypeScriptSyntaxValidator().validate_syntax("const n: number = 1", "app.ts")
    assert result.status is ValidationStatus.SYNTAX_ERROR
    assert result.error_line == 2
    assert "TS1005" in (result.error_message or "")


def test_fix_validator_routes_ts_away_from_new_function() -> None:
    validator = FixValidator()
    valid = MagicMock(status=ValidationStatus.VALID, is_valid=True, syntax_valid=True)
    with patch.object(validator, "_ts_validator") as ts_validator:
        ts_validator.validate_syntax.return_value = valid
        result = validator.validate("const n: number = 1;", "const n: number = 2;", "app.ts")
    ts_validator.validate_syntax.assert_called_once()
    assert result.is_valid


def test_js_still_uses_javascript_path() -> None:
    validator = FixValidator(use_ast_validation=False)
    result = validator.validate("const n = 1;", "const n = 2;", "app.js")
    assert result.is_valid
    assert validator._ts_validator is None
