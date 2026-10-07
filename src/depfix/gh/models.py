"""Value objects for GitHub App authentication.

Two-tier credential model: an App JWT (signed locally with the App's
private key, short-lived, valid only against ``/app/*`` endpoints) mints an
Installation Access Token (scoped to specific repositories and permissions
at mint time, ~1hr TTL). Only the installation token is ever used against
repo-content endpoints -- see ``app.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class Installation:
    """One GitHub App installation (an org or user that installed the app)."""

    id: int
    account_login: str
    account_type: str  # "Organization" | "User"
    repository_selection: str  # "all" | "selected"


@dataclass(frozen=True)
class Repository:
    """A repo visible to a given installation."""

    id: int
    full_name: str  # "owner/repo"
    default_branch: str
    private: bool


@dataclass(frozen=True)
class InstallationToken:
    """A minted installation access token.

    ``__repr__``/``__str__`` are overridden so this object can never leak
    the secret into a log line, exception message, or debugger repr --
    printing it is expected to happen (accidentally) somewhere eventually.
    """

    token: str
    expires_at: datetime
    installation_id: int
    repositories: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            f"InstallationToken(installation_id={self.installation_id}, "
            f"expires_at={self.expires_at.isoformat()}, token=***)"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.__repr__()

    def is_expired(self, *, skew_seconds: int = 30) -> bool:
        """True if the token has expired, or will within ``skew_seconds``.

        The skew margin exists so a caller who mints a token and then makes
        a handful of requests doesn't race GitHub's own expiry.
        """
        return datetime.now(UTC) >= self.expires_at - timedelta(seconds=skew_seconds)


@dataclass(frozen=True)
class PullRequest:
    """A PR opened (or found already open) for a depfix branch."""

    number: int
    html_url: str
    head_branch: str
    base_branch: str
    already_existed: bool = False
