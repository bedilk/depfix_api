"""Unit tests for :class:`depfix.apply.workspace.WorkspaceEditor`.

Uses a plain :class:`~depfix.clone.service.Checkout` pointed at ``tmp_path``
rather than going through :class:`~depfix.clone.service.CloneService` --
the editor only needs ``checkout.path``/``resolve_inside``, not a real
clone or copy.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from depfix.apply.models import EditVerdict
from depfix.apply.workspace import WorkspaceEditor
from depfix.clone.service import Checkout, CloneError
from depfix.core.models import ValidationResult, ValidationStatus


def _checkout(root: Path, *, is_temporary: bool = False) -> Checkout:
    return Checkout(path=root, is_temporary=is_temporary)


# -- write_fix: basic behavior ------------------------------------------------


def test_write_fix_writes_new_content_and_returns_applied(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_text("old\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path))

    edit = editor.write_fix("chat.js", "new\n")

    assert edit.verdict == EditVerdict.APPLIED
    assert edit.relpath == "chat.js"
    assert edit.original_content == "old\n"
    assert edit.fixed_content == "new\n"
    assert "old" in edit.diff and "new" in edit.diff
    assert target.read_text(encoding="utf-8") == "new\n"
    assert edit.is_on_disk is True
    assert edit.changed is True


def test_write_fix_is_noop_when_fixed_equals_original(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_text("same\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path))

    edit = editor.write_fix("chat.js", "same\n")

    assert edit.verdict == EditVerdict.SKIPPED
    assert edit.diff == ""
    assert edit.is_on_disk is False
    assert target.read_text(encoding="utf-8") == "same\n"


def test_write_fix_returns_skipped_with_error_message_when_file_missing(tmp_path: Path) -> None:
    editor = WorkspaceEditor(_checkout(tmp_path))

    edit = editor.write_fix("does-not-exist.js", "new\n")

    assert edit.verdict == EditVerdict.SKIPPED
    assert "could not read" in edit.error_message


def test_write_fix_returns_skipped_on_undecodable_bytes(tmp_path: Path) -> None:
    target = tmp_path / "binary.js"
    target.write_bytes(b"\xff\xfe\x00\x01")
    editor = WorkspaceEditor(_checkout(tmp_path))

    edit = editor.write_fix("binary.js", "new\n")

    assert edit.verdict == EditVerdict.SKIPPED
    assert "could not decode" in edit.error_message


def test_write_fix_sets_suspect_when_validation_invalid(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_text("old\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path))
    validation = ValidationResult(
        status=ValidationStatus.SYNTAX_ERROR,
        is_valid=False,
        syntax_valid=False,
        changes_detected=True,
        error_message="bad syntax",
    )

    edit = editor.write_fix("chat.js", "new(\n", validation=validation)

    assert edit.verdict == EditVerdict.SUSPECT
    assert edit.is_on_disk is True  # still written, just flagged


def test_write_fix_records_confidence_and_usages_fixed(tmp_path: Path) -> None:
    (tmp_path / "chat.js").write_text("old\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path))

    edit = editor.write_fix("chat.js", "new\n", confidence=0.87, usages_fixed=3)

    assert edit.confidence == 0.87
    assert edit.usages_fixed == 3


# -- write_fix: byte-level format preservation --------------------------------


def test_write_fix_preserves_crlf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_bytes(b"line1\r\nline2\r\n")
    editor = WorkspaceEditor(_checkout(tmp_path))

    editor.write_fix("chat.js", "line1\nline2changed\n")

    assert target.read_bytes() == b"line1\r\nline2changed\r\n"


def test_write_fix_preserves_bom(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_bytes(b"\xef\xbb\xbfold\n")
    editor = WorkspaceEditor(_checkout(tmp_path))

    edit = editor.write_fix("chat.js", "new\n")

    assert edit.original_content == "old\n"
    assert target.read_bytes() == b"\xef\xbb\xbfnew\n"


def test_write_fix_preserves_missing_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_bytes(b"old-no-newline")
    editor = WorkspaceEditor(_checkout(tmp_path))

    editor.write_fix("chat.js", "new-no-newline\n")

    assert target.read_bytes() == b"new-no-newline"


def test_write_fix_preserves_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "script.js"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o755)
    editor = WorkspaceEditor(_checkout(tmp_path))

    editor.write_fix("script.js", "new\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


# -- revert / revert_all --------------------------------------------------------


def test_revert_restores_original_bytes(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_text("old\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path))
    editor.write_fix("chat.js", "new\n")

    reverted = editor.revert("chat.js")

    assert reverted is True
    assert target.read_text(encoding="utf-8") == "old\n"


def test_revert_returns_false_when_never_written(tmp_path: Path) -> None:
    editor = WorkspaceEditor(_checkout(tmp_path))

    assert editor.revert("never-touched.js") is False


def test_second_write_does_not_reclaim_original_state(tmp_path: Path) -> None:
    """The *first* write's bytes are what gets restored, even after a
    second write to the same relpath overwrites it again."""
    target = tmp_path / "chat.js"
    target.write_text("v0\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path))

    editor.write_fix("chat.js", "v1\n")
    editor.write_fix("chat.js", "v2\n")
    editor.revert("chat.js")

    assert target.read_text(encoding="utf-8") == "v0\n"


def test_revert_all_reverts_every_touched_file(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("a-old\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("b-old\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path))
    editor.write_fix("a.js", "a-new\n")
    editor.write_fix("b.js", "b-new\n")

    editor.revert_all()

    assert (tmp_path / "a.js").read_text(encoding="utf-8") == "a-old\n"
    assert (tmp_path / "b.js").read_text(encoding="utf-8") == "b-old\n"


def test_touched_reports_written_relpaths_in_order(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("b\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path))
    editor.write_fix("a.js", "a2\n")
    editor.write_fix("b.js", "b2\n")

    assert editor.touched == ("a.js", "b.js")


# -- resolve() delegates to Checkout.resolve_inside ----------------------------


def test_resolve_blocks_path_traversal(tmp_path: Path) -> None:
    editor = WorkspaceEditor(_checkout(tmp_path))

    with pytest.raises(CloneError, match="escapes checkout root"):
        editor.resolve("../outside.js")


# -- session() context manager -------------------------------------------------


def test_session_reverts_all_edits_on_normal_exit(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_text("old\n", encoding="utf-8")
    checkout = _checkout(tmp_path, is_temporary=True)
    editor = WorkspaceEditor(checkout)

    with editor.session() as session_editor:
        session_editor.write_fix("chat.js", "new\n")
        assert target.read_text(encoding="utf-8") == "new\n"

    assert target.read_text(encoding="utf-8") == "old\n"


def test_session_reverts_all_edits_on_exception(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_text("old\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path, is_temporary=True))

    with pytest.raises(RuntimeError):
        with editor.session() as session_editor:
            session_editor.write_fix("chat.js", "new\n")
            raise RuntimeError("boom")

    assert target.read_text(encoding="utf-8") == "old\n"


def test_session_keep_on_exit_is_honored_for_temporary_checkout(tmp_path: Path) -> None:
    target = tmp_path / "chat.js"
    target.write_text("old\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path, is_temporary=True))

    with editor.session(keep_on_exit=True) as session_editor:
        session_editor.write_fix("chat.js", "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"


def test_session_keep_on_exit_is_ignored_for_non_temporary_checkout(tmp_path: Path) -> None:
    """A caller-supplied local directory (``is_temporary=False``) is never
    left modified, even if the caller asked to keep edits on exit."""
    target = tmp_path / "chat.js"
    target.write_text("old\n", encoding="utf-8")
    editor = WorkspaceEditor(_checkout(tmp_path, is_temporary=False))

    with editor.session(keep_on_exit=True) as session_editor:
        session_editor.write_fix("chat.js", "new\n")

    assert target.read_text(encoding="utf-8") == "old\n"
