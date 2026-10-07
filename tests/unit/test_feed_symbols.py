"""Feeds -> symbols -> scan -> ACTIONABLE (old) vs CURRENT (already migrated)."""

from pathlib import Path

from depfix.classify.classifier import Classifier
from depfix.classify.llm import LLMResponse
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.ecosystems.callsites import GenericImportScanner
from depfix.ecosystems.specs import PYTHON
from depfix.providers.models import ProviderSpec, SdkPackage
from depfix.scanners.callsites import CallSiteScanner
from depfix.scanners.matching import ScanMatchStatus, assess_scan_change
from depfix.scanners.models import CallSiteKind, MatchConfidence
from depfix.scanners.repo import build_scan_target, feed_symbols_for_changes, symbols_for_change
from depfix.sources.models import ChangeEvent, SourceKind

PROVIDER = ProviderSpec(
    id="openai",
    name="OpenAI",
    sdk_packages=(SdkPackage("openai"), SdkPackage("openai", "pypi")),
)


def _rename(**kw) -> BreakingChange:
    base = {
        "package": "openai",
        "old_version": "3.3.0",
        "new_version": "4.0.0",
        "old_api": "openai.createModeration",
        "new_api": "openai.moderations.create",
        "description": "d",
        "migration_guide": "",
        "kind": ChangeKind.METHOD_RENAMED,
        "source": ClassificationSource.RELEASE_NOTES,
        "provider_id": "openai",
    }
    base.update(kw)
    return BreakingChange(**base)


def _js(tmp_path: Path, body: str, change: BreakingChange | None = None):
    (tmp_path / "a.js").write_text(
        f'const OpenAI = require("openai");\nconst client = new OpenAI();\n{body}\n'
    )
    return CallSiteScanner().scan(
        tmp_path, build_scan_target(PROVIDER, feed_changes=[change or _rename()])
    )


def _py(tmp_path: Path, source: str):
    (tmp_path / "app.py").write_text(source)
    target = build_scan_target(PROVIDER, feed_changes=[_rename()])
    return GenericImportScanner(PYTHON).scan(tmp_path, target)


# -- symbol derivation from feeds --------------------------------------------


def test_feed_symbols_cover_old_and_replacement() -> None:
    assert feed_symbols_for_changes("openai", [_rename()]) == (
        "openai.createModeration",
        "openai.moderations.create",
    )


def test_replacement_hint_does_not_count_as_old_api() -> None:
    change = _rename(call_site_hints=["openai.createModeration", "openai.moderations.create"])
    assert symbols_for_change("openai", change) == ("openai.createModeration",)


def test_broad_target_has_feed_symbols_but_no_narrowing() -> None:
    target = build_scan_target(PROVIDER, feed_changes=[_rename()])
    assert target.symbols == ()
    assert "openai.moderations.create" in target.feed_symbols


def test_registry_drift_contributes_no_symbols() -> None:
    drift = _rename(
        old_api="openai@3.3.0",
        new_api="openai@4.0.0",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )
    assert feed_symbols_for_changes("openai", [drift]) == ()


# -- JavaScript: binding-aware scanner ---------------------------------------


def test_js_unmigrated_is_actionable(tmp_path: Path) -> None:
    result = _js(tmp_path, "client.createModeration({});")
    assessment = assess_scan_change(result, _rename())
    assert assessment.status is ScanMatchStatus.ACTIONABLE
    assert [s.symbol for s in assessment.matched_sites] == ["openai.createModeration"]


def test_js_migrated_is_current(tmp_path: Path) -> None:
    result = _js(tmp_path, "client.moderations.create({});")
    assert assess_scan_change(result, _rename()).status is ScanMatchStatus.CURRENT


def test_js_partially_migrated_is_actionable_on_old_sites_only(tmp_path: Path) -> None:
    result = _js(tmp_path, "client.moderations.create({});\nclient.createModeration({});")
    assessment = assess_scan_change(result, _rename())
    assert assessment.status is ScanMatchStatus.ACTIONABLE
    assert {s.symbol for s in assessment.matched_sites} == {"openai.createModeration"}


def test_same_method_param_change_is_never_current(tmp_path: Path) -> None:
    change = _rename(
        old_api="POST /v1/responses (connector_id)",
        new_api="openai.responses.create",
        kind=ChangeKind.PARAM_REMOVED,
        source=ClassificationSource.SPEC_DIFF,
        call_site_hints=["responses.create"],
    )
    result = _js(tmp_path, "client.responses.create({});", change)
    assert assess_scan_change(result, change).status is ScanMatchStatus.ACTIONABLE


def test_narrowed_scan_still_sees_the_replacement(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text(
        'const OpenAI = require("openai");\nconst c = new OpenAI();\nc.moderations.create({});\n'
    )
    target = build_scan_target(PROVIDER, _rename())  # explicit selection narrows
    result = CallSiteScanner().scan(tmp_path, target)
    assert assess_scan_change(result, _rename()).status is ScanMatchStatus.CURRENT


# -- Python: generic import scanner (previously found imports only) ----------


def test_python_unmigrated_is_actionable(tmp_path: Path) -> None:
    result = _py(
        tmp_path,
        "from openai import OpenAI\nclient = OpenAI()\nclient.createModeration(input='x')\n",
    )
    assert assess_scan_change(result, _rename()).status is ScanMatchStatus.ACTIONABLE


def test_python_migrated_is_current(tmp_path: Path) -> None:
    result = _py(
        tmp_path,
        "from openai import OpenAI\nclient = OpenAI()\nclient.moderations.create(input='x')\n",
    )
    assert assess_scan_change(result, _rename()).status is ScanMatchStatus.CURRENT


def test_python_member_path_without_import_is_report_only(tmp_path: Path) -> None:
    result = _py(tmp_path, "thing.moderations.create(1)\n")
    sites = [s for s in result.call_sites if s.kind is CallSiteKind.METHOD_CALL]
    assert all(s.confidence is MatchConfidence.LOW for s in sites)
    assert assess_scan_change(result, _rename()).status is ScanMatchStatus.NO_CALL_SITES


# -- registry events with release notes yield real migrations ----------------


class _Notes:
    def complete(self, prompt: str, *, temperature: float = 0.0) -> LLMResponse:
        return LLMResponse(
            text=(
                '[{"kind": "method_renamed", '
                '"old_api": "openai.createModeration()", '
                '"new_api": "openai.moderations.create()", '
                '"description": "renamed", '
                '"evidence": "Removed `openai.createModeration()`"}]'
            )
        )


def test_npm_event_with_notes_produces_drift_and_migration() -> None:
    event = ChangeEvent(
        provider_id="openai",
        feed_key="npm:openai:latest",
        source_kind=SourceKind.NPM_DIST_TAG,
        old_token="3.3.0",
        new_token="4.0.0",
        body=(
            "## 4.0.0\n### Breaking\n- Removed `openai.createModeration()`. "
            "Use `openai.moderations.create()`."
        ),
    )
    changes = Classifier(_Notes()).classify(event).changes
    kinds = {c.kind for c in changes}
    assert kinds == {ChangeKind.DEPENDENCY_VERSION_BUMP, ChangeKind.METHOD_RENAMED}
    assert "openai.moderations.create" in feed_symbols_for_changes("openai", changes)
