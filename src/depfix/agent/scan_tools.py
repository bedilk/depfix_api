"""Read-only, bounded toolset the scan agent uses.

Extends the FixToolset pattern (agent/tools.py) with feed-polling and
manifest-parsing helpers. Every tool:

- reads, never writes,
- caps output size,
- refuses paths outside the checkout root (for repo tools),
- refuses URLs outside the registered registry/host allowlist (for feed
  tools),
- logs its call for the transcript.

The agent never touches an httpx.Client or subprocess directly — it goes
through this facade so revoking a capability later is a one-file change.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from depfix.registry.client import (
    NpmRegistryClient,
    PackageNotFoundError,
    RegistryUnavailableError,
)
from depfix.registry.pypi_client import (
    PypiPackageNotFoundError,
    PypiRegistryClient,
    PypiUnavailableError,
)
from depfix.scanners.manifest import scan_manifests
from depfix.sources.http import github_headers

logger = logging.getLogger(__name__)

_MAX_TOOL_OUTPUT = 24_000
_MAX_FILE_BYTES = 1_000_000
_MAX_LIST_ENTRIES = 200
_MAX_URL_BYTES = 500_000

_ALLOWED_HOSTS = frozenset(
    {
        "registry.npmjs.org",
        "pypi.org",
        "api.github.com",
        "raw.githubusercontent.com",
    }
)


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(ok=False, content=message)

    def truncate(self) -> ToolResult:
        if len(self.content) <= _MAX_TOOL_OUTPUT:
            return self
        return ToolResult(
            ok=self.ok,
            content=self.content[:_MAX_TOOL_OUTPUT]
            + f"\n… [truncated at {_MAX_TOOL_OUTPUT} bytes]",
        )


class ScanToolset:
    """Bound to one (repo checkout, HTTP client) for one agent run."""

    def __init__(
        self,
        *,
        checkout_root: Path | None,
        http: httpx.Client | None = None,
        npm: NpmRegistryClient | None = None,
        pypi: PypiRegistryClient | None = None,
        github_api_url: str = "https://api.github.com",
    ) -> None:
        self._checkout_root = checkout_root.resolve() if checkout_root else None
        self._http = http
        self._npm = npm
        self._pypi = pypi
        self._github_api_url = github_api_url.rstrip("/")
        self.call_log: list[tuple[str, str]] = []
        self.files_read: set[str] = set()
        # Agent-discovered old-API call sites, staged in-memory until the
        # orchestrator persists them. verify_line is the hallucination gate.
        self.discovered_anchors: list[dict] = []

    # -- repo tools -----------------------------------------------------------

    @property
    def checkout_root(self) -> Path | None:
        return self._checkout_root

    def _resolve(self, relpath: str) -> Path | None:
        if self._checkout_root is None:
            return None
        candidate = (self._checkout_root / relpath).resolve()
        if not candidate.is_relative_to(self._checkout_root):
            logger.warning("scan agent path escapes checkout: %r", relpath)
            return None
        return candidate

    def read_file(self, relpath: str) -> ToolResult:
        self.call_log.append(("read_file", relpath))
        path = self._resolve(relpath)
        if path is None:
            return ToolResult.error(f"{relpath!r} is outside the repository")
        if not path.is_file():
            return ToolResult.error(f"{relpath!r} does not exist")
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ToolResult.error(f"{relpath!r} is larger than {_MAX_FILE_BYTES} bytes")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult.error(f"could not read {relpath!r}: {exc}")
        self.files_read.add(relpath)
        return ToolResult(ok=True, content=text).truncate()

    def list_dir(self, relpath: str, *, depth: int = 1) -> ToolResult:
        self.call_log.append(("list_dir", relpath))
        base = self._resolve(relpath) if relpath else self._checkout_root
        if base is None or not base.is_dir():
            return ToolResult.error(f"{relpath!r} is not a directory")
        entries: list[str] = []
        for path in sorted(base.rglob("*") if depth > 1 else base.iterdir()):
            if len(entries) >= _MAX_LIST_ENTRIES:
                entries.append("… [truncated]")
                break
            rel = path.relative_to(self._checkout_root).as_posix()  # type: ignore[arg-type]
            entries.append(f"{'D' if path.is_dir() else 'F'} {rel}")
        return ToolResult(ok=True, content="\n".join(entries)).truncate()

    def grep(self, *, pattern: str, relpath: str = "", max_results: int = 50) -> ToolResult:
        self.call_log.append(("grep", f"{pattern}@{relpath}"))
        base = self._resolve(relpath) if relpath else self._checkout_root
        if base is None:
            return ToolResult.error("no checkout available")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult.error(f"invalid regex: {exc}")
        hits: list[str] = []
        paths = base.rglob("*") if base.is_dir() else [base]
        for path in paths:
            if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
                continue
            if any(part in {"node_modules", ".git", "dist", "build"} for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = path.relative_to(self._checkout_root).as_posix()  # type: ignore[arg-type]
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        return ToolResult(ok=True, content="\n".join(hits)).truncate()
        return ToolResult(ok=True, content="\n".join(hits) or "(no matches)").truncate()

    def find_files(self, *, pattern: str, max_results: int = 50) -> ToolResult:
        self.call_log.append(("find_files", pattern))
        if self._checkout_root is None:
            return ToolResult.error("no checkout available")
        matches: list[str] = []
        for path in sorted(self._checkout_root.rglob(pattern)):
            if any(part in {"node_modules", ".git"} for part in path.parts):
                continue
            matches.append(path.relative_to(self._checkout_root).as_posix())
            if len(matches) >= max_results:
                break
        return ToolResult(ok=True, content="\n".join(matches) or "(no matches)").truncate()

    def read_manifest(self, relpath: str) -> ToolResult:
        self.call_log.append(("read_manifest", relpath))
        return self.read_file(relpath)

    def resolve_installed_version(self, package: str) -> ToolResult:
        """Read the exact installed version from the nearest lockfile."""
        self.call_log.append(("resolve_installed_version", package))
        if self._checkout_root is None:
            return ToolResult.error("no checkout available")
        scan = scan_manifests(self._checkout_root, [package])
        matches = [m for m in scan.matches if m.package == package]
        if not matches:
            return ToolResult(ok=True, content=f"{package}: not declared in any manifest")
        rows = [
            f"{m.package}@{m.resolved_version or m.declared_range or '?'} "
            f"({m.source}, {m.manifest_path})"
            for m in matches
        ]
        return ToolResult(ok=True, content="\n".join(rows))

    def verify_line(self, relpath: str, line_number: int, expected_content: str) -> bool:
        """Confirm the agent's claimed line actually exists and matches."""
        if self._checkout_root is None:
            return False
        path = self._resolve(relpath)
        if path is None or not path.is_file():
            return False
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return False
        if line_number < 1 or line_number > len(lines):
            return False
        # Substring match — the agent may report a stripped or reformatted
        # line, and we only need to know the claim isn't fabricated.
        expected_snippet = expected_content.strip()[:80]
        return not expected_snippet or expected_snippet in lines[line_number - 1]

    def commit_sha(self) -> str:
        if self._checkout_root is None:
            return ""
        try:
            result = subprocess.run(  # nosec B607 — argv is a fixed ["git", ...] list with no user-controlled elements
                ["git", "-C", str(self._checkout_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def record_call_site_anchor(
        self,
        *,
        symbol: str,
        filepath: str,
        line_number: int,
        line_content: str,
    ) -> ToolResult:
        """Verify the line exists, then stage it as a discovered old-API call
        site. This is the agent's ONLY write; everything else stays read-only.
        The orchestrator flushes ``discovered_anchors`` to disk after the run."""
        self.call_log.append(("record_call_site_anchor", f"{symbol}@{filepath}:{line_number}"))
        if not symbol or not filepath or line_number <= 0:
            return ToolResult.error("symbol, filepath, and a positive line_number are required")
        if not self.verify_line(filepath, line_number, line_content):
            return ToolResult.error(
                f"{filepath}:{line_number} does not match the claimed content; not recorded"
            )
        self.discovered_anchors.append(
            {
                "symbol": symbol,
                "filepath": filepath,
                "line_number": line_number,
                "line_content": line_content,
            }
        )
        return ToolResult(ok=True, content=f"anchor recorded: {symbol} at {filepath}:{line_number}")

    # -- feed tools -----------------------------------------------------------

    def poll_npm(self, *, package: str, dist_tag: str = "latest") -> ToolResult:
        self.call_log.append(("poll_npm", f"{package}:{dist_tag}"))
        if self._npm is None:
            return ToolResult.error("npm client not available")
        try:
            metadata = self._npm.get_package_metadata(package)
        except PackageNotFoundError:
            return ToolResult.error(f"npm package not found: {package}")
        except RegistryUnavailableError as exc:
            return ToolResult.error(f"npm registry unavailable: {exc}")
        version = metadata.dist_tags.get(dist_tag, "")
        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "package": package,
                    "dist_tag": dist_tag,
                    "version": version,
                    "all_versions": metadata.versions[-20:],
                    "source_url": f"https://www.npmjs.com/package/{package}/v/{version}",
                },
                indent=2,
            ),
        )

    def poll_pypi(self, *, package: str) -> ToolResult:
        self.call_log.append(("poll_pypi", package))
        if self._pypi is None:
            return ToolResult.error("pypi client not available")
        try:
            metadata = self._pypi.get_package_metadata(package)
        except PypiPackageNotFoundError:
            return ToolResult.error(f"PyPI project not found: {package}")
        except PypiUnavailableError as exc:
            return ToolResult.error(f"PyPI unavailable: {exc}")
        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "package": package,
                    "latest": metadata.latest,
                    "recent_versions": metadata.versions[-20:],
                    "source_url": f"https://pypi.org/project/{package}/{metadata.latest}/",
                },
                indent=2,
            ),
        )

    def poll_github_releases(self, *, repo: str) -> ToolResult:
        self.call_log.append(("poll_github_releases", repo))
        if self._http is None:
            return ToolResult.error("http client not available")
        url = f"{self._github_api_url}/repos/{repo}/releases"
        try:
            response = self._http.get(url, params={"per_page": 5}, headers=github_headers())
        except httpx.HTTPError as exc:
            return ToolResult.error(f"GitHub API error: {exc}")
        if response.status_code >= 400:
            return ToolResult.error(f"GitHub returned {response.status_code}")
        raw = response.json()
        if not isinstance(raw, list):
            return ToolResult.error("unexpected GitHub payload")
        summary = [
            {
                "tag_name": r.get("tag_name"),
                "name": r.get("name"),
                "prerelease": r.get("prerelease"),
                "html_url": r.get("html_url"),
                "body": (r.get("body") or "")[:2000],
                "published_at": r.get("published_at"),
            }
            for r in raw[:5]
            if isinstance(r, dict)
        ]
        return ToolResult(ok=True, content=json.dumps(summary, indent=2)).truncate()

    def fetch_openapi_spec(self, *, url: str) -> ToolResult:
        self.call_log.append(("fetch_openapi_spec", url))
        if not _host_allowed(url):
            return ToolResult.error(f"host not allowed: {url}")
        if self._http is None:
            return ToolResult.error("http client not available")
        try:
            response = self._http.get(url)
        except httpx.HTTPError as exc:
            return ToolResult.error(f"fetch failed: {exc}")
        if response.status_code >= 400:
            return ToolResult.error(f"HTTP {response.status_code}")
        import hashlib

        body = response.content[:_MAX_URL_BYTES]
        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "url": url,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body),
                    "preview": body[:2000].decode("utf-8", errors="replace"),
                },
                indent=2,
            ),
        )

    def fetch_url(self, *, url: str, max_bytes: int = 200_000) -> ToolResult:
        self.call_log.append(("fetch_url", url))
        if not _host_allowed(url):
            return ToolResult.error(f"host not allowed: {url}")
        if self._http is None:
            return ToolResult.error("http client not available")
        try:
            response = self._http.get(url)
        except httpx.HTTPError as exc:
            return ToolResult.error(f"fetch failed: {exc}")
        if response.status_code >= 400:
            return ToolResult.error(f"HTTP {response.status_code}")
        body = response.content[: min(max_bytes, _MAX_URL_BYTES)]
        return ToolResult(ok=True, content=body.decode("utf-8", errors="replace")).truncate()


def _host_allowed(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_HOSTS)
