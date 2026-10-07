"""Refreshing Go's generated files after a ``go.mod`` edit.

Unlike npm/PyPI/RubyGems, the Go refresh has to run **before** verification,
not after: without the matching ``go.sum`` hashes, ``go mod download`` and
``go test`` both refuse to build at all ("missing go.sum entry"), so a
perfectly good manifest bump would be verified as a failure.

``go mod tidy`` is tried first. It is allowed to rewrite ``go.mod`` itself
(dropping a require that nothing imports), which is why
:func:`go_generated_paths` reports ``go.mod`` alongside ``go.sum``: the
caller re-registers whatever actually changed. When tidy cannot run, the
fallback is ``go mod download``, which only ever appends hashes.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES, run_sandboxed

_SKIP_DIRS = frozenset({"vendor", ".git", "node_modules", "testdata"})


def _iter_named(root: Path, names: frozenset[str]) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        found.extend(Path(dirpath) / name for name in filenames if name in names)
    return sorted(found)


def go_lockfile_paths(root: Path) -> list[Path]:
    """Every ``go.sum`` under ``root``."""
    return _iter_named(root, frozenset({"go.sum"}))


def go_generated_paths(root: Path) -> list[Path]:
    """Files a refresh may legitimately rewrite: ``go.sum`` and ``go.mod``."""
    return _iter_named(root, frozenset({"go.sum", "go.mod"}))


def try_refresh_go_lockfile(
    root: Path,
    *,
    package: str = "",
    timeout: float = 120.0,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[bool, str]:
    """Bring ``go.sum`` in line with the edited ``go.mod``."""
    from depfix.ecosystems.specs import GO

    if not (root / "go.mod").is_file():
        return False, "no go.mod to refresh"
    if shutil.which("go") is None:
        return False, "go not on PATH; run `go mod tidy` before merging"

    tidy = run_sandboxed(
        ["go", "mod", "tidy"],
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        extra_allowlist=GO.env_allowlist_extra,
    )
    if tidy.ok:
        return True, "go.sum refreshed via `go mod tidy`"

    argv = ["go", "mod", "download", package] if package else ["go", "mod", "download"]
    download = run_sandboxed(
        argv,
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        extra_allowlist=GO.env_allowlist_extra,
    )
    tidy_error = (tidy.error or tidy.stderr.strip() or f"exit {tidy.exit_code}")[:200]
    if download.ok:
        return True, (
            f"`go mod tidy` failed ({tidy_error}); go.sum refreshed with "
            f"`{' '.join(argv)}` instead -- a new transitive dependency may still "
            "need `go mod tidy` before merging"
        )
    message = (download.error or download.stderr.strip() or f"exit {download.exit_code}")[:200]
    return False, (
        f"go.sum refresh failed (`go mod tidy`: {tidy_error}; `{' '.join(argv)}`: "
        f"{message}); run `go mod tidy` before merging"
    )
