"""Best-effort Gemfile.lock refresh via `bundle lock`."""

from __future__ import annotations

import shutil
from pathlib import Path

from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES, run_sandboxed


def ruby_lockfile_paths(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("Gemfile.lock")
        if "vendor" not in path.parts and ".bundle" not in path.parts
    ]


def try_refresh_ruby_lockfile(
    root: Path,
    *,
    timeout: float = 120.0,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[bool, str]:
    if not (root / "Gemfile").is_file():
        return False, "no Gemfile to lock"
    if shutil.which("bundle") is None:
        return False, "bundler not on PATH; run `bundle lock` before merging"

    result = run_sandboxed(
        ["bundle", "lock", "--update", "--conservative"],
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )
    if result.ok:
        return True, "Gemfile.lock refreshed via `bundle lock --update --conservative`"
    message = result.error or result.stderr.strip() or f"exit {result.exit_code}"
    return False, f"Gemfile.lock refresh failed: {message[:200]}; run `bundle lock` before merging"
