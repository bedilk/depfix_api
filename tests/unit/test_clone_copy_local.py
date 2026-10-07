"""Unit tests for :meth:`depfix.clone.service.CloneService.copy_local`.

Unlike ``clone()`` (see ``test_clone.py``), ``copy_local`` never shells out
to git -- it's pure ``shutil``/``os`` plumbing, so these tests don't need
to skip when git is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.clone.service import CloneError, CloneService


def test_copy_local_copies_content_into_a_fresh_temporary_directory(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello\n", encoding="utf-8")
    (src / "nested").mkdir()
    (src / "nested" / "b.txt").write_text("world\n", encoding="utf-8")

    checkout = CloneService().copy_local(src)

    assert checkout.is_temporary is True
    assert checkout.path != src.resolve()
    assert (checkout.path / "a.txt").read_text(encoding="utf-8") == "hello\n"
    assert (checkout.path / "nested" / "b.txt").read_text(encoding="utf-8") == "world\n"


def test_copy_local_raises_for_non_directory(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")

    with pytest.raises(CloneError, match="not a directory"):
        CloneService().copy_local(not_a_dir)


def test_copy_local_excludes_git_and_node_modules_from_size_check_and_copy(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "small.txt").write_bytes(b"hello")
    git_dir = src / ".git"
    git_dir.mkdir()
    (git_dir / "big.pack").write_bytes(b"0" * (2 * 1024 * 1024))
    nm_dir = src / "node_modules"
    nm_dir.mkdir()
    (nm_dir / "big.js").write_bytes(b"0" * (2 * 1024 * 1024))

    # 1MB cap: the excluded dirs alone would blow this, but they must not
    # count against the size check, and must not end up in the copy.
    service = CloneService(max_repo_mb=1)

    checkout = service.copy_local(src)

    assert (checkout.path / "small.txt").read_bytes() == b"hello"
    assert not (checkout.path / ".git").exists()
    assert not (checkout.path / "node_modules").exists()


def test_copy_local_raises_when_real_content_exceeds_max_repo_mb(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(b"0" * (2 * 1024 * 1024))

    service = CloneService(max_repo_mb=1)

    with pytest.raises(CloneError, match="exceeds the 1MB cap"):
        service.copy_local(src)


def test_copy_local_dereferences_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "link.txt").symlink_to(outside)

    checkout = CloneService().copy_local(src)

    copied = checkout.path / "link.txt"
    assert copied.is_symlink() is False
    assert copied.read_text(encoding="utf-8") == "secret\n"


def test_copy_local_cleans_up_dest_dir_on_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("x", encoding="utf-8")
    clone_dir = tmp_path / "clones"

    def fake_copytree(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr("depfix.clone.service.shutil.copytree", fake_copytree)
    service = CloneService(clone_dir=str(clone_dir))

    with pytest.raises(CloneError, match="could not copy"):
        service.copy_local(src)

    assert list(clone_dir.iterdir()) == []


def test_copy_local_honors_clone_dir_option(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("x", encoding="utf-8")
    clone_dir = tmp_path / "clones"

    checkout = CloneService(clone_dir=str(clone_dir)).copy_local(src)

    assert checkout.path.parent == clone_dir


def test_cleanup_removes_temporary_checkout_created_by_copy_local(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("x", encoding="utf-8")
    service = CloneService()
    checkout = service.copy_local(src)
    assert checkout.path.exists()

    service.cleanup(checkout)

    assert not checkout.path.exists()
