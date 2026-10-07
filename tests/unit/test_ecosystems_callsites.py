"""Unit tests for the generic (non-JS) import-level call-site scanner.

``GenericImportScanner`` is the one-notch-simpler sibling of the
binding-aware JS scanner in ``scanners/callsites.py``: import regexes and
feed-derived symbols matched per line after comment masking. These tests
pin the confidence ladder (import/symbol), comment masking per ecosystem,
and the skip-dir and package-selection plumbing.
"""

from __future__ import annotations

from pathlib import Path

from depfix.ecosystems.callsites import GenericImportScanner, mask_comments
from depfix.ecosystems.registry import get
from depfix.ecosystems.specs import JAVA, PHP, PYTHON, RUBY
from depfix.scanners.models import CallSiteKind, MatchConfidence, ScanTarget


def _target(**overrides: object) -> ScanTarget:
    defaults: dict[str, object] = {"provider_id": "openai", "sdk_packages": ("openai",)}
    defaults.update(overrides)
    return ScanTarget(**defaults)


def _kinds(result: object, kind: CallSiteKind) -> list:
    return [site for site in result.call_sites if site.kind == kind]


# -- mask_comments -------------------------------------------------------------


def test_mask_comments_blanks_python_line_comment_preserving_length() -> None:
    source = "x = 1  # import openai\ny = 2\n"

    masked = mask_comments(source, PYTHON)

    assert len(masked) == len(source)
    assert "import openai" not in masked
    assert "x = 1" in masked and "y = 2" in masked


def test_mask_comments_blanks_c_style_block_comment_preserving_newlines() -> None:
    source = "a();\n/* import com.stripe.Foo;\nmore */\nb();"

    masked = mask_comments(source, JAVA)

    assert len(masked) == len(source)
    assert "com.stripe" not in masked
    assert masked.count("\n") == source.count("\n")
    assert "a();" in masked and "b();" in masked


def test_mask_comments_blanks_ruby_begin_end_block() -> None:
    source = "a\n=begin\nrequire 'stripe'\n=end\nb\n"

    masked = mask_comments(source, RUBY)

    assert "require 'stripe'" not in masked
    assert masked.count("\n") == source.count("\n")


def test_mask_comments_leaves_comment_marker_inside_a_string_alone() -> None:
    source = 'url = "https://api.openai.com"  # a real comment\n'

    masked = mask_comments(source, PYTHON)

    assert "https://api.openai.com" in masked
    assert "a real comment" not in masked


def test_mask_comments_is_a_noop_for_a_spec_without_comment_syntax() -> None:
    spec = get("javascript")
    assert spec is not None
    bare = spec.__class__(
        id="x",
        display_name="X",
        registry_aliases=("x",),
        source_extensions=frozenset({".x"}),
        markers=(),
    )

    assert mask_comments("# not a comment here\n", bare) == "# not a comment here\n"


# -- python imports ------------------------------------------------------------


def test_python_import_statement_is_high_confidence_sdk_import(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "import openai\n\nclient = openai.Client()\n", encoding="utf-8"
    )

    result = GenericImportScanner(PYTHON).scan(tmp_path, _target())

    imports = _kinds(result, CallSiteKind.SDK_IMPORT)
    assert len(imports) == 1
    assert imports[0].confidence == MatchConfidence.HIGH
    assert imports[0].symbol == "openai"
    assert imports[0].filepath == "app.py"
    assert imports[0].line_number == 1
    assert imports[0].provider_id == "openai"
    assert "Python import of openai" in imports[0].evidence


def test_python_from_import_is_an_sdk_import_hit(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("from openai import OpenAI\n", encoding="utf-8")

    result = GenericImportScanner(PYTHON).scan(tmp_path, _target())

    assert len(_kinds(result, CallSiteKind.SDK_IMPORT)) == 1


def test_python_commented_import_is_not_a_call_site(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("# import openai\nvalue = 1\n", encoding="utf-8")

    result = GenericImportScanner(PYTHON).scan(tmp_path, _target())

    assert result.files_scanned == 1
    assert result.call_sites == []


def test_python_import_of_a_different_package_does_not_match(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import openaix\nimport not_openai\n", encoding="utf-8")

    result = GenericImportScanner(PYTHON).scan(tmp_path, _target())

    assert result.call_sites == []


def test_sdk_import_captures_surrounding_context_lines(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "one\ntwo\nthree\nimport openai\nfive\nsix\n", encoding="utf-8"
    )

    result = GenericImportScanner(PYTHON).scan(tmp_path, _target())

    site = _kinds(result, CallSiteKind.SDK_IMPORT)[0]
    assert site.line_content == "import openai"
    assert site.context_before == ("one", "two", "three")
    assert site.context_after == ("five", "six")


# -- symbols and anchors -------------------------------------------------------


def test_symbol_match_is_a_medium_confidence_method_call(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "result = openai.ChatCompletion.create(model='gpt-4')\n", encoding="utf-8"
    )

    result = GenericImportScanner(PYTHON).scan(
        tmp_path, _target(symbols=("openai.ChatCompletion.create",))
    )

    methods = _kinds(result, CallSiteKind.METHOD_CALL)
    assert len(methods) == 1
    assert methods[0].confidence == MatchConfidence.MEDIUM
    assert methods[0].symbol == "openai.ChatCompletion.create"


def test_commented_symbol_is_masked_out(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("# openai.ChatCompletion.create()\n", encoding="utf-8")

    result = GenericImportScanner(PYTHON).scan(
        tmp_path,
        _target(symbols=("openai.ChatCompletion.create",)),
    )

    assert result.call_sites == []


# -- traversal -----------------------------------------------------------------


def test_scan_skips_vendored_and_virtualenv_directories(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import openai\n", encoding="utf-8")
    for skipped in (".venv", "node_modules", "__pycache__"):
        directory = tmp_path / skipped / "pkg"
        directory.mkdir(parents=True)
        (directory / "vendored.py").write_text("import openai\n", encoding="utf-8")

    result = GenericImportScanner(PYTHON).scan(tmp_path, _target())

    assert result.files_scanned == 1
    assert {site.filepath for site in result.call_sites} == {"app.py"}


def test_scan_ignores_files_outside_the_spec_source_extensions(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("import openai\n", encoding="utf-8")
    (tmp_path / "app.rb").write_text("import openai\n", encoding="utf-8")

    result = GenericImportScanner(PYTHON).scan(tmp_path, _target())

    assert result.files_scanned == 0
    assert result.call_sites == []


def test_scan_records_an_error_when_the_file_cap_is_reached(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"mod{index}.py").write_text("import openai\n", encoding="utf-8")

    result = GenericImportScanner(PYTHON, max_files=2).scan(tmp_path, _target())

    assert result.files_scanned == 2
    assert any("file cap reached" in error for error in result.errors)


def test_scan_skips_files_larger_than_the_byte_cap(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("import openai\n" + "x = 1\n" * 100, encoding="utf-8")

    result = GenericImportScanner(PYTHON, max_file_bytes=10).scan(tmp_path, _target())

    assert result.files_scanned == 0
    assert result.call_sites == []


def test_scan_finds_call_sites_in_nested_directories(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    (nested / "client.py").write_text("import openai\n", encoding="utf-8")

    result = GenericImportScanner(PYTHON).scan(tmp_path, _target())

    assert [site.filepath for site in result.call_sites] == ["src/pkg/client.py"]


# -- php: namespace guesses are MEDIUM confidence -------------------------------


def test_php_import_hit_is_medium_confidence_because_namespace_is_a_guess(
    tmp_path: Path,
) -> None:
    (tmp_path / "Client.php").write_text("<?php\nuse Stripe\\StripeClient;\n", encoding="utf-8")

    result = GenericImportScanner(PHP).scan(
        tmp_path, _target(provider_id="stripe", sdk_packages=("stripe/stripe-php",))
    )

    imports = _kinds(result, CallSiteKind.SDK_IMPORT)
    assert len(imports) == 1
    assert imports[0].confidence == MatchConfidence.MEDIUM
    assert imports[0].symbol == "stripe/stripe-php"


def test_php_hash_comment_masks_a_namespace_use(tmp_path: Path) -> None:
    (tmp_path / "Client.php").write_text(
        "<?php\n# use Stripe\\StripeClient;\n// new \\Stripe\\Foo();\n", encoding="utf-8"
    )

    result = GenericImportScanner(PHP).scan(
        tmp_path, _target(provider_id="stripe", sdk_packages=("stripe/stripe-php",))
    )

    assert result.call_sites == []


# -- package selection ---------------------------------------------------------


def test_scan_falls_back_to_sdk_packages_when_no_refs_are_present(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import openai\n", encoding="utf-8")

    # A plain ScanTarget carries no ``sdk_package_refs``; the scanner then
    # treats every listed package as belonging to the scanned ecosystem.
    target = ScanTarget(provider_id="openai", sdk_packages=("openai",))
    result = GenericImportScanner(PYTHON).scan(tmp_path, target)

    assert len(_kinds(result, CallSiteKind.SDK_IMPORT)) == 1


def test_scan_uses_only_refs_belonging_to_the_scanned_ecosystem(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import openai\nimport stripe\n", encoding="utf-8")

    target = ScanTarget(
        provider_id="openai",
        sdk_packages=("openai", "stripe"),
        sdk_package_refs=(("openai", "pypi"), ("stripe", "npm")),
    )
    result = GenericImportScanner(PYTHON).scan(tmp_path, target)

    imports = _kinds(result, CallSiteKind.SDK_IMPORT)
    assert [site.symbol for site in imports] == ["openai"]


def test_scan_finds_nothing_when_no_ref_matches_the_scanned_ecosystem(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import openai\n", encoding="utf-8")

    target = ScanTarget(
        provider_id="openai",
        sdk_packages=("openai",),
        sdk_package_refs=(("openai", "npm"),),
    )
    result = GenericImportScanner(PYTHON).scan(tmp_path, target)

    assert result.call_sites == []


def test_scan_without_import_regexes_reports_no_import_sites(tmp_path: Path) -> None:
    javascript = get("javascript")
    assert javascript is not None
    (tmp_path / "app.js").write_text('require("openai");\n', encoding="utf-8")

    # JavaScript deliberately has no generic import regexes -- the
    # binding-aware scanner handles it instead.
    result = GenericImportScanner(javascript).scan(tmp_path, _target())

    assert result.files_scanned == 1
    assert _kinds(result, CallSiteKind.SDK_IMPORT) == []
