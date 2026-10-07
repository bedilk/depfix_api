"""The per-scan file ceiling is one number, enforced on both sides."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from depfix.config import MAX_SCANNABLE_FILES, Settings
from depfix.ecosystems.callsites import GenericImportScanner
from depfix.ecosystems.specs import PYTHON
from depfix.scanners.callsites import CallSiteScanner
from depfix.scanners.limits import clamp_max_files


def test_ceiling_is_fifteen_thousand() -> None:
    assert MAX_SCANNABLE_FILES == 15_000


def test_settings_rejects_a_configured_value_above_the_ceiling() -> None:
    with pytest.raises(ValidationError):
        Settings(scan_max_files=MAX_SCANNABLE_FILES + 1)


def test_settings_default_is_the_ceiling() -> None:
    assert Settings().scan_max_files == MAX_SCANNABLE_FILES


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, MAX_SCANNABLE_FILES),
        (0, 1),
        (-5, 1),
        (10, 10),
        (MAX_SCANNABLE_FILES, MAX_SCANNABLE_FILES),
        (MAX_SCANNABLE_FILES + 1, MAX_SCANNABLE_FILES),
        (10_000_000, MAX_SCANNABLE_FILES),
    ],
)
def test_clamp_max_files(requested: int | None, expected: int) -> None:
    assert clamp_max_files(requested) == expected


def test_js_scanner_clamps_an_oversized_programmatic_cap() -> None:
    scanner = CallSiteScanner(max_files=10_000_000)

    assert scanner.max_files == MAX_SCANNABLE_FILES


def test_js_scanner_resolves_none_to_the_ceiling_not_unlimited() -> None:
    assert CallSiteScanner(max_files=None).max_files == MAX_SCANNABLE_FILES


def test_generic_scanner_clamps_an_oversized_programmatic_cap() -> None:
    scanner = GenericImportScanner(PYTHON, max_files=99_999)

    assert scanner._max_files == MAX_SCANNABLE_FILES


def test_a_small_explicit_cap_is_still_honoured(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"mod{index}.py").write_text("import openai\n", encoding="utf-8")
    from depfix.scanners.models import ScanTarget

    result = GenericImportScanner(PYTHON, max_files=2).scan(
        tmp_path, ScanTarget(provider_id="openai", sdk_packages=("openai",))
    )

    assert result.files_scanned == 2
