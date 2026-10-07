"""Fix validators — verify generated patches before writing them out."""

from __future__ import annotations

from pathlib import Path

from depfix.validators.go import GoFixValidator
from depfix.validators.javascript import FixValidator, create_unified_diff
from depfix.validators.python import PythonFixValidator
from depfix.validators.ruby import RubyFixValidator
from depfix.validators.typescript import TypeScriptSyntaxValidator, is_typescript


def validator_for_ecosystem(
    ecosystem_id: str,
    *,
    use_ast_validation: bool = True,
    checkout_root: str | Path | None = None,
):
    """The syntax validator for a fix-supported ecosystem.

    ``typescript`` maps to the JS validator (detection folds TS into
    javascript, but explicit callers may still pass it). Any other id
    also gets the JS validator -- purely to preserve the historical
    default; ids outside ``ecosystems.fixable_ids()`` never reach fix
    generation at all (gated in ``core.fix_service``), so this fallback
    is never asked to judge non-JS code in practice.
    """
    if ecosystem_id == "python":
        return PythonFixValidator(use_ast_validation=use_ast_validation)
    if ecosystem_id == "ruby":
        return RubyFixValidator(use_ast_validation=use_ast_validation)
    if ecosystem_id == "go":
        return GoFixValidator(use_ast_validation=use_ast_validation)
    return FixValidator(
        use_ast_validation=use_ast_validation,
        checkout_root=checkout_root,
    )


__all__ = [
    "FixValidator",
    "GoFixValidator",
    "PythonFixValidator",
    "RubyFixValidator",
    "TypeScriptSyntaxValidator",
    "create_unified_diff",
    "is_typescript",
    "validator_for_ecosystem",
]
