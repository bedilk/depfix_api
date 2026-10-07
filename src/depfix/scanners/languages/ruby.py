"""Ruby binding-aware scanner.

Ruby import forms:
    require 'stripe'              # loads gem, exposes Stripe module
    require 'stripe/checkout'     # nested path — still the stripe gem
    require_relative './local'    # local file — skip (not a gem)
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
from depfix.scanners.masking import RUBY_RULES, mask_source

_RB_EXTS = {".rb", ".rake", ".ru"}
_SKIP_DIRS = {"vendor", "tmp", "log", "node_modules", ".bundle", ".git"}
_REQUIRE = re.compile(r"""^\s*require\s+['"]([^'"]+)['"]""")


class RubyScanner:
    def __init__(
        self,
        packages: list[str],
        modules: dict[str, str] | None = None,
    ) -> None:
        # packages = gem names as in the Gemfile
        self._sdk_gems = set(packages)
        # gem_name → Ruby module name (e.g. "stripe" → "Stripe")
        # Provider spec should supply this for non-obvious cases.
        self._modules = modules or {}

    def scan_repo(self, repo_root: Path) -> LanguageScanResult:
        result = LanguageScanResult(language="ruby")
        for path in repo_root.rglob("*"):
            if not path.is_file() or path.suffix not in _RB_EXTS:
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
            # Extract bindings from original (require strings must not be masked);
            # mask for use-site grep only.
            file_bindings = self._extract_bindings(source, source, relpath)
            result.bindings.extend(file_bindings)
            if file_bindings:
                masked = mask_source(source, RUBY_RULES)
                result.uses.extend(self._collect_uses(masked, relpath, file_bindings))
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
            m = _REQUIRE.match(line)
            if not m:
                continue
            required = m.group(1)
            # require_relative and paths starting with ./ or / are local
            if required.startswith("./") or required.startswith("/"):
                continue
            gem = required.split("/", 1)[0]
            if gem not in self._sdk_gems:
                continue
            module_name = self._modules.get(gem, self._default_module(gem))
            orig_line = orig_lines[i - 1] if i - 1 < len(orig_lines) else line
            out.append(
                ImportBinding(
                    package=gem,
                    local_name=module_name,
                    filepath=relpath,
                    line_number=i,
                    line_content=orig_line,
                    kind=BindingKind.DIRECT,
                )
            )
        return out

    @staticmethod
    def _default_module(gem: str) -> str:
        # Convention: "stripe" → "Stripe", "omniauth-google" → "OmniauthGoogle"
        return "".join(p[:1].upper() + p[1:] for p in re.split(r"[-_]", gem) if p)

    def _collect_uses(
        self,
        masked: str,
        relpath: str,
        bindings: list[ImportBinding],
    ) -> list[SymbolUse]:
        names = {b.local_name for b in bindings}
        if not names:
            return []
        # Ruby uses Foo.method AND Foo::Bar.method
        pattern = re.compile(
            r"\b("
            + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            + r")((?:(?:\.|::)\w+)*)"
        )
        uses: list[SymbolUse] = []
        for i, line in enumerate(masked.splitlines(), start=1):
            if re.match(r"\s*require\b", line):
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
