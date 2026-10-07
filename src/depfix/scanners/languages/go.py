"""Go binding-aware scanner.

Handles all import forms:
    import "github.com/stripe/stripe-go/v76"        # default alias = "stripe"
    import s "github.com/stripe/stripe-go/v76"      # explicit alias
    import . "github.com/stripe/stripe-go/v76"      # dot import
    import _ "github.com/lib/pq"                    # side-effect only
    import ( ... )                                  # grouped form
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from depfix.scanners.language import (
    BindingKind,
    ImportBinding,
    LanguageScanResult,
    SymbolUse,
)
from depfix.scanners.masking import GO_RULES, mask_source

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"vendor", ".git", "node_modules", ".idea", "testdata"}
_VERSION_SEG = re.compile(r"^v\d+$")
_IMPORT_BODY = re.compile(r"""^\s*(?:(?P<alias>[\w.]+)\s+)?"(?P<path>[^"]+)"\s*(?://.*)?$""")


class GoScanner:
    def __init__(self, packages: list[str]) -> None:
        self._sdk_paths = list(packages)

    def scan_repo(self, repo_root: Path) -> LanguageScanResult:
        result = LanguageScanResult(language="go")
        for path in repo_root.rglob("*.go"):
            if any(p in _SKIP_DIRS for p in path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.errors.append(f"read {path}: {exc}")
                continue
            result.files_scanned += 1
            relpath = str(path.relative_to(repo_root))
            # Extract bindings from ORIGINAL source (import paths are in strings;
            # masking would destroy them). Use masking only for use-site grep.
            file_bindings = self._extract_bindings_raw(source, relpath)
            result.bindings.extend(file_bindings)
            if file_bindings:
                masked = mask_source(source, GO_RULES)
                result.uses.extend(self._collect_uses(masked, relpath, file_bindings))
        return result

    def _extract_bindings_raw(self, source: str, relpath: str) -> list[ImportBinding]:
        return self._extract_bindings(source, source, relpath)

    def _extract_bindings(self, masked: str, original: str, relpath: str) -> list[ImportBinding]:
        out: list[ImportBinding] = []
        masked_lines = masked.splitlines()
        orig_lines = original.splitlines()
        i = 0
        while i < len(masked_lines):
            line = masked_lines[i]
            stripped = line.strip()

            # Single-line import not followed by (
            if re.match(r"\bimport\b", stripped) and "(" not in stripped:
                m = re.search(r"import\s+(.*)", stripped)
                if m:
                    self._parse_body(
                        m.group(1).strip(),
                        relpath,
                        i + 1,
                        orig_lines[i] if i < len(orig_lines) else "",
                        out,
                    )
                i += 1
                continue

            # Grouped import block
            if re.match(r"\bimport\s*\(", stripped):
                j = i + 1
                while j < len(masked_lines):
                    inner = masked_lines[j]
                    if inner.strip() == ")":
                        break
                    if inner.strip() and not inner.strip().startswith("//"):
                        self._parse_body(
                            inner.strip(),
                            relpath,
                            j + 1,
                            orig_lines[j] if j < len(orig_lines) else inner,
                            out,
                        )
                    j += 1
                i = j + 1
                continue

            i += 1
        return out

    def _parse_body(
        self,
        body: str,
        relpath: str,
        lineno: int,
        line_content: str,
        out: list[ImportBinding],
    ) -> None:
        m = _IMPORT_BODY.match(body)
        if not m:
            return
        path = m.group("path")
        if not self._matches(path):
            return
        canonical = self._canonical(path)
        alias = m.group("alias") or ""

        if alias == "_":
            out.append(
                ImportBinding(
                    package=canonical,
                    local_name="_",
                    filepath=relpath,
                    line_number=lineno,
                    line_content=line_content.rstrip("\n"),
                    kind=BindingKind.SIDE_EFFECT,
                )
            )
            return
        if alias == ".":
            out.append(
                ImportBinding(
                    package=canonical,
                    local_name=".",
                    filepath=relpath,
                    line_number=lineno,
                    line_content=line_content.rstrip("\n"),
                    kind=BindingKind.DOT,
                )
            )
            return

        local = alias if alias else self._default_alias(path)
        out.append(
            ImportBinding(
                package=canonical,
                local_name=local,
                filepath=relpath,
                line_number=lineno,
                line_content=line_content.rstrip("\n"),
                kind=BindingKind.ALIASED if alias else BindingKind.DIRECT,
            )
        )

    @staticmethod
    def _default_alias(path: str) -> str:
        parts = [p for p in path.split("/") if p]
        while parts and _VERSION_SEG.match(parts[-1]):
            parts.pop()
        return parts[-1] if parts else path

    def _matches(self, path: str) -> bool:
        return any(path == s or path.startswith(s + "/") for s in self._sdk_paths)

    def _canonical(self, path: str) -> str:
        for s in self._sdk_paths:
            if path == s or path.startswith(s + "/"):
                return s
        return path

    def _collect_uses(
        self,
        masked: str,
        relpath: str,
        bindings: list[ImportBinding],
    ) -> list[SymbolUse]:
        aliases = {
            b.local_name
            for b in bindings
            if b.kind not in (BindingKind.DOT, BindingKind.SIDE_EFFECT)
        }
        if not aliases:
            return []
        pattern = re.compile(
            r"\b("
            + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
            + r")((?:\.\w+)*)"
        )
        uses: list[SymbolUse] = []
        for i, line in enumerate(masked.splitlines(), start=1):
            if re.match(r"\s*import\b", line):
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
