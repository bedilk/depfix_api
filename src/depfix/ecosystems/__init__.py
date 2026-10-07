"""Multi-ecosystem support -- see :mod:`depfix.ecosystems.base` for the
seam's design and the two support tiers."""

from depfix.ecosystems.base import COMMON_SKIP_DIRS, DependencyDeclaration, EcosystemSpec
from depfix.ecosystems.callsites import GenericImportScanner
from depfix.ecosystems.go_lockfile import go_lockfile_paths, try_refresh_go_lockfile
from depfix.ecosystems.go_manifests import (
    GoModuleBump,
    build_go_dependency_drift_bumps,
    declares_module,
)
from depfix.ecosystems.registry import (
    all_ids,
    detect,
    ecosystem_for_registry,
    fixable_ids,
    get,
)
from depfix.ecosystems.symbol_index import SymbolIndex, build_index, build_index_from_tarball

__all__ = [
    "COMMON_SKIP_DIRS",
    "DependencyDeclaration",
    "EcosystemSpec",
    "GenericImportScanner",
    "GoModuleBump",
    "SymbolIndex",
    "all_ids",
    "build_go_dependency_drift_bumps",
    "build_index",
    "build_index_from_tarball",
    "declares_module",
    "detect",
    "ecosystem_for_registry",
    "fixable_ids",
    "get",
    "go_lockfile_paths",
    "try_refresh_go_lockfile",
]
