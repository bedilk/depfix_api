"""Checkout-scoped, read-only tools for the opt-in fix agent."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from depfix.clone import Checkout
from depfix.redaction import redact_text
from depfix.scanners import CallSiteScanner
from depfix.scanners.models import ScanTarget

logger = logging.getLogger(__name__)

_MAX_TOOL_OUTPUT_BYTES = 16_000


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(ok=False, content=message)


class FixToolset:
    """Read-only tools bound to precisely one disposable checkout."""

    def __init__(
        self, checkout: Checkout, *, scanner: CallSiteScanner, max_file_bytes: int
    ) -> None:
        self._root = checkout.path.resolve()
        self._scanner = scanner
        self._max_file_bytes = max_file_bytes
        self.call_log: list[tuple[str, str]] = []

    def _resolve(self, relpath: str) -> Path | None:
        candidate = (self._root / relpath).resolve()
        if not candidate.is_relative_to(self._root):
            logger.warning("agent tool path escapes checkout: %r", relpath)
            return None
        return candidate

    def read_file(self, relpath: str) -> ToolResult:
        self.call_log.append(("read_file", relpath))
        path = self._resolve(relpath)
        if path is None:
            return ToolResult.error(f"path {relpath!r} is outside the repository")
        if not path.is_file() or path.stat().st_size > self._max_file_bytes:
            return ToolResult.error(f"{relpath!r} does not exist or is too large to read")
        # Redact before truncating so a secret can never be cut in half and slip past detection.
        text = redact_text(path.read_text(encoding="utf-8", errors="replace"))
        if len(text.encode()) > _MAX_TOOL_OUTPUT_BYTES:
            text = text.encode()[:_MAX_TOOL_OUTPUT_BYTES].decode("utf-8", errors="ignore")
            text += "\n… [truncated]"
        return ToolResult(ok=True, content=text)

    def find_usages(self, symbol: str) -> ToolResult:
        self.call_log.append(("find_usages", symbol))
        result = self._scanner.scan(self._root, ScanTarget(provider_id="", symbols=(symbol,)))
        if not result.call_sites:
            return ToolResult(ok=True, content=f"no usages of {symbol!r} found")
        lines = [
            f"{site.filepath}:{site.line_number}: {redact_text(site.line_content.strip())}"
            for site in result.call_sites[:50]
        ]
        return ToolResult(ok=True, content="\n".join(lines))


def tool_specs() -> list[dict[str, object]]:
    """The exact, deliberately small tool schema exposed to the model."""
    return [
        {
            "name": "read_file",
            "description": "Read a repository-relative source file for context.",
            "parameters": {"relpath": "string"},
        },
        {
            "name": "find_usages",
            "description": "Find call sites of a symbol in this checkout.",
            "parameters": {"symbol": "string"},
        },
    ]
