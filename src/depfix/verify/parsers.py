"""Parsers turning a test runner's raw output into :class:`~depfix.verify.models.TestCase`\\ s.

Structured parsers (Jest/Vitest JSON, Mocha JSON, Node's TAP) come first
and are preferred whenever the repo's configured reporter produces one --
they give stable per-test identities, which is what
:mod:`depfix.verify.attribution` needs to blame a specific file for a
specific new failure. :func:`parse_fallback` exists for everything else
(a compound `"test": "eslint && jest"` script, a custom reporter, output
we failed to parse) and can only recover aggregate pass/fail *counts*,
which :mod:`depfix.verify.verifier` treats as a signal to degrade to
"SUSPECT the whole edit" rather than trust a specific attribution.
"""

from __future__ import annotations

import json
import re

import defusedxml.ElementTree as ElementTree
from defusedxml.ElementTree import ParseError as _XML_PARSE_ERROR

from depfix.verify.models import TestCase, TestFramework, TestStatus


def parse_jest_json(raw: str) -> list[TestCase]:
    """Parse Jest's ``--json`` reporter output. Vitest's ``--reporter=json``
    output is shape-compatible with Jest's, so :data:`parse_vitest_json` is
    just an alias below rather than a second implementation."""
    data = json.loads(raw)
    cases: list[TestCase] = []
    for suite in data.get("testResults", []):
        file = suite.get("name", "") or suite.get("testFilePath", "")
        for result in suite.get("assertionResults", []):
            status_str = result.get("status", "failed")
            status = _JEST_STATUS_MAP.get(status_str, TestStatus.FAILED)
            title = result.get("fullName") or result.get("title") or "<unnamed>"
            message = "\n".join(result.get("failureMessages", []) or [])
            cases.append(TestCase(name=title, file=file, status=status, message=message))
    return cases


_JEST_STATUS_MAP = {
    "passed": TestStatus.PASSED,
    "failed": TestStatus.FAILED,
    "pending": TestStatus.SKIPPED,
    "skipped": TestStatus.SKIPPED,
    "todo": TestStatus.SKIPPED,
}

#: Vitest's JSON reporter is jest-compatible in shape.
parse_vitest_json = parse_jest_json


def parse_vitest_verbose(raw: str) -> list[TestCase]:
    """Best-effort parser for Vitest's default Unicode reporter."""
    cases: list[TestCase] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith(("✓", "√")):
            cases.append(TestCase(name=line.lstrip("✓√ ").strip(), status=TestStatus.PASSED))
        elif line.startswith(("×", "✕")):  # noqa: RUF001
            cases.append(TestCase(name=line.lstrip("×✕ ").strip(), status=TestStatus.FAILED))  # noqa: RUF001
    return cases


def parse_mocha_json(raw: str) -> list[TestCase]:
    """Parse Mocha's ``--reporter json`` output. Mocha's JSON has no notion
    of "skipped as distinct from pending" in the flat pass/fail lists, so
    anything not in ``passes``/``failures`` is treated as SKIPPED."""
    data = json.loads(raw)
    cases: list[TestCase] = []
    seen: set[tuple[str, str]] = set()

    for entry in data.get("passes", []):
        name, file = _mocha_identity(entry)
        cases.append(TestCase(name=name, file=file, status=TestStatus.PASSED))
        seen.add((name, file))

    for entry in data.get("failures", []):
        name, file = _mocha_identity(entry)
        message = (
            entry.get("err", {}).get("message", "") if isinstance(entry.get("err"), dict) else ""
        )
        cases.append(TestCase(name=name, file=file, status=TestStatus.FAILED, message=message))
        seen.add((name, file))

    for entry in data.get("pending", []):
        name, file = _mocha_identity(entry)
        if (name, file) in seen:
            continue
        cases.append(TestCase(name=name, file=file, status=TestStatus.SKIPPED))

    return cases


def _mocha_identity(entry: dict) -> tuple[str, str]:
    name = entry.get("fullTitle") or entry.get("title") or "<unnamed>"
    file = entry.get("file", "") or ""
    return name, file


#: Matches Node's ``--test`` TAP output lines like ``ok 1 - some test name``
#: or ``not ok 2 - some other test name``.
_TAP_LINE_RE = re.compile(r"^(?P<ok>ok|not ok)\s+\d+\s+-\s+(?P<name>.+?)\s*$")
#: TAP diagnostics sometimes carry a ``# file:`` hint via YAML-ish blocks;
#: this is best-effort only, absence just means ``file=""``.
_TAP_FILE_HINT_RE = re.compile(r"^\s*file:\s*(?P<file>.+?)\s*$")


def parse_tap(raw: str) -> list[TestCase]:
    """Parse the TAP dialect emitted by ``node --test``."""
    cases: list[TestCase] = []
    current_file = ""
    for line in raw.splitlines():
        file_hint = _TAP_FILE_HINT_RE.match(line)
        if file_hint:
            current_file = file_hint.group("file")
            continue
        match = _TAP_LINE_RE.match(line)
        if not match:
            continue
        status = TestStatus.PASSED if match.group("ok") == "ok" else TestStatus.FAILED
        cases.append(TestCase(name=match.group("name"), file=current_file, status=status))
    return cases


def parse_junit_xml(raw: str) -> list[TestCase]:
    """Parse a JUnit XML report -- pytest's ``--junit-xml`` output, and the
    lingua franca of most other ecosystems' test runners.

    Identity is ``classname::name`` (pytest encodes the module/class path
    in ``classname``), stable across runs the same way Jest's ``fullName``
    is. A ``<failure>``/``<error>`` child means FAILED; ``<skipped>`` means
    SKIPPED; a bare ``<testcase>`` passed.
    """
    root = ElementTree.fromstring(raw)
    cases: list[TestCase] = []
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or "<unnamed>"
        title = f"{classname}::{name}" if classname else name
        file = case.get("file") or ""
        failure = case.find("failure")
        error = case.find("error")
        if failure is not None or error is not None:
            node = failure if failure is not None else error
            message = (node.get("message") or "") if node is not None else ""
            cases.append(TestCase(name=title, file=file, status=TestStatus.FAILED, message=message))
        elif case.find("skipped") is not None:
            cases.append(TestCase(name=title, file=file, status=TestStatus.SKIPPED))
        else:
            cases.append(TestCase(name=title, file=file, status=TestStatus.PASSED))
    return cases


def parse_rspec_json(raw: str) -> list[TestCase]:
    """Parse RSpec's ``--format json`` output.

    Identity is ``file_path::full_description``, stable across runs the same
    way Jest's ``fullName`` is. RSpec statuses are passed/failed/pending.
    """
    data = json.loads(raw)
    cases: list[TestCase] = []
    for example in data.get("examples", []):
        status_str = example.get("status", "failed")
        status = _RSPEC_STATUS_MAP.get(status_str, TestStatus.FAILED)
        name = example.get("full_description") or example.get("description") or "<unnamed>"
        file = example.get("file_path", "") or ""
        message = ""
        if status is TestStatus.FAILED:
            exc = example.get("exception") or {}
            message = str(exc.get("message", "")) if isinstance(exc, dict) else ""
        cases.append(TestCase(name=name, file=file, status=status, message=message))
    return cases


_RSPEC_STATUS_MAP = {
    "passed": TestStatus.PASSED,
    "failed": TestStatus.FAILED,
    "pending": TestStatus.SKIPPED,
}


_GO_ACTION_STATUS = {
    "pass": TestStatus.PASSED,
    "fail": TestStatus.FAILED,
    "skip": TestStatus.SKIPPED,
}

_GO_FRAMING_PREFIXES = ("=== RUN", "=== PAUSE", "=== CONT", "=== NAME", "--- PASS", "--- SKIP")


def parse_go_test_json(raw: str) -> list[TestCase]:
    """Parse ``go test -json`` output.

    The format is newline-delimited events, not a document: one object per
    action, with the terminal ``pass``/``fail``/``skip`` arriving after any
    number of ``output`` events. Package-level events (no ``Test`` field) are
    dropped -- a package that fails to build has no per-test identity to diff.
    """
    statuses: dict[tuple[str, str], TestStatus] = {}
    messages: dict[tuple[str, str], list[str]] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        test = event.get("Test")
        if not test:
            continue
        key = (str(event.get("Package") or ""), str(test))
        action = event.get("Action")
        if action == "output":
            text = str(event.get("Output") or "").strip()
            if text and not text.startswith(_GO_FRAMING_PREFIXES):
                messages.setdefault(key, []).append(text)
        elif action in _GO_ACTION_STATUS:
            statuses[key] = _GO_ACTION_STATUS[action]

    return [
        TestCase(
            name=name,
            file=package,
            status=status,
            message=(
                "\n".join(messages.get((package, name), []))[:2000]
                if status is TestStatus.FAILED
                else ""
            ),
        )
        for (package, name), status in statuses.items()
    ]


_GO_VERBOSE_RE = re.compile(r"^\s*---\s+(?P<status>PASS|FAIL|SKIP):\s+(?P<name>\S+)")

_GO_VERBOSE_STATUS = {
    "PASS": TestStatus.PASSED,
    "FAIL": TestStatus.FAILED,
    "SKIP": TestStatus.SKIPPED,
}


def parse_go_test_verbose(raw: str) -> list[TestCase]:
    """Best-effort parser for plain ``go test -v`` output.

    Used only when ``-json`` produced nothing usable. Carries no package, so
    identities are bare test names -- weaker than the JSON path.
    """
    cases: list[TestCase] = []
    for line in raw.splitlines():
        match = _GO_VERBOSE_RE.match(line)
        if match:
            cases.append(
                TestCase(
                    name=match.group("name"),
                    status=_GO_VERBOSE_STATUS[match.group("status")],
                )
            )
    return cases


#: Best-effort aggregate counters for the human-readable default reporters
#: of Jest/Vitest/Mocha/pytest/RSpec, e.g. "Tests: 2 failed, 8 passed, 10 total"
#: (Jest), "10 passing (1s)" / "2 failing" (Mocha), pytest's
#: "==== 2 failed, 8 passed in 1.23s ====" summary line, or RSpec's
#: "3 examples, 1 failure" (note: "examples" is total, not passed count).
_COUNT_PATTERNS: tuple[tuple[TestStatus, re.Pattern[str]], ...] = (
    (TestStatus.FAILED, re.compile(r"(?P<n>\d+)\s+failures?")),
    (TestStatus.FAILED, re.compile(r"(?P<n>\d+)\s+failing")),
    (TestStatus.FAILED, re.compile(r"Tests:.*?(?P<n>\d+)\s+failed")),
    (TestStatus.FAILED, re.compile(r"(?P<n>\d+)\s+failed")),
    (TestStatus.PASSED, re.compile(r"(?P<n>\d+)\s+passing")),
    (TestStatus.PASSED, re.compile(r"Tests:.*?(?P<n>\d+)\s+passed")),
    (TestStatus.PASSED, re.compile(r"(?P<n>\d+)\s+passed")),
    (TestStatus.PASSED, re.compile(r"(?P<n>\d+)\s+examples?")),  # RSpec total; see note
)


def parse_fallback(raw: str) -> list[TestCase]:
    """Recover only aggregate pass/fail *counts* from unrecognized output,
    as synthetic ``<aggregate {status} #N>`` cases.

    These synthetic identities are NOT stable across runs in the way real
    test identities are -- two different tests that happen to produce the
    same failure count in two different runs are indistinguishable here.
    Callers (:mod:`depfix.verify.verifier`) must treat any comparison
    involving fallback-parsed results as degraded, not as a reliable
    per-test diff. See :class:`depfix.verify.models.TestRunResult`.
    """
    cases: list[TestCase] = []
    counted: dict[TestStatus, int] = {}
    for status, pattern in _COUNT_PATTERNS:
        if status in counted:
            continue
        match = pattern.search(raw)
        if match:
            counted[status] = int(match.group("n"))

    for status, count in counted.items():
        for i in range(count):
            cases.append(TestCase(name=f"<aggregate {status.value} #{i}>", status=status))
    return cases


def parse_output(
    framework: TestFramework, stdout: str, stderr: str
) -> tuple[list[TestCase], str, bool]:
    """Dispatch to the structured parser for ``framework``, falling back to
    :func:`parse_fallback` (over the combined stdout+stderr) if the
    structured parse raises or yields nothing.

    Returns ``(cases, parse_error, used_fallback_parser)``.
    """
    parser = _STRUCTURED_PARSERS.get(framework)
    combined = stdout if stdout.strip() else stderr

    if parser is not None:
        try:
            cases = parser(stdout) if stdout.strip() else parser(stderr)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError, _XML_PARSE_ERROR) as exc:
            fallback_cases = parse_fallback(stdout + "\n" + stderr)
            return fallback_cases, f"structured parse failed: {exc}", True
        if cases:
            return cases, "", False
        # Structured parser ran clean but found nothing -- fall through to
        # the aggregate fallback rather than silently reporting "0 tests".

    if framework in (TestFramework.VITEST, TestFramework.UNKNOWN):
        vitest_cases = parse_vitest_verbose(combined)
        if vitest_cases:
            return vitest_cases, "", True
    if framework is TestFramework.GO_TEST:
        go_cases = parse_go_test_verbose(combined)
        if go_cases:
            return go_cases, "", True
    fallback_cases = parse_fallback(combined)
    error = "" if fallback_cases else "no recognizable test output"
    return fallback_cases, error, True


_STRUCTURED_PARSERS = {
    TestFramework.JEST: parse_jest_json,
    TestFramework.VITEST: parse_vitest_json,
    TestFramework.MOCHA: parse_mocha_json,
    TestFramework.NODE_TEST: parse_tap,
    TestFramework.PYTEST: parse_junit_xml,
    TestFramework.RSPEC: parse_rspec_json,
    TestFramework.MINITEST: parse_junit_xml,  # via minitest-reporters
    TestFramework.GO_TEST: parse_go_test_json,
}
