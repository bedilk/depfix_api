"""Transactional in-place file editing.

:class:`WorkspaceEditor` writes candidate fixes to real files on disk, one
:class:`~depfix.apply.models.FileEdit` at a time, while keeping enough state
to revert everything it touched -- byte for byte, including line-ending
style, trailing-newline presence, BOM, and file mode. This is what lets
:class:`depfix.verify.verifier.Verifier` run the repo's real test suite
against a "with fix applied" tree and then put the tree back exactly as it
found it, regardless of whether the fix is ultimately kept.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from depfix.apply.models import EditOrigin, EditVerdict, FileEdit
from depfix.clone import Checkout
from depfix.core.models import ValidationResult
from depfix.validators.javascript import create_unified_diff

logger = logging.getLogger(__name__)

_BOM = b"\xef\xbb\xbf"


@dataclass
class _Original:
    """Everything needed to restore a file exactly as it was found."""

    raw_bytes: bytes
    mode: int


class WorkspaceEditor:
    """Applies and reverts :class:`FileEdit`\\ s against files under
    ``checkout.path``.

    Not safe to share across threads -- it tracks per-instance state
    (``_originals``) about what it has changed on this one checkout.
    """

    def __init__(self, checkout: Checkout) -> None:
        self._checkout = checkout
        self._originals: dict[str, _Original] = {}

    @property
    def checkout(self) -> Checkout:
        return self._checkout

    @property
    def touched(self) -> tuple[str, ...]:
        """Repo-relative paths this editor has written to (and can revert)."""
        return tuple(self._originals)

    def resolve(self, relpath: str) -> Path:
        """Resolve ``relpath`` to a real path inside the checkout, raising
        :class:`depfix.clone.CloneError` if it would escape the checkout
        root (see :meth:`depfix.clone.service.Checkout.resolve_inside`)."""
        return self._checkout.resolve_inside(relpath)

    def write_fix(
        self,
        relpath: str,
        fixed_content: str,
        *,
        confidence: float = 0.0,
        usages_fixed: int = 0,
        validation: ValidationResult | None = None,
        origin: EditOrigin = EditOrigin.LLM,
        codemod_id: str = "",
    ) -> FileEdit:
        """Write ``fixed_content`` to ``relpath``, preserving the original
        file's byte-level formatting quirks (line endings, trailing newline,
        BOM) so the diff -- and the file itself, if reverted -- reflects only
        the actual fix, not incidental re-normalization.

        The first write to a given ``relpath`` this editor makes captures the
        original bytes/mode for later :meth:`revert`; subsequent writes to
        the same ``relpath`` do not re-capture (the *first* observed state is
        what gets restored).
        """
        path = self.resolve(relpath)
        try:
            original_bytes = path.read_bytes()
            original_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            return FileEdit(
                relpath=relpath,
                original_content="",
                fixed_content=fixed_content,
                diff="",
                verdict=EditVerdict.SKIPPED,
                confidence=confidence,
                usages_fixed=usages_fixed,
                validation=validation,
                error_message=f"could not read {relpath}: {exc}",
                origin=origin,
                codemod_id=codemod_id,
            )

        try:
            original_text = _decode(original_bytes)
        except UnicodeDecodeError as exc:
            return FileEdit(
                relpath=relpath,
                original_content="",
                fixed_content=fixed_content,
                diff="",
                verdict=EditVerdict.SKIPPED,
                confidence=confidence,
                usages_fixed=usages_fixed,
                validation=validation,
                error_message=f"could not decode {relpath} as utf-8: {exc}",
                origin=origin,
                codemod_id=codemod_id,
            )

        if relpath not in self._originals:
            self._originals[relpath] = _Original(raw_bytes=original_bytes, mode=original_mode)

        if fixed_content == original_text:
            return FileEdit(
                relpath=relpath,
                original_content=original_text,
                fixed_content=fixed_content,
                diff="",
                verdict=EditVerdict.SKIPPED,
                confidence=confidence,
                usages_fixed=usages_fixed,
                validation=validation,
                origin=origin,
                codemod_id=codemod_id,
            )

        diff = create_unified_diff(original_text, fixed_content, relpath)
        encoded = _encode_like(fixed_content, original_bytes)

        try:
            _atomic_write(path, encoded, mode=original_mode)
        except OSError as exc:
            return FileEdit(
                relpath=relpath,
                original_content=original_text,
                fixed_content=fixed_content,
                diff=diff,
                verdict=EditVerdict.SKIPPED,
                confidence=confidence,
                usages_fixed=usages_fixed,
                validation=validation,
                error_message=f"could not write {relpath}: {exc}",
                origin=origin,
                codemod_id=codemod_id,
            )

        verdict = (
            EditVerdict.SUSPECT
            if validation is not None and not validation.is_valid
            else EditVerdict.APPLIED
        )
        return FileEdit(
            relpath=relpath,
            original_content=original_text,
            fixed_content=fixed_content,
            diff=diff,
            verdict=verdict,
            confidence=confidence,
            usages_fixed=usages_fixed,
            validation=validation,
            origin=origin,
            codemod_id=codemod_id,
        )

    def revert(self, relpath: str) -> bool:
        """Restore ``relpath`` to the bytes/mode it had before this editor
        first touched it. No-op (returns ``False``) if this editor never
        wrote to it."""
        original = self._originals.get(relpath)
        if original is None:
            return False
        path = self.resolve(relpath)
        _atomic_write(path, original.raw_bytes, mode=original.mode)
        return True

    def revert_all(self) -> None:
        """Revert every file this editor has written to, in the order they
        were first touched."""
        for relpath in tuple(self._originals):
            self.revert(relpath)

    @contextmanager
    def session(self, *, keep_on_exit: bool = False) -> Iterator[WorkspaceEditor]:
        """Context manager guaranteeing every edit made inside the ``with``
        block is reverted on exit -- including on exception -- unless
        ``keep_on_exit`` is set.

        ``keep_on_exit`` is only honored when the underlying checkout is
        temporary (``checkout.is_temporary``): a caller-supplied local
        directory (``depfix fix --path``) is never left modified by this
        tool no matter what the caller asks for, since that would silently
        mutate the user's own working tree outside of a throwaway clone.
        """
        try:
            yield self
        finally:
            # Not `if ...: return` -- a bare `return` in a `finally` block
            # silently discards any exception propagating out of the `try`,
            # which would turn a real failure inside the `with` block into a
            # quiet no-op whenever `keep_on_exit` happened to be set.
            if not (keep_on_exit and self._checkout.is_temporary):
                self.revert_all()


def _decode(raw: bytes) -> str:
    if raw.startswith(_BOM):
        return raw[len(_BOM) :].decode("utf-8")
    return raw.decode("utf-8")


def _encode_like(text: str, original_bytes: bytes) -> bytes:
    """Re-encode ``text`` matching the original file's BOM, line-ending
    style, and trailing-newline presence, so a fix that changes only a few
    lines doesn't produce a diff full of incidental whitespace noise."""
    has_bom = original_bytes.startswith(_BOM)
    body = original_bytes[len(_BOM) :] if has_bom else original_bytes

    uses_crlf = b"\r\n" in body
    ends_with_newline = body.endswith(b"\n") if body else text.endswith("\n")

    normalized = text.replace("\r\n", "\n")
    if ends_with_newline and not normalized.endswith("\n"):
        normalized += "\n"
    elif not ends_with_newline and normalized.endswith("\n"):
        normalized = normalized[:-1]

    if uses_crlf:
        normalized = normalized.replace("\n", "\r\n")

    encoded = normalized.encode("utf-8")
    return _BOM + encoded if has_bom else encoded


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    """Write ``data`` to ``path`` atomically: write to a same-directory temp
    file first, then ``os.replace`` it into place. Avoids leaving a
    truncated/partial file behind if the process is interrupted mid-write."""
    tmp_path = path.with_name(f".{path.name}.depfix-tmp-{os.getpid()}")
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(data)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
