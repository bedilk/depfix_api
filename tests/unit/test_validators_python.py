"""Unit tests for :class:`depfix.validators.python.PythonFixValidator`.

Syntax checking is in-process ``ast.parse``, so these tests need no
monkeypatching and no subprocess -- they exercise the validator's four
outcomes (no-changes, syntax error, too-much-changed, valid) and the
small-file carve-out on the change-ratio heuristic.
"""

from __future__ import annotations

from depfix.core.models import ValidationStatus
from depfix.validators.python import PythonFixValidator


def _long_module(prefix: str, offset: int = 0) -> str:
    """A 10-line module whose every line differs from the other prefix's."""
    return "".join(f"{prefix}{i} = {i + offset}\n" for i in range(10))


# -- no changes ---------------------------------------------------------------------


def test_identical_code_is_reported_as_no_changes() -> None:
    validator = PythonFixValidator()
    code = "value = client.old_method()\n"

    result = validator.validate(code, code, "app.py")

    assert result.status == ValidationStatus.NO_CHANGES
    assert result.is_valid is False
    assert result.changes_detected is False
    assert result.error_message == "No changes were made to the code"


def test_whitespace_only_difference_is_reported_as_no_changes() -> None:
    validator = PythonFixValidator()
    original = "value = client.old_method()\n"
    fixed = "\n  value = client.old_method()  \n\n"

    result = validator.validate(original, fixed, "app.py")

    # The comparison is on `.strip()`ed text, so surrounding whitespace alone
    # does not count as a change.
    assert result.status == ValidationStatus.NO_CHANGES


# -- syntax errors ------------------------------------------------------------------


def test_syntax_error_in_fixed_code_is_reported_with_a_line_number() -> None:
    validator = PythonFixValidator()
    original = "value = client.old_method()\n"
    fixed = "value = client.new_method(\n"

    result = validator.validate(original, fixed, "app.py")

    assert result.status == ValidationStatus.SYNTAX_ERROR
    assert result.is_valid is False
    assert result.syntax_valid is False
    assert result.error_line == 1
    assert result.error_message


def test_syntax_error_line_number_points_at_the_offending_line() -> None:
    validator = PythonFixValidator()
    original = "a = 1\nb = 2\nc = 3\n"
    fixed = "a = 1\nb = 2\ndef c(:\n"

    result = validator.validate(original, fixed, "app.py")

    assert result.status == ValidationStatus.SYNTAX_ERROR
    assert result.error_line == 3


def test_ast_validation_can_be_disabled() -> None:
    validator = PythonFixValidator(use_ast_validation=False)
    original = "a = 1\n"
    fixed = "def c(:\n"

    result = validator.validate(original, fixed, "app.py")

    # With the syntax gate off, unparseable output falls straight through to
    # the change-ratio heuristic, which accepts it (tiny file).
    assert result.status == ValidationStatus.VALID
    assert result.is_valid is True


# -- change-ratio heuristic ---------------------------------------------------------


def test_focused_change_is_valid() -> None:
    validator = PythonFixValidator()
    original = (
        "import stripe\n\n\ndef charge(amount):\n    return stripe.Charge.create(amount=amount)\n"
    )
    fixed = (
        "import stripe\n\n\n"
        "def charge(amount):\n"
        "    return stripe.PaymentIntent.create(amount=amount)\n"
    )

    result = validator.validate(original, fixed, "billing.py")

    assert result.status == ValidationStatus.VALID
    assert result.is_valid is True
    assert result.syntax_valid is True
    assert result.changes_detected is True


def test_wholesale_rewrite_of_a_long_file_is_rejected() -> None:
    validator = PythonFixValidator()

    result = validator.validate(_long_module("a"), _long_module("b", offset=100), "app.py")

    assert result.status == ValidationStatus.UNEXPECTED_CHANGES
    assert result.is_valid is False
    assert result.syntax_valid is True
    assert result.changes_detected is True
    assert "Too many changes detected" in (result.error_message or "")


def test_wholesale_rewrite_of_a_tiny_file_is_accepted() -> None:
    validator = PythonFixValidator()

    result = validator.validate("x = sdk.old()\n", "x = sdk.new()\n", "app.py")

    # One line, 100% changed -- below the 8-line floor the ratio heuristic is
    # meaningless, so the fix is allowed through.
    assert result.status == ValidationStatus.VALID
    assert result.is_valid is True


def test_large_but_mostly_unchanged_file_is_accepted() -> None:
    validator = PythonFixValidator()
    original = _long_module("a")
    fixed = original.replace("a3 = 3", "a3 = 33")

    result = validator.validate(original, fixed, "app.py")

    assert result.status == ValidationStatus.VALID
    assert result.is_valid is True


def test_change_ratio_is_zero_for_identical_line_sequences() -> None:
    assert PythonFixValidator._calculate_change_ratio("a\nb\nc\n", "a\nb\nc\n") == 0.0


def test_change_ratio_is_one_for_fully_disjoint_line_sequences() -> None:
    ratio = PythonFixValidator._calculate_change_ratio("a", "b")

    assert ratio == 1.0
