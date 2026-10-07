"""Generic import-level call-site scanning for non-JS ecosystems.

The JS scanner (:mod:`depfix.scanners.callsites`) is binding-aware: it
follows what an import is *bound to* and propagates through re-exports.
This scanner is one notch simpler -- import statements and feed-derived
symbols (old APIs to migrate, plus their replacements as migration evidence),
matched per line after comment masking. Confidence encodes exactly that
honesty: import hits carry the spec's ``import_confidence`` (HIGH only where
the pattern derives mechanically from the package name), symbol hits are
MEDIUM (with SDK import) or LOW (no import in file).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from depfix.ecosystems.base import EcosystemSpec
from depfix.ecosystems.registry import ecosystem_for_registry
from depfix.scanners.limits import MAX_SCANNABLE_FILES, clamp_max_files
from depfix.scanners.models import (
    CallSite,
    CallSiteKind,
    MatchConfidence,
    RepoScanResult,
    ScanTarget,
    dedupe_call_sites,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_FILE_BYTES = 1_000_000
_DEFAULT_MAX_FILES = MAX_SCANNABLE_FILES
_CONTEXT_LINES = 3


def _member_call_pattern(symbol: str, provider_id: str) -> re.Pattern[str] | None:
    """``openai.chat.completions.create`` -> ``.chat.completions.create(`` on any
    receiver -- real code calls it on ``client``, not on ``openai``."""
    member = (
        symbol[len(provider_id) + 1 :]
        if provider_id and symbol.startswith(f"{provider_id}.")
        else symbol.partition(".")[2]
    )
    if not member:
        return None
    return re.compile(
        r"\.\s*" + r"\s*\.\s*".join(re.escape(part) for part in member.split(".")) + r"\s*\("
    )


def mask_comments(source: str, spec: EcosystemSpec) -> str:
    """Blank out comments (preserving line structure) so a commented-out
    import doesn't become a call site."""
    masked = source
    for open_token, close_token in spec.block_comments:
        pattern = re.compile(re.escape(open_token) + r".*?" + re.escape(close_token), re.DOTALL)
        masked = pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), masked)
    if spec.line_comments:
        lines = masked.splitlines(keepends=True)
        for i, line in enumerate(lines):
            for prefix in spec.line_comments:
                index = line.find(prefix)
                # Crude string-awareness: a prefix inside quotes is more
                # likely a URL ("https://...") than a comment; skip those.
                if (
                    index >= 0
                    and line[:index].count('"') % 2 == 0
                    and line[:index].count("'") % 2 == 0
                ):
                    lines[i] = line[:index] + re.sub(r"[^\n]", " ", line[index:])
                    break
        masked = "".join(lines)
    return masked


class GenericImportScanner:
    """Scans one ecosystem's source files for SDK imports, change symbols,
    and provider anchors. Produces the same :class:`RepoScanResult` shape
    as the JS scanner so storage/reporting need no second code path."""

    def __init__(
        self,
        spec: EcosystemSpec,
        *,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        max_files: int | None = None,
        max_data_bytes: int = 0,
    ) -> None:
        self._spec = spec
        self._max_file_bytes = max_file_bytes
        self._max_files = clamp_max_files(max_files)
        self._max_data_bytes = max_data_bytes  # 0 = no cap

    def scan(self, root: Path, target: ScanTarget) -> RepoScanResult:
        spec = self._spec
        import_confidence = (
            MatchConfidence.HIGH if spec.import_confidence == "high" else MatchConfidence.MEDIUM
        )
        packages = packages_for_spec(target, spec)
        import_patterns: list[tuple[str, re.Pattern[str]]] = []
        if spec.import_regexes is not None:
            for package in packages:
                for pattern in spec.import_regexes(package):
                    import_patterns.append((package, pattern))

        feed_symbols = list(dict.fromkeys((*target.symbols, *target.feed_symbols)))
        symbol_patterns = [
            (
                symbol,
                re.compile(re.escape(symbol)),
                _member_call_pattern(symbol, target.provider_id),
            )
            for symbol in feed_symbols
        ]

        result = RepoScanResult(repo_full_name="", commit_sha="")
        sites: list[CallSite] = []
        skip = spec.all_skip_dirs()

        files: list[Path] = []
        for path in sorted(root.rglob("*")):
            if len(files) >= self._max_files:
                result.errors.append(f"file cap reached ({self._max_files}); scan truncated")
                break
            if not path.is_file() or path.suffix not in spec.source_extensions:
                continue
            relative = path.relative_to(root)
            if any(part in skip for part in relative.parts[:-1]):
                continue
            files.append(path)

        data_bytes_read = 0
        for path in files:
            try:
                file_size = path.stat().st_size
                if file_size > self._max_file_bytes:
                    continue
                if self._max_data_bytes > 0 and data_bytes_read + file_size > self._max_data_bytes:
                    break
                source = path.read_text(encoding="utf-8")
                data_bytes_read += file_size
            except (OSError, UnicodeDecodeError) as exc:
                result.errors.append(f"{path}: {exc}")
                continue
            result.files_scanned += 1
            masked = mask_comments(source, spec)
            relpath = path.relative_to(root).as_posix()
            lines = masked.splitlines()
            raw_lines = source.splitlines()
            file_imports_sdk = any(p.search(masked) for _pkg, p in import_patterns)

            for line_number, line in enumerate(lines, start=1):
                for package, pattern in import_patterns:
                    match = pattern.search(line)
                    if match:
                        sites.append(
                            _site(
                                relpath,
                                line_number,
                                match.start(),
                                raw_lines,
                                kind=CallSiteKind.SDK_IMPORT,
                                confidence=import_confidence,
                                symbol=package,
                                provider_id=target.provider_id,
                                evidence=f"{self._spec.display_name} import of {package}",
                            )
                        )
                for symbol, full_pattern, member_pattern in symbol_patterns:
                    match = full_pattern.search(line)
                    confidence = MatchConfidence.MEDIUM
                    evidence = f"references feed symbol {symbol}"
                    if match is None and member_pattern is not None:
                        match = member_pattern.search(line)
                        if match is not None:
                            confidence = (
                                MatchConfidence.MEDIUM if file_imports_sdk else MatchConfidence.LOW
                            )
                            evidence = f"calls the member path of feed symbol {symbol}" + (
                                "" if file_imports_sdk else " (no SDK import in this file)"
                            )
                    if match is not None:
                        sites.append(
                            _site(
                                relpath,
                                line_number,
                                match.start(),
                                raw_lines,
                                kind=CallSiteKind.METHOD_CALL,
                                confidence=confidence,
                                symbol=symbol,
                                provider_id=target.provider_id,
                                evidence=evidence,
                            )
                        )

        result.call_sites = dedupe_call_sites(sites)
        return result


def packages_for_spec(target: ScanTarget, spec: EcosystemSpec) -> list[str]:
    """The target's SDK packages that belong to this ecosystem.

    ``sdk_package_refs`` carries (name, registry) pairs when the caller
    built the target from a provider; a bare ``sdk_packages`` tuple (older
    callers, tests) is assumed to be all for this ecosystem.
    """
    refs = getattr(target, "sdk_package_refs", ())
    if not refs:
        return list(target.sdk_packages)
    out = []
    for name, registry in refs:
        ref_spec = ecosystem_for_registry(registry)
        if ref_spec is not None and ref_spec.id == spec.id:
            out.append(name)
    return out


def _site(
    relpath: str,
    line_number: int,
    column: int,
    raw_lines: list[str],
    *,
    kind: CallSiteKind,
    confidence: MatchConfidence,
    symbol: str,
    provider_id: str,
    evidence: str,
) -> CallSite:
    index = line_number - 1
    return CallSite(
        filepath=relpath,
        line_number=line_number,
        column=column,
        line_content=raw_lines[index] if index < len(raw_lines) else "",
        kind=kind,
        confidence=confidence,
        symbol=symbol,
        provider_id=provider_id,
        evidence=evidence,
        context_before=tuple(raw_lines[max(0, index - _CONTEXT_LINES) : index]),
        context_after=tuple(raw_lines[index + 1 : index + 1 + _CONTEXT_LINES]),
    )
