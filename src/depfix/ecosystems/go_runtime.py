"""Go install/test/smoke plumbing for the verify subsystem.

Two choices worth stating:

* ``go test`` is run with ``-count=1``. Go caches test results keyed on the
  package and its inputs; without this flag the after-fix run could be served
  from the cache populated by the baseline run and report a pass for code it
  never executed.
* ``-json`` is requested so :func:`depfix.verify.parsers.parse_go_test_json`
  can recover stable per-test identities.

Isolation model: Go keeps downloaded modules in ``GOMODCACHE``, outside the
checkout, so there is no ``node_modules``/``.depfix-venv`` equivalent to
create. ``go mod download`` writes only to that shared cache and to
``go.sum``; it does not execute package code, so the install step here is the
least dangerous of the four fix-supported ecosystems.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_SKIP_DIRS = frozenset({"vendor", ".git", "node_modules", "testdata"})


def go() -> str | None:
    return shutil.which("go")


def gofmt() -> str | None:
    """``gofmt`` ships with the toolchain, but may be absent in a trimmed image."""
    return shutil.which("gofmt")


def has_go_mod(root: str | Path) -> bool:
    return (Path(root) / "go.mod").is_file()


def install_argv(root: str | Path) -> list[str] | None:
    """Populate the module cache and ``go.sum``, or None when unavailable."""
    if go() is None or not has_go_mod(root):
        return None
    return ["go", "mod", "download"]


def has_test_setup(root: str | Path) -> bool:
    """Whether any ``*_test.go`` exists outside a vendored tree."""
    for _dirpath, dirnames, filenames in os.walk(Path(root), followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if any(name.endswith("_test.go") for name in filenames):
            return True
    return False


def test_argv(root: str | Path) -> list[str] | None:
    """``go test`` over the whole module, cache-defeating and JSON-reported."""
    if go() is None or not has_go_mod(root):
        return None
    return ["go", "test", "-json", "-count=1", "./..."]


def smoke_argv(root: str | Path, relpath: str) -> list[str] | None:
    """Parse-only check for one edited ``.go`` file via ``gofmt -e -l``."""
    if Path(relpath).suffix != ".go":
        return None
    formatter = gofmt()
    if formatter is None:
        return None
    return [formatter, "-e", "-l", relpath]
