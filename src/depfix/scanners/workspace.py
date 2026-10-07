"""Small workspace-root discovery seam for install and verification."""

from __future__ import annotations

import json
from pathlib import Path


def find_workspace_root(start: Path) -> Path:
    current = start.resolve()
    fallback = current
    while True:
        package = current / "package.json"
        if package.is_file():
            fallback = current
            try:
                if isinstance(json.loads(package.read_text()).get("workspaces"), (list, dict)):
                    return current
            except (OSError, json.JSONDecodeError):
                pass
        if any(
            (current / name).is_file() for name in ("pnpm-workspace.yaml", "nx.json", "turbo.json")
        ):
            return current
        if current.parent == current:
            return fallback
        current = current.parent


def owning_package_dir(root: Path, relpath: str) -> Path:
    """Find the workspace package whose test script owns a changed file."""
    root = root.resolve()
    candidate = (root / relpath).resolve().parent
    while candidate != root and root in candidate.parents:
        if (candidate / "package.json").is_file():
            return candidate
        candidate = candidate.parent
    return root
