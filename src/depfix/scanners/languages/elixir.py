"""Elixir binding-aware scanner.

Elixir module directives:
  alias   MyApp.Stripe              # short alias: Stripe
  alias   MyApp.Stripe, as: Pay     # explicit alias: Pay
  import  Stripe                    # imports functions into scope
  use     Stripe                    # injects code via macro (__using__)
  require Stripe                    # makes macros available

Dependencies in mix.exs use atom names like {:stripe, "~> 2.0"}.
The module name in code is typically the CamelCase gem name (Stripe, OpenAI).

Because Elixir doesn't have import-style package names (you reference modules
by their CamelCase module name directly), we match on:
1. `alias <ModulePath>` lines where the root matches a known SDK module
2. Direct module references in code: `Stripe.charge(...)`, `OpenAI.chat(...)`
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
from depfix.scanners.masking import MaskingRules, mask_source

_EX_EXTS = {".ex", ".exs"}
_SKIP_DIRS = {"_build", ".elixir_ls", "deps", "node_modules", ".git", "cover"}

ELIXIR_RULES = MaskingRules(
    line_comment=("#",),
    # Elixir heredocs: ~s"""...""" and ~S"""...""" — approximate with triple
    triple_string_delims=('"""', "'''"),
    string_delims=('"',),
)

# alias Foo.Bar, as: Baz  or  alias Foo.Bar
_ALIAS = re.compile(r"^\s*alias\s+([\w.]+)(?:\s*,\s*as:\s*(\w+))?")
# import / use / require Foo.Bar
_DIRECTIVE = re.compile(r"^\s*(?:import|use|require)\s+([\w.]+)")


class ElixirScanner:
    def __init__(self, packages: list[str]) -> None:
        # In Elixir, SDK "packages" are module name prefixes (e.g. "Stripe", "OpenAI")
        # Providers should list the Elixir module root, not the hex package name.
        self._sdk_modules = list(packages)

    def scan_repo(self, repo_root: Path) -> LanguageScanResult:
        result = LanguageScanResult(language="elixir")
        for path in repo_root.rglob("*"):
            if not path.is_file() or path.suffix not in _EX_EXTS:
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

            # Bindings from original (module names in aliases must not be masked)
            file_bindings = self._extract_bindings(source, relpath)
            result.bindings.extend(file_bindings)

            if file_bindings:
                masked = mask_source(source, ELIXIR_RULES)
                result.uses.extend(self._collect_uses(masked, relpath, file_bindings))
        return result

    def _extract_bindings(self, source: str, relpath: str) -> list[ImportBinding]:
        out: list[ImportBinding] = []
        for i, line in enumerate(source.splitlines(), start=1):
            # alias Foo.Bar or alias Foo.Bar, as: Baz
            m = _ALIAS.match(line)
            if m:
                module = m.group(1)
                if self._is_sdk(module):
                    alias = m.group(2)
                    local = alias if alias else module.rsplit(".", 1)[-1]
                    out.append(
                        ImportBinding(
                            package=self._canonical(module),
                            local_name=local,
                            filepath=relpath,
                            line_number=i,
                            line_content=line,
                            kind=BindingKind.ALIASED if alias else BindingKind.DIRECT,
                            bound_symbol=module,
                        )
                    )
                continue

            # import / use / require Foo.Bar
            m = _DIRECTIVE.match(line)
            if m:
                module = m.group(1)
                if self._is_sdk(module):
                    local = module.rsplit(".", 1)[-1]
                    out.append(
                        ImportBinding(
                            package=self._canonical(module),
                            local_name=local,
                            filepath=relpath,
                            line_number=i,
                            line_content=line,
                            kind=BindingKind.NAMED,
                            bound_symbol=module,
                        )
                    )
        return out

    def _collect_uses(
        self,
        masked: str,
        relpath: str,
        bindings: list[ImportBinding],
    ) -> list[SymbolUse]:
        names = {b.local_name for b in bindings}
        if not names:
            return []
        # Elixir: Foo.bar(args) or Foo.Bar.baz()
        pattern = re.compile(
            r"\b("
            + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            + r")((?:\.\w+)*)"
        )
        uses: list[SymbolUse] = []
        for i, line in enumerate(masked.splitlines(), start=1):
            if re.match(r"\s*(?:alias|import|use|require)\s", line):
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

    def _is_sdk(self, module: str) -> bool:
        root = module.split(".", 1)[0]
        return any(
            root == s or module == s or module.startswith(s + ".") for s in self._sdk_modules
        )

    def _canonical(self, module: str) -> str:
        root = module.split(".", 1)[0]
        for s in self._sdk_modules:
            if root == s or module == s or module.startswith(s + "."):
                return s
        return root
