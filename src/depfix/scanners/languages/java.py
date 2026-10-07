"""Java and Kotlin binding-aware scanner.

Import forms handled:
    import com.stripe.Stripe;                   # single class
    import com.stripe.*;                         # wildcard
    import static com.stripe.Stripe.setApiKey;  # static single
    import static com.stripe.Stripe.*;           # static wildcard
    import com.stripe.Stripe as S               # Kotlin alias
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
from depfix.scanners.masking import JAVA_RULES, mask_source

_JAVA_EXTS = {".java", ".kt", ".kts"}
_SKIP_DIRS = {"target", "build", "out", ".gradle", ".idea", "node_modules", ".git"}
_IMPORT = re.compile(
    r"^\s*import\s+(?P<static>static\s+)?(?P<fqn>[\w.*]+)"
    r"(?:\s+as\s+(?P<alias>\w+))?\s*;?\s*$"
)


class JavaScanner:
    def __init__(self, packages: list[str]) -> None:
        # Java packages are dotted prefixes, e.g. "com.stripe", "com.stripe.model"
        self._sdk_prefixes = list(packages)

    def scan_repo(self, repo_root: Path) -> LanguageScanResult:
        result = LanguageScanResult(language="java")
        for path in repo_root.rglob("*"):
            if not path.is_file() or path.suffix not in _JAVA_EXTS:
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
            masked = mask_source(source, JAVA_RULES)
            file_bindings = self._extract_bindings(masked, source, relpath)
            result.bindings.extend(file_bindings)
            if file_bindings:
                result.uses.extend(self._collect_uses(masked, relpath, file_bindings))
            result.uses.extend(self._collect_fqn_uses(masked, relpath))
        return result

    def _extract_bindings(
        self,
        masked: str,
        original: str,
        relpath: str,
    ) -> list[ImportBinding]:
        out: list[ImportBinding] = []
        orig_lines = original.splitlines()
        for i, line in enumerate(masked.splitlines(), start=1):
            m = _IMPORT.match(line)
            if not m:
                continue
            fqn = m.group("fqn")
            if not self._matches(fqn):
                continue
            canonical = self._canonical(fqn)
            orig_line = orig_lines[i - 1] if i - 1 < len(orig_lines) else line
            if fqn.endswith(".*"):
                out.append(
                    ImportBinding(
                        package=canonical,
                        local_name="*",
                        filepath=relpath,
                        line_number=i,
                        line_content=orig_line,
                        kind=BindingKind.WILDCARD,
                        bound_symbol=fqn,
                    )
                )
                continue
            last = fqn.rsplit(".", 1)[-1]
            alias = m.group("alias")
            out.append(
                ImportBinding(
                    package=canonical,
                    local_name=alias or last,
                    filepath=relpath,
                    line_number=i,
                    line_content=orig_line,
                    kind=BindingKind.ALIASED if alias else BindingKind.NAMED,
                    bound_symbol=fqn,
                )
            )
        return out

    def _matches(self, fqn: str) -> bool:
        t = fqn.rstrip(".*")
        return any(t == s or t.startswith(s + ".") for s in self._sdk_prefixes)

    def _canonical(self, fqn: str) -> str:
        t = fqn.rstrip(".*")
        for s in self._sdk_prefixes:
            if t == s or t.startswith(s + "."):
                return s
        return t

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
        return self._grep_uses(masked, relpath, pattern, skip_prefix="import ")

    def _collect_fqn_uses(self, masked: str, relpath: str) -> list[SymbolUse]:
        if not self._sdk_prefixes:
            return []
        pattern = re.compile(
            r"\b("
            + "|".join(re.escape(p) for p in sorted(self._sdk_prefixes, key=len, reverse=True))
            + r")((?:\.\w+)+)"
        )
        return self._grep_uses(masked, relpath, pattern, skip_prefix="import ")

    @staticmethod
    def _grep_uses(masked: str, relpath: str, pattern, skip_prefix: str) -> list[SymbolUse]:
        uses: list[SymbolUse] = []
        for i, line in enumerate(masked.splitlines(), start=1):
            if line.lstrip().startswith(skip_prefix):
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
