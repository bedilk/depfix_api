"""Transactional in-place file editing for Week 4 fix generation.

Exposes :class:`EditVerdict` (the outcome of one edit), :class:`FileEdit`
(one edit's full record), and :class:`WorkspaceEditor` (applies/reverts
edits against a real checkout).
"""

from depfix.apply.models import ON_DISK_VERDICTS, EditVerdict, FileEdit
from depfix.apply.workspace import WorkspaceEditor

__all__ = ["ON_DISK_VERDICTS", "EditVerdict", "FileEdit", "WorkspaceEditor"]
