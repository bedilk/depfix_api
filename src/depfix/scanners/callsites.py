"""Three-pass, binding-aware call-site scanner.

Grepping for a bare string like ``openai.chat.completions.create`` misses the
overwhelming majority of real call sites: repos wrap the SDK in a local
module (``import { client } from './lib/openai'``), re-export it under a
different name, or destructure a single function out of it
(``import { createModeration } from 'openai'``). A naive grep either misses
these entirely or, worse, can't tell a real hit from a coincidental string
match.

This scanner instead tracks *bindings* -- "the local name ``oaiClient`` is an
instance of the ``openai`` provider's client" -- and follows them across
files through relative imports, so that matching happens against the
provider's canonical dotted symbol (``openai.moderations.create``) rather
than whatever a given file happens to call the variable.

**Three passes, per scan:**

1. **Collect** -- regex-parse every file's imports/requires, ``new X(...)``
   constructions, and re-exports into a same-file map of local name ->
   :class:`_Binding`, plus a list of unresolved relative imports.
2. **Propagate** -- repeatedly resolve relative imports against already-known
   bindings in the target file, until nothing new resolves or
   ``MAX_PROPAGATION_HOPS`` is hit. A propagated binding inherits the
   confidence of the SDK binding it traces back to: distance from the SDK
   import does not matter. A call site is either traced to the SDK (found,
   actionable) or not reported at all. The hop count is kept only in
   ``evidence`` for transparency.
3. **Match** -- walk each file's comment-masked source for member calls,
   bare calls (only against bindings known to be a destructured bare
   function), raw HTTP calls against known ``api_base_urls``, API-version
   literals. ``feed_symbols`` (old APIs + replacements) are also matched
   so a narrowed scan can still detect "already migrated" (CURRENT) sites.

Regex, not an AST. Week 3's own risk note (see ``docs/plan.md``): if
measured recall against the eval corpus falls below ~85%, that's the
trigger to bring in tree-sitter -- not a silent guess made here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from depfix.scanners.base import CodebaseScanner
from depfix.scanners.limits import MAX_SCANNABLE_FILES, clamp_max_files
from depfix.scanners.models import (
    CallSite,
    CallSiteKind,
    MatchConfidence,
    RepoScanResult,
    ScanTarget,
)

logger = logging.getLogger(__name__)

MAX_PROPAGATION_HOPS = 4
CONTEXT_LINES = 3
_MAX_FILE_BYTES = 2_000_000  # skip pathologically large bundled/generated files
_DEFAULT_MAX_FILES = MAX_SCANNABLE_FILES

_SKIP_DIRS = CodebaseScanner.SKIP_DIRS
_SUPPORTED_EXTENSIONS = CodebaseScanner.SUPPORTED_EXTENSIONS

# --- import / require forms --------------------------------------------
# ``(?:type\s+)?`` tolerates TypeScript's ``import type { Foo } from '...'``,
# which is erased at compile time but still a valid binding source for
# matching purposes -- a repo mixing type-only and value imports of the same
# SDK is common enough that failing to parse it would cost real recall.

_IMPORT_DEFAULT_RE = re.compile(
    r"""import\s+(?:type\s+)?(\w+)\s+from\s+['"](\.{0,2}/[^'"]+|[^'"./][^'"]*)['"]"""
)
_IMPORT_NAMESPACE_RE = re.compile(
    r"""import\s+(?:type\s+)?\*\s+as\s+(\w+)\s+from\s+['"]([^'"]+)['"]"""
)
_IMPORT_NAMED_RE = re.compile(r"""import\s+(?:type\s+)?\{([^}]+)\}\s+from\s+['"]([^'"]+)['"]""")
_IMPORT_DEFAULT_AND_NAMED_RE = re.compile(
    r"""import\s+(?:type\s+)?(\w+)\s*,\s*\{([^}]+)\}\s+from\s+['"]([^'"]+)['"]"""
)
_REQUIRE_BARE_RE = re.compile(
    r"""(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)"""
)
_REQUIRE_DESTRUCTURE_RE = re.compile(
    r"""(?:const|let|var)\s*\{([^}]+)\}\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)"""
)

# --- construction ---------------------------------------------------------

_NEW_CONSTRUCT_RE = re.compile(r"""\bnew\s+(\w+)\s*\(""")
# Matches ``const client = algoliasearch('APP', 'KEY')`` and similar factory-
# function patterns where an SDK default/namespace import is called directly
# (not via `new`) to produce a client instance.
_FACTORY_CALL_RE = re.compile(
    r"""(?:const|let|var)\s+(\w+)\s*=\s*(?:await\s+)?(\w+(?:\.\w+)*)\s*\("""
)

# --- re-exports (wrapper modules) ----------------------------------------

_EXPORT_DEFAULT_NAME_RE = re.compile(r"""export\s+default\s+(\w+)\b""")
_EXPORT_NAMED_LIST_RE = re.compile(r"""export\s*\{([^}]+)\}\s*;?\s*$""", re.MULTILINE)
_EXPORT_DECL_RE = re.compile(
    # ``export const alias = existingLocal`` -- aliasing an existing bare
    # local. The negative lookahead excludes ``export const x = new Foo()``
    # / ``export const x = someCall()``, which are construction/call
    # expressions handled by `_collect_constructions`, not name aliases;
    # without it, "new"/the callee name would be captured as if it were the
    # aliased local, poisoning `_resolve_export` for wrapper modules that
    # declare-and-export a client in one statement.
    r"""export\s+(?:const|let|var)\s+(\w+)\s*=\s*(?!new\b)(\w+)\b(?!\s*\()"""
)
_MODULE_EXPORTS_ASSIGN_RE = re.compile(r"""module\.exports\s*=\s*(\w+)\s*;?\s*$""", re.MULTILINE)
# ``module.exports = { openai }`` -- the object-literal-shorthand form of a
# CJS re-export, at least as common as the single-identifier assignment
# above (``_MODULE_EXPORTS_ASSIGN_RE``) once a wrapper module exports more
# than one thing. Only plain/shorthand names are handled (not ``{ a: b }``
# property-remapping) -- that's a rarer form and, left unhandled, just costs
# a bit of recall rather than producing a wrong binding.
_MODULE_EXPORTS_OBJECT_RE = re.compile(r"""module\.exports\s*=\s*\{([^}]+)\}""")
_MODULE_EXPORTS_PROP_RE = re.compile(r"""module\.exports\.(\w+)\s*=\s*(\w+)\b""")
_EXPORTS_PROP_RE = re.compile(r"""(?<!module\.)exports\.(\w+)\s*=\s*(\w+)\b""")

# --- usage -----------------------------------------------------------------

_MEMBER_CALL_RE = re.compile(r"""\b(\w+)((?:\.\w+)+)\s*\(""")
_BARE_CALL_RE = re.compile(r"""(?<![.\w])(\w+)\s*\(""")
_FETCH_LIKE_RE = re.compile(r"""\b(?:fetch|axios(?:\.\w+)?|got|request)\s*\(""")


def _version_pin_re(provider_id: str) -> re.Pattern[str]:
    """``apiVersion``/``api_version`` plus this provider's own dated version
    header, derived rather than enumerated so a new provider needs no
    scanner edit (``Stripe-Version``, ``anthropic-version``)."""
    keys = ["apiVersion", "api_version"]
    if provider_id:
        keys.append(re.escape(f"{provider_id}-Version"))
    return re.compile(rf"(?:{'|'.join(keys)})\s*[:=]\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


# ``apiVersion``/``api_version`` are common field names on config objects
# with nothing to do with any provider this scanner targets (Kubernetes
# manifests-as-JS and plenty of unrelated SDKs both use ``apiVersion:
# "apps/v1"``-shaped literals) -- unlike provider-specific headers like
# ``Stripe-Version``, which are unambiguous. Gate the generic field names on the literal actually looking
# like a dated API version pin (``"2020-08-27"``, ``"2023-05-15-preview"``,
# ...) -- the convention this scanner is meant to catch per
# ``CallSiteKind.API_VERSION_PIN`` -- so a same-named-but-unrelated config
# field doesn't get reported as a provider call site.
_DATED_VERSION_RE = re.compile(r"""\d{4}-\d{2}-\d{2}""")


def mask_comments(source: str) -> str:
    """Blank out ``//`` and ``/* */`` comments, preserving every other
    byte's position (offsets stay valid) and every string literal's
    *contents* verbatim -- import paths, URLs, and version-pin literals live
    inside strings and must stay visible to every regex pass above.
    """
    out = list(source)
    n = len(source)
    i = 0
    in_string: str | None = None  # active quote char, or None
    while i < n:
        ch = source[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                if source[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
            continue
        i += 1
    return "".join(out)


MAX_CHAIN_HOPS = 3


@dataclass
class _Binding:
    """What a local name in one file is believed to refer to."""

    kind: CallSiteKind
    confidence: MatchConfidence
    provider_id: str
    hops: int = 0
    evidence: str = ""
    # True for a destructured bare function (``createModeration``), False
    # for a module/namespace/client-instance binding (``openai``/``stripe``).
    is_bare_symbol: bool = False
    # Accessor path already accumulated onto this binding, "" for a root
    # client. ``const b = storage.bucket(n)`` binds ``b`` with path
    # ``"bucket"``, so ``b.setCors()`` emits ``provider.bucket.setCors``.
    path: str = ""
    # Number of accessor/factory steps folded into ``path``. Deliberately
    # separate from ``hops`` (file boundaries crossed): a client re-exported
    # through several wrapper files must still be able to follow a chain.
    chain_depth: int = 0


@dataclass
class _RelativeImportRef:
    local_name: str
    imported_name: str  # "*" for a namespace/default import of the whole module
    target_path: Path


@dataclass
class _FileFacts:
    path: Path
    rel_path: str
    lines: list[str]
    masked_lines: list[str]
    direct_bindings: dict[str, _Binding] = field(default_factory=dict)
    relative_imports: list[_RelativeImportRef] = field(default_factory=list)
    # local name -> the name that same file imports/requires it as, for
    # re-export resolution (``export { client }`` after ``import { client }``).
    export_map: dict[str, str] = field(default_factory=dict)


def _split_names(raw: str) -> list[tuple[str, str]]:
    """``"Foo as bar, Baz"`` -> ``[("Foo", "bar"), ("Baz", "Baz")]`` (imported
    name, local name) -- resolves ``as`` aliases, since the local name is
    what matching cares about, not the imported name."""
    names: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if " as " in part:
            _, _, alias = part.partition(" as ")
            names.append((part.split(" as ")[0].strip(), alias.strip()))
        else:
            names.append((part, part))
    return names


def resolve_relative_import(from_file: Path, spec: str) -> Path | None:
    """Resolve a relative import spec (``./foo``, ``../lib/bar``) against
    ``from_file``'s directory, trying the usual extensionless / index
    fallbacks. Returns ``None`` for non-relative specs (bare package names).

    Public (not ``_``-prefixed) because :mod:`depfix.verify.attribution`
    needs the exact same resolution logic to answer "does this test file
    import that source file" -- a second, slightly-different resolver would
    silently drift from what the scanner itself considers a match.
    """
    if not spec.startswith("."):
        return None
    base = (from_file.parent / spec).resolve()
    candidates = [base]
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        candidates.append(base.with_suffix(ext) if not base.suffix else base)
        candidates.append(Path(str(base) + ext))
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        candidates.append(base / f"index{ext}")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class CallSiteScanner:
    """Finds call sites for a :class:`ScanTarget` across one checkout."""

    def __init__(
        self,
        context_lines: int = CONTEXT_LINES,
        *,
        max_file_bytes: int = _MAX_FILE_BYTES,
        max_files: int | None = None,
    ) -> None:
        self.context_lines = context_lines
        self.max_file_bytes = max_file_bytes
        # Always an int after clamping -- `None` resolves to the hard ceiling,
        # never to "unlimited".
        self.max_files = clamp_max_files(max_files)

    def scan(self, root: Path, target: ScanTarget) -> RepoScanResult:
        # `resolve_relative_import` resolves relative-import targets with
        # `Path.resolve()`; if `root` itself isn't already resolved, those
        # targets silently fail to match `facts_by_path`'s keys below (a
        # relative vs. absolute/symlink-resolved Path never compares equal),
        # and cross-file propagation quietly finds nothing -- not an error,
        # just a scan that under-reports. Resolving once here means callers
        # don't need to know that detail.
        root = Path(root).resolve()
        result = RepoScanResult(repo_full_name="", commit_sha="")
        files = self._discover_files(root)

        facts_by_path: dict[Path, _FileFacts] = {}
        for path in files:
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                result.errors.append(f"could not read {path}: {exc}")
                continue
            result.files_scanned += 1
            lines = content.split("\n")
            masked = mask_comments(content).split("\n")
            facts = _FileFacts(
                path=path,
                rel_path=path.relative_to(root).as_posix(),
                lines=lines,
                masked_lines=masked,
            )
            self._collect(facts, target)
            facts_by_path[path] = facts

        self._propagate(facts_by_path)

        sites: list[CallSite] = []
        for facts in facts_by_path.values():
            sites.extend(self._match_file(facts, target))

        from depfix.scanners.models import dedupe_call_sites

        result.call_sites = dedupe_call_sites(sites)
        return result

    def _discover_files(self, root: Path) -> list[Path]:
        found: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in _SUPPORTED_EXTENSIONS:
                continue
            if any(part in _SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
                continue
            try:
                if path.stat().st_size > self.max_file_bytes:
                    continue
            except OSError:
                continue
            found.append(path)
        found.sort()
        if len(found) > self.max_files:
            logger.warning(
                "repo has %d scannable files, capping at max_files=%d", len(found), self.max_files
            )
            found = found[: self.max_files]
        return found

    # -- pass 1: collect ---------------------------------------------------

    def _collect(self, facts: _FileFacts, target: ScanTarget) -> None:
        source = "\n".join(facts.masked_lines)
        self._collect_imports(facts, source, target)
        self._collect_constructions(facts, source)
        self._collect_exports(facts, source)

    def _collect_imports(self, facts: _FileFacts, source: str, target: ScanTarget) -> None:
        for match in _IMPORT_DEFAULT_RE.finditer(source):
            local, spec = match.group(1), match.group(2)
            self._bind_or_defer(facts, local, "*", spec, target)
        for match in _IMPORT_NAMESPACE_RE.finditer(source):
            local, spec = match.group(1), match.group(2)
            self._bind_or_defer(facts, local, "*", spec, target)
        for match in _IMPORT_NAMED_RE.finditer(source):
            names, spec = match.group(1), match.group(2)
            for imported, local in _split_names(names):
                self._bind_or_defer(facts, local, imported, spec, target)
        for match in _IMPORT_DEFAULT_AND_NAMED_RE.finditer(source):
            default_local, names, spec = match.group(1), match.group(2), match.group(3)
            self._bind_or_defer(facts, default_local, "*", spec, target)
            for imported, local in _split_names(names):
                self._bind_or_defer(facts, local, imported, spec, target)
        for match in _REQUIRE_BARE_RE.finditer(source):
            local, spec = match.group(1), match.group(2)
            self._bind_or_defer(facts, local, "*", spec, target)
        for match in _REQUIRE_DESTRUCTURE_RE.finditer(source):
            names, spec = match.group(1), match.group(2)
            for imported, local in _split_names(names):
                self._bind_or_defer(facts, local, imported, spec, target)

    def _bind_or_defer(
        self, facts: _FileFacts, local: str, imported_name: str, spec: str, target: ScanTarget
    ) -> None:
        relative_target = resolve_relative_import(facts.path, spec)
        if relative_target is not None:
            facts.relative_imports.append(
                _RelativeImportRef(
                    local_name=local, imported_name=imported_name, target_path=relative_target
                )
            )
            return

        matched_package = next(
            (pkg for pkg in target.sdk_packages if spec == pkg or spec.startswith(pkg + "/")),
            None,
        )
        if matched_package is None:
            return

        is_bare_symbol = imported_name not in (None, "*")
        facts.direct_bindings[local] = _Binding(
            kind=CallSiteKind.SDK_IMPORT,
            confidence=MatchConfidence.HIGH,
            provider_id=target.provider_id,
            hops=0,
            evidence=f"imported from {spec!r}",
            is_bare_symbol=is_bare_symbol,
        )

    def _collect_constructions(self, facts: _FileFacts, source: str) -> None:
        for match in _NEW_CONSTRUCT_RE.finditer(source):
            base_name = match.group(1)
            base = facts.direct_bindings.get(base_name)
            if base is None:
                continue
            # Note: `base.is_bare_symbol` is deliberately *not* checked here.
            # A destructured class import (``const { OpenAIApi } =
            # require("openai")``) is bound as bare_symbol=True by
            # `_bind_or_defer` -- the regex layer can't tell "destructured
            # class" from "destructured function" apart from each other --
            # but ``new OpenAIApi(...)`` is unambiguous evidence that this
            # particular usage is a constructor call, not a bare function
            # call, regardless of how the name was imported.
            line_no = source.count("\n", 0, match.start()) + 1
            # The constructed *instance* is what a wrapper module actually
            # re-exports (``const client = new OpenAI(); export default client``),
            # so also promote the assignment target, if this construction is
            # the right-hand side of one, to the same binding.
            line = facts.lines[line_no - 1] if 0 <= line_no - 1 < len(facts.lines) else ""
            # ``export const x = new Foo()`` is the common TS/ESM
            # declare-and-export-in-one-statement wrapper pattern -- the
            # leading `export` keyword must not prevent the assignment
            # target from being recognized here.
            assign = re.match(r"""\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=""", line)
            if assign:
                facts.direct_bindings[assign.group(1)] = _Binding(
                    kind=CallSiteKind.CLIENT_CONSTRUCTION,
                    confidence=base.confidence,
                    provider_id=base.provider_id,
                    hops=base.hops,
                    evidence=f"constructed from {base_name}",
                    is_bare_symbol=False,
                )

        for match in _FACTORY_CALL_RE.finditer(source):
            assigned_name = match.group(1)
            callee = match.group(2)
            # callee may be dotted: "storage.bucket" → head "storage", accessor "bucket"
            head, _, accessor = callee.partition(".")
            if assigned_name == callee:
                continue
            base = facts.direct_bindings.get(head)
            if base is None:
                continue
            if base.chain_depth >= MAX_CHAIN_HOPS:
                continue
            new_path = ".".join(p for p in (base.path, accessor) if p)
            # Allow dotted factory callees (storage.bucket) even when base
            # isn't a bare SDK import, as long as it's a known provider binding.
            if (
                not accessor
                and not base.is_bare_symbol
                and base.kind is not CallSiteKind.SDK_IMPORT
            ):
                continue
            facts.direct_bindings.setdefault(
                assigned_name,
                _Binding(
                    kind=CallSiteKind.CLIENT_CONSTRUCTION,
                    confidence=base.confidence,
                    provider_id=base.provider_id,
                    hops=base.hops,
                    evidence=(
                        f"derived from {callee}(...)" if accessor else f"factory call on {head}"
                    ),
                    is_bare_symbol=False,
                    path=new_path,
                    chain_depth=base.chain_depth + (1 if accessor else 0),
                ),
            )

    def _collect_exports(self, facts: _FileFacts, source: str) -> None:
        for match in _EXPORT_DEFAULT_NAME_RE.finditer(source):
            facts.export_map["default"] = match.group(1)
        for match in _EXPORT_NAMED_LIST_RE.finditer(source):
            for imported, local in _split_names(match.group(1)):
                # ``export { client }`` -- local var name is `imported` here
                # since there's no rename; map the exported name to it.
                facts.export_map[local] = imported
        for match in _MODULE_EXPORTS_OBJECT_RE.finditer(source):
            # ``module.exports = { client }`` -- the CJS equivalent of the
            # ESM named-list form just above, same shorthand-only handling.
            for imported, local in _split_names(match.group(1)):
                facts.export_map[local] = imported
        for match in _EXPORT_DECL_RE.finditer(source):
            facts.export_map[match.group(1)] = match.group(2)
        for match in _MODULE_EXPORTS_ASSIGN_RE.finditer(source):
            facts.export_map["default"] = match.group(1)
        for match in _MODULE_EXPORTS_PROP_RE.finditer(source):
            facts.export_map[match.group(1)] = match.group(2)
        for match in _EXPORTS_PROP_RE.finditer(source):
            facts.export_map[match.group(1)] = match.group(2)

    # -- pass 2: propagate ---------------------------------------------------

    def _propagate(self, facts_by_path: dict[Path, _FileFacts]) -> None:
        for _ in range(MAX_PROPAGATION_HOPS):
            changed = False
            for facts in facts_by_path.values():
                for ref in facts.relative_imports:
                    if ref.local_name in facts.direct_bindings:
                        continue
                    target_facts = facts_by_path.get(ref.target_path)
                    if target_facts is None:
                        continue
                    source_binding = self._resolve_export(target_facts, ref.imported_name)
                    if source_binding is None:
                        continue
                    new_hops = source_binding.hops + 1
                    facts.direct_bindings[ref.local_name] = _Binding(
                        kind=CallSiteKind.WRAPPER_IMPORT,
                        # Found is found: a binding traced through N wrapper
                        # files is the same SDK binding as a direct import.
                        confidence=source_binding.confidence,
                        provider_id=source_binding.provider_id,
                        hops=new_hops,
                        evidence=f"re-exported via {target_facts.rel_path} ({new_hops} hop(s))",
                        is_bare_symbol=source_binding.is_bare_symbol,
                        path=source_binding.path,
                        chain_depth=source_binding.chain_depth,
                    )
                    changed = True
            if not changed:
                break

    def _resolve_export(self, facts: _FileFacts, imported_name: str) -> _Binding | None:
        """What does ``facts`` export under ``imported_name`` (or as its
        default/namespace, if ``imported_name`` is ``"*"``) resolve to,
        among that file's own already-known bindings?"""
        if imported_name == "*":
            local = facts.export_map.get("default")
            if local is None:
                # A namespace import of a module with no single default
                # export -- fall back to any direct SDK binding in that file.
                for binding in facts.direct_bindings.values():
                    return binding
                return None
        else:
            local = facts.export_map.get(imported_name, imported_name)
        return facts.direct_bindings.get(local)

    # -- pass 3: match -------------------------------------------------------

    def _match_file(self, facts: _FileFacts, target: ScanTarget) -> list[CallSite]:
        sites: list[CallSite] = []
        source = "\n".join(facts.masked_lines)

        # Narrowing (explicit selection) still admits the change's replacement
        # symbols, so a narrowed scan can tell "migrated" from "absent".
        wanted_symbols = (*target.symbols, *target.feed_symbols) if target.symbols else ()

        def symbol_wanted(symbol: str) -> bool:
            return not wanted_symbols or any(
                symbol == wanted or symbol.startswith(f"{wanted}.") for wanted in wanted_symbols
            )

        for match in _MEMBER_CALL_RE.finditer(source):
            base_name, chain = match.group(1), match.group(2)
            binding = facts.direct_bindings.get(base_name)
            if binding is None or binding.is_bare_symbol:
                continue
            # Incorporate accumulated path from dotted factory bindings.
            member = ".".join(p for p in (binding.path, chain.lstrip(".")) if p)
            symbol = f"{binding.provider_id}.{member}"
            if symbol_wanted(symbol):
                sites.append(
                    self._make_site(
                        facts,
                        match.start(),
                        kind=CallSiteKind.METHOD_CALL,
                        confidence=binding.confidence,
                        symbol=symbol,
                        provider_id=binding.provider_id,
                        evidence=binding.evidence,
                    )
                )
            # Walk inline chains: storage.bucket(n).setCorsConfiguration(...)
            self._walk_inline_chain(
                facts, source, match, binding, member, target, sites, symbol_wanted
            )

        for match in _BARE_CALL_RE.finditer(source):
            name = match.group(1)
            binding = facts.direct_bindings.get(name)
            if binding is None or not binding.is_bare_symbol:
                continue
            symbol = f"{binding.provider_id}.{name}"
            if not symbol_wanted(symbol):
                continue
            sites.append(
                self._make_site(
                    facts,
                    match.start(),
                    kind=CallSiteKind.BARE_SYMBOL,
                    confidence=binding.confidence,
                    symbol=symbol,
                    provider_id=binding.provider_id,
                    evidence=binding.evidence,
                )
            )

        if target.api_base_urls:
            for match in _FETCH_LIKE_RE.finditer(source):
                line_no = source.count("\n", 0, match.start())
                line = facts.masked_lines[line_no] if line_no < len(facts.masked_lines) else ""
                if any(url in line for url in target.api_base_urls):
                    sites.append(
                        self._make_site(
                            facts,
                            match.start(),
                            kind=CallSiteKind.RAW_HTTP,
                            confidence=MatchConfidence.MEDIUM,
                            symbol=f"{target.provider_id}.<raw_http>",
                            provider_id=target.provider_id,
                            evidence="raw HTTP call against a known api_base_url",
                        )
                    )

        has_any_sdk_binding = bool(facts.direct_bindings)
        version_pin_re = _version_pin_re(target.provider_id)
        for match in version_pin_re.finditer(source):
            literal = match.group(1)
            # Check if this is a provider-specific header (e.g., "Stripe-Version")
            # derived from provider_id, rather than generic apiVersion/api_version
            is_provider_header = f"{target.provider_id}-version" in match.group(0).lower()

            if not is_provider_header and not _DATED_VERSION_RE.search(literal):
                continue

            # Generic ``apiVersion``/``api_version`` dated pins are common
            # across many SDKs. Report them at MEDIUM confidence when the file
            # has at least one SDK import binding (from any provider), or at LOW
            # confidence when it doesn't -- a Stripe config in config/stripe.ts
            # that doesn't itself import the SDK is a real pin worth reporting,
            # just not actionable for auto-fix.
            confidence = (
                MatchConfidence.MEDIUM
                if is_provider_header or has_any_sdk_binding
                else MatchConfidence.LOW
            )
            sites.append(
                self._make_site(
                    facts,
                    match.start(),
                    kind=CallSiteKind.API_VERSION_PIN,
                    confidence=confidence,
                    symbol=f"{target.provider_id}.<api_version_pin>",
                    provider_id=target.provider_id,
                    evidence=f"API version literal {match.group(1)!r}",
                )
            )

        return sites

    def _walk_inline_chain(
        self,
        facts: _FileFacts,
        source: str,
        match: re.Match[str],
        binding: _Binding,
        accumulated_member: str,
        target: ScanTarget,
        sites: list[CallSite],
        symbol_wanted: Callable[[str], bool],
    ) -> None:
        """Walk ``recv.method1(...).method2(...)`` chains inline.

        After matching ``recv.method1(``, follow the close-paren and look for
        ``.method2(``, emitting an additional MEDIUM-confidence call site with
        the accumulated member path so ``storage.bucket(n).setCorsConfiguration(...)``
        emits ``provider.bucket.setCorsConfiguration``.
        Capped at MAX_CHAIN_HOPS to avoid runaway on deeply nested chains.
        """
        from depfix.codemods.text import matching_paren

        open_paren = match.end() - 1
        accumulated = accumulated_member
        depth = binding.chain_depth  # chain steps only, never file hops
        while depth < MAX_CHAIN_HOPS:
            close = matching_paren(source, open_paren)
            if close < 0:
                return
            tail = source[close + 1 :]
            follow = re.match(r"\s*\.\s*(\w+(?:\s*\.\s*\w+)*)\s*\(", tail)
            if follow is None:
                return
            next_member = re.sub(r"\s+", "", follow.group(1))
            accumulated = f"{accumulated}.{next_member}"
            symbol = f"{binding.provider_id}.{accumulated}"
            if symbol_wanted(symbol):
                site_offset = close + 1 + follow.start()
                sites.append(
                    self._make_site(
                        facts,
                        site_offset,
                        kind=CallSiteKind.METHOD_CALL,
                        confidence=MatchConfidence.MEDIUM,
                        symbol=symbol,
                        provider_id=binding.provider_id,
                        evidence=f"derived from a {binding.provider_id} return-value chain",
                    )
                )
            open_paren = close + 1 + follow.end() - 1
            depth += 1

    def _make_site(
        self,
        facts: _FileFacts,
        char_offset: int,
        *,
        kind: CallSiteKind,
        confidence: MatchConfidence,
        symbol: str,
        provider_id: str,
        evidence: str,
    ) -> CallSite:
        source = "\n".join(facts.masked_lines)
        line_no = source.count("\n", 0, char_offset)
        line_start = source.rfind("\n", 0, char_offset) + 1
        column = char_offset - line_start
        line_content = facts.lines[line_no] if line_no < len(facts.lines) else ""
        before_start = max(0, line_no - self.context_lines)
        after_end = min(len(facts.lines), line_no + self.context_lines + 1)
        return CallSite(
            filepath=facts.rel_path,
            line_number=line_no + 1,
            column=column,
            line_content=line_content,
            kind=kind,
            confidence=confidence,
            symbol=symbol,
            provider_id=provider_id,
            evidence=evidence,
            context_before=tuple(facts.lines[before_start:line_no]),
            context_after=tuple(facts.lines[line_no + 1 : after_end]),
        )
