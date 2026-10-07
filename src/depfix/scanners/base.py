"""
Codebase scanner for finding API usages.
"""

import logging
import os
import re
from pathlib import Path
from typing import ClassVar

from depfix.core.models import FileUsage, Usage

logger = logging.getLogger(__name__)


class CodebaseScanner:
    """
    Scans a codebase for usages of specific APIs.
    """

    # File extensions to scan
    SUPPORTED_EXTENSIONS: ClassVar[set[str]] = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

    # Directories to skip
    SKIP_DIRS: ClassVar[set[str]] = {
        "node_modules",
        ".git",
        "dist",
        "build",
        "coverage",
        ".next",
        "__pycache__",
    }

    def __init__(self, context_lines: int = 5):
        """
        Initialize the scanner.
        """
        self.context_lines = context_lines

    def scan_codebase(
        self, path: str, patterns: list[str], file_pattern: str | None = None
    ) -> list[FileUsage]:
        """
        Scan a codebase for usages matching the given patterns.
        """
        codebase_root = Path(path)
        if not codebase_root.exists():
            raise FileNotFoundError(f"Codebase path does not exist: {codebase_root}")

        file_usages = []
        files_scanned = 0

        # Compile regex patterns
        compiled_patterns = [re.compile(p) for p in patterns]

        # Walk the directory tree
        for root, dirs, files in os.walk(path):
            # Filter out directories to skip
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]

            for filename in files:
                filepath = Path(root) / filename

                # Check file extension
                if filepath.suffix not in self.SUPPORTED_EXTENSIONS:
                    continue

                # Apply file pattern filter if specified
                if file_pattern and not filepath.match(file_pattern):
                    continue

                files_scanned += 1

                # Scan the file
                file_usage = self._scan_file(filepath, compiled_patterns)
                if file_usage and file_usage.usages:
                    file_usages.append(file_usage)

        logger.info(f"Scanned {files_scanned} files, found {len(file_usages)} with usages")
        return file_usages

    def _scan_file(self, filepath: Path, patterns: list[re.Pattern]) -> FileUsage | None:
        """
        Scan a single file for pattern matches.
        """
        try:
            content = filepath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError) as e:
            logger.warning(f"Could not read file {filepath}: {e}")
            return None

        lines = content.split("\n")
        usages = []

        for pattern in patterns:
            for line_idx, line in enumerate(lines):
                for match in pattern.finditer(line):
                    usage = Usage(
                        line_number=line_idx + 1,  # 1-indexed
                        column=match.start(),
                        line_content=line,
                        context_before=self._get_context_before(lines, line_idx),
                        context_after=self._get_context_after(lines, line_idx),
                        match_text=match.group(),
                    )
                    usages.append(usage)

        if usages:
            return FileUsage(filepath=str(filepath), usages=usages, file_content=content)
        return None

    def _get_context_before(self, lines: list[str], line_idx: int) -> list[str]:
        """Get lines before the current line for context."""
        start = max(0, line_idx - self.context_lines)
        return lines[start:line_idx]

    def _get_context_after(self, lines: list[str], line_idx: int) -> list[str]:
        """Get lines after the current line for context."""
        end = min(len(lines), line_idx + self.context_lines + 1)
        return lines[line_idx + 1 : end]


def build_pattern_for_api(api_pattern: str, package: str) -> list[str]:
    """
    Build regex patterns for matching API usages.
    """
    patterns = []

    if "." in api_pattern:
        method_name = api_pattern.split(".")[1].split("(")[0]

        # Direct usage: _.method(...)
        if api_pattern.startswith("_."):
            patterns.append(rf"_\.{method_name}\s*\(")

        # Direct usage on the package/client object: lodash.method(...) or
        # openai.method(...) — not gated on the lodash "_." convention, since
        # SDKs like openai-node only ever call methods off a client object.
        patterns.append(rf"{re.escape(package)}\.{method_name}\s*\(")

        # Destructured import: const { pluck } = require('lodash')
        patterns.append(rf"(?<![.\w]){method_name}\s*\(")

    return patterns
