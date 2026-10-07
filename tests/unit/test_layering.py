"""Guard against a regression of the sources/providers import cycle.

``sources`` is the leaf layer. ``providers`` depends on ``sources`` (for
``SourceKind``), never the reverse — importing upward from ``sources`` into
``providers`` creates a circular import via ``sources/__init__.py``.
"""

from __future__ import annotations

from pathlib import Path


def test_sources_does_not_import_providers() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "depfix" / "sources"
    offenders = [
        p.name
        for p in root.glob("*.py")
        if "from depfix.providers" in p.read_text() or "import depfix.providers" in p.read_text()
    ]
    assert offenders == [], f"sources must not import providers: {offenders}"
