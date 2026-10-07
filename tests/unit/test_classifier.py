"""Unit tests for ``Classifier.classify``'s three-way branch: deterministic
spec-diff, LLM-backed release notes, and the terminal ``unclassifiable``
outcome for events with no signal at all.
"""

from __future__ import annotations

from depfix.classify.classifier import Classifier
from depfix.classify.llm import LLMResponse
from depfix.core.models import ChangeKind, ClassificationSource
from depfix.sources.models import ChangeEvent, Severity, SourceKind, SpecChange, SpecChangeKind


def _event(**overrides: object) -> ChangeEvent:
    base: dict[str, object] = {
        "provider_id": "acme",
        "feed_key": "npm:widgets:latest",
        "source_kind": SourceKind.GITHUB_RELEASE,
        "old_token": "1.0.0",
        "new_token": "2.0.0",
    }
    base.update(overrides)
    return ChangeEvent(**base)  # type: ignore[arg-type]


def test_classify_spec_diff_path_needs_no_llm() -> None:
    spec_change = SpecChange(
        kind=SpecChangeKind.PATH_REMOVED,
        severity=Severity.BREAKING,
        subject="POST /v1/widgets",
        pointer="paths./v1/widgets",
    )
    classifier = Classifier(None)

    outcome = classifier.classify(_event(spec_changes=[spec_change]))

    assert outcome.ok
    assert not outcome.unclassifiable
    assert len(outcome.changes) == 1


def test_classify_is_unclassifiable_when_no_spec_changes_and_empty_body() -> None:
    classifier = Classifier(None)

    outcome = classifier.classify(_event(body=""))

    assert outcome.ok
    assert outcome.unclassifiable
    assert outcome.changes == []


def test_classify_npm_dist_tag_as_dependency_version_drift_without_an_llm() -> None:
    """A registry version move is actionable even without release notes.

    This is deliberately a manifest-level change, not an invented API
    migration: the later plan/apply policy decides whether this version class
    is safe to update automatically.
    """
    classifier = Classifier(None)

    outcome = classifier.classify(
        _event(source_kind=SourceKind.NPM_DIST_TAG, raw={"package": "acme-sdk"}, body="")
    )

    assert outcome.ok
    assert len(outcome.changes) == 1
    change = outcome.changes[0]
    assert change.package == "acme-sdk"
    assert change.kind is ChangeKind.DEPENDENCY_VERSION_BUMP
    assert change.source is ClassificationSource.REGISTRY
    assert change.old_api == "acme-sdk@1.0.0"
    assert change.new_api == "acme-sdk@2.0.0"


def test_classify_persisted_npm_event_recovers_the_package_from_its_feed_key() -> None:
    outcome = Classifier(None).classify(_event(source_kind=SourceKind.NPM_DIST_TAG, body=""))

    assert outcome.changes[0].package == "widgets"


def test_classify_persisted_pypi_event_recovers_the_package_from_its_feed_key() -> None:
    outcome = Classifier(None).classify(
        _event(source_kind=SourceKind.PYPI_DIST_TAG, feed_key="pypi:openai", body="")
    )

    assert outcome.changes[0].package == "openai"
    assert outcome.changes[0].source is ClassificationSource.REGISTRY


def test_classify_is_unclassifiable_for_whitespace_only_body() -> None:
    classifier = Classifier(None)

    outcome = classifier.classify(_event(body="   \n  "))

    assert outcome.unclassifiable


def test_classify_errors_not_unclassifiable_when_body_present_but_no_llm_configured() -> None:
    classifier = Classifier(None)

    outcome = classifier.classify(_event(body="some release notes"))

    assert not outcome.ok
    assert not outcome.unclassifiable
    assert "no LLM configured" in outcome.errors[0]


class _FakeCompleter:
    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, prompt: str, *, temperature: float = 0.0) -> LLMResponse:
        return LLMResponse(text=self.text)


def test_classify_real_llm_verdict_of_zero_changes_is_not_unclassifiable() -> None:
    classifier = Classifier(_FakeCompleter("[]"))

    outcome = classifier.classify(_event(body="nothing breaking here"))

    assert outcome.ok
    assert not outcome.unclassifiable
    assert outcome.changes == []


def test_classify_synthesizes_a_grounded_migration_from_spec_and_notes() -> None:
    spec_change = SpecChange(
        kind=SpecChangeKind.OPERATION_REMOVED,
        severity=Severity.BREAKING,
        subject="POST /v1/widgets",
        pointer="paths./v1/widgets",
    )
    response = (
        '{"old_api":"acme.widgets.create","new_api":"acme.responses.create",'
        '"migration_guide":"Move widget calls to responses.",'
        '"call_site_hints":["widgets.create"],'
        '"evidence":"The widgets endpoint was removed"}'
    )

    outcome = Classifier(_FakeCompleter(response)).classify(
        _event(body="The widgets endpoint was removed", spec_changes=[spec_change])
    )

    assert outcome.changes[0].source in (
        ClassificationSource.LLM_SYNTHESIS,
        ClassificationSource.SPEC_DIFF,
    )
    assert outcome.changes[0].old_api == "widgets.create"
