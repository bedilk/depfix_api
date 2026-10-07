"""V8 coverage: verify that the migrated lines were actually executed during tests.

A test suite that passes without ever running the changed code proves nothing
about whether the migration is correct. This module uses Node's built-in V8
coverage to answer: "did the test run actually exercise the line we fixed?"

How it works:

1. Set ``NODE_V8_COVERAGE=<dir>`` before running the test suite.
2. Node writes a ``coverage-*.json`` file for every script it executes.
3. Parse those files and check whether the migrated file's changed lines
   have ``count > 0`` in the coverage report.

A line is "executed" if its ``count`` field is non-zero. A fix is
"exercised" if at least one changed line was executed. A passing suite
that never exercised the fix earns at most MEDIUM confidence, never HIGH.

Why V8 coverage and not Istanbul/nyc? V8 coverage is zero-overhead (no
instrumentation transform) and is built into Node. Repos that already use
a coverage tool still produce V8 coverage when the env var is set; repos
without any coverage setup work out of the box. We don't need a coverage
*percentage* -- we need a boolean: "was this one line run or not?"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class FileCoverage:
    """Coverage for one file."""

    relpath: str
    changed_lines: tuple[int, ...]
    covered_lines: tuple[int, ...]


@dataclass
class CoverageReport:
    """Outcome of checking whether the fix was executed."""

    ran: bool = False
    skipped_reason: str = ""
    files: list[FileCoverage] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.files is None:
            self.files = []

    @property
    def any_uncovered(self) -> bool:
        """True if any changed line was not executed."""
        return any(len(f.covered_lines) < len(f.changed_lines) for f in self.files)

    def summary(self) -> str:
        if not self.ran:
            return f"skipped: {self.skipped_reason}"
        uncovered_count = sum(len(f.changed_lines) - len(f.covered_lines) for f in self.files)
        if uncovered_count > 0:
            return f"test suite never executed {uncovered_count} migrated line(s)"
        return "all migrated lines were executed"


def changed_line_numbers(original: str, fixed: str) -> set[int]:
    """Return the line numbers in fixed that differ from original."""
    import difflib

    original_lines = original.splitlines(keepends=True)
    fixed_lines = fixed.splitlines(keepends=True)

    changed = set()
    matcher = difflib.SequenceMatcher(None, original_lines, fixed_lines)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            # Line numbers are 1-indexed
            for line_no in range(j1 + 1, j2 + 1):
                changed.add(line_no)
    return changed


def line_start_offsets(content: str) -> dict[int, int]:
    """Return a map of line number (1-indexed) to byte offset."""
    offsets = {1: 0}
    offset = 0
    line_no = 1
    for char in content:
        offset += len(char.encode("utf-8"))
        if char == "\n":
            line_no += 1
            offsets[line_no] = offset
    return offsets


def parse_v8_coverage(coverage_dir: Path, file_path: Path) -> set[int]:
    """Parse V8 coverage JSON files and return executed line numbers for file_path.

    V8 coverage format (one file per script):
    {
      "result": [
        {
          "url": "file:///path/to/file.js",
          "functions": [
            {
              "ranges": [
                {"startOffset": 0, "endOffset": 100, "count": 5},
                ...
              ]
            }
          ]
        }
      ]
    }

    We convert byte offsets to line numbers using the file's content.
    """
    if not coverage_dir.is_dir():
        return set()

    executed = set()
    abs_file = file_path.resolve()

    try:
        file_content = file_path.read_text(encoding="utf-8")
    except OSError:
        return set()

    # Build a byte-offset-to-line-number map
    offset_to_line = {}
    offset = 0
    for line_no, line in enumerate(file_content.splitlines(keepends=True), start=1):
        for _ in range(len(line.encode("utf-8"))):
            offset_to_line[offset] = line_no
            offset += 1

    for cov_file in coverage_dir.glob("coverage-*.json"):
        try:
            data = json.loads(cov_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        for script in data.get("result", []):
            url = script.get("url", "")
            # V8 URLs are file:// URIs; resolve to absolute path for comparison
            if url.startswith("file://"):
                script_path = Path(url[7:]).resolve()
                if script_path != abs_file:
                    continue
            else:
                continue

            for func in script.get("functions", []):
                for rng in func.get("ranges", []):
                    count = rng.get("count", 0)
                    if count == 0:
                        continue
                    start = rng.get("startOffset", 0)
                    end = rng.get("endOffset", 0)
                    for offset in range(start, end):
                        line = offset_to_line.get(offset)  # type: ignore[assignment]
                        if line is not None:
                            executed.add(line)

    return executed  # type: ignore[return-value]


def check_changed_lines(coverage_dir: Path, root: Path, edits: list) -> CoverageReport:
    """Check whether changed lines were executed during the test run.

    Args:
        coverage_dir: Directory where NODE_V8_COVERAGE wrote coverage-*.json files.
        root: Repository root path.
        edits: List of FileEdit objects with relpath, original_content, fixed_content.

    Returns:
        CoverageReport indicating whether coverage is available and which lines were executed.
    """
    if not coverage_dir.is_dir():
        return CoverageReport(
            ran=False,
            skipped_reason="no V8 coverage data",
        )

    file_coverages = []
    for edit in edits:
        file_path = root / edit.relpath
        changed = changed_line_numbers(edit.original_content, edit.fixed_content)
        if not changed:
            continue

        executed = parse_v8_coverage(coverage_dir, file_path)
        covered = tuple(sorted(executed & changed))

        file_coverages.append(
            FileCoverage(
                relpath=edit.relpath,
                changed_lines=tuple(sorted(changed)),
                covered_lines=covered,
            )
        )

    if not file_coverages:
        return CoverageReport(ran=False, skipped_reason="no changed lines to check")

    return CoverageReport(ran=True, files=file_coverages)
