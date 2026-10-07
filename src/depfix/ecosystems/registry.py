"""Lookup and detection over the ecosystem specs.

This module is the single authority the rest of depfix asks three
questions of: *which ecosystem is this repo* (:func:`detect`), *which
ecosystems can we fully fix* (:func:`fixable_ids`), and *which ecosystem
does this providers.yaml registry name belong to*
(:func:`ecosystem_for_registry`). ``scanners.repo.detect_repo_ecosystem``
and ``SUPPORTED_ECOSYSTEMS`` delegate here, so adding ecosystem number
eleven touches :mod:`depfix.ecosystems.specs` and nothing else.
"""

from __future__ import annotations

from pathlib import Path

from depfix.ecosystems.base import EcosystemSpec
from depfix.ecosystems.specs import ALL_SPECS

_BY_ID: dict[str, EcosystemSpec] = {spec.id: spec for spec in ALL_SPECS}
_BY_REGISTRY: dict[str, EcosystemSpec] = {
    alias: spec for spec in ALL_SPECS for alias in spec.registry_aliases
}


def get(ecosystem_id: str) -> EcosystemSpec | None:
    return _BY_ID.get(ecosystem_id)


def all_ids() -> tuple[str, ...]:
    return tuple(spec.id for spec in ALL_SPECS)


def fixable_ids() -> frozenset[str]:
    return frozenset(spec.id for spec in ALL_SPECS if spec.fix_supported)


def ecosystem_for_registry(registry_name: str) -> EcosystemSpec | None:
    """The spec whose package registry a providers.yaml ``ecosystem:`` value
    names -- ``npm`` -> javascript, ``pypi`` -> python, ``maven`` -> java."""
    return _BY_REGISTRY.get(registry_name.strip().lower())


def detect(root: Path) -> str:
    """The dominant ecosystem for a checkout, or ``"unknown"``.

    Marker-weight scoring, ties broken by spec order (javascript first --
    the historical behavior ``detect_repo_ecosystem`` promised). Kotlin
    build files score kotlin; a repo with both ``build.gradle`` and
    ``build.gradle.kts`` resolves by weight like everything else.
    """
    best_id = "unknown"
    best_score = 0
    for spec in ALL_SPECS:
        score = spec.matches_score(root)
        if score > best_score:
            best_id, best_score = spec.id, score
    return best_id
