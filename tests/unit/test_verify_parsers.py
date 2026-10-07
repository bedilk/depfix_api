"""Unit tests for :mod:`depfix.verify.parsers`.

Covers the structured per-framework parsers (Jest/Vitest JSON, Mocha JSON,
Node's TAP) plus the aggregate-count regex fallback, and the dispatch/
degrade logic in :func:`parse_output`.
"""

from __future__ import annotations

import json

from depfix.verify.models import TestFramework, TestStatus
from depfix.verify.parsers import (
    parse_fallback,
    parse_jest_json,
    parse_mocha_json,
    parse_output,
    parse_tap,
    parse_vitest_json,
)

# -- parse_jest_json / parse_vitest_json ----------------------------------------


def test_parse_jest_json_extracts_name_file_and_status() -> None:
    raw = json.dumps(
        {
            "testResults": [
                {
                    "name": "chat.test.js",
                    "assertionResults": [
                        {"fullName": "does a thing", "status": "passed"},
                        {"fullName": "does another thing", "status": "failed"},
                    ],
                }
            ]
        }
    )

    cases = parse_jest_json(raw)

    assert len(cases) == 2
    assert cases[0].name == "does a thing"
    assert cases[0].file == "chat.test.js"
    assert cases[0].status == TestStatus.PASSED
    assert cases[1].status == TestStatus.FAILED


def test_parse_jest_json_maps_pending_skipped_todo_to_skipped() -> None:
    raw = json.dumps(
        {
            "testResults": [
                {
                    "name": "f.js",
                    "assertionResults": [
                        {"fullName": "a", "status": "pending"},
                        {"fullName": "b", "status": "skipped"},
                        {"fullName": "c", "status": "todo"},
                    ],
                }
            ]
        }
    )

    cases = parse_jest_json(raw)

    assert all(c.status == TestStatus.SKIPPED for c in cases)


def test_parse_jest_json_unrecognized_status_defaults_to_failed() -> None:
    raw = json.dumps(
        {
            "testResults": [
                {"name": "f.js", "assertionResults": [{"fullName": "a", "status": "weird"}]}
            ]
        }
    )

    cases = parse_jest_json(raw)

    assert cases[0].status == TestStatus.FAILED


def test_parse_jest_json_falls_back_to_title_and_test_file_path() -> None:
    raw = json.dumps(
        {
            "testResults": [
                {
                    "testFilePath": "/abs/f.js",
                    "assertionResults": [{"title": "a", "status": "passed"}],
                }
            ]
        }
    )

    cases = parse_jest_json(raw)

    assert cases[0].name == "a"
    assert cases[0].file == "/abs/f.js"


def test_parse_jest_json_uses_unnamed_placeholder_when_no_title(tmp_path) -> None:
    raw = json.dumps(
        {"testResults": [{"name": "f.js", "assertionResults": [{"status": "failed"}]}]}
    )

    cases = parse_jest_json(raw)

    assert cases[0].name == "<unnamed>"


def test_parse_jest_json_joins_failure_messages() -> None:
    raw = json.dumps(
        {
            "testResults": [
                {
                    "name": "f.js",
                    "assertionResults": [
                        {
                            "fullName": "a",
                            "status": "failed",
                            "failureMessages": ["boom", "again"],
                        }
                    ],
                }
            ]
        }
    )

    cases = parse_jest_json(raw)

    assert cases[0].message == "boom\nagain"


def test_parse_vitest_json_is_the_same_function_as_jest() -> None:
    assert parse_vitest_json is parse_jest_json


# -- parse_mocha_json --------------------------------------------------------------


def test_parse_mocha_json_passes_and_failures() -> None:
    raw = json.dumps(
        {
            "passes": [{"fullTitle": "a passes", "file": "a.js"}],
            "failures": [{"fullTitle": "b fails", "file": "b.js", "err": {"message": "boom"}}],
            "pending": [],
        }
    )

    cases = parse_mocha_json(raw)

    assert len(cases) == 2
    passed = next(c for c in cases if c.status == TestStatus.PASSED)
    failed = next(c for c in cases if c.status == TestStatus.FAILED)
    assert passed.name == "a passes"
    assert passed.file == "a.js"
    assert failed.message == "boom"


def test_parse_mocha_json_pending_treated_as_skipped_when_not_seen_elsewhere() -> None:
    raw = json.dumps(
        {"passes": [], "failures": [], "pending": [{"fullTitle": "c pending", "file": "c.js"}]}
    )

    cases = parse_mocha_json(raw)

    assert len(cases) == 1
    assert cases[0].status == TestStatus.SKIPPED


def test_parse_mocha_json_pending_entry_deduped_against_passes() -> None:
    """A (name, file) already counted in passes/failures must not also be
    emitted as a duplicate SKIPPED case from the pending list."""
    raw = json.dumps(
        {
            "passes": [{"fullTitle": "dup", "file": "d.js"}],
            "failures": [],
            "pending": [{"fullTitle": "dup", "file": "d.js"}],
        }
    )

    cases = parse_mocha_json(raw)

    assert len(cases) == 1
    assert cases[0].status == TestStatus.PASSED


def test_parse_mocha_json_err_not_a_dict_yields_empty_message() -> None:
    raw = json.dumps({"passes": [], "failures": [{"fullTitle": "x", "err": "not-a-dict"}]})

    cases = parse_mocha_json(raw)

    assert cases[0].message == ""


def test_parse_mocha_json_falls_back_to_title_and_unnamed() -> None:
    raw = json.dumps({"passes": [{"title": "only-title"}], "failures": []})

    cases = parse_mocha_json(raw)

    assert cases[0].name == "only-title"
    assert cases[0].file == ""


# -- parse_tap ----------------------------------------------------------------------


def test_parse_tap_parses_ok_and_not_ok_lines() -> None:
    raw = "ok 1 - first test\nnot ok 2 - second test\n"

    cases = parse_tap(raw)

    assert len(cases) == 2
    assert cases[0].name == "first test"
    assert cases[0].status == TestStatus.PASSED
    assert cases[1].name == "second test"
    assert cases[1].status == TestStatus.FAILED


def test_parse_tap_ignores_unrelated_lines() -> None:
    raw = "TAP version 13\n# Subtest: foo\nok 1 - passes\n1..1\n"

    cases = parse_tap(raw)

    assert len(cases) == 1
    assert cases[0].name == "passes"


def test_parse_tap_applies_file_hint_to_subsequent_lines() -> None:
    raw = "    file: chat.test.js\nok 1 - uses hint\nnot ok 2 - also uses hint\n"

    cases = parse_tap(raw)

    assert cases[0].file == "chat.test.js"
    assert cases[1].file == "chat.test.js"


def test_parse_tap_defaults_to_empty_file_without_hint() -> None:
    raw = "ok 1 - no hint here\n"

    cases = parse_tap(raw)

    assert cases[0].file == ""


# -- parse_fallback -----------------------------------------------------------------


def test_parse_fallback_recovers_mocha_style_counts() -> None:
    raw = "10 passing (12ms)\n2 failing\n"

    cases = parse_fallback(raw)

    passed = [c for c in cases if c.status == TestStatus.PASSED]
    failed = [c for c in cases if c.status == TestStatus.FAILED]
    assert len(passed) == 10
    assert len(failed) == 2


def test_parse_fallback_recovers_jest_style_counts() -> None:
    raw = "Tests:       2 failed, 8 passed, 10 total\n"

    cases = parse_fallback(raw)

    assert len([c for c in cases if c.status == TestStatus.FAILED]) == 2
    assert len([c for c in cases if c.status == TestStatus.PASSED]) == 8


def test_parse_fallback_uses_first_matching_pattern_per_status_only() -> None:
    """Both a mocha-style ``N failing`` and a jest-style ``Tests: N failed``
    pattern could match the same blob -- only the first pattern per status
    in ``_COUNT_PATTERNS`` order should be counted, not summed."""
    raw = "3 failing\nTests: 99 failed, 1 passed, 100 total\n"

    cases = parse_fallback(raw)

    assert len([c for c in cases if c.status == TestStatus.FAILED]) == 3


def test_parse_fallback_returns_empty_list_when_nothing_recognizable() -> None:
    assert parse_fallback("complete gibberish, no counts here") == []


def test_parse_fallback_synthetic_names_are_unique_per_index() -> None:
    cases = parse_fallback("2 failing\n")

    names = {c.name for c in cases}
    assert len(names) == 2
    assert all("<aggregate failed #" in n for n in names)


# -- parse_output ---------------------------------------------------------------------


def test_parse_output_dispatches_to_jest_parser_and_reports_no_fallback() -> None:
    raw = json.dumps(
        {
            "testResults": [
                {"name": "f.js", "assertionResults": [{"fullName": "a", "status": "passed"}]}
            ]
        }
    )

    cases, error, used_fallback = parse_output(TestFramework.JEST, stdout=raw, stderr="")

    assert len(cases) == 1
    assert error == ""
    assert used_fallback is False


def test_parse_output_prefers_stdout_over_stderr_when_stdout_nonempty() -> None:
    raw = json.dumps(
        {
            "testResults": [
                {"name": "f.js", "assertionResults": [{"fullName": "a", "status": "passed"}]}
            ]
        }
    )

    cases, _, used_fallback = parse_output(TestFramework.JEST, stdout=raw, stderr="garbage")

    assert len(cases) == 1
    assert used_fallback is False


def test_parse_output_uses_stderr_when_stdout_blank() -> None:
    raw = json.dumps(
        {
            "testResults": [
                {"name": "f.js", "assertionResults": [{"fullName": "a", "status": "passed"}]}
            ]
        }
    )

    cases, _, used_fallback = parse_output(TestFramework.JEST, stdout="   ", stderr=raw)

    assert len(cases) == 1
    assert used_fallback is False


def test_parse_output_falls_back_on_json_decode_error() -> None:
    cases, error, used_fallback = parse_output(
        TestFramework.JEST, stdout="not json at all, 3 failing", stderr=""
    )

    assert used_fallback is True
    assert "structured parse failed" in error
    assert any(c.status == TestStatus.FAILED for c in cases)


def test_parse_output_falls_back_when_structured_parser_yields_no_cases() -> None:
    empty_jest = json.dumps({"testResults": []})

    cases, _error, used_fallback = parse_output(
        TestFramework.JEST, stdout=empty_jest + "\n5 passing\n", stderr=""
    )

    assert used_fallback is True
    assert len(cases) == 5


def test_parse_output_reports_no_recognizable_test_output_when_fallback_also_empty() -> None:
    cases, error, used_fallback = parse_output(
        TestFramework.UNKNOWN, stdout="nothing useful", stderr=""
    )

    assert cases == []
    assert used_fallback is True
    assert error == "no recognizable test output"


def test_parse_output_unknown_framework_goes_straight_to_fallback() -> None:
    cases, _error, used_fallback = parse_output(
        TestFramework.UNKNOWN, stdout="2 passing\n", stderr=""
    )

    assert used_fallback is True
    assert len(cases) == 2


def test_parse_output_node_test_uses_tap_parser() -> None:
    raw = "ok 1 - works\n"

    cases, _error, used_fallback = parse_output(TestFramework.NODE_TEST, stdout=raw, stderr="")

    assert used_fallback is False
    assert cases[0].name == "works"
