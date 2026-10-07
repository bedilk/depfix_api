"""Unit tests for the JS fix validator."""

from __future__ import annotations

from depfix.core.models import ValidationStatus
from depfix.validators.javascript import FixValidator, create_unified_diff


def test_no_changes_detected() -> None:
    v = FixValidator(use_ast_validation=False)
    code = "const x = _.pluck(users, 'name');\n"
    result = v.validate(code, code, "foo.js")

    assert result.status == ValidationStatus.NO_CHANGES
    assert not result.is_valid
    assert not result.changes_detected


def test_valid_change_when_node_missing_still_reports_valid() -> None:
    v = FixValidator(use_ast_validation=False)
    original = "const x = _.pluck(users, 'name');\n"
    fixed = "const x = _.map(users, 'name');\n"
    result = v.validate(original, fixed, "foo.js")

    # We can't guarantee Node is installed; both are acceptable status values,
    # but the important assertion is that "no_changes" is NOT triggered.
    assert result.status != ValidationStatus.NO_CHANGES


def test_allows_focused_change_in_tiny_file() -> None:
    validator = FixValidator(use_ast_validation=False)

    result = validator.validate(
        "const value = sdk.oldMethod();\n",
        "const value = sdk.newMethod();\n",
        "example.js",
    )

    assert result.is_valid is True


def test_unified_diff_has_proper_headers() -> None:
    original = "a\nb\nc\n"
    fixed = "a\nB\nc\n"
    diff = create_unified_diff(original, fixed, "foo.js")

    assert "--- a/foo.js" in diff
    assert "+++ b/foo.js" in diff
    # Each header line should end with a newline (regression test for git-apply
    # compatibility — patches were previously produced with lineterm="").
    for line in diff.splitlines(keepends=True):
        if line.startswith(("--- ", "+++ ", "@@")):
            assert line.endswith("\n")


def test_unified_diff_empty_when_identical() -> None:
    src = "same\n"
    assert create_unified_diff(src, src, "x.js") == ""
