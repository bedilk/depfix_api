"""Unit tests for :mod:`depfix.verify.attribution`.

Exercises the four trust-order branches documented on the module (self-
touched test file, import graph, filename convention, sole-candidate
elimination) plus the two helpers directly.
"""

from __future__ import annotations

from pathlib import Path

from depfix.verify.attribution import (
    _candidate_source_names,
    _imports_touched_file,
    attribute_failures,
)
from depfix.verify.models import TestCase, TestStatus


def _failure(name: str, file: str) -> TestCase:
    return TestCase(name=name, file=file, status=TestStatus.FAILED)


# -- attribute_failures: branch 1 -- test file itself is touched ----------------


def test_attributes_to_test_file_when_the_test_file_itself_was_touched(tmp_path: Path) -> None:
    (tmp_path / "chat.test.js").write_text("test('x', () => {});\n", encoding="utf-8")

    result = attribute_failures(
        [_failure("does a thing", "chat.test.js")],
        touched_relpaths=("chat.test.js",),
        checkout_root=tmp_path,
    )

    assert result.by_file == {"chat.test.js": [result.by_file["chat.test.js"][0]]}
    assert result.unattributed == []


# -- branch 2 -- import graph -----------------------------------------------------


def test_attributes_via_relative_require_to_the_touched_source_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "chat.js").write_text("module.exports = {};\n", encoding="utf-8")
    (tmp_path / "src" / "chat.test.js").write_text(
        'const { x } = require("./chat");\ntest("x", () => {});\n', encoding="utf-8"
    )

    result = attribute_failures(
        [_failure("uses chat", "src/chat.test.js")],
        touched_relpaths=("src/chat.js", "src/other.js"),
        checkout_root=tmp_path,
    )

    assert "src/chat.js" in result.by_file
    assert result.unattributed == []


def test_import_graph_ambiguous_when_test_imports_multiple_touched_files_falls_through(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.js").write_text("module.exports = {};\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("module.exports = {};\n", encoding="utf-8")
    (tmp_path / "index.test.js").write_text('require("./a");\nrequire("./b");\n', encoding="utf-8")

    result = attribute_failures(
        [_failure("uses both", "index.test.js")],
        touched_relpaths=("a.js", "b.js"),
        checkout_root=tmp_path,
    )

    # Ambiguous import match falls through; "index" doesn't match either
    # touched filename by convention, and there's more than one touched
    # file, so this ends up unattributed.
    assert result.by_file == {}
    assert len(result.unattributed) == 1


# -- branch 3 -- filename convention ----------------------------------------------


def test_attributes_via_test_dot_suffix_naming_convention_when_no_import_found(
    tmp_path: Path,
) -> None:
    (tmp_path / "chat.test.js").write_text("test('x', () => {});\n", encoding="utf-8")

    result = attribute_failures(
        [_failure("does a thing", "chat.test.js")],
        touched_relpaths=("chat.js", "other.js"),
        checkout_root=tmp_path,
    )

    assert "chat.js" in result.by_file
    assert result.unattributed == []


def test_attributes_via_dunder_tests_directory_convention(tmp_path: Path) -> None:
    (tmp_path / "__tests__").mkdir()
    (tmp_path / "__tests__" / "chat.js").write_text("test('x', () => {});\n", encoding="utf-8")

    result = attribute_failures(
        [_failure("does a thing", "__tests__/chat.js")],
        touched_relpaths=("chat.js", "other.js"),
        checkout_root=tmp_path,
    )

    assert "chat.js" in result.by_file


# -- branch 4 -- sole-candidate elimination ---------------------------------------


def test_sole_touched_file_gets_every_new_failure_with_no_other_signal(tmp_path: Path) -> None:
    result = attribute_failures(
        [_failure("some failing test", "unrelated.test.js")],
        touched_relpaths=("only.js",),
        checkout_root=tmp_path,
    )

    assert result.by_file == {"only.js": [result.by_file["only.js"][0]]}


# -- branch 5 -- unattributed -------------------------------------------------------


def test_unattributed_when_multiple_touched_files_and_no_matching_signal(tmp_path: Path) -> None:
    result = attribute_failures(
        [_failure("some failing test", "unrelated.test.js")],
        touched_relpaths=("a.js", "b.js"),
        checkout_root=tmp_path,
    )

    assert result.by_file == {}
    assert len(result.unattributed) == 1


def test_case_with_no_file_field_skips_straight_to_sole_or_unattributed(tmp_path: Path) -> None:
    result = attribute_failures(
        [_failure("no file info", "")],
        touched_relpaths=("a.js", "b.js"),
        checkout_root=tmp_path,
    )

    assert result.unattributed == [result.unattributed[0]]


def test_multiple_failures_are_grouped_under_the_same_attributed_file(tmp_path: Path) -> None:
    (tmp_path / "chat.test.js").write_text("test('x', () => {});\n", encoding="utf-8")

    result = attribute_failures(
        [
            _failure("first failing test", "chat.test.js"),
            _failure("second failing test", "chat.test.js"),
        ],
        touched_relpaths=("chat.js",),
        checkout_root=tmp_path,
    )

    assert len(result.by_file["chat.js"]) == 2


# -- _candidate_source_names -------------------------------------------------------


def test_candidate_source_names_from_test_dot_suffix() -> None:
    candidates = _candidate_source_names("chat.test.js")

    assert str(Path("chat.js")) in candidates
    assert str(Path("chat.ts")) in candidates


def test_candidate_source_names_from_spec_dot_suffix() -> None:
    candidates = _candidate_source_names("chat.spec.ts")

    assert str(Path("chat.js")) in candidates


def test_candidate_source_names_from_dunder_tests_directory() -> None:
    candidates = _candidate_source_names("src/__tests__/chat.js")

    assert str(Path("src/chat.js")) in candidates


def test_candidate_source_names_empty_when_no_convention_matches() -> None:
    assert _candidate_source_names("helpers.js") == []


# -- _imports_touched_file ----------------------------------------------------------


def test_imports_touched_file_true_for_matching_relative_require(tmp_path: Path) -> None:
    (tmp_path / "chat.js").write_text("module.exports = {};\n", encoding="utf-8")
    test_file = tmp_path / "chat.test.js"
    test_file.write_text('require("./chat");\n', encoding="utf-8")

    assert _imports_touched_file(test_file, {(tmp_path / "chat.js").resolve()}) is True


def test_imports_touched_file_false_when_no_relative_import_matches(tmp_path: Path) -> None:
    (tmp_path / "chat.js").write_text("module.exports = {};\n", encoding="utf-8")
    test_file = tmp_path / "unrelated.test.js"
    test_file.write_text('require("some-package");\n', encoding="utf-8")

    assert _imports_touched_file(test_file, {(tmp_path / "chat.js").resolve()}) is False


def test_imports_touched_file_false_when_file_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.test.js"

    assert _imports_touched_file(missing, {(tmp_path / "chat.js").resolve()}) is False
