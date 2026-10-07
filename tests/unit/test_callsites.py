"""Unit tests for the binding-propagation call-site scanner.

The eval corpus (``evals/corpus/call-sites.yaml``) exercises this scanner
end-to-end against realistic fixtures; these tests target specific
behaviors (and regressions previously found and fixed) directly, with
small inline files rather than full fixture projects.
"""

from __future__ import annotations

from pathlib import Path

from depfix.scanners.callsites import CallSiteScanner, mask_comments
from depfix.scanners.models import CallSiteKind, MatchConfidence, ScanTarget


def _target(**overrides: object) -> ScanTarget:
    defaults: dict[str, object] = {"provider_id": "openai", "sdk_packages": ("openai",)}
    defaults.update(overrides)
    return ScanTarget(**defaults)


# -- mask_comments -----------------------------------------------------------


def test_mask_comments_blanks_line_comment_but_preserves_length() -> None:
    source = "const x = 1; // openai.foo()\nconst y = 2;"
    masked = mask_comments(source)

    assert len(masked) == len(source)
    assert "openai.foo" not in masked
    assert "const y = 2;" in masked


def test_mask_comments_blanks_block_comment_preserving_newlines() -> None:
    source = "a();\n/* openai.bar()\nmore */\nb();"
    masked = mask_comments(source)

    assert len(masked) == len(source)
    assert "openai.bar" not in masked
    assert masked.count("\n") == source.count("\n")
    assert "a();" in masked and "b();" in masked


def test_mask_comments_preserves_string_contents() -> None:
    source = 'const spec = "./lib/openai.js"; // not a comment marker inside the string above'
    masked = mask_comments(source)

    assert './lib/openai.js"' in masked
    assert "not a comment marker" not in masked


# -- direct import + method call ----------------------------------------------


def test_direct_import_and_method_call_is_high_confidence(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text(
        'const OpenAI = require("openai");\n'
        "const client = new OpenAI();\n"
        'client.chat.completions.create({ model: "gpt-4" });\n',
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(tmp_path, _target())

    method_sites = [s for s in result.call_sites if s.kind == CallSiteKind.METHOD_CALL]
    assert len(method_sites) == 1
    assert method_sites[0].symbol == "openai.chat.completions.create"
    assert method_sites[0].confidence == MatchConfidence.HIGH


def test_destructured_bare_function_is_bare_symbol_kind(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text(
        'const { createModeration } = require("openai");\ncreateModeration({ input: "x" });\n',
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(tmp_path, _target())

    bare_sites = [s for s in result.call_sites if s.kind == CallSiteKind.BARE_SYMBOL]
    assert len(bare_sites) == 1
    assert bare_sites[0].symbol == "openai.createModeration"
    assert bare_sites[0].confidence == MatchConfidence.HIGH


def test_symbol_prefix_narrows_to_methods_below_an_old_api_namespace(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text(
        'const OpenAI = require("openai");\n'
        "const client = new OpenAI();\n"
        "client.Completion.create({});\n"
        "client.chat.completions.create({});\n",
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(tmp_path, _target(symbols=("openai.Completion",)))

    assert [site.symbol for site in result.call_sites] == ["openai.Completion.create"]


# -- wrapper propagation across hops -------------------------------------------


def test_one_hop_wrapper_reexport_is_found_at_full_confidence(tmp_path: Path) -> None:
    (tmp_path / "lib.js").write_text(
        'const OpenAI = require("openai");\n'
        "const client = new OpenAI();\n"
        "module.exports = client;\n",
        encoding="utf-8",
    )
    (tmp_path / "index.js").write_text(
        "const client = require(\"./lib\");\nclient.moderations.create({ input: 'x' });\n",
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(tmp_path, _target())

    method_sites = [s for s in result.call_sites if s.kind == CallSiteKind.METHOD_CALL]
    assert len(method_sites) == 1
    assert method_sites[0].confidence == MatchConfidence.HIGH
    assert method_sites[0].is_actionable
    assert "1 hop(s)" in method_sites[0].evidence


def test_two_hop_wrapper_reexport_is_still_found_and_actionable(tmp_path: Path) -> None:
    (tmp_path / "raw.js").write_text(
        'const OpenAI = require("openai");\n'
        "const client = new OpenAI();\n"
        "module.exports = client;\n",
        encoding="utf-8",
    )
    (tmp_path / "lib.js").write_text(
        'const client = require("./raw");\nmodule.exports = client;\n', encoding="utf-8"
    )
    (tmp_path / "index.js").write_text(
        'const client = require("./lib");\nclient.moderations.create({});\n', encoding="utf-8"
    )

    result = CallSiteScanner().scan(tmp_path, _target())

    method_sites = [s for s in result.call_sites if s.kind == CallSiteKind.METHOD_CALL]
    assert len(method_sites) == 1
    assert method_sites[0].confidence == MatchConfidence.HIGH
    assert method_sites[0].is_actionable


def test_module_exports_object_literal_shorthand_propagates(tmp_path: Path) -> None:
    """Regression: ``module.exports = { openai }`` (object-literal form) must
    resolve the same as a bare-identifier ``module.exports = openai``."""
    (tmp_path / "client.js").write_text(
        'const { OpenAIApi } = require("openai");\n'
        "const openai = new OpenAIApi();\n"
        "module.exports = { openai };\n",
        encoding="utf-8",
    )
    (tmp_path / "index.js").write_text(
        'const { openai } = require("./client");\nopenai.createChatCompletion({});\n',
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(tmp_path, _target())

    method_sites = [s for s in result.call_sites if s.kind == CallSiteKind.METHOD_CALL]
    assert len(method_sites) == 1
    assert method_sites[0].symbol == "openai.createChatCompletion"


def test_declare_and_export_in_one_statement_is_recognized(tmp_path: Path) -> None:
    """Regression: ``export const client = new OpenAI(...)`` must bind
    ``client`` to the construction despite the leading ``export`` keyword."""
    (tmp_path / "client.ts").write_text(
        'import OpenAI from "openai";\nexport const client = new OpenAI();\n',
        encoding="utf-8",
    )
    (tmp_path / "index.ts").write_text(
        'import { client } from "./client";\nclient.moderations.create({});\n',
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(tmp_path, _target())

    method_sites = [s for s in result.call_sites if s.kind == CallSiteKind.METHOD_CALL]
    assert len(method_sites) == 1
    assert method_sites[0].symbol == "openai.moderations.create"


def test_destructured_class_is_still_constructible(tmp_path: Path) -> None:
    """Regression: a destructured class import is bound bare_symbol=True,
    but ``new X()`` is unambiguous evidence of construction regardless."""
    (tmp_path / "index.js").write_text(
        'const { OpenAIApi } = require("openai");\n'
        "const client = new OpenAIApi();\n"
        "client.createEmbedding({});\n",
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(tmp_path, _target())

    method_sites = [s for s in result.call_sites if s.kind == CallSiteKind.METHOD_CALL]
    assert len(method_sites) == 1
    assert method_sites[0].symbol == "openai.createEmbedding"


# -- root path resolution -------------------------------------------------------


def test_relative_root_path_still_propagates_across_files(tmp_path: Path, monkeypatch) -> None:
    """Regression: an unresolved relative ``root`` must not break cross-file
    propagation (relative-import targets are resolved absolute internally)."""
    (tmp_path / "lib.js").write_text(
        'const OpenAI = require("openai");\nmodule.exports = new OpenAI();\n', encoding="utf-8"
    )
    (tmp_path / "index.js").write_text(
        'const client = require("./lib");\nclient.moderations.create({});\n', encoding="utf-8"
    )

    monkeypatch.chdir(tmp_path.parent)
    relative_root = Path(tmp_path.name)

    result = CallSiteScanner().scan(relative_root, _target())

    method_sites = [s for s in result.call_sites if s.kind == CallSiteKind.METHOD_CALL]
    assert len(method_sites) == 1


# -- API version pin precision --------------------------------------------------


def test_dated_api_version_literal_is_matched(tmp_path: Path) -> None:
    (tmp_path / "client.js").write_text(
        'import Stripe from "stripe";\nconst opts = { apiVersion: "2020-08-27" };\n',
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(
        tmp_path, _target(provider_id="stripe", sdk_packages=("stripe",))
    )

    pins = [s for s in result.call_sites if s.kind == CallSiteKind.API_VERSION_PIN]
    assert len(pins) == 1


def test_kubernetes_style_apiversion_field_is_not_matched(tmp_path: Path) -> None:
    """Regression: ``apiVersion: "apps/v1"`` (Kubernetes) is not a
    Stripe/OpenAI version pin and must not be reported."""
    (tmp_path / "deployment.js").write_text(
        'module.exports = { apiVersion: "apps/v1", kind: "Deployment" };\n', encoding="utf-8"
    )

    result = CallSiteScanner().scan(
        tmp_path, _target(provider_id="stripe", sdk_packages=("stripe",))
    )

    pins = [s for s in result.call_sites if s.kind == CallSiteKind.API_VERSION_PIN]
    assert pins == []


def test_stripe_version_header_matches_even_without_dated_literal(tmp_path: Path) -> None:
    # A raw HTTP header line built into a template literal -- the unquoted
    # key immediately followed by `:` and a quoted value is what the
    # `_VERSION_PIN_RE` pattern is shaped to catch.
    (tmp_path / "client.js").write_text(
        'const headers = `Stripe-Version: "custom-pin"`;\n', encoding="utf-8"
    )

    result = CallSiteScanner().scan(
        tmp_path, _target(provider_id="stripe", sdk_packages=("stripe",))
    )

    pins = [s for s in result.call_sites if s.kind == CallSiteKind.API_VERSION_PIN]
    assert len(pins) == 1


# -- symbol narrowing ------------------------------------------------------------


def test_symbols_narrowing_suppresses_other_call_sites(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text(
        'const OpenAI = require("openai");\n'
        "const client = new OpenAI();\n"
        "client.moderations.create({});\n"
        "client.chat.completions.create({});\n",
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(tmp_path, _target(symbols=("openai.moderations.create",)))

    method_sites = [s for s in result.call_sites if s.kind == CallSiteKind.METHOD_CALL]
    assert len(method_sites) == 1
    assert method_sites[0].symbol == "openai.moderations.create"


# -- unrelated code produces no false positives ---------------------------------


def test_unrelated_variable_named_openai_produces_no_call_sites(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text(
        "class LocalClient {\n  createModeration(x) { return x; }\n}\n"
        "const openai = new LocalClient();\n"
        "module.exports = { openai };\n",
        encoding="utf-8",
    )

    result = CallSiteScanner().scan(tmp_path, _target())

    assert result.call_sites == []
