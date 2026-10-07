"""Subset of a PyPI JSON response used by the watcher."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PypiPackageMetadata(BaseModel):
    """PyPI's latest pointer and the versions it exposes."""

    model_config = ConfigDict(extra="ignore")

    name: str
    latest: str
    versions: list[str]
