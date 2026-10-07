"""Minimal semver handling. We only ever need "did the major move".

Deliberately not a dependency — full semver range resolution is a Week 3
concern (matching a repo's declared range), not a detection concern.
"""

from __future__ import annotations

import re

_SEMVER_RE = re.compile(r"^[vV]?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?(?:[-+].*)?$")


def parse_semver(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    m = _SEMVER_RE.match(value.strip())
    if not m:
        return None
    return (
        int(m.group("major")),
        int(m.group("minor")),
        int(m.group("patch") or 0),
    )


def is_major_bump(old: str | None, new: str | None) -> bool:
    """True only when both parse and the major component increased."""
    o, n = parse_semver(old), parse_semver(new)
    if o is None or n is None:
        return False
    return n[0] > o[0]


def in_upgrade_window(migration_version: str, installed: str | None, target: str) -> bool | None:
    """Does a migration published at ``migration_version`` apply when moving
    ``installed`` → ``target``?

    Three-valued: None means "cannot place this migration in the window".
    """
    migration, goal = parse_semver(migration_version), parse_semver(target)
    if migration is None or goal is None:
        return None
    if migration > goal:
        return False  # beyond this upgrade
    current = parse_semver(installed)
    return not (current is not None and migration <= current)


def drift_risk(old: str | None, new: str | None) -> str:
    """Classify a forward version move for reporting and PR policy.

    ``""`` means not a forward semver move (unparseable, equal, or downgrade).
    """
    o, n = parse_semver(old), parse_semver(new)
    if o is None or n is None or n <= o:
        return ""
    gap = n[0] - o[0]
    if gap == 0:
        return "patch-minor"
    if gap == 1:
        return "major"
    return "multi-major"
