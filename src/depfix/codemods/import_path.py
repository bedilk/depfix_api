"""Codemod for import path changes: package renames, subpath changes, etc.

Handles migrations like:
- @openai/sdk -> openai
- stripe/v2 -> stripe
- aws-sdk -> @aws-sdk/client-s3
"""

from __future__ import annotations

import re

from depfix.codemods.base import Codemod, CodemodResult
from depfix.core.models import BreakingChange


class ImportPathCodemod(Codemod):
    """Rewrite import statements when a package's import path changes."""

    id = "import-path"
    specificity = 80

    # Patterns for different import styles
    _IMPORT_FROM = re.compile(r"""(from\s+['"])([\w@/-]+)(['"])""", re.MULTILINE)
    _REQUIRE = re.compile(r"""(require\s*\(\s*['"])([\w@/-]+)(['"]\s*\))""", re.MULTILINE)
    _IMPORT_STAR = re.compile(r"""(import\s+.*?\s+from\s+['"])([\w@/-]+)(['"])""", re.MULTILINE)

    def claims(self, change: BreakingChange) -> bool:
        """Claim if the migration guide mentions 'import from' or 'package renamed'."""
        guide = (change.migration_guide or "").lower()
        return (
            any(
                phrase in guide
                for phrase in [
                    "import from",
                    "package renamed",
                    "moved to",
                    "import path",
                    "require(",
                ]
            )
            and "->" in guide
        )

    def apply(self, source: str, change: BreakingChange) -> CodemodResult:
        """Rewrite all import statements referencing the old package."""
        old_pkg, new_pkg = self._extract_package_rename(change)
        if not old_pkg or not new_pkg:
            return CodemodResult.decline(
                self.id, "could not parse old->new package names from migration guide"
            )

        modified = source
        changes = 0

        # Replace in different import styles
        def replace_import(match: re.Match) -> str:
            nonlocal changes
            prefix, pkg, suffix = match.groups()
            if pkg == old_pkg or pkg.startswith(old_pkg + "/"):
                changes += 1
                # Preserve subpath if present
                subpath = pkg[len(old_pkg) :] if len(pkg) > len(old_pkg) else ""
                return f"{prefix}{new_pkg}{subpath}{suffix}"
            return match.group(0)

        modified = self._IMPORT_FROM.sub(replace_import, modified)
        modified = self._REQUIRE.sub(replace_import, modified)
        modified = self._IMPORT_STAR.sub(replace_import, modified)

        if changes == 0:
            return CodemodResult.decline(self.id, f"no import statements found for '{old_pkg}'")

        return CodemodResult.rewrote(
            self.id,
            modified,
            f"rewrote {changes} import statement(s) from '{old_pkg}' to '{new_pkg}'",
        )

    def _extract_package_rename(self, change: BreakingChange) -> tuple[str, str]:
        """Parse 'old-package -> new-package' from the migration guide."""
        guide = change.migration_guide or ""
        # Look for patterns like:
        # "import from 'old-pkg' -> import from 'new-pkg'"
        # "require('old') -> require('new')"
        # "old-package -> new-package"

        # Simple heuristic: find quoted strings around an arrow
        import_match = re.search(r"""['"](\S+)['"]\s*->\s*['"](\S+)['"]""", guide)
        if import_match:
            return import_match.group(1), import_match.group(2)

        # Fallback: use package name as old, parse new from guide
        if change.package and " -> " in guide:
            parts = guide.split(" -> ", 1)
            for part in parts[1].split():
                if re.match(r"^[@\w/-]+$", part):
                    return change.package, part

        return "", ""
