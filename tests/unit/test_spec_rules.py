"""Unit tests for deterministic spec-diff classification."""

from __future__ import annotations

from depfix.classify.spec_rules import classify_spec_changes, sdk_path_hints
from depfix.core.models import ChangeKind, ClassificationSource
from depfix.sources.models import Severity, SpecChange, SpecChangeKind


def test_path_removed_maps_to_method_removed() -> None:
    changes = [
        SpecChange(
            kind=SpecChangeKind.PATH_REMOVED,
            severity=Severity.BREAKING,
            subject="GET /v1/widgets",
            pointer="paths./v1/widgets",
        )
    ]
    out = classify_spec_changes(
        changes,
        package="widgetapi",
        old_version="1.0",
        new_version="2.0",
        provider_id="widgetapi",
        source_url="https://example.com/spec.json",
    )
    assert len(out) == 1
    bc = out[0]
    assert bc.kind is ChangeKind.METHOD_REMOVED
    assert bc.source is ClassificationSource.SPEC_DIFF
    assert bc.confidence == 1.0
    assert bc.old_api == "widgets.list"  # dotted SDK symbol
    assert "GET /v1/widgets" in bc.description  # HTTP subject preserved for reviewers
    assert bc.new_api == ""


def test_operation_id_changed_maps_to_method_renamed_and_uses_after() -> None:
    changes = [
        SpecChange(
            kind=SpecChangeKind.OPERATION_ID_CHANGED,
            severity=Severity.BREAKING,
            subject="POST /v1/charges",
            pointer="paths./v1/charges.post.operationId",
            before="createCharge",
            after="charges.create",
        )
    ]
    out = classify_spec_changes(
        changes,
        package="stripe",
        old_version="1.0",
        new_version="2.0",
        provider_id="stripe",
        source_url=None,
    )
    assert len(out) == 1
    assert out[0].kind is ChangeKind.METHOD_RENAMED
    assert out[0].new_api == "charges.create"


def test_param_removed_request_vs_response_direction_differs() -> None:
    changes = [
        SpecChange(
            kind=SpecChangeKind.PROPERTY_REMOVED,
            severity=Severity.BREAKING,
            subject="POST /v1/charges",
            pointer="paths./v1/charges.post.requestBody.source",
            direction="request",
            before="source",
        ),
        SpecChange(
            kind=SpecChangeKind.PROPERTY_REMOVED,
            severity=Severity.BREAKING,
            subject="GET /v1/charges/{id}",
            pointer="paths./v1/charges/{id}.get.responses.200.legacy_id",
            direction="response",
            before="legacy_id",
        ),
    ]
    out = classify_spec_changes(
        changes,
        package="stripe",
        old_version="1.0",
        new_version="2.0",
        provider_id="stripe",
        source_url=None,
    )
    kinds = {bc.kind for bc in out}
    assert ChangeKind.PARAM_REMOVED in kinds
    assert ChangeKind.FIELD_REMOVED in kinds


def test_additive_changes_are_ignored() -> None:
    changes = [
        SpecChange(
            kind=SpecChangeKind.OPERATION_ADDED,
            severity=Severity.ADDITIVE,
            subject="POST /v1/new-thing",
            pointer="paths./v1/new-thing",
        ),
        SpecChange(
            kind=SpecChangeKind.PROPERTY_ADDED,
            severity=Severity.ADDITIVE,
            subject="POST /v1/charges",
            pointer="paths./v1/charges.post.requestBody.metadata",
        ),
    ]
    out = classify_spec_changes(
        changes,
        package="stripe",
        old_version="1.0",
        new_version="2.0",
        provider_id="stripe",
        source_url=None,
    )
    assert out == []


def test_multiple_removed_params_on_same_endpoint_collapse_into_one_change() -> None:
    changes = [
        SpecChange(
            kind=SpecChangeKind.PARAM_REMOVED,
            severity=Severity.BREAKING,
            subject="POST /v1/charges",
            pointer="paths./v1/charges.post.parameters.source",
            direction="request",
            before="source",
        ),
        SpecChange(
            kind=SpecChangeKind.PARAM_REMOVED,
            severity=Severity.BREAKING,
            subject="POST /v1/charges",
            pointer="paths./v1/charges.post.parameters.card",
            direction="request",
            before="card",
        ),
    ]
    out = classify_spec_changes(
        changes,
        package="stripe",
        old_version="1.0",
        new_version="2.0",
        provider_id="stripe",
        source_url=None,
    )
    assert len(out) == 1
    assert "source" in out[0].old_api
    assert "card" in out[0].old_api


def test_sdk_path_hints_maps_verbs() -> None:
    assert sdk_path_hints("POST /v1/charges") == "charges.create"
    assert sdk_path_hints("GET /v1/charges/{id}") == "charges.retrieve"
    assert sdk_path_hints("GET /v1/charges") == "charges.list"
    assert sdk_path_hints("DELETE /v1/charges/{id}") == "charges.delete"


def test_sdk_path_hints_returns_none_for_malformed_subject() -> None:
    assert sdk_path_hints("not-a-valid-subject") is None
