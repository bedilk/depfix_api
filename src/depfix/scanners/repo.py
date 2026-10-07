"""Orchestrates one repo scan: acquire a checkout, run the manifest and
call-site scanners against it, and assemble a :class:`RepoScanResult`.

This is the seam between "how do we get the code" (:mod:`depfix.clone`,
:mod:`depfix.gh`) and "what do we do with it" (:mod:`depfix.scanners.manifest`,
:mod:`depfix.scanners.callsites`) -- callers that already have a checkout
(tests, a CLI pointed at a local directory) use :func:`scan_checkout` /
:func:`scan_path` directly and never touch GitHub App auth at all.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from depfix.clone import Checkout, CloneService
from depfix.core.models import BreakingChange
from depfix.ecosystems import registry as ecosystems_registry
from depfix.gh import GitHubAppAuth
from depfix.providers.models import ProviderSpec
from depfix.scanners.callsites import CallSiteScanner
from depfix.scanners.manifest import scan_manifests
from depfix.scanners.models import (
    CallSite,
    CallSiteKind,
    DeclaredDependency,
    MatchConfidence,
    RepoScanResult,
    ScanTarget,
)

logger = logging.getLogger(__name__)

# Both names delegate to depfix.ecosystems -- the single authority on
# which ecosystems exist and which support the full fix pipeline.
# ``typescript`` stays in SUPPORTED_ECOSYSTEMS for callers that still pass
# it explicitly; detection itself folds TS markers into "javascript".
SUPPORTED_ECOSYSTEMS = frozenset(ecosystems_registry.fixable_ids()) | {"typescript"}


def detect_repo_ecosystem(root: Path) -> str:
    """Return the dominant root ecosystem, or ``unknown`` when unmarked."""
    return ecosystems_registry.detect(root)


def build_scan_target(
    provider: ProviderSpec,
    change: BreakingChange | None = None,
    *,
    feed_changes: Iterable[BreakingChange] = (),
) -> ScanTarget:
    """``change`` narrows reporting (explicit selection only); ``feed_changes``
    never narrows -- it supplies the old/replacement symbols to look for."""
    symbols = symbols_for_change(provider.id, change) if change is not None else ()
    feed = feed_symbols_for_changes(
        provider.id, [c for c in (change, *feed_changes) if c is not None]
    )
    return ScanTarget(
        provider_id=provider.id,
        sdk_packages=tuple(pkg.name for pkg in provider.sdk_packages),
        api_base_urls=provider.api_base_urls,
        symbols=symbols,
        feed_symbols=feed,
        sdk_package_refs=tuple((pkg.name, pkg.ecosystem) for pkg in provider.sdk_packages),
    )


def build_scan_target_for_changes(
    provider: ProviderSpec, changes: Iterable[BreakingChange]
) -> ScanTarget:
    changes = list(changes)
    symbols: list[str] = []
    for change in changes:
        for symbol in symbols_for_change(provider.id, change):
            if symbol not in symbols:
                symbols.append(symbol)
    return replace(build_scan_target(provider, feed_changes=changes), symbols=tuple(symbols))


def feed_symbols_for_changes(
    provider_id: str, changes: Iterable[BreakingChange]
) -> tuple[str, ...]:
    """Old symbols to migrate plus replacement symbols that prove a migration
    already happened, for every classified change of this provider."""
    out: list[str] = []
    for change in changes:
        if change.provider_id and change.provider_id != provider_id:
            continue
        for symbol in (
            *symbols_for_change(provider_id, change),
            *symbols_for_api(provider_id, change.new_api),
        ):
            if symbol not in out:
                out.append(symbol)
    return tuple(out)


def symbols_for_change(provider_id: str, change: BreakingChange) -> tuple[str, ...]:
    """The *old* dotted symbols a change is about.

    Hints from release-notes classifiers have historically included the
    replacement symbol alongside the old one. When old_api names a *different*
    symbol than the replacement, a replacement-shaped hint is pollution: keeping
    it makes already-migrated code look actionable. When old == new (a param or
    field change on the same method), nothing is dropped.
    """
    symbols: list[str] = []

    def add(raw: str, *, allow_bare: bool) -> None:
        symbol = _qualify_symbol(provider_id, raw, allow_bare_identifier=allow_bare)
        if symbol is not None and symbol not in symbols:
            symbols.append(symbol)

    for hint in change.call_site_hints:
        add(hint, allow_bare=True)

    add_api_symbol(change.old_api, add)

    # Drop replacement-shaped hints unless old == new (param/field change).
    old_named = set(symbols_for_api(provider_id, change.old_api))
    if old_named and change.new_api:
        stale = set(symbols_for_api(provider_id, change.new_api)) - old_named
        symbols = [s for s in symbols if s not in stale]

    return tuple(symbols)


def symbols_for_api(provider_id: str, api: str) -> tuple[str, ...]:
    """Normalize one documented API symbol into the scanner namespace."""
    symbols: list[str] = []

    def add(raw: str, *, allow_bare: bool) -> None:
        symbol = _qualify_symbol(provider_id, raw, allow_bare_identifier=allow_bare)
        if symbol is not None and symbol not in symbols:
            symbols.append(symbol)

    add_api_symbol(api, add)
    return tuple(symbols)


def add_api_symbol(api: str, add: Callable[..., None]) -> None:
    """Remove a call suffix before handing one documented API to ``add``."""
    candidate = api.strip()
    if candidate.endswith(")"):
        candidate = candidate.split("(", 1)[0].strip()
    add(candidate, allow_bare=False)


# Private compatibility alias for older internal callers/tests. New code uses
# the public helper above so the matcher and scanner share one normalization
# rule.
_symbols_from_change = symbols_for_change


_MEMBER_ID = r"[A-Za-z_]\w*"
_MEMBER_PATH_RE = re.compile(rf"{_MEMBER_ID}(?:\.{_MEMBER_ID})+")
_BARE_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")


def _looks_like_member_path(text: str) -> bool:
    """Does ``text`` look like a dotted chain of JS identifiers?"""
    return bool(_MEMBER_PATH_RE.fullmatch(text))


def _qualify_symbol(provider_id: str, raw: str, *, allow_bare_identifier: bool) -> str | None:
    """Return a scanner-namespace symbol or reject free-form prose.

    Handles hyphenated provider heads: ``google-cloud.bucket.setCors`` splits
    on the provider head, and the member path after it is held to the
    JS-identifier rule while the head itself is trusted as a provider id.
    """
    text = raw.strip()
    if not text:
        return None

    # Already provider-qualified with a hyphenated or normal head.
    if provider_id and text.startswith(f"{provider_id}."):
        member = text[len(provider_id) + 1 :]
        if _looks_like_member_path(member) or _BARE_IDENTIFIER_RE.fullmatch(member):
            return text
        return None
    if provider_id and text == provider_id:
        return text

    # Plain dotted symbol (no hyphenated head): qualify with provider if needed.
    if _looks_like_member_path(text):
        head = text.split(".", 1)[0]
        return text if not provider_id or head == provider_id else f"{provider_id}.{text}"

    if allow_bare_identifier and provider_id and _BARE_IDENTIFIER_RE.fullmatch(text):
        return f"{provider_id}.{text}"

    return None


def self_hosted_sdk_package(root: Path, target: ScanTarget) -> str | None:
    """If this checkout *is* one of the provider's SDK packages, return its name.

    ``bedilk/nodejs-storage`` declares itself ``@google-cloud/storage``; its
    own tests import from ``../src``, correctly out of scope for depfix (which
    scans *consumers* of an SDK, not the SDK itself). Detecting this lets the
    report say so instead of reporting a mysterious zero.
    """
    import json

    pkg_json = root / "package.json"
    if not pkg_json.is_file():
        return None
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    if not name or not isinstance(name, str):
        return None
    if name in target.sdk_packages:
        return name
    return None


def _current_commit_sha(path: Path) -> str:
    """Resolve HEAD for ``path`` -- but only when ``path`` is itself the
    root git finds, not some subdirectory nested inside an unrelated
    ancestor repo.

    ``git -C <path> ...`` walks *up* the filesystem tree to find the
    nearest ``.git`` if ``path`` isn't a repo root itself. A shallow clone
    from :meth:`CloneService.clone` is never affected -- its ``.git`` lives
    right at the checkout path -- but a caller-supplied local directory
    (``depfix scan --path ...``) commonly is *not* a repo root of its own,
    e.g. a fixture directory nested inside this tool's own git checkout.
    Without this check, git would silently report the *outer* repo's HEAD
    as if it belonged to the scanned code -- misleading commit metadata
    rather than an obvious error. Requiring the discovered toplevel to
    equal ``path`` catches that case and blanks out the commit instead of
    guessing.
    """
    git_path = shutil.which("git")
    if git_path is None:
        return ""

    try:
        toplevel = subprocess.run(
            [git_path, "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if toplevel.returncode != 0:
        return ""
    if Path(toplevel.stdout.strip()).resolve() != Path(path).resolve():
        return ""

    try:
        result = subprocess.run(
            [git_path, "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _npm_package_names(target: ScanTarget) -> list[str]:
    """The target's npm-ecosystem packages -- all of ``sdk_packages`` when no
    per-package ecosystem refs were provided (legacy callers/tests)."""
    if not target.sdk_package_refs:
        return list(target.sdk_packages)
    names = []
    for name, registry_name in target.sdk_package_refs:
        spec = ecosystems_registry.ecosystem_for_registry(registry_name)
        if spec is not None and spec.id == "javascript":
            names.append(name)
    return names


def scan_checkout(
    checkout: Checkout,
    target: ScanTarget,
    *,
    repo_full_name: str = "",
    scanner: CallSiteScanner | None = None,
    change: BreakingChange | None = None,
    judge: object | None = None,
    prepare_package_manager_runtime: bool = False,
    package_manager_timeout: float = 90.0,
    # NEW: opt-in agent strategy
    strategy: str = "deterministic",
    agent_completer: object | None = None,
    provider: ProviderSpec | None = None,
    ledger: object | None = None,
    max_data_bytes: int = 0,
) -> RepoScanResult:
    """Scan an already-acquired checkout for manifests + call sites.

    Two strategies:

    - ``deterministic`` (default): the historical path — regex/AST
      scanners in :mod:`depfix.scanners.callsites` plus lockfile-aware
      manifest parsing.
    - ``agent`` (opt-in): a bounded LLM agent (see
      :mod:`depfix.agent.scan_agent`) reads per-provider and per-repo
      skill files and drives a read-only toolset to produce the same
      RepoScanResult shape. Requires ``provider`` and ``agent_completer``.

    Both strategies produce identical output types, so every downstream
    consumer (classifier, assessor, orchestrator, fix pipeline) is
    strategy-agnostic. The deterministic path is preserved unchanged;
    the agent path is a peer, not a replacement.

    ``scanner`` lets a caller (e.g. the CLI, wiring in ``scan_max_file_bytes``/
    ``scan_max_files`` from :class:`depfix.config.Settings`) supply a
    pre-configured :class:`CallSiteScanner`; a default is constructed when
    omitted so tests and simple callers don't have to.
    """
    if strategy in ("agent", "hybrid"):
        if agent_completer is None or provider is None:
            if strategy == "agent":
                raise ValueError(
                    "SCAN_STRATEGY=agent but no agent completer was provided to scan_checkout. "
                    "This usually means no LLM is configured (set GOOGLE_API_KEY, "
                    "LLM_PROVIDER=ollama, or configure Bedrock). Refusing to silently fall "
                    "back to the deterministic scanner — set "
                    "SCAN_STRATEGY=deterministic explicitly if that is what you want."
                )
            # hybrid with no LLM configured: fall back gracefully to deterministic
            logger.info(
                "SCAN_STRATEGY=hybrid but no LLM available for %s — "
                "using deterministic scanner only",
                repo_full_name or "local",
            )
        else:
            if strategy == "hybrid":
                # HYBRID: run deterministic first (free, O(n)), then augment
                baseline = _scan_checkout_deterministic(
                    checkout,
                    target,
                    repo_full_name=repo_full_name,
                    scanner=scanner,
                    change=change,
                    judge=None,  # judge is now the agent's job
                    prepare_package_manager_runtime=prepare_package_manager_runtime,
                    package_manager_timeout=package_manager_timeout,
                    max_data_bytes=max_data_bytes,
                )
                return _scan_checkout_agent_augment(
                    baseline,
                    provider,
                    agent_completer,
                    checkout_path=checkout.path,
                    ledger=ledger,
                )
            else:
                # PURE AGENT: skip deterministic entirely
                return _scan_checkout_agent(
                    checkout,
                    target,
                    provider,
                    agent_completer,
                    repo_full_name=repo_full_name,
                    ledger=ledger,
                )

    # Deterministic path
    return _scan_checkout_deterministic(
        checkout,
        target,
        repo_full_name=repo_full_name,
        scanner=scanner,
        change=change,
        judge=judge,
        prepare_package_manager_runtime=prepare_package_manager_runtime,
        package_manager_timeout=package_manager_timeout,
        max_data_bytes=max_data_bytes,
    )


def scan_path(
    path: str | Path,
    target: ScanTarget,
    *,
    repo_full_name: str = "",
    scanner: CallSiteScanner | None = None,
    change: BreakingChange | None = None,
    judge: object | None = None,
    prepare_package_manager_runtime: bool = False,
    package_manager_timeout: float = 90.0,
    strategy: str = "deterministic",
    agent_completer: object | None = None,
    provider: ProviderSpec | None = None,
    ledger: object | None = None,
    max_data_bytes: int = 0,
) -> RepoScanResult:
    """Scan a local directory directly, with no clone/GitHub App involved."""
    checkout = CloneService().local(path)
    return scan_checkout(
        checkout,
        target,
        repo_full_name=repo_full_name,
        scanner=scanner,
        change=change,
        judge=judge,
        prepare_package_manager_runtime=prepare_package_manager_runtime,
        package_manager_timeout=package_manager_timeout,
        strategy=strategy,
        agent_completer=agent_completer,
        provider=provider,
        ledger=ledger,
        max_data_bytes=max_data_bytes,
    )


def scan_repo(
    owner: str,
    repo: str,
    target: ScanTarget,
    *,
    gh_auth: GitHubAppAuth,
    clone_service: CloneService,
    ref: str | None = None,
    scanner: CallSiteScanner | None = None,
    change: BreakingChange | None = None,
    judge: object | None = None,
    prepare_package_manager_runtime: bool = False,
    package_manager_timeout: float = 90.0,
    strategy: str = "deterministic",
    agent_completer: object | None = None,
    provider: ProviderSpec | None = None,
    ledger: object | None = None,
    max_data_bytes: int = 0,
    max_repo_mb: int | None = None,
) -> RepoScanResult:
    """Resolve the App installation for ``owner/repo``, mint a
    least-privilege token, shallow-clone, scan, and clean up -- always,
    even if scanning raises.
    """
    installation = gh_auth.resolve_installation_for_repo(owner, repo)
    token = gh_auth.installation_token(installation.id, repositories=(repo,))
    resolved_ref = ref or gh_auth.default_ref(owner, repo)

    checkout = clone_service.clone(
        owner, repo, ref=resolved_ref, token=token.token, max_repo_mb=max_repo_mb
    )
    try:
        return scan_checkout(
            checkout,
            target,
            repo_full_name=f"{owner}/{repo}",
            scanner=scanner,
            change=change,
            judge=judge,
            prepare_package_manager_runtime=prepare_package_manager_runtime,
            package_manager_timeout=package_manager_timeout,
            strategy=strategy,
            agent_completer=agent_completer,
            provider=provider,
            ledger=ledger,
            max_data_bytes=max_data_bytes,
        )
    finally:
        clone_service.cleanup(checkout)


def _scan_checkout_deterministic(
    checkout: Checkout,
    target: ScanTarget,
    *,
    repo_full_name: str = "",
    scanner: CallSiteScanner | None = None,
    change: BreakingChange | None = None,
    judge: object | None = None,
    prepare_package_manager_runtime: bool = False,
    package_manager_timeout: float = 90.0,
    max_data_bytes: int = 0,
) -> RepoScanResult:
    """Pure deterministic scan — O(n) regex/AST path across all languages.

    The JS/TS binding-aware scanner runs as before. For every other
    language detected in the checkout (Python, Go, Java/Kotlin, Ruby),
    the language-specific binding-aware scanner in
    depfix.scanners.languages runs in parallel and its call sites are
    merged into the same RepoScanResult.

    Extracted so both the standalone 'deterministic' strategy and the
    hybrid first-pass can call it without duplicating the body.
    """
    from depfix.scanners.models import dedupe_call_sites as _dedupe

    ecosystem_id = ecosystems_registry.detect(checkout.path)
    spec = ecosystems_registry.get(ecosystem_id)
    dependencies: list[DeclaredDependency] = []
    preparation: object | None = None
    all_call_sites: list[CallSite] = []
    total_files_scanned = 0
    errors: list[str] = []

    # Detect self-hosted SDK early and surface it as an informational error
    # rather than a mysterious zero. depfix scans consumers; the SDK's own
    # tests import from relative paths and are correctly out of scope.
    if target is not None:
        self_pkg = self_hosted_sdk_package(checkout.path, target)
        if self_pkg:
            errors.append(
                f"this checkout declares itself as {self_pkg!r} (one of the provider's "
                "sdk_packages). depfix scans consumers of an SDK, not the SDK itself. "
                "Its own tests import from relative paths and are out of scope. "
                "Samples/ subdirectory (if present) imports the published package and will be scanned."
            )

    if prepare_package_manager_runtime and (spec is None or spec.id == "javascript"):
        from depfix.verify.manager import prepare_package_manager

        preparation = prepare_package_manager(checkout.path, timeout=package_manager_timeout)

    # --- JS/TS path (binding-aware, unchanged) --------------------------------
    if spec is None or spec.id == "javascript":
        manifest_scan = scan_manifests(checkout.path, _npm_package_names(target))
        call_site_result = (scanner or CallSiteScanner()).scan(checkout.path, target)
        dependencies = [
            DeclaredDependency(
                package=match.package,
                manifest_path=match.manifest_path,
                declared_range=match.declared_range,
                resolved_version=match.resolved_version,
                source=match.source,
            )
            for match in manifest_scan.matches
        ]
        all_call_sites.extend(call_site_result.call_sites)
        total_files_scanned += call_site_result.files_scanned
        errors.extend(call_site_result.errors)

    # --- Non-JS ecosystem path (existing generic scanner) ---------------------
    elif spec is not None:
        from depfix.ecosystems.callsites import GenericImportScanner, packages_for_spec

        call_site_result = GenericImportScanner(spec, max_data_bytes=max_data_bytes).scan(
            checkout.path, target
        )
        all_call_sites.extend(call_site_result.call_sites)
        total_files_scanned += call_site_result.files_scanned
        errors.extend(call_site_result.errors)
        if spec.scan_dependencies is not None:
            packages = packages_for_spec(target, spec)
            for declaration in spec.scan_dependencies(checkout.path, packages):
                dependencies.append(
                    DeclaredDependency(
                        package=declaration.package,
                        manifest_path=declaration.manifest_path,
                        declared_range=declaration.declared_version,
                        resolved_version=declaration.resolved_version,
                        source=declaration.source,
                    )
                )

    # --- Language-specific binding-aware scanners (Python/Go/Java/Ruby) ------
    lang_sites, lang_files, lang_errors = _run_language_scanners(checkout.path, target)
    all_call_sites.extend(lang_sites)
    total_files_scanned += lang_files
    errors.extend(lang_errors)

    # --- Type-resolved call sites (TS/Python, deterministic, HIGH confidence) ---
    from depfix.config import get_settings as _get_settings

    _scan_settings = _get_settings()
    if _scan_settings.scan_type_resolve:
        from depfix.scanners.typeresolve import resolve_ts_call_sites

        npm_pkgs = _npm_package_names(target)
        if npm_pkgs and (spec is None or spec.id == "javascript"):
            type_sites = resolve_ts_call_sites(
                checkout.path,
                npm_pkgs,
                provider_id=target.provider_id,
            )
            if type_sites:
                logger.info(
                    "typeresolve: %d type-resolved call site(s) for %s",
                    len(type_sites),
                    repo_full_name or "local",
                )
                all_call_sites.extend(type_sites)

    if judge is not None and change is not None and all_call_sites:
        all_call_sites = judge.judge(change, all_call_sites, checkout.path)  # type: ignore[attr-defined]

    manifest_matches = [
        f"{d.package}@{d.resolved_version or d.declared_range or '?'} "
        f"({d.source}, {d.manifest_path})"
        for d in dependencies
    ]

    return RepoScanResult(
        repo_full_name=repo_full_name,
        commit_sha=_current_commit_sha(checkout.path),
        call_sites=_dedupe(all_call_sites),
        manifest_matches=manifest_matches,
        dependencies=dependencies,
        files_scanned=total_files_scanned,
        errors=errors,
        package_manager_preparation=preparation,
    )


def _run_language_scanners(
    checkout_root: Path,
    target: ScanTarget,
) -> tuple[list[CallSite], int, list[str]]:
    """Run binding-aware scanners for Python/Go/Java/Ruby and return
    (call_sites, files_scanned, errors). Returns empty lists when the
    registry has no packages for a detected language.

    JS/TS is handled by the existing CallSiteScanner above; this function
    only covers the non-JS registry entries.
    """
    from depfix.scanners.registry import scanner_registry

    # Build ecosystem → package list from sdk_package_refs
    ecosystem_packages: dict[str, list[str]] = {}
    if target.sdk_package_refs:
        for name, ecosystem in target.sdk_package_refs:
            if ecosystem not in ("npm",):  # npm is the JS scanner's job
                ecosystem_packages.setdefault(ecosystem, []).append(name)
    elif target.sdk_packages:
        # Legacy callers have no ecosystem tags; skip — JS scanner covers them.
        pass

    # Map ecosystem registry names to language keys
    eco_to_lang = {
        "pypi": "python",
        "go": "go",
        "maven": "java",
        "kotlin": "kotlin",
        "rubygems": "ruby",
        "hex": "elixir",
        "packagist": "php",
    }

    all_sites: list[CallSite] = []
    total_files = 0
    errors: list[str] = []

    for ecosystem, packages in ecosystem_packages.items():
        language = eco_to_lang.get(ecosystem)
        if language is None or not scanner_registry.has(language):
            continue
        if not packages:
            continue
        try:
            lang_scanner = scanner_registry.build(language, packages)
            lang_result = lang_scanner.scan_repo(checkout_root)
        except Exception as exc:
            logger.warning("language scanner %s failed: %s", language, exc)
            errors.append(f"{language}: {exc}")
            continue

        total_files += lang_result.files_scanned
        errors.extend(lang_result.errors)
        all_sites.extend(_resolve_call_sites(lang_result, target))

    return all_sites, total_files, errors


def _resolve_call_sites(lang_result: object, target: ScanTarget) -> list[CallSite]:
    """Convert (bindings, uses) from a LanguageScanResult into CallSite objects.

    Confidence rules — uniform across all languages:
    - HIGH   : use in same file as a DIRECT/ALIASED/NAMED binding
    - MEDIUM : binding is WILDCARD/DOT, or no direct binding found in this
               file but the local_name matches a binding elsewhere (one hop)
    - LOW    : symbol appears with no traceable binding
    """
    from depfix.scanners.language import BindingKind

    # Index bindings by file
    bindings_by_file: dict[str, list] = {}
    for b in lang_result.bindings:  # type: ignore[attr-defined]
        bindings_by_file.setdefault(b.filepath, []).append(b)

    # Build a cross-file index for one-hop detection
    all_local_names: set[str] = {b.local_name for b in lang_result.bindings}  # type: ignore[attr-defined]

    narrowing = set(target.symbols or ())
    sites: list[CallSite] = []

    for use in lang_result.uses:  # type: ignore[attr-defined]
        if narrowing and not any(
            use.attribute_path.startswith(s) or use.attribute_path == s for s in narrowing
        ):
            continue

        file_bindings = bindings_by_file.get(use.filepath, [])
        direct_match = [b for b in file_bindings if b.local_name == use.local_name]

        if direct_match:
            binding = direct_match[0]
            if binding.kind in (BindingKind.WILDCARD, BindingKind.DOT):
                confidence = MatchConfidence.MEDIUM
            else:
                confidence = MatchConfidence.HIGH
            evidence = f"{binding.kind.value} import at {binding.filepath}:{binding.line_number}"
            package = binding.package
        elif use.local_name in all_local_names:
            # Binding exists in a different file (re-export). Distance does
            # not matter: it traces to the SDK, so it is found.
            confidence = MatchConfidence.HIGH
            evidence = "binding found via cross-file re-export"
            # Find the package from any binding with this name
            pkg_b = next((b for b in lang_result.bindings if b.local_name == use.local_name), None)  # type: ignore[attr-defined]
            package = pkg_b.package if pkg_b else target.provider_id
        else:
            confidence = MatchConfidence.LOW
            evidence = "bare symbol use, no traceable binding"
            package = target.provider_id

        # Determine call site kind from attribute_path shape
        if "." in use.attribute_path or "::" in use.attribute_path:
            kind = CallSiteKind.METHOD_CALL
        else:
            kind = CallSiteKind.BARE_SYMBOL

        # Derive the symbol name: package + attribute chain after local_name
        attr_suffix = use.attribute_path[len(use.local_name) :]
        symbol = package + attr_suffix if attr_suffix else package

        sites.append(
            CallSite(
                filepath=use.filepath,
                line_number=use.line_number,
                column=use.column,
                line_content=use.line_content,
                kind=kind,
                confidence=confidence,
                symbol=symbol,
                provider_id=target.provider_id,
                evidence=evidence,
            )
        )

    return sites


def _scan_checkout_agent_augment(
    baseline: RepoScanResult,
    provider: ProviderSpec,
    completer: object,
    *,
    checkout_path: Path,
    ledger: object | None,
) -> RepoScanResult:
    """Hybrid second pass: let the agent augment the deterministic baseline.

    Only costs tokens when the baseline has LOW-confidence ambiguous sites
    or when the provider has raw-HTTP anchors to chase. Returns baseline
    unchanged when neither condition holds.
    """
    from depfix.agent.scan_agent import ScanAgent
    from depfix.agent.scan_tools import ScanToolset
    from depfix.sources.factory import SourceDeps

    with SourceDeps.from_settings() as deps:
        toolset = ScanToolset(
            checkout_root=checkout_path,  # needed so judge agent can read files
            http=deps.http,
            npm=deps.npm,
            pypi=deps.pypi,
            github_api_url=deps.github_api_url,
        )
        agent = ScanAgent(completer, ledger=ledger)  # type: ignore[arg-type]
        target = ScanTarget(provider_id=provider.id)
        augmented = agent.augment_baseline(
            provider,
            target,
            toolset,
            baseline,
            repo_full_name=baseline.repo_full_name,
        )
        low_before = sum(1 for s in baseline.call_sites if not s.is_actionable)
        low_after = sum(1 for s in augmented.call_sites if not s.is_actionable)
        logger.info(
            "hybrid augment %s/%s: %d actionable before → %d after (low sites %d → %d)",
            baseline.repo_full_name,
            provider.id,
            len(baseline.actionable_sites),
            len(augmented.actionable_sites),
            low_before,
            low_after,
        )
        return augmented


def _scan_checkout_agent(
    checkout: Checkout,
    target: ScanTarget,
    provider: ProviderSpec,
    completer: object,
    *,
    repo_full_name: str,
    ledger: object | None,
) -> RepoScanResult:
    """Dispatch to the agent scanner. Isolated so the deterministic path
    has zero new imports at module load time."""
    from depfix.agent.scan_agent import ScanAgent
    from depfix.agent.scan_tools import ScanToolset
    from depfix.sources.factory import SourceDeps

    with SourceDeps.from_settings() as deps:
        toolset = ScanToolset(
            checkout_root=checkout.path,
            http=deps.http,
            npm=deps.npm,
            pypi=deps.pypi,
            github_api_url=deps.github_api_url,
        )
        agent = ScanAgent(completer, ledger=ledger)  # type: ignore[arg-type]
        result, transcript = agent.scan_repo(
            provider, target, toolset, repo_full_name=repo_full_name
        )
        logger.info(
            "agent scan %s: %d step(s), %d tool call(s), stopped=%s, anchors=%d",
            provider.id,
            transcript.steps_used,
            len(transcript.tool_calls),
            transcript.stopped_reason or "final",
            len(transcript.discovered_anchors),
        )
        if transcript.discovered_anchors or transcript.api_mapping:
            from dataclasses import replace as _replace

            result = _replace(
                result,
                discovered_anchors=transcript.discovered_anchors,
                api_mapping=transcript.api_mapping,
            )
        return result
