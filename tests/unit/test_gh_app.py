"""Unit tests for GitHub App authentication.

The App JWT is signed for real with a locally-generated throwaway RSA key
(cheap: ~tens of ms); all GitHub HTTP traffic is intercepted with respx --
no live network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from depfix.gh.app import GitHubAppAuth, GitHubAppError
from depfix.gh.models import InstallationToken

_BASE = "https://api.github.example"


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _auth(private_key_pem: str) -> GitHubAppAuth:
    return GitHubAppAuth(app_id="12345", private_key=private_key_pem, api_url=_BASE)


@respx.mock(base_url=_BASE)
def test_list_installations_paginates(respx_mock: respx.Router, private_key_pem: str) -> None:
    # A single route with a `side_effect` list, rather than two routes
    # distinguished by query string -- a route with no query filter matches
    # *any* query string, so a second, more-specific route for `?page=2`
    # would still be shadowed by this one and never take over, looping
    # forever on the same "next" Link header.
    respx_mock.get("/app/installations").mock(
        side_effect=[
            httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "account": {"login": "acme", "type": "Organization"},
                        "repository_selection": "selected",
                    }
                ],
                headers={"Link": f'<{_BASE}/app/installations?page=2>; rel="next"'},
            ),
            httpx.Response(
                200,
                json=[
                    {
                        "id": 2,
                        "account": {"login": "someone", "type": "User"},
                        "repository_selection": "all",
                    }
                ],
            ),
        ]
    )

    with _auth(private_key_pem) as auth:
        installations = auth.list_installations()

    assert [i.id for i in installations] == [1, 2]
    assert installations[0].account_login == "acme"
    assert installations[1].account_type == "User"


@respx.mock(base_url=_BASE)
def test_app_jwt_is_sent_as_bearer_and_verifies_with_public_key(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    route = respx_mock.get("/app/installations").mock(return_value=httpx.Response(200, json=[]))

    with _auth(private_key_pem) as auth:
        auth.list_installations()

    request = route.calls.last.request
    token = request.headers["Authorization"].removeprefix("Bearer ")
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    payload = jwt.decode(token, public_pem, algorithms=["RS256"])
    assert payload["iss"] == "12345"
    assert payload["exp"] > payload["iat"]


@respx.mock(base_url=_BASE)
def test_resolve_installation_for_repo_404_raises_actionable_error(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").mock(return_value=httpx.Response(404))

    with (
        _auth(private_key_pem) as auth,
        pytest.raises(GitHubAppError, match="no GitHub App installation"),
    ):
        auth.resolve_installation_for_repo("acme", "widgets")


@respx.mock(base_url=_BASE)
def test_resolve_installation_for_repo_is_cached_per_repo(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    """A repo's installation binding doesn't change mid-run -- every
    pre-flight read that needs it (get_file_contents, ref_sha,
    list_open_pull_requests, ...) should share one lookup per repo, not
    re-resolve it on every call. A different repo still gets its own
    lookup."""
    widgets_route = respx_mock.get("/repos/acme/widgets/installation").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "account": {"login": "acme", "type": "Organization"}}
        )
    )
    other_route = respx_mock.get("/repos/acme/other/installation").mock(
        return_value=httpx.Response(
            200, json={"id": 2, "account": {"login": "acme", "type": "Organization"}}
        )
    )

    with _auth(private_key_pem) as auth:
        first = auth.resolve_installation_for_repo("acme", "widgets")
        second = auth.resolve_installation_for_repo("acme", "widgets")
        other = auth.resolve_installation_for_repo("acme", "other")

    assert first.id == second.id == 1
    assert other.id == 2
    assert widgets_route.call_count == 1
    assert other_route.call_count == 1


@respx.mock(base_url=_BASE)
def test_unauthorized_raises_clock_skew_hint(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    respx_mock.get("/app/installations").mock(return_value=httpx.Response(401))

    with _auth(private_key_pem) as auth, pytest.raises(GitHubAppError, match="clock skew"):
        auth.list_installations()


@respx.mock(base_url=_BASE)
def test_422_raises_permission_hint(respx_mock: respx.Router, private_key_pem: str) -> None:
    respx_mock.post("/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(422, text="Validation Failed")
    )

    with _auth(private_key_pem) as auth, pytest.raises(GitHubAppError, match="permission"):
        auth.installation_token(1, repositories=("widgets",))


@respx.mock(base_url=_BASE)
def test_422_names_the_permissions_requested_for_a_write_token(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    respx_mock.post("/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(422, text="Validation Failed")
    )

    with (
        _auth(private_key_pem) as auth,
        pytest.raises(GitHubAppError, match=r"contents=write, pull_requests=write"),
    ):
        auth.installation_token(
            1,
            repositories=("widgets",),
            permissions={"contents": "write", "pull_requests": "write"},
        )


@respx.mock(base_url=_BASE)
def test_installation_token_requests_least_privilege_by_default(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    route = respx_mock.post("/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "ghs_abc123",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1))
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
    )

    with _auth(private_key_pem) as auth:
        token = auth.installation_token(1, repositories=("widgets",))

    assert token.token == "ghs_abc123"
    sent_body = route.calls.last.request.content
    import json as _json

    body = _json.loads(sent_body)
    assert body["permissions"] == {"contents": "read"}
    assert body["repositories"] == ["widgets"]


@respx.mock(base_url=_BASE)
def test_installation_token_is_cached_until_expiry(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    route = respx_mock.post("/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "ghs_first",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1))
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
    )

    with _auth(private_key_pem) as auth:
        first = auth.installation_token(1, repositories=("widgets",))
        second = auth.installation_token(1, repositories=("widgets",))

    assert first.token == second.token
    assert route.call_count == 1


@respx.mock(base_url=_BASE)
def test_installation_token_remints_after_expiry(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    respx_mock.post("/app/installations/1/access_tokens").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "token": "ghs_expired",
                    "expires_at": (datetime.now(UTC) - timedelta(seconds=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            ),
            httpx.Response(
                200,
                json={
                    "token": "ghs_fresh",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            ),
        ]
    )

    with _auth(private_key_pem) as auth:
        first = auth.installation_token(1, repositories=("widgets",))
        second = auth.installation_token(1, repositories=("widgets",))

    assert first.token == "ghs_expired"
    assert second.token == "ghs_fresh"


@respx.mock(base_url=_BASE)
def test_installation_token_does_not_reuse_cached_token_for_different_permissions(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    respx_mock.post("/app/installations/1/access_tokens").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "token": "ghs_readonly",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            ),
            httpx.Response(
                200,
                json={
                    "token": "ghs_readwrite",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            ),
        ]
    )

    with _auth(private_key_pem) as auth:
        read_token = auth.installation_token(1, repositories=("widgets",))
        write_token = auth.installation_token(
            1, repositories=("widgets",), permissions={"contents": "write"}
        )
        # A second call with the same write permissions must hit the cache,
        # not mint a third token.
        write_token_again = auth.installation_token(
            1, repositories=("widgets",), permissions={"contents": "write"}
        )

    assert read_token.token == "ghs_readonly"
    assert write_token.token == "ghs_readwrite"
    assert write_token_again.token == "ghs_readwrite"


def test_installation_token_repr_never_leaks_the_secret() -> None:
    token = InstallationToken(
        token="ghs_super_secret",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        installation_id=1,
    )

    assert "ghs_super_secret" not in repr(token)
    assert "ghs_super_secret" not in str(token)


@respx.mock(base_url=_BASE)
def test_default_ref_resolves_via_installation_and_repo(
    respx_mock: respx.Router, private_key_pem: str
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 7,
                "account": {"login": "acme", "type": "Organization"},
                "repository_selection": "all",
            },
        )
    )
    respx_mock.post("/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "ghs_token",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1))
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
    )
    respx_mock.get("/repos/acme/widgets").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 99,
                "full_name": "acme/widgets",
                "default_branch": "develop",
                "private": True,
            },
        )
    )

    with _auth(private_key_pem) as auth:
        ref = auth.default_ref("acme", "widgets")

    assert ref == "develop"
