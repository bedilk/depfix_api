"""Unit tests for the four new structured-spec sources.

Each shares StructuredSpecSource's lifecycle, so the tests focus on the two
subclass hooks -- parse() and diff() -- plus the baseline-on-first-sight
invariant the base guarantees.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from depfix.sources.asyncapi_spec import AsyncApiSpecSource
from depfix.sources.models import FeedStateSnapshot, SpecChangeKind
from depfix.sources.redis_commands import RedisCommandsSource
from depfix.sources.smithy_spec import SmithySpecSource
from depfix.sources.ts_exports import TsExportsSource, _names_in_dts

_BASE = "https://raw.example"


def _http() -> httpx.Client:
    return httpx.Client()


def _state_for(source, tmp_path: Path, document: dict) -> FeedStateSnapshot:
    """Run one baseline poll so the next poll has a prior document to diff."""
    raw = json.dumps(document).encode()
    sha = hashlib.sha256(raw).hexdigest()
    path = source._write_cache(sha, source.parse(raw))
    return FeedStateSnapshot(
        last_token="v1", last_payload_sha256="stale", last_payload_path=str(path)
    )


# ── Smithy ────────────────────────────────────────────────────────────────────


def _smithy(ops: dict) -> dict:
    shapes = {"com.amazonaws.s3#S3": {"type": "service"}}
    for name, body in ops.items():
        shapes[f"com.amazonaws.s3#{name}"] = {"type": "operation", **body}
    return {"smithy": "2.0", "shapes": shapes}


@respx.mock
def test_smithy_detects_a_removed_operation(tmp_path: Path) -> None:
    old = _smithy({"PutObject": {}, "GetObject": {}})
    new = _smithy({"GetObject": {}})
    source = SmithySpecSource("aws-s3", {"url": f"{_BASE}/s3.json"}, _http(), tmp_path, 10_000_000)
    respx.get(f"{_BASE}/s3.json").mock(
        return_value=httpx.Response(200, content=json.dumps(new).encode())
    )

    result = source.poll(_state_for(source, tmp_path, old))

    (event,) = result.events
    kinds = {c.kind for c in event.spec_changes}
    assert SpecChangeKind.OPERATION_REMOVED in kinds
    assert any(c.subject == "PutObject" for c in event.spec_changes)


@respx.mock
def test_smithy_detects_a_removed_input_member(tmp_path: Path) -> None:
    def model(members: list[str]) -> dict:
        doc = _smithy({"PutObject": {"input": {"target": "com.amazonaws.s3#PutObjectRequest"}}})
        doc["shapes"]["com.amazonaws.s3#PutObjectRequest"] = {
            "type": "structure",
            "members": {m: {"target": "smithy.api#String"} for m in members},
        }
        return doc

    source = SmithySpecSource("aws-s3", {"url": f"{_BASE}/s3.json"}, _http(), tmp_path, 10_000_000)
    respx.get(f"{_BASE}/s3.json").mock(
        return_value=httpx.Response(200, content=json.dumps(model(["Bucket"])).encode())
    )

    result = source.poll(_state_for(source, tmp_path, model(["Bucket", "ACL"])))

    changes = result.events[0].spec_changes
    assert any(c.kind is SpecChangeKind.PARAM_REMOVED and c.before == "ACL" for c in changes)


def test_smithy_rejects_a_model_without_shapes(tmp_path: Path) -> None:
    source = SmithySpecSource("aws-s3", {"url": "http://x/s3.json"}, _http(), tmp_path, 1000)
    with pytest.raises(ValueError, match="shapes"):
        source.parse(b'{"smithy": "2.0"}')


# ── AsyncAPI ──────────────────────────────────────────────────────────────────


def _asyncapi(channels: list[str], *, operations: list[str] | None = None) -> dict:
    doc: dict = {
        "asyncapi": "2.6.0",
        "info": {"version": "1.0.0"},
        "channels": {c: {} for c in channels},
    }
    if operations is not None:
        doc["operations"] = {o: {} for o in operations}
    return doc


@respx.mock
def test_asyncapi_detects_a_removed_channel(tmp_path: Path) -> None:
    source = AsyncApiSpecSource("kafka", {"url": f"{_BASE}/a.yaml"}, _http(), tmp_path, 10_000_000)
    new = _asyncapi(["orders"])
    respx.get(f"{_BASE}/a.yaml").mock(
        return_value=httpx.Response(200, content=json.dumps(new).encode())
    )

    result = source.poll(_state_for(source, tmp_path, _asyncapi(["orders", "shipments"])))

    changes = result.events[0].spec_changes
    assert any(c.subject == "shipments" for c in changes)


@respx.mock
def test_asyncapi_detects_a_removed_v3_operation(tmp_path: Path) -> None:
    source = AsyncApiSpecSource("kafka", {"url": f"{_BASE}/a.yaml"}, _http(), tmp_path, 10_000_000)
    new = _asyncapi(["orders"], operations=["sendOrder"])
    respx.get(f"{_BASE}/a.yaml").mock(
        return_value=httpx.Response(200, content=json.dumps(new).encode())
    )

    result = source.poll(
        _state_for(source, tmp_path, _asyncapi(["orders"], operations=["sendOrder", "cancelOrder"]))
    )

    changes = result.events[0].spec_changes
    assert any(c.subject == "cancelOrder" for c in changes)


def test_asyncapi_rejects_a_non_asyncapi_document(tmp_path: Path) -> None:
    source = AsyncApiSpecSource("kafka", {"url": "http://x/a.yaml"}, _http(), tmp_path, 1000)
    with pytest.raises(ValueError, match="asyncapi"):
        source.parse(b"openapi: 3.0.0")


# ── Redis commands ────────────────────────────────────────────────────────────


@respx.mock
def test_redis_detects_a_removed_command(tmp_path: Path) -> None:
    source = RedisCommandsSource("redis", {"url": f"{_BASE}/c.json"}, _http(), tmp_path, 10_000_000)
    new_cmds = {"GET": {"arguments": [{}]}}
    old = {"GET": {"arguments": [{}]}, "SUBSTR": {"arguments": [{}]}}
    respx.get(f"{_BASE}/c.json").mock(
        return_value=httpx.Response(200, content=json.dumps(new_cmds).encode())
    )

    result = source.poll(_state_for(source, tmp_path, old))

    changes = result.events[0].spec_changes
    assert any(c.kind is SpecChangeKind.COMMAND_REMOVED and c.subject == "SUBSTR" for c in changes)


@respx.mock
def test_redis_detects_an_arity_change(tmp_path: Path) -> None:
    source = RedisCommandsSource("redis", {"url": f"{_BASE}/c.json"}, _http(), tmp_path, 10_000_000)
    new_cmds = {"SET": {"arguments": [{}, {}, {}]}}
    old = {"SET": {"arguments": [{}, {}]}}
    respx.get(f"{_BASE}/c.json").mock(
        return_value=httpx.Response(200, content=json.dumps(new_cmds).encode())
    )

    result = source.poll(_state_for(source, tmp_path, old))

    changes = result.events[0].spec_changes
    assert any(c.kind is SpecChangeKind.COMMAND_ARITY_CHANGED for c in changes)


def test_redis_rejects_an_empty_map(tmp_path: Path) -> None:
    source = RedisCommandsSource("redis", {"url": "http://x/c.json"}, _http(), tmp_path, 1000)
    with pytest.raises(ValueError, match="command map"):
        source.parse(b"{}")


# ── Baseline invariant (shared) ───────────────────────────────────────────────


@respx.mock
def test_first_sight_is_a_baseline_with_no_event(tmp_path: Path) -> None:
    source = RedisCommandsSource("redis", {"url": f"{_BASE}/c.json"}, _http(), tmp_path, 10_000_000)
    respx.get(f"{_BASE}/c.json").mock(
        return_value=httpx.Response(200, content=json.dumps({"GET": {}}).encode())
    )

    result = source.poll(FeedStateSnapshot(last_token=None))

    assert result.changed is True
    assert result.events == []
    assert "baseline" in (result.note or "")


# ── TS exports ────────────────────────────────────────────────────────────────


def test_names_in_dts_extracts_every_export_form() -> None:
    dts = """
    export declare function generateText(opts: unknown): Promise<string>;
    export declare class StreamText {}
    export interface Message { role: string; }
    export type Role = "user" | "assistant";
    export { convertToModelMessages, tool as toolHelper };
    export default class Client {}
    """
    names = _names_in_dts(dts)
    assert {
        "generateText",
        "StreamText",
        "Message",
        "Role",
        "convertToModelMessages",
        "toolHelper",
        "Client",
    } <= names


def _tgz_with_dts(dts: str) -> bytes:
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        data = dts.encode()
        info = tarfile.TarInfo(name="package/dist/index.d.ts")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return blob.getvalue()


def test_ts_exports_detects_a_removed_export(tmp_path: Path) -> None:
    old_dts = "export declare function foo(): void;\nexport declare function bar(): void;\n"
    new_dts = "export declare function foo(): void;\n"
    old_tgz = _tgz_with_dts(old_dts)
    new_tgz = _tgz_with_dts(new_dts)

    fake_meta_old = MagicMock()
    fake_meta_old.dist_tags = {"latest": "1.0.0"}

    fake_meta_new = MagicMock()
    fake_meta_new.dist_tags = {"latest": "2.0.0"}

    npm = MagicMock()
    npm.base_url = "https://registry.example"
    npm.get_package_metadata.return_value = fake_meta_new

    http = MagicMock()
    http.get.side_effect = [
        MagicMock(status_code=200, content=old_tgz),
        MagicMock(status_code=200, content=new_tgz),
    ]

    source = TsExportsSource("vercel-ai", {"package": "ai"}, http, npm)
    state = FeedStateSnapshot(last_token="1.0.0")

    result = source.poll(state)

    assert result.changed
    changes = result.events[0].spec_changes
    assert any(
        c.kind is SpecChangeKind.EXPORT_REMOVED and c.subject == "vercel-ai.bar" for c in changes
    )


def test_ts_exports_first_sight_compares_against_previous_major() -> None:
    versions = ["1.0.0", "1.5.2", "2.0.0-beta.1", "2.0.0", "2.1.0"]
    assert TsExportsSource._previous_major_release(versions, "2.1.0") == "1.5.2"
    assert TsExportsSource._previous_major_release(["1.0.0", "1.2.0"], "1.2.0") is None
