"""Shared vocabulary for language-specific binding-aware scanners.

Every language scanner reduces its own grammar to these three record
types. The downstream call-site matcher, the hybrid CallSiteJudgeAgent,
and the RepoScanResult builder are all language-agnostic — they see
ImportBinding / SymbolUse / Anchor regardless of which grammar produced them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BindingKind(StrEnum):
    DIRECT = "direct"  # `import stripe` / `require('stripe')`
    ALIASED = "aliased"  # `import stripe as S` / `alias "pkg"`
    NAMED = "named"  # `from stripe import Client`
    WILDCARD = "wildcard"  # `from stripe import *` / `import x.y.*;`
    DOT = "dot"  # Go `import . "pkg"` — symbols land in file scope
    SIDE_EFFECT = "side_effect"  # Go `import _ "pkg"` — no bindings
    RE_EXPORT = "re_export"  # local wrapper re-exports the SDK


@dataclass(frozen=True)
class ImportBinding:
    package: str
    local_name: str
    filepath: str
    line_number: int
    line_content: str
    kind: BindingKind
    bound_symbol: str | None = None


@dataclass(frozen=True)
class SymbolUse:
    filepath: str
    line_number: int
    column: int
    line_content: str
    local_name: str
    attribute_path: str


@dataclass(frozen=True)
class Anchor:
    filepath: str
    line_number: int
    line_content: str
    pattern: str
    kind: str  # 'raw_http' | 'api_version_pin' | 'header'


@dataclass
class LanguageScanResult:
    language: str
    bindings: list[ImportBinding] = field(default_factory=list)
    uses: list[SymbolUse] = field(default_factory=list)
    anchors: list[Anchor] = field(default_factory=list)
    files_scanned: int = 0
    errors: list[str] = field(default_factory=list)
