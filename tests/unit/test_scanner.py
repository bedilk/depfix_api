"""Unit tests for the codebase scanner."""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.scanners.base import CodebaseScanner, build_pattern_for_api


def test_build_pattern_for_api_lodash_pluck() -> None:
    patterns = build_pattern_for_api("_.pluck(collection, propertyName)", "lodash")
    assert any("pluck" in p for p in patterns)
    # Should contain _.pluck-style pattern
    assert any(p.startswith(r"_\.") for p in patterns)


def test_build_pattern_for_api_openai_style() -> None:
    patterns = build_pattern_for_api("openai.createChatCompletion(params)", "openai")
    assert any("createChatCompletion" in p for p in patterns)
    # Package-qualified pattern should fire even without a `_.`-style prefix.
    assert any(p.startswith(r"openai\.") for p in patterns)


def test_scanner_finds_chat_completion_usage(sample_codebase: Path) -> None:
    scanner = CodebaseScanner(context_lines=2)
    patterns = build_pattern_for_api("openai.createChatCompletion(params)", "openai")
    file_usages = scanner.scan_codebase(str(sample_codebase), patterns)

    assert len(file_usages) > 0, "Expected at least one file with createChatCompletion usage"
    all_usage_texts = [u.match_text for fu in file_usages for u in fu.usages]
    assert any("createChatCompletion" in t for t in all_usage_texts)


def test_scanner_skips_node_modules(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / "lodash").mkdir(parents=True)
    (tmp_path / "node_modules" / "lodash" / "index.js").write_text("_.pluck(x, 'a');")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text("_.pluck(users, 'name');")

    scanner = CodebaseScanner()
    file_usages = scanner.scan_codebase(str(tmp_path), [r"_\.pluck\s*\("])

    # Only the src/app.js should match; node_modules is skipped.
    assert len(file_usages) == 1
    assert "src" in file_usages[0].filepath
    assert "node_modules" not in file_usages[0].filepath


def test_scanner_raises_on_missing_path(tmp_path: Path) -> None:
    scanner = CodebaseScanner()
    with pytest.raises(FileNotFoundError):
        scanner.scan_codebase(str(tmp_path / "does-not-exist"), [r"x"])


def test_scanner_captures_context(tmp_path: Path) -> None:
    src = tmp_path / "a.js"
    src.write_text("a\nb\n_.pluck(users, 'name');\nc\nd\n")

    scanner = CodebaseScanner(context_lines=1)
    file_usages = scanner.scan_codebase(str(tmp_path), [r"_\.pluck\s*\("])

    assert len(file_usages) == 1
    usage = file_usages[0].usages[0]
    assert usage.line_number == 3
    assert usage.context_before == ["b"]
    assert usage.context_after == ["c"]
