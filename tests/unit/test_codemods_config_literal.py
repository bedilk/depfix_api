"""Unit tests for the dated-API-version and config-key-rename codemods."""

from __future__ import annotations

from depfix.codemods.config_literal import ApiVersionLiteralCodemod, ConfigKeyCodemod
from depfix.core.models import BreakingChange, ChangeKind


def _change(
    *, kind: ChangeKind, old_api: str, new_api: str, package: str = "stripe"
) -> BreakingChange:
    return BreakingChange(
        package=package,
        old_version="1",
        new_version="2",
        old_api=old_api,
        new_api=new_api,
        description="",
        migration_guide="",
        kind=kind,
    )


# -- ApiVersionLiteralCodemod ------------------------------------------------


def test_api_version_codemod_claims_dated_version_bump() -> None:
    codemod = ApiVersionLiteralCodemod()
    change = _change(kind=ChangeKind.API_VERSION_BUMP, old_api="2023-10-16", new_api="2024-06-20")
    assert codemod.claims(change)


def test_api_version_codemod_does_not_claim_non_dated_values() -> None:
    codemod = ApiVersionLiteralCodemod()
    change = _change(kind=ChangeKind.API_VERSION_BUMP, old_api="v3", new_api="v4")
    assert not codemod.claims(change)


def test_api_version_codemod_rewrites_matching_literal() -> None:
    codemod = ApiVersionLiteralCodemod()
    change = _change(kind=ChangeKind.API_VERSION_BUMP, old_api="2023-10-16", new_api="2024-06-20")
    source = 'const client = new Stripe(key, { apiVersion: "2023-10-16" });\n'

    result = codemod.apply(source, change)

    assert result.changed
    assert '"2024-06-20"' in result.source
    assert "2023-10-16" not in result.source


def test_api_version_codemod_is_unchanged_when_literal_absent() -> None:
    codemod = ApiVersionLiteralCodemod()
    change = _change(kind=ChangeKind.API_VERSION_BUMP, old_api="2023-10-16", new_api="2024-06-20")

    result = codemod.apply("const x = 1;\n", change)

    assert not result.changed


# -- ConfigKeyCodemod ---------------------------------------------------------


def test_config_key_codemod_renames_object_literal_key() -> None:
    codemod = ConfigKeyCodemod()
    change = _change(kind=ChangeKind.PARAM_RENAMED, old_api="oldKey", new_api="newKey")
    source = "const config = {\n  oldKey: true,\n};\n"

    result = codemod.apply(source, change)

    assert result.changed
    assert "newKey: true" in result.source
    assert "oldKey" not in result.source


def test_config_key_codemod_ignores_ternary_reference() -> None:
    """`cond ? oldKey : fallback` names a *variable*, not an object key --
    the position guard (must follow `{`, `,`, `(`, or a line start) must
    not treat the ternary's else-branch as a rename site."""
    codemod = ConfigKeyCodemod()
    change = _change(kind=ChangeKind.PARAM_RENAMED, old_api="oldKey", new_api="newKey")
    source = "const value = flag ? oldKey : fallback;\n"

    result = codemod.apply(source, change)

    assert not result.changed
    assert "oldKey" in source  # unchanged input, sanity check


def test_config_key_codemod_renames_key_at_start_of_line() -> None:
    codemod = ConfigKeyCodemod()
    change = _change(kind=ChangeKind.FIELD_RENAMED, old_api="oldKey", new_api="newKey")
    source = "oldKey: 1,\nother: 2,\n"

    result = codemod.apply(source, change)

    assert result.changed
    assert result.source.startswith("newKey: 1,")
