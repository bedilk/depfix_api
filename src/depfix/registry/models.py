"""Pydantic models for npm registry responses."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PackageMetadata(BaseModel):
    """Subset of the npm registry document we care about.

    Populated from the abbreviated response
    (``Accept: application/vnd.npm.install-v1+json``). Only ``name``,
    ``dist-tags`` and the list of published versions are load-bearing for
    Day 3-4; Week 2's changelog fetcher will extend this.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str
    dist_tags: dict[str, str] = Field(alias="dist-tags")
    versions: list[str]

    @property
    def latest(self) -> str:
        """Convenience accessor for the canonical ``latest`` tag."""
        return self.dist_tags["latest"]
