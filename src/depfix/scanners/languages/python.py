"""Python binding-aware scanner.

Uses stdlib `ast` for imports (handles every legal form including
multi-line parenthesized `from x import (a, b, c)`). Falls back to
line-oriented regex for files that fail to parse (Py2, templates).
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from depfix.scanners.language import (
    BindingKind,
    ImportBinding,
    LanguageScanResult,
    SymbolUse,
)
from depfix.scanners.masking import PYTHON_RULES, mask_source

logger = logging.getLogger(__name__)

_PY_EXTENSIONS = {".py", ".pyi"}
_MAX_FILE_BYTES = 2_000_000
_SKIP_DIRS = {
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".tox",
    "build",
    "dist",
    ".eggs",
    "site-packages",
    "node_modules",
}


class PythonScanner:
    def __init__(self, packages: list[str]) -> None:
        self._sdk_packages = {self._norm(p) for p in packages}
        self._sdk_modules = {p.replace("-", "_") for p in packages} | self._sdk_packages

    @staticmethod
    def _norm(name: str) -> str:
        return name.lower().replace("_", "-")

    def scan_repo(self, repo_root: Path) -> LanguageScanResult:
        result = LanguageScanResult(language="python")
        for path in self._iter_files(repo_root):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.errors.append(f"read {path}: {exc}")
                continue
            if len(source.encode("utf-8", errors="replace")) > _MAX_FILE_BYTES:
                continue
            result.files_scanned += 1
            relpath = str(path.relative_to(repo_root))
            file_bindings = self._extract_bindings(source, relpath)
            result.bindings.extend(file_bindings)
            if file_bindings:
                masked = mask_source(source, PYTHON_RULES)
                result.uses.extend(self._collect_uses(masked, relpath, file_bindings))
        return result

    def _iter_files(self, root: Path):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _PY_EXTENSIONS:
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            yield path

    def _extract_bindings(self, source: str, relpath: str) -> list[ImportBinding]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return self._extract_bindings_regex(source, relpath)

        out: list[ImportBinding] = []
        lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".", 1)[0]
                    if not self._is_sdk(root_mod):
                        continue
                    local = alias.asname or root_mod
                    out.append(
                        ImportBinding(
                            package=self._canonical(root_mod),
                            local_name=local,
                            filepath=relpath,
                            line_number=node.lineno,
                            line_content=self._line(lines, node.lineno),
                            kind=BindingKind.ALIASED if alias.asname else BindingKind.DIRECT,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                root_mod = node.module.split(".", 1)[0]
                if not self._is_sdk(root_mod):
                    continue
                pkg = self._canonical(root_mod)
                for alias in node.names:
                    if alias.name == "*":
                        out.append(
                            ImportBinding(
                                package=pkg,
                                local_name="*",
                                filepath=relpath,
                                line_number=node.lineno,
                                line_content=self._line(lines, node.lineno),
                                kind=BindingKind.WILDCARD,
                            )
                        )
                        continue
                    local = alias.asname or alias.name
                    out.append(
                        ImportBinding(
                            package=pkg,
                            local_name=local,
                            filepath=relpath,
                            line_number=node.lineno,
                            line_content=self._line(lines, node.lineno),
                            kind=BindingKind.NAMED,
                            bound_symbol=alias.name,
                        )
                    )
        return out

    def _extract_bindings_regex(self, source: str, relpath: str) -> list[ImportBinding]:
        out: list[ImportBinding] = []
        masked = mask_source(source, PYTHON_RULES)
        src_lines = source.splitlines()
        for i, line in enumerate(masked.splitlines(), start=1):
            m = re.match(r"\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?", line)
            if m:
                root = m.group(1).split(".", 1)[0]
                if self._is_sdk(root):
                    out.append(
                        ImportBinding(
                            package=self._canonical(root),
                            local_name=m.group(2) or root,
                            filepath=relpath,
                            line_number=i,
                            line_content=src_lines[i - 1] if i - 1 < len(src_lines) else "",
                            kind=BindingKind.ALIASED if m.group(2) else BindingKind.DIRECT,
                        )
                    )
                continue
            m = re.match(r"\s*from\s+([\w.]+)\s+import\s+(.+)", line)
            if m:
                root = m.group(1).split(".", 1)[0]
                if not self._is_sdk(root):
                    continue
                pkg = self._canonical(root)
                for part in re.split(r",\s*", m.group(2).strip("()")):
                    part = part.strip()
                    if not part:
                        continue
                    if part == "*":
                        out.append(
                            ImportBinding(
                                package=pkg,
                                local_name="*",
                                filepath=relpath,
                                line_number=i,
                                line_content=src_lines[i - 1] if i - 1 < len(src_lines) else "",
                                kind=BindingKind.WILDCARD,
                            )
                        )
                        continue
                    m2 = re.match(r"(\w+)(?:\s+as\s+(\w+))?", part)
                    if m2:
                        out.append(
                            ImportBinding(
                                package=pkg,
                                local_name=m2.group(2) or m2.group(1),
                                filepath=relpath,
                                line_number=i,
                                line_content=src_lines[i - 1] if i - 1 < len(src_lines) else "",
                                kind=BindingKind.NAMED,
                                bound_symbol=m2.group(1),
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
            if re.match(r"\s*(import|from)\s", line):
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

    @staticmethod
    def _line(lines: list[str], one_indexed: int) -> str:
        return lines[one_indexed - 1] if 0 < one_indexed <= len(lines) else ""

    def _is_sdk(self, module: str) -> bool:
        return (
            self._norm(module) in self._sdk_packages
            or module.replace("-", "_") in self._sdk_modules
            or module in self._sdk_modules
        )

    def _canonical(self, module: str) -> str:
        n = self._norm(module)
        return n if n in self._sdk_packages else module
