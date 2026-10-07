"""Selective test execution: run only the tests that import a changed file.

Full-suite verification is the gold standard but is expensive (minutes per
fix). When a fix touches ``src/auth/client.js``, re-running the entire test
suite is overkill -- only tests that actually import that module can
possibly detect a regression.

This module builds a coarse import graph (which test files reach which
source files) and returns the minimal test set. Two strategies:

1. **Static import analysis** (fast, incomplete): grep for
   ``import ... from './auth/client'`` / ``require('./auth/client')``.
2. **Filename convention** (fallback): if ``src/auth/client.js`` changed,
   run ``test/auth/client.test.js`` and ``test/auth.test.js``.

The tradeoff: much cheaper per fix, but a regression in an *unrelated* test
is invisible. This is opt-in per fleet (``verify_selective_tests``), never
silent default behaviour.

Ecosystem support: JavaScript/TypeScript first. Python/Go have different
import semantics but the same principle applies.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Regex patterns for import statements (simplified, not a full parser)
_JS_IMPORT = re.compile(
    r"""(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]|"""
    r"""require\s*\(\s*['"]([^'"]+)['"]\s*\))""",
    re.MULTILINE,
)

_PY_IMPORT = re.compile(
    r"""(?:^from\s+([\w.]+)\s+import\s+|^import\s+([\w.]+))""",
    re.MULTILINE,
)


@dataclass
class TestSelection:
    """Result of selecting tests for a set of changed files."""

    test_files: list[Path]
    """Test files that should be run (relative to repo root)."""

    strategy: str
    """How the selection was made: 'import-graph', 'filename-convention', 'fallback-all'."""

    detail: str = ""
    """Human-readable explanation."""


def select_test_files(
    root: Path | str,
    changed_relpaths: tuple[str, ...] | list[str],
    *,
    ecosystem: str = "javascript",
) -> tuple[str, ...]:
    """Select test files that should run for the given changed files.

    Returns a tuple of relative test file paths, or empty tuple to run all tests.
    """
    root_path = Path(root) if isinstance(root, str) else root
    changed_paths = [
        root_path / relpath if not Path(relpath).is_absolute() else Path(relpath)
        for relpath in changed_relpaths
    ]

    pattern = "**/*.test.{js,ts,jsx,tsx}" if ecosystem == "javascript" else "**/*test*.py"
    selection = select_tests_for_changes(root_path, changed_paths, test_pattern=pattern)

    return tuple(str(f.relative_to(root_path)) for f in selection.test_files)


def select_tests_for_changes(
    root: Path, changed_files: list[Path], test_pattern: str = "**/*.test.{js,ts,jsx,tsx,py}"
) -> TestSelection:
    """Return the minimal set of test files that cover changed_files.

    Args:
        root: Repository root.
        changed_files: Paths to modified source files (absolute or relative to root).
        test_pattern: Glob pattern for test files (default: JS/TS/Py convention).

    Returns:
        TestSelection with the test files to run.
    """
    abs_root = root.resolve()
    abs_changed = [
        f.resolve() if f.is_absolute() else (abs_root / f).resolve() for f in changed_files
    ]

    # Gather all test files
    test_files = _find_test_files(abs_root, test_pattern)
    if not test_files:
        return TestSelection(
            test_files=[],
            strategy="fallback-all",
            detail="no test files found; would run full suite",
        )

    # Try import-graph strategy first
    selected = _select_by_import_graph(abs_root, abs_changed, test_files)
    if selected:
        return TestSelection(
            test_files=[f.relative_to(abs_root) for f in selected],
            strategy="import-graph",
            detail=f"selected {len(selected)} test files by import analysis",
        )

    # Fallback: filename convention
    selected = _select_by_filename_convention(abs_root, abs_changed, test_files)
    if selected:
        return TestSelection(
            test_files=[f.relative_to(abs_root) for f in selected],
            strategy="filename-convention",
            detail=f"selected {len(selected)} test files by naming convention",
        )

    # No match: run all tests
    return TestSelection(
        test_files=[f.relative_to(abs_root) for f in test_files],
        strategy="fallback-all",
        detail="no specific test match; running all tests",
    )


def _find_test_files(root: Path, pattern: str) -> list[Path]:
    """Find all test files matching the pattern."""
    test_files: list[Path] = []
    # Expand {js,ts} style patterns manually
    if "{" in pattern and "}" in pattern:
        # Simple brace expansion: **/*.test.{js,ts} -> [**/*.test.js, **/*.test.ts]
        base, rest = pattern.split("{", 1)
        options, suffix = rest.split("}", 1)
        for opt in options.split(","):
            test_files.extend(root.glob(base + opt + suffix))
    else:
        test_files.extend(root.glob(pattern))
    return sorted(set(test_files))


def _select_by_import_graph(
    root: Path, changed_files: list[Path], test_files: list[Path]
) -> list[Path]:
    """Select test files that import any of the changed files (static analysis)."""
    selected = []
    for test_file in test_files:
        if _test_imports_any(root, test_file, changed_files):
            selected.append(test_file)
    return selected


def _test_imports_any(root: Path, test_file: Path, changed_files: list[Path]) -> bool:
    """Check if test_file imports any of the changed_files."""
    try:
        content = test_file.read_text(encoding="utf-8")
    except OSError:
        return False

    # Determine the pattern based on file extension
    if test_file.suffix in {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}:
        pattern = _JS_IMPORT
    elif test_file.suffix == ".py":
        pattern = _PY_IMPORT
    else:
        return False

    imports = pattern.findall(content)
    # Flatten tuples from regex groups
    imports = [imp for match in imports for imp in match if imp]

    for changed in changed_files:
        rel_changed = changed.relative_to(root) if changed.is_relative_to(root) else changed
        # Check if any import resolves to this changed file
        for imp in imports:
            if _import_resolves_to(root, test_file, imp, rel_changed):
                return True
    return False


def _import_resolves_to(root: Path, importer: Path, import_spec: str, target: Path) -> bool:
    """Check if import_spec from importer resolves to target file.

    This is a simplified heuristic, not a full module resolver:
    - Relative imports: resolve from importer's directory
    - Bare imports: assume they're in node_modules or sys.path (skip)
    """
    # Bare import (e.g., 'lodash', 'requests') - not a local file
    if not import_spec.startswith(("..", ".")):
        return False

    # Resolve relative import
    importer_dir = importer.parent
    resolved = (importer_dir / import_spec).resolve()

    # Try with common extensions
    for ext in ["", ".js", ".ts", ".jsx", ".tsx", ".py", ".mjs", ".cjs", "/index.js", "/index.ts"]:
        candidate = resolved.parent / (resolved.name + ext) if ext else resolved
        if candidate == target or candidate == root / target:
            return True

    return False


def _select_by_filename_convention(
    root: Path, changed_files: list[Path], test_files: list[Path]
) -> list[Path]:
    """Select tests by naming convention: src/foo/bar.js -> test/foo/bar.test.js."""
    selected = []
    for changed in changed_files:
        rel_changed = changed.relative_to(root) if changed.is_relative_to(root) else changed
        # Strip src/ prefix if present
        parts = rel_changed.parts
        if parts and parts[0] in {"src", "lib", "app"}:
            parts = parts[1:]
        # Build test path candidates
        stem = Path(*parts).stem  # foo/bar.js -> foo/bar
        candidates = [
            root / "test" / Path(*parts[:-1]) / f"{stem}.test{changed.suffix}",
            root / "tests" / Path(*parts[:-1]) / f"{stem}.test{changed.suffix}",
            root / "__tests__" / Path(*parts[:-1]) / f"{stem}.test{changed.suffix}",
            root / Path(*parts[:-1]) / f"{stem}.test{changed.suffix}",
        ]
        for candidate in candidates:
            if candidate in test_files:
                selected.append(candidate)
    return list(set(selected))
