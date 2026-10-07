"""Shallow git clone of a repository into a temporary working directory.

Credentials are passed to git via ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/
``GIT_CONFIG_VALUE_n`` environment variables carrying an
``http.extraheader`` Basic-auth header -- never via argv (visible to any
other process on the box via ``ps``) and never written into ``.git/config``
on disk, where it would outlive the clone.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_REPO_MB = 500


class CloneError(RuntimeError):
    """Raised for any failure to obtain a usable checkout."""


class RepoTooLargeError(CloneError):
    """Raised when a repo's working tree exceeds ``clone_max_repo_mb``.

    Caught separately from generic CloneError so callers can treat
    an oversized repo as a skippable info condition rather than a hard
    failure -- the repo is real and the App is installed, it's just
    too large for the current fleet configuration.
    """


@dataclass(frozen=True)
class Checkout:
    """A local working copy -- either a fresh shallow clone or an existing
    directory the caller pointed us at.

    ``is_temporary`` controls whether :class:`CloneService` will delete it on
    cleanup; a caller-supplied local directory (``CloneService.local()``) is
    never deleted by this module.
    """

    path: Path
    is_temporary: bool

    def resolve_inside(self, relative: str) -> Path:
        """Resolve ``relative`` against this checkout, raising if it would
        escape the checkout root.

        Untrusted repo content (a symlink, a ``../../`` path in a manifest
        or scan result) must never let a caller read or write outside the
        checkout -- this is the guard that enforces it.
        """
        candidate = (self.path / relative).resolve()
        root = self.path.resolve()
        if candidate != root and root not in candidate.parents:
            raise CloneError(f"path escapes checkout root: {relative!r}")
        return candidate


#: Directories excluded from both the size check and the copy itself in
#: :meth:`CloneService.copy_local` -- ``.git`` is never needed by the
#: fix/verify pipeline, and ``node_modules`` gets reinstalled fresh by
#: :mod:`depfix.verify.manager` anyway, so copying it just wastes time and
#: inflates the size check against the ``max_repo_mb`` cap for no benefit.
_COPY_EXCLUDE_DIRS = frozenset({".git", "node_modules"})


def _dir_size_bytes(path: Path, *, exclude_dirs: frozenset[str] = frozenset()) -> int:
    """Sum file sizes under ``path``, walking with ``followlinks=False``.

    ``Path.rglob`` follows symlinked directories when recursing (a known
    footgun), which would let a symlink inside an untrusted checkout make
    this walk loop forever (a symlink cycle) or count a target far outside
    the checkout against the size cap. ``os.walk(..., followlinks=False)``
    doesn't descend into symlinked directories at all, and symlinked files
    are skipped explicitly below rather than having their target's size
    counted.
    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for name in filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                continue
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _rmtree_onerror(func, path, exc_info) -> None:
    """Retry a failed removal after chmod'ing the path writable.

    Git writes pack files read-only; on some platforms removing them fails
    outright rather than falling back to the parent directory's
    permissions, so cleanup needs this one retry to be reliable.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        logger.warning("failed to remove %s during checkout cleanup", path, exc_info=True)


class CloneService:
    """Obtains a local, read-only working copy of a repository."""

    def __init__(
        self,
        *,
        clone_dir: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_repo_mb: int = DEFAULT_MAX_REPO_MB,
    ) -> None:
        self._clone_dir = clone_dir
        self._timeout = timeout
        self._max_repo_mb = max_repo_mb
        self._git_path = shutil.which("git")

    def local(self, path: str | Path) -> Checkout:
        """Treat an existing directory as a checkout.

        Never deletes it -- it is presumed to be the caller's own working
        tree, not something this service created.
        """
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            raise CloneError(f"not a directory: {resolved}")
        return Checkout(path=resolved, is_temporary=False)

    def copy_local(self, path: str | Path) -> Checkout:
        """Copy an existing local directory into a fresh temporary checkout,
        for callers (Week 4's ``depfix fix``) that need to install
        dependencies and run the repo's own test suite.

        Unlike :meth:`local`, the result is ``is_temporary=True`` -- the
        whole point is to get a disposable copy so
        :class:`depfix.apply.workspace.WorkspaceEditor` and
        :class:`depfix.verify.verifier.Verifier` can freely write files,
        run ``npm install``/tests, and revert, without ever touching the
        caller's real working tree (which ``local()`` deliberately never
        deletes or mutates in place).

        Symlinks are dereferenced during the copy (``symlinks=False``),
        same rationale as ``clone()``'s ``core.symlinks=false``: a symlink
        materialized as a real file/directory can't be used to smuggle a
        reference to something outside the copy back into a "sandboxed"
        checkout.
        """
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            raise CloneError(f"not a directory: {resolved}")

        size_mb = _dir_size_bytes(resolved, exclude_dirs=_COPY_EXCLUDE_DIRS) / (1024 * 1024)
        if size_mb > self._max_repo_mb:
            raise RepoTooLargeError(
                f"{resolved} is {size_mb:.0f}MB (excluding .git/node_modules), "
                f"exceeds the {self._max_repo_mb}MB cap (clone_max_repo_mb)"
            )

        if self._clone_dir:
            Path(self._clone_dir).mkdir(parents=True, exist_ok=True)
        dest = Path(tempfile.mkdtemp(prefix=f"depfix-copy-{resolved.name}-", dir=self._clone_dir))

        try:
            shutil.copytree(
                resolved,
                dest,
                symlinks=False,
                ignore=shutil.ignore_patterns(*_COPY_EXCLUDE_DIRS),
                dirs_exist_ok=True,
            )
        except OSError as exc:
            self._cleanup(dest)
            raise CloneError(f"could not copy {resolved}: {exc}") from exc

        return Checkout(path=dest, is_temporary=True)

    def clone(
        self,
        owner: str,
        repo: str,
        *,
        ref: str | None = None,
        token: str | None = None,
        url: str | None = None,
        max_repo_mb: int | None = None,
    ) -> Checkout:
        """Shallow-clone ``owner/repo`` at ``ref`` (default branch if unset).

        ``url`` overrides the derived ``https://github.com/...`` clone URL;
        it exists so tests can point this at a local bare repo instead of
        the real GitHub host.

        ``max_repo_mb`` overrides the instance cap for this one clone only,
        and only upward -- a repo's own .depfix.yml can widen its limit but
        not tighten the fleet's.
        """
        if self._git_path is None:
            raise CloneError("git is not available on PATH")

        clone_url = url or f"https://github.com/{owner}/{repo}.git"
        if self._clone_dir:
            Path(self._clone_dir).mkdir(parents=True, exist_ok=True)
        dest = Path(tempfile.mkdtemp(prefix=f"depfix-clone-{repo}-", dir=self._clone_dir))

        env = dict(os.environ)
        # Never hang waiting for a credential prompt on a private repo with a
        # bad/expired token -- fail fast instead.
        env["GIT_TERMINAL_PROMPT"] = "0"

        args = [
            self._git_path,
            # Untrusted repo content: materialize symlinks as plain files
            # rather than following them, which forecloses a symlink-based
            # path-traversal trick in a cloned repo's tree.
            "-c",
            "core.symlinks=false",
            "clone",
            "--depth=1",
            "--single-branch",
            "--no-tags",
        ]
        if ref:
            args += ["--branch", ref]

        if token:
            header_value = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {header_value}"

        args += [clone_url, str(dest)]

        try:
            result = subprocess.run(
                args, env=env, capture_output=True, text=True, timeout=self._timeout
            )
        except subprocess.TimeoutExpired as exc:
            self._cleanup(dest)
            raise CloneError(f"clone of {owner}/{repo} timed out after {self._timeout}s") from exc

        if result.returncode != 0:
            self._cleanup(dest)
            raise CloneError(f"git clone failed for {owner}/{repo}: {_scrub(result.stderr, token)}")

        effective_cap = max(max_repo_mb, self._max_repo_mb) if max_repo_mb else self._max_repo_mb
        size_mb = _dir_size_bytes(dest) / (1024 * 1024)
        if size_mb > effective_cap:
            self._cleanup(dest)
            raise RepoTooLargeError(
                f"{owner}/{repo} is {size_mb:.0f}MB — larger than the {effective_cap}MB "
                f"scan cap. Skipping. To include it, raise the cap for this repo only "
                f"by adding `max_repo_mb: {int(size_mb) + 50}` to its .depfix.yml, "
                f"or fleet-wide via CLONE_MAX_REPO_MB."
            )

        return Checkout(path=dest, is_temporary=True)

    def cleanup(self, checkout: Checkout) -> None:
        """Remove a temporary checkout. No-op for caller-supplied local dirs."""
        if checkout.is_temporary:
            self._cleanup(checkout.path)

    def _cleanup(self, path: Path) -> None:
        shutil.rmtree(path, onerror=_rmtree_onerror)


def _scrub(text: str, token: str | None) -> str:
    """Strip an auth token out of git's stderr before it reaches a log line."""
    if token:
        text = text.replace(token, "***")
    return text[:500]
