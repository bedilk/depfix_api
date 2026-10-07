"""The ecosystem seam -- everything language/package-manager-specific that
the rest of depfix must never hardcode again.

One :class:`EcosystemSpec` describes one language ecosystem declaratively:
how to *detect* it (marker files), how to *scan* it (source extensions,
skip dirs, import patterns for an SDK package), and how to *run* it
(install/test commands, extra sandbox env vars). The pipeline, orchestrator
and verifier consume specs through :mod:`depfix.ecosystems.registry` and
stay ecosystem-agnostic.

Two support tiers, by design rather than accident:

- ``fix_supported=True`` (javascript, python): the full pipeline -- call-site
  scan, LLM fix, syntax validation, install + real-test-suite verification.
- ``fix_supported=False`` (the rest of the top-10): detection, manifest
  dependency scanning, and import-level call-site scanning -- enough for
  ``depfix scan`` to answer "is this repo affected", while ``depfix plan``
  declines with an honest message instead of generating unverifiable edits.

A spec is data plus a few small callables. Resist the urge to grow this
into a wide Protocol with twenty abstract methods: ten ecosystems differ
mostly by *data* (filenames, commands, regex shapes), and the two genuinely
behavioral cases (JS, Python) plug in behavior only where they need it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: Directories no ecosystem ever wants scanned -- merged into every spec's
#: ``skip_dirs`` so a polyglot monorepo doesn't rescan another ecosystem's
#: dependency tree.
COMMON_SKIP_DIRS = frozenset(
    {
        ".git",
        "dist",
        "build",
        "coverage",
        ".next",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".depfix-venv",
        "vendor",
        "target",
        ".tox",
        ".mypy_cache",
    }
)

#: Credit for a monorepo with no root manifest but depth-1 or depth-2 manifests
_NESTED_MANIFEST_WEIGHT = 3


@dataclass(frozen=True)
class DependencyDeclaration:
    """One SDK package found declared in one manifest.

    The cross-ecosystem analogue of
    :class:`depfix.scanners.manifest.DependencyMatch` -- kept separate
    because npm's lockfile-resolution semantics (declared range vs resolved
    version, three-valued ``affects_version``) don't map one-to-one onto
    e.g. go.mod's exact-version requires or a Gemfile's constraint list.
    """

    package: str
    manifest_path: str
    declared_version: str | None  # whatever constraint/pin the manifest states
    resolved_version: str | None  # exact version from a lockfile, when parseable
    source: str  # "lockfile" | "manifest"


@dataclass(frozen=True)
class EcosystemSpec:
    """Everything depfix knows about one language ecosystem."""

    id: str  # "javascript", "python", "java", ... -- what detect() returns
    display_name: str
    #: Registry names accepted for ``SdkPackage.ecosystem`` in providers.yaml.
    #: The first entry is canonical (what new config should use).
    registry_aliases: tuple[str, ...]
    source_extensions: frozenset[str]
    #: (filename-or-glob, weight) pairs for repo detection. A glob (leading
    #: ``*``) matches against the checkout root's direct children.
    markers: tuple[tuple[str, int], ...]
    manifest_names: tuple[str, ...] = ()
    lockfile_names: tuple[str, ...] = ()
    skip_dirs: frozenset[str] = frozenset()
    fix_supported: bool = False
    #: Extra env vars :func:`depfix.verify.sandbox.run_sandboxed` must let
    #: through for this ecosystem's tooling (the base allowlist is npm/node
    #: flavored for historical reasons).
    env_allowlist_extra: frozenset[str] = frozenset()
    #: ``line`` prefixes and ``(open, close)`` block pairs for comment
    #: masking in the generic import scanner.
    line_comments: tuple[str, ...] = ()
    block_comments: tuple[tuple[str, str], ...] = ()
    #: Confidence assigned to an import-regex hit. HIGH when the pattern is
    #: derived mechanically from the package name (python, go, rust, ruby,
    #: maven group ids); "medium" when it rests on a namespace *guess*
    #: (composer/NuGet/SwiftPM names don't determine code namespaces).
    import_confidence: str = "high"
    #: Given an SDK package name as spelled in providers.yaml, return the
    #: regexes that match an import/require/use of it in this ecosystem's
    #: source. None means "no import-level scanning" (javascript: the real
    #: binding-aware scanner in scanners/callsites.py is used instead).
    import_regexes: Callable[[str], tuple[re.Pattern[str], ...]] | None = None
    #: Parse dependency declarations for ``packages`` out of a checkout.
    #: None means "no manifest scanning for this ecosystem yet".
    scan_dependencies: Callable[[Path, list[str]], list[DependencyDeclaration]] | None = None
    #: Build the install command for a checkout, or None when the checkout
    #: has nothing to install. Only meaningful for fix-supported ecosystems.
    install_argv: Callable[[Path], list[str] | None] | None = None
    #: Build the test command, or None when no test setup was detected.
    test_argv: Callable[[Path], list[str] | None] | None = None

    def all_skip_dirs(self) -> frozenset[str]:
        return self.skip_dirs | COMMON_SKIP_DIRS

    def matches_score(self, root: Path) -> int:
        """Marker-file score for ``root`` -- higher wins in detection.

        A glob marker never counts a hit inside a skipped directory: a
        vendored ``yarn.lock`` under ``node_modules/`` says nothing about what
        this repo is written in.
        """
        skip = self.all_skip_dirs()
        score = sum(
            weight
            for marker, weight in self.markers
            if (self._glob_hit(root, marker, skip) if "*" in marker else (root / marker).exists())
        )
        return score + self._nested_manifest_score(root, skip)

    def _nested_manifest_score(self, root: Path, skip: frozenset[str]) -> int:
        """Depth-bounded credit for a monorepo with no root manifest.

        Derived from ``manifest_names`` so every ecosystem gets it: a Go
        monorepo must not resolve to whichever spec remembered to add a
        ``**/`` marker. Bounded to two levels (``packages/*``, ``apps/*/``)
        rather than ``**`` so a miss costs a directory listing, not a walk of
        an installed dependency tree.
        """
        if any((root / name).exists() for name in self.manifest_names if "*" not in name):
            return 0
        return next(
            (
                _NESTED_MANIFEST_WEIGHT
                for depth in ("*/", "*/*/")
                for name in self.manifest_names
                if self._glob_hit(root, f"{depth}{name}", skip)
            ),
            0,
        )

    @staticmethod
    def _glob_hit(root: Path, pattern: str, skip: frozenset[str]) -> bool:
        return any(
            not any(part in skip for part in match.relative_to(root).parts[:-1])
            for match in root.glob(pattern)
        )


def compile_all(patterns: list[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.MULTILINE) for p in patterns)
