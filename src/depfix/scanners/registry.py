"""Language scanner registry.

Every ecosystem registers a factory. Adding a language is one file plus
one registration line. Detection is by file extension; a monorepo may
register multiple languages and all scanners run on the same checkout.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from depfix.scanners.language import LanguageScanResult

logger = logging.getLogger(__name__)

_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        "vendor",
        "venv",
        ".venv",
        "__pycache__",
        "target",
        "build",
        "dist",
        ".eggs",
        "site-packages",
    }
)


class LanguageScanner(Protocol):
    def scan_repo(self, repo_root: Path) -> LanguageScanResult: ...


ScannerFactory = Callable[[list[str]], LanguageScanner]


class ScannerRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ScannerFactory] = {}
        self._ext_map: dict[str, str] = {}  # ext → language

    def register(
        self,
        language: str,
        factory: ScannerFactory,
        extensions: tuple[str, ...],
    ) -> None:
        self._factories[language] = factory
        for ext in extensions:
            self._ext_map[ext] = language

    def detect_languages(self, root: Path) -> list[str]:
        """Which languages have source files in this checkout."""
        found: set[str] = set()
        for path in root.rglob("*"):
            if any(p in _SKIP_DIRS for p in path.parts):
                continue
            if not path.is_file():
                continue
            lang = self._ext_map.get(path.suffix)
            if lang:
                found.add(lang)
        return sorted(found)

    def build(self, language: str, packages: list[str]) -> LanguageScanner:
        factory = self._factories.get(language)
        if factory is None:
            raise KeyError(f"no scanner registered for language: {language}")
        return factory(packages)

    def has(self, language: str) -> bool:
        return language in self._factories


# ---------------------------------------------------------------------------
# Global registry
# ---------------------------------------------------------------------------

scanner_registry = ScannerRegistry()


def _register_builtins() -> None:
    from depfix.scanners.languages.elixir import ElixirScanner
    from depfix.scanners.languages.go import GoScanner
    from depfix.scanners.languages.java import JavaScanner
    from depfix.scanners.languages.kotlin import KotlinScanner
    from depfix.scanners.languages.php import PhpScanner
    from depfix.scanners.languages.python import PythonScanner
    from depfix.scanners.languages.ruby import RubyScanner

    scanner_registry.register(
        "python",
        lambda pkgs: PythonScanner(pkgs),
        (".py", ".pyi"),
    )
    scanner_registry.register(
        "go",
        lambda pkgs: GoScanner(pkgs),
        (".go",),
    )
    # Java gets .java only; Kotlin gets .kt/.kts with Kotlin-specific patterns
    scanner_registry.register(
        "java",
        lambda pkgs: JavaScanner(pkgs),
        (".java",),
    )
    scanner_registry.register(
        "kotlin",
        lambda pkgs: KotlinScanner(pkgs),
        (".kt", ".kts"),
    )
    scanner_registry.register(
        "ruby",
        lambda pkgs: RubyScanner(pkgs),
        (".rb", ".rake", ".ru"),
    )
    scanner_registry.register(
        "elixir",
        lambda pkgs: ElixirScanner(pkgs),
        (".ex", ".exs"),
    )
    scanner_registry.register(
        "php",
        lambda pkgs: PhpScanner(pkgs),
        (".php", ".phtml"),
    )


_register_builtins()
