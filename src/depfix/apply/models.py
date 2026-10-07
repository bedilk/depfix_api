"""Data models for the transactional in-place file-editing subsystem.

See :mod:`depfix.apply.workspace` for the editor that produces these.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from depfix.core.models import ValidationResult


class EditOrigin(enum.StrEnum):
    """Who authored an edit."""

    CODEMOD = "codemod"
    LLM = "llm"
    PLAN = "plan"


class EditVerdict(enum.StrEnum):
    """Outcome of writing one candidate fix to one file.

    ``APPLIED``: the edit is on disk and nothing has flagged it as suspect.
    ``KEPT``: the edit is on disk after an inspection step (e.g. post-fix
        validation, or test verification) explicitly confirmed it should
        stay -- distinguished from ``APPLIED`` only by *when* it was decided.
    ``REVERTED``: the edit was written and then rolled back (validation
        failed, verification regressed, or the session was closed without
        ``keep_on_exit``).
    ``SUSPECT``: the edit was left on disk but something about it could not
        be fully confirmed (e.g. verification was skipped or unavailable) --
        surfaced distinctly from ``APPLIED``/``KEPT`` so callers don't
        over-trust it, and so patches are still written for it (see
        :mod:`depfix.core.pipeline`) rather than silently dropped.
    ``SKIPPED``: no edit was attempted (e.g. fixed content identical to the
        original, or the file could not be read/written).
    """

    APPLIED = "applied"
    KEPT = "kept"
    REVERTED = "reverted"
    SUSPECT = "suspect"
    SKIPPED = "skipped"


#: Verdicts for which the fixed content is (as far as we know) currently
#: written to disk -- i.e. NOT reverted and NOT skipped.
ON_DISK_VERDICTS = frozenset({EditVerdict.APPLIED, EditVerdict.KEPT, EditVerdict.SUSPECT})


@dataclass
class FileEdit:
    """One candidate fix applied (or attempted) against one repo-relative file.

    ``relpath`` is repo-relative POSIX, matching the convention used by
    :class:`depfix.scanners.models.CallSite` -- diff headers
    (``a/{relpath}`` / ``b/{relpath}``, see
    :func:`depfix.validators.javascript.create_unified_diff`) and any DB
    persistence only make sense with a relative path, not an absolute one
    tied to a throwaway clone directory.
    """

    relpath: str
    original_content: str
    fixed_content: str
    diff: str
    verdict: EditVerdict
    confidence: float = 0.0
    usages_fixed: int = 0
    validation: ValidationResult | None = None
    error_message: str = ""
    origin: EditOrigin = EditOrigin.LLM
    codemod_id: str = ""
    derivation_trace: str = ""

    @property
    def is_on_disk(self) -> bool:
        """Whether ``fixed_content`` is (as far as we know) currently written
        to the real file, i.e. not reverted and not skipped."""
        return self.verdict in ON_DISK_VERDICTS

    @property
    def changed(self) -> bool:
        """Whether the fix actually differs from the original content."""
        return self.original_content != self.fixed_content
