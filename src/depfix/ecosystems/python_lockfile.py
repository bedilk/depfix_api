"""Safe, best-effort refresh for Poetry, uv, and Pipenv lockfiles.

pip-tools is deliberately excluded.  Its ``requirements.txt`` is generated
from ``requirements.in``; regenerating it before Depfix can safely edit that
input would discard a verified dependency bump.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from depfix.ecosystems.specs import PYTHON
from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES, run_sandboxed


@dataclass(frozen=True)
class _Refresher:
    """One dedicated lockfile and its fixed refresh command."""

    lockfile: str
    argv: tuple[str, ...]


_REFRESHERS: tuple[_Refresher, ...] = (
    _Refresher("poetry.lock", ("poetry", "lock", "--no-interaction")),
    _Refresher("uv.lock", ("uv", "lock")),
    _Refresher("Pipfile.lock", ("pipenv", "lock")),
)


def python_lockfile_paths(root: Path) -> list[Path]:
    names = {"poetry.lock", "uv.lock", "Pipfile.lock"}
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or ".venv" in path.parts:
            continue
        if path.name in names:
            found.append(path)
    return found


def uses_pip_tools(root: Path) -> bool:
    """Return whether a project has pip-tools source input outside ``.venv``."""
    return any(
        path.is_file() and ".venv" not in path.parts for path in root.rglob("requirements.in")
    )


def try_refresh_python_lockfile(
    root: Path,
    *,
    timeout: float = 120.0,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[bool, str]:
    """Refresh the first configured Python lockfile, without installing it."""
    for refresher in _REFRESHERS:
        if not (root / refresher.lockfile).is_file():
            continue
        if shutil.which(refresher.argv[0]) is None:
            return False, (
                f"{refresher.argv[0]} not on PATH; regenerate {refresher.lockfile} before merging"
            )
        result = run_sandboxed(
            refresher.argv,
            cwd=root,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            extra_allowlist=PYTHON.env_allowlist_extra,
        )
        if result.ok:
            return True, f"{refresher.lockfile} refreshed via `{' '.join(refresher.argv)}`"
        return False, (
            f"{refresher.lockfile} refresh failed (exit {result.exit_code}): "
            f"{result.stderr[:200]}; regenerate it before merging"
        )
    if uses_pip_tools(root):
        return False, (
            "pip-tools project detected: requirements.txt is verified output, but "
            "requirements.in was not updated; bump the pin in requirements.in and "
            "re-run `pip-compile` before merging. depfix does not recompile "
            "automatically because that would revert this change."
        )
    return False, "no supported Python lockfile to refresh (poetry.lock/uv.lock/Pipfile.lock)"
