"""PHP binding-aware scanner.

PHP import forms (PSR-4 / Composer):
  use Stripe\\Stripe;                    # class import
  use Stripe\\Stripe as StripeClient;    # aliased
  use Stripe\\{Charge, Customer};        # grouped (PHP 7+)
  use function Stripe\\someFunction;     # function import
  use const Stripe\\SOME_CONST;          # const import
  require 'vendor/autoload.php';         # Composer autoloader (skip — not a binding)
  require_once __DIR__ . '/stripe.php';  # local file (skip)

After `use Stripe\\Stripe`, code calls `Stripe::charge(...)` or
`new Stripe(...)`. After `use Stripe\\Stripe as S`, it calls `S::charge(...)`.

PHP backslash namespace separator is normalised to forward-slash for
matching against provider package names (composer uses vendor/package format,
but SDK classes use backslash namespace notation).
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

_PHP_EXTS = {".php", ".phtml", ".php8", ".php7"}
_SKIP_DIRS = {"vendor", "node_modules", ".git", "storage", "cache", "var"}

PHP_RULES = MaskingRules(
    line_comment=("//", "#"),
    block_comment=(("/*", "*/"),),
    string_delims=('"', "'"),
)

# use [function|const] Namespace\Class [as Alias];
_USE = re.compile(
    r"^\s*use\s+(?:function\s+|const\s+)?"
    r"(?P<fqn>[\w\\]+(?:\\\{[^}]+\})?)"
    r"(?:\s+as\s+(?P<alias>\w+))?\s*;"
)
# grouped: use Stripe\{Charge, Customer as Cust};
_USE_GROUPED = re.compile(r"^\s*use\s+(?P<ns>[\w\\]+)\\\{(?P<names>[^}]+)\}\s*;")


def _normalise_ns(ns: str) -> str:
    return ns.replace("\\", "/").strip("/")


class PhpScanner:
    def __init__(self, packages: list[str]) -> None:
        # packages are Composer vendor/package paths or top-level namespace roots.
        # e.g. "stripe/stripe-php" → namespace root "Stripe"
        # Providers should list the PHP namespace root (e.g. "Stripe", "Anthropic").
        self._sdk_namespaces = list(packages)

    def scan_repo(self, repo_root: Path) -> LanguageScanResult:
        result = LanguageScanResult(language="php")
        for path in repo_root.rglob("*"):
            if not path.is_file() or path.suffix not in _PHP_EXTS:
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

            # Extract bindings from original source (namespaces are in use statements)
            file_bindings = self._extract_bindings(source, relpath)
            result.bindings.extend(file_bindings)

            if file_bindings:
                masked = mask_source(source, PHP_RULES)
                result.uses.extend(self._collect_uses(masked, relpath, file_bindings))
        return result

    def _extract_bindings(self, source: str, relpath: str) -> list[ImportBinding]:
        out: list[ImportBinding] = []
        for i, line in enumerate(source.splitlines(), start=1):
            # Grouped: use Stripe\{Charge, Customer as Cust};
            m = _USE_GROUPED.match(line)
            if m:
                ns = m.group("ns")
                if not self._is_sdk(ns):
                    continue
                canonical = self._canonical(ns)
                for part in m.group("names").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    m2 = re.match(r"(\w+)(?:\s+as\s+(\w+))?", part)
                    if not m2:
                        continue
                    out.append(
                        ImportBinding(
                            package=canonical,
                            local_name=m2.group(2) or m2.group(1),
                            filepath=relpath,
                            line_number=i,
                            line_content=line,
                            kind=BindingKind.ALIASED if m2.group(2) else BindingKind.NAMED,
                            bound_symbol=f"{ns}\\{m2.group(1)}",
                        )
                    )
                continue

            # Single: use Namespace\Class [as Alias];
            m = _USE.match(line)
            if not m:
                continue
            fqn = m.group("fqn")
            if not self._is_sdk(fqn):
                continue
            canonical = self._canonical(fqn)
            alias = m.group("alias")
            last = fqn.rsplit("\\", 1)[-1]
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
        # PHP call patterns: Foo::method(...), new Foo(...), Foo->method(...)
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")"
            r"((?:::\w+|->(?:\w+))*)"
        )
        uses: list[SymbolUse] = []
        for i, line in enumerate(masked.splitlines(), start=1):
            if re.match(r"\s*use\s", line):
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

    def _is_sdk(self, fqn: str) -> bool:
        root = fqn.split("\\", 1)[0]
        return any(root == s or fqn.startswith(s + "\\") for s in self._sdk_namespaces)

    def _canonical(self, fqn: str) -> str:
        root = fqn.split("\\", 1)[0]
        for s in self._sdk_namespaces:
            if root == s or fqn.startswith(s + "\\"):
                return s
        return root
