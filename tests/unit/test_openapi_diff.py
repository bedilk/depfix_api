"""Unit tests for the OpenAPI structural differ. Pure functions, no I/O."""

from __future__ import annotations

from typing import Any

from depfix.sources.models import Severity, SpecChangeKind
from depfix.sources.openapi_diff import diff_specs


def _spec(paths: dict[str, Any], components: dict[str, Any] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {"openapi": "3.0.0", "info": {"version": "1"}, "paths": paths}
    if components:
        doc["components"] = components
    return doc


def _op(**kwargs: Any) -> dict[str, Any]:
    return {"operationId": "doThing", "responses": {"200": {}}, **kwargs}


def _kinds(changes: list[Any]) -> set[SpecChangeKind]:
    return {c.kind for c in changes}


def test_removed_path_is_breaking() -> None:
    old = _spec({"/v1/charges": {"post": _op()}})
    new = _spec({})
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.PATH_REMOVED
    assert change.severity is Severity.BREAKING
    assert change.subject == "/v1/charges"


def test_removed_operation_is_breaking() -> None:
    old = _spec({"/v1/charges": {"post": _op(), "get": _op()}})
    new = _spec({"/v1/charges": {"get": _op()}})
    changes = diff_specs(old, new)
    assert _kinds(changes) == {SpecChangeKind.OPERATION_REMOVED}
    assert changes[0].subject == "POST /v1/charges"


def test_added_operation_is_additive() -> None:
    old = _spec({"/v1/charges": {"get": _op()}})
    new = _spec({"/v1/charges": {"get": _op(), "post": _op()}})
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.OPERATION_ADDED
    assert change.severity is Severity.ADDITIVE


def test_operation_id_rename_is_potentially_breaking() -> None:
    old = _spec({"/x": {"get": _op(operationId="listThings")}})
    new = _spec({"/x": {"get": _op(operationId="thingsList")}})
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.OPERATION_ID_CHANGED
    assert change.severity is Severity.POTENTIALLY_BREAKING
    assert change.before == "listThings"
    assert change.after == "thingsList"


def test_param_becoming_required_is_breaking() -> None:
    old = _spec({"/x": {"get": _op(parameters=[{"name": "limit", "in": "query"}])}})
    new = _spec(
        {"/x": {"get": _op(parameters=[{"name": "limit", "in": "query", "required": True}])}}
    )
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.PARAM_NOW_REQUIRED
    assert change.severity is Severity.BREAKING


def test_removed_param_is_breaking() -> None:
    old = _spec({"/x": {"get": _op(parameters=[{"name": "cursor", "in": "query"}])}})
    new = _spec({"/x": {"get": _op(parameters=[])}})
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.PARAM_REMOVED
    assert change.direction == "request"


def test_path_level_params_are_merged_into_operations() -> None:
    old = _spec({"/x": {"parameters": [{"name": "id", "in": "path"}], "get": _op()}})
    new = _spec({"/x": {"parameters": [], "get": _op()}})
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.PARAM_REMOVED


def _with_body(schema: dict[str, Any]) -> dict[str, Any]:
    return _op(requestBody={"content": {"application/json": {"schema": schema}}})


def test_removed_request_property_is_breaking() -> None:
    old = _spec(
        {
            "/x": {
                "post": _with_body(
                    {"type": "object", "properties": {"amount": {"type": "integer"}}}
                )
            }
        }
    )
    new = _spec({"/x": {"post": _with_body({"type": "object", "properties": {}})}})
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.PROPERTY_REMOVED
    assert change.severity is Severity.BREAKING
    assert change.direction == "request"


def test_newly_required_request_property_is_breaking() -> None:
    base = {"type": "object", "properties": {"currency": {"type": "string"}}}
    old = _spec({"/x": {"post": _with_body(base)}})
    new = _spec({"/x": {"post": _with_body({**base, "required": ["currency"]})}})
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.PROPERTY_NOW_REQUIRED


def test_new_optional_request_property_is_additive() -> None:
    old = _spec({"/x": {"post": _with_body({"type": "object", "properties": {}})}})
    new = _spec(
        {"/x": {"post": _with_body({"type": "object", "properties": {"memo": {"type": "string"}}})}}
    )
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.PROPERTY_ADDED
    assert change.severity is Severity.ADDITIVE


def test_type_change_is_breaking() -> None:
    old = _spec(
        {
            "/x": {
                "post": _with_body(
                    {"type": "object", "properties": {"amount": {"type": "integer"}}}
                )
            }
        }
    )
    new = _spec(
        {
            "/x": {
                "post": _with_body({"type": "object", "properties": {"amount": {"type": "string"}}})
            }
        }
    )
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.PROPERTY_TYPE_CHANGED
    assert (change.before, change.after) == ("integer", "string")


def test_enum_removal_severity_depends_on_direction() -> None:
    req_old = _spec({"/x": {"post": _with_body({"type": "string", "enum": ["a", "b"]})}})
    req_new = _spec({"/x": {"post": _with_body({"type": "string", "enum": ["a"]})}})
    (req_change,) = diff_specs(req_old, req_new)
    assert req_change.kind is SpecChangeKind.ENUM_VALUE_REMOVED
    assert req_change.severity is Severity.BREAKING

    def resp(enum: list[str]) -> dict[str, Any]:
        return {
            "operationId": "doThing",
            "responses": {
                "200": {
                    "content": {"application/json": {"schema": {"type": "string", "enum": enum}}}
                }
            },
        }

    (resp_change,) = diff_specs(
        _spec({"/x": {"get": resp(["a", "b"])}}), _spec({"/x": {"get": resp(["a"])}})
    )
    assert resp_change.severity is Severity.POTENTIALLY_BREAKING


def test_local_refs_are_resolved() -> None:
    components = {
        "schemas": {"Charge": {"type": "object", "properties": {"amount": {"type": "integer"}}}}
    }
    old = _spec({"/x": {"post": _with_body({"$ref": "#/components/schemas/Charge"})}}, components)
    new = _spec(
        {"/x": {"post": _with_body({"$ref": "#/components/schemas/Charge"})}},
        {"schemas": {"Charge": {"type": "object", "properties": {}}}},
    )
    changes = diff_specs(old, new)
    assert SpecChangeKind.PROPERTY_REMOVED in _kinds(changes)


def test_recursive_ref_does_not_hang() -> None:
    components = {
        "schemas": {
            "Node": {
                "type": "object",
                "properties": {
                    "child": {"$ref": "#/components/schemas/Node"},
                    "id": {"type": "string"},
                },
            }
        }
    }
    old = _spec({"/x": {"post": _with_body({"$ref": "#/components/schemas/Node"})}}, components)
    assert diff_specs(old, old) == []


def test_all_of_is_flattened() -> None:
    def body(props: dict[str, Any]) -> dict[str, Any]:
        return _with_body({"allOf": [{"type": "object", "properties": props}, {"type": "object"}]})

    old = _spec({"/x": {"post": body({"amount": {"type": "integer"}})}})
    new = _spec({"/x": {"post": body({})}})
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.PROPERTY_REMOVED


def test_removed_component_schema_is_flagged() -> None:
    old = _spec({}, {"schemas": {"Charge": {"type": "object"}, "Refund": {"type": "object"}}})
    new = _spec({}, {"schemas": {"Charge": {"type": "object"}}})
    (change,) = diff_specs(old, new)
    assert change.kind is SpecChangeKind.SCHEMA_REMOVED
    assert change.severity is Severity.POTENTIALLY_BREAKING


def test_identical_specs_produce_no_changes() -> None:
    spec = _spec({"/x": {"get": _op(parameters=[{"name": "q", "in": "query"}])}})
    assert diff_specs(spec, spec) == []


def test_change_list_is_capped() -> None:
    old = _spec({f"/p{i}": {"get": _op()} for i in range(50)})
    changes = diff_specs(old, _spec({}), max_changes=10)
    assert len(changes) == 10
