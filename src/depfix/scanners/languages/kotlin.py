"""Kotlin binding-aware scanner.

Kotlin import syntax is syntactically identical to Java for the common
cases, with two additions:
  - `import com.stripe.Stripe as S`  (aliased — Kotlin-native, rare in Java)
  - Extension functions on SDK types: `fun Stripe.customMethod()` — not
    an import but an association, kept as LOW confidence.

The Java scanner already handles .kt/.kts files at MEDIUM/HIGH confidence
for straightforward imports. This dedicated scanner replaces it for those
extensions and adds Kotlin-specific patterns.
"""

from __future__ import annotations

import re
from pathlib import Path

from depfix.scanners.language import (
    BindingKind,
    ImportBinding,
    LanguageScanResult,
    SymbolUse,
)
from depfix.scanners.masking import JAVA_RULES, mask_source  # same comment/string rules

_KT_EXTS = {".kt", ".kts"}
_SKIP_DIRS = {"build", ".gradle", ".idea", "target", "node_modules", ".git", "generated"}

# Kotlin import: `import com.stripe.Stripe` or `import com.stripe.Stripe as S`
_IMPORT = re.compile(r"^\s*import\s+(?P<fqn>[\w.*]+)(?:\s+as\s+(?P<alias>\w+))?\s*$")


class KotlinScanner:
    def __init__(self, packages: list[str]) -> None:
        # packages are dotted prefixes: e.g. "com.stripe", "com.stripe.model"
        self._sdk_prefixes = list(packages)

    def scan_repo(self, repo_root: Path) -> LanguageScanResult:
        result = LanguageScanResult(language="kotlin")
        for path in repo_root.rglob("*"):
            if not path.is_file() or path.suffix not in _KT_EXTS:
                continue
            if any(p in _SKIP_DIRS for p in path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.errors.append(f"read {path}: {exc}")
                continue
            result.files_scanned += 1
            relpath = str(path.relative_to(repo_root))

            # Extract bindings from original (import paths must not be masked)
            file_bindings = self._extract_bindings(source, relpath)
            result.bindings.extend(file_bindings)

            if file_bindings:
                masked = mask_source(source, JAVA_RULES)
                result.uses.extend(self._collect_uses(masked, relpath, file_bindings))
                # Kotlin extension function associations (LOW confidence)
                result.uses.extend(self._collect_extension_uses(masked, relpath, file_bindings))
        return result

    def _extract_bindings(self, source: str, relpath: str) -> list[ImportBinding]:
        out: list[ImportBinding] = []
        lines = source.splitlines()
        for i, line in enumerate(lines, start=1):
            m = _IMPORT.match(line)
            if not m:
                continue
            fqn = m.group("fqn")
            if not self._matches(fqn):
                continue
            canonical = self._canonical(fqn)
            alias = m.group("alias")

            if fqn.endswith(".*"):
                out.append(
                    ImportBinding(
                        package=canonical,
                        local_name="*",
                        filepath=relpath,
                        line_number=i,
                        line_content=line,
                        kind=BindingKind.WILDCARD,
                        bound_symbol=fqn,
                    )
                )
                continue

            last = fqn.rsplit(".", 1)[-1]
            out.append(
                ImportBinding(
                    package=canonical,
                    local_name=alias or last,
                    filepath=relpath,
                    line_number=i,
                    line_content=line,
                    kind=BindingKind.ALIASED if alias else BindingKind.NAMED,
                    bound_symbol=fqn,
                )
            )
        return out

    def _collect_uses(
        self,
        masked: str,
        relpath: str,
        bindings: list[ImportBinding],
    ) -> list[SymbolUse]:
        names = {b.local_name for b in bindings if b.local_name != "*"}
        if not names:
            return []
        pattern = re.compile(
            r"\b("
            + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            + r")((?:\.\w+)*)"
        )
        uses: list[SymbolUse] = []
        for i, line in enumerate(masked.splitlines(), start=1):
            if line.lstrip().startswith("import "):
                continue
            for m in pattern.finditer(line):
                uses.append(
                    SymbolUse(
                        filepath=relpath,
                        line_number=i,
                        column=m.start(),
                        line_content=line,
                        local_name=m.group(1),
                        attribute_path=m.group(1) + m.group(2),
                    )
                )
        return uses

    def _collect_extension_uses(
        self,
        masked: str,
        relpath: str,
        bindings: list[ImportBinding],
    ) -> list[SymbolUse]:
        """Kotlin extension functions: `fun Stripe.foo()` — LOW confidence."""
        names = {b.local_name for b in bindings if b.local_name != "*"}
        if not names:
            return []
        # Match `fun <SDK_TYPE>.<method>(` — extension function definition
        pattern = re.compile(
            r"\bfun\s+("
            + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            + r")((?:\.\w+)+)\s*\("
        )
        uses: list[SymbolUse] = []
        for i, line in enumerate(masked.splitlines(), start=1):
            for m in pattern.finditer(line):
                uses.append(
                    SymbolUse(
                        filepath=relpath,
                        line_number=i,
                        column=m.start(),
                        line_content=line,
                        local_name=m.group(1),
                        attribute_path=m.group(1) + m.group(2),
                    )
                )
        return uses

    def _matches(self, fqn: str) -> bool:
        t = fqn.rstrip(".*")
        return any(t == s or t.startswith(s + ".") for s in self._sdk_prefixes)

    def _canonical(self, fqn: str) -> str:
        t = fqn.rstrip(".*")
        for s in self._sdk_prefixes:
            if t == s or t.startswith(s + "."):
                return s
        return t
