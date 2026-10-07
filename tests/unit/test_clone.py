"""Unit tests for the shallow-clone service.

``CloneService.clone`` is exercised against a real local git repo (via the
``url=`` override, which points git at a plain filesystem path instead of
``https://github.com/...``) -- this covers the actual git subprocess
plumbing without ever touching the network.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from depfix.clone.service import CloneError, CloneService

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _init_source_repo(path: Path, *, file_bytes: int = 0) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hello\n" + ("x" * file_bytes), encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)
    return path


# -- local() ------------------------------------------------------------------


def test_local_returns_checkout_pointing_at_resolved_path(tmp_path: Path) -> None:
    checkout = CloneService().local(tmp_path)

    assert checkout.path == tmp_path.resolve()
    assert checkout.is_temporary is False


def test_local_raises_for_nonexistent_directory(tmp_path: Path) -> None:
    with pytest.raises(CloneError):
        CloneService().local(tmp_path / "does-not-exist")


def test_cleanup_is_noop_for_local_checkout(tmp_path: Path) -> None:
    checkout = CloneService().local(tmp_path)
    CloneService().cleanup(checkout)

    assert tmp_path.exists()


# -- Checkout.resolve_inside ----------------------------------------------------


def test_resolve_inside_allows_legitimate_subpath(tmp_path: Path) -> None:
    checkout = CloneService().local(tmp_path)
    (tmp_path / "src").mkdir()

    resolved = checkout.resolve_inside("src/index.js")

    assert resolved == (tmp_path.resolve() / "src" / "index.js")


def test_resolve_inside_blocks_path_traversal(tmp_path: Path) -> None:
    checkout = CloneService().local(tmp_path)

    with pytest.raises(CloneError, match="escapes checkout root"):
        checkout.resolve_inside("../../etc/passwd")


# -- clone() --------------------------------------------------------------------


def test_clone_shallow_clones_a_local_repo(tmp_path: Path) -> None:
    source = _init_source_repo(tmp_path / "source")
    service = CloneService(clone_dir=str(tmp_path / "clones"))

    checkout = service.clone("acme", "widgets", url=str(source))
    try:
        assert checkout.is_temporary is True
        assert (checkout.path / "README.md").is_file()
        # --depth=1: exactly one commit should have been fetched.
        log = subprocess.run(
            ["git", "-C", str(checkout.path), "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert log.stdout.strip() == "1"
    finally:
        service.cleanup(checkout)

    assert not checkout.path.exists()


def test_clone_raises_when_git_not_on_path(tmp_path: Path) -> None:
    source = _init_source_repo(tmp_path / "source")
    service = CloneService()
    service._git_path = None  # simulate git missing from PATH

    with pytest.raises(CloneError, match="git is not available"):
        service.clone("acme", "widgets", url=str(source))


def test_clone_failure_scrubs_token_from_error_message(tmp_path: Path) -> None:
    service = CloneService(clone_dir=str(tmp_path / "clones"), timeout=10.0)
    secret_token = "ghs_supersecrettoken123"

    with pytest.raises(CloneError) as excinfo:
        service.clone("acme", "widgets", url=str(tmp_path / "does-not-exist"), token=secret_token)

    assert secret_token not in str(excinfo.value)


def test_clone_enforces_max_repo_mb_cap_and_cleans_up(tmp_path: Path) -> None:
    source = _init_source_repo(tmp_path / "source")
    clone_dir = tmp_path / "clones"
    service = CloneService(clone_dir=str(clone_dir), max_repo_mb=0)

    with pytest.raises(CloneError, match="larger than"):
        service.clone("acme", "widgets", url=str(source))

    # The oversized checkout must not be left behind.
    assert list(clone_dir.iterdir()) == []
