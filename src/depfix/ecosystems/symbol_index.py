"""SDK symbol index — the real exported member surface of one SDK version.

Replaces the heuristic ``sdk_path_hints`` (``POST /v1/charges`` →
``charges.create``) with an authoritative index built from the SDK's own
``.d.ts`` files. A guess that misses produces a change nothing can ever
match, which is exactly the 14-changes/0-call-sites failure.

Two immediate wins, both free:

* ``removed_since`` gives deterministic, authoritative removed-API lists.
* ``resolve("findMany")`` → ``["PrismaClient.<model>.findMany"]``, so a
  classifier's bare name becomes a matchable symbol.
"""

from __future__ import annotations

import io
import json
import logging
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from depfix.registry.client import NpmRegistryClient

logger = logging.getLogger(__name__)

_MAX_TARBALL_BYTES = 30_000_000

_MEMBER_RE = re.compile(
    r"^\s*(?:export\s+)?(?:declare\s+)?(?:readonly\s+)?"
    r"([A-Za-z_$][\w$]*)\s*[\(:?]",
    re.MULTILINE,
)

_EXPORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*export\s+declare\s+"
        r"(?:function|class|const|let|var|enum|abstract\s+class)\s+"
        r"([A-Za-z_$][\w$]*)",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*export\s+"
        r"(?:interface|type|enum|class|function|const|abstract\s+class)\s+"
        r"([A-Za-z_$][\w$]*)",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*export\s+default\s+(?:function|class)\s+([A-Za-z_$][\w$]*)",
        re.MULTILINE,
    ),
)
_EXPORT_LIST_RE = re.compile(r"export\s*\{([^}]*)\}", re.DOTALL)

_CLASS_RE = re.compile(
    r"(?:export\s+)?(?:declare\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)",
)
_INTERFACE_RE = re.compile(
    r"(?:export\s+)?(?:declare\s+)?interface\s+([A-Za-z_$][\w$]*)",
)


def _names_in_dts(text: str) -> set[str]:
    names: set[str] = set()
    for pattern in _EXPORT_PATTERNS:
        names.update(pattern.findall(text))
    for block in _EXPORT_LIST_RE.findall(text):
        for entry in block.split(","):
            entry = entry.strip()
            if not entry or entry == "default":
                continue
            name = entry.split(" as ")[-1].strip().removeprefix("type ").strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                names.add(name)
    return names


def _dotted_members_in_dts(text: str) -> dict[str, set[str]]:
    """Extract ``Class.member`` pairs from .d.ts declarations."""
    result: dict[str, set[str]] = {}
    current_class: str | None = None
    brace_depth = 0

    for line in text.splitlines():
        stripped = line.strip()

        cls_match = _CLASS_RE.search(stripped) or _INTERFACE_RE.search(stripped)
        if cls_match and "{" in stripped:
            current_class = cls_match.group(1)
            brace_depth = 1
            continue

        if current_class is not None:
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0:
                current_class = None
                continue
            member_match = _MEMBER_RE.match(stripped)
            if member_match:
                member = member_match.group(1)
                if member not in ("constructor", "static", "private", "protected"):
                    result.setdefault(current_class, set()).add(member)

    return result


@dataclass(frozen=True)
class SymbolIndex:
    """The real dotted member surface of one SDK version."""

    package: str
    version: str
    symbols: frozenset[str]
    roots: frozenset[str]

    def resolve(self, name: str) -> tuple[str, ...]:
        """Candidates whose member path ends with ``name``'s leaf."""
        tail = name.split(".")[-1]
        return tuple(sorted(s for s in self.symbols if s.split(".")[-1] == tail))

    def removed_since(self, older: SymbolIndex) -> frozenset[str]:
        """Symbols present in ``older`` but absent in ``self``."""
        return older.symbols - self.symbols


def _cache_path(cache_dir: Path, package: str, version: str) -> Path:
    safe = package.replace("/", "__").replace("@", "")
    return cache_dir / f"{safe}@{version}.json"


def _load_cached(path: Path) -> SymbolIndex | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SymbolIndex(
            package=data["package"],
            version=data["version"],
            symbols=frozenset(data["symbols"]),
            roots=frozenset(data["roots"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _save_cached(path: Path, index: SymbolIndex) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "package": index.package,
                "version": index.version,
                "symbols": sorted(index.symbols),
                "roots": sorted(index.roots),
            }
        ),
        encoding="utf-8",
    )


def build_index_from_tarball(package: str, version: str, blob: bytes) -> SymbolIndex:
    """Build a SymbolIndex from a downloaded npm tarball."""
    roots: set[str] = set()
    symbols: set[str] = set()

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".d.ts"):
                continue
            if member.size > 2_000_000:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            text = handle.read().decode("utf-8", errors="replace")

            names = _names_in_dts(text)
            roots |= names
            symbols |= names

            members = _dotted_members_in_dts(text)
            for cls_name, member_names in members.items():
                for m in member_names:
                    symbols.add(f"{cls_name}.{m}")

    return SymbolIndex(
        package=package,
        version=version,
        symbols=frozenset(symbols),
        roots=frozenset(roots),
    )


def build_index(
    package: str,
    version: str,
    *,
    http: httpx.Client,
    npm: NpmRegistryClient,
    cache_dir: Path | None = None,
) -> SymbolIndex | None:
    """Build or load a cached SymbolIndex for ``package@version``."""
    if cache_dir is not None:
        cached = _load_cached(_cache_path(cache_dir, package, version))
        if cached is not None:
            return cached

    scope_safe = package.replace("/", "%2f")
    short = package.rsplit("/", 1)[-1]
    url = f"{npm.base_url}/{scope_safe}/-/{short}-{version}.tgz"

    try:
        response = http.get(url)
        if response.status_code != 200 or len(response.content) > _MAX_TARBALL_BYTES:
            logger.debug(
                "symbol_index: fetch failed for %s@%s (status=%s)",
                package,
                version,
                response.status_code,
            )
            return None
    except httpx.HTTPError as exc:
        logger.debug("symbol_index: fetch error for %s@%s: %s", package, version, exc)
        return None

    try:
        index = build_index_from_tarball(package, version, response.content)
    except (tarfile.TarError, OSError) as exc:
        logger.debug("symbol_index: tarball parse failed for %s@%s: %s", package, version, exc)
        return None

    if cache_dir is not None:
        _save_cached(_cache_path(cache_dir, package, version), index)

    return index
