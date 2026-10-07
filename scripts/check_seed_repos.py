#!/usr/bin/env python3
"""Diagnose which controlled seed repos the configured GitHub App can reach.

Usage:
    python scripts/check_seed_repos.py

Requires ``GITHUB_APP_ID`` and ``GITHUB_APP_PRIVATE_KEY`` (or
``GITHUB_APP_PRIVATE_KEY_PATH``) in the environment or ``.env``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

# Allow running from the repository root without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depfix.clone.service import CloneError, CloneService
from depfix.config import get_settings
from depfix.gh.app import GitHubAppAuth, GitHubAppError

EXPECTED_SEED_REPOS = (
    "bedilk/seed-anthropic-v0-v0x",
    "bedilk/seed-mongodb-v4-v5",
    "bedilk/seed-openai-v3-to-v4",
    "bedilk/seed-sentry-v7-v8",
    "bedilk/seed-stripe-apiversion",
    "bedilk/seed-supabase-v1-v2",
    "bedilk/seed-vercel-ai-v2-v3",
    "bedilk/seed-multi-sdk",
)


def main() -> int:
    settings = get_settings()
    private_key = settings.github_app_private_key
    if not private_key and settings.github_app_private_key_path:
        private_key = Path(settings.github_app_private_key_path).read_text(encoding="utf-8")
    if not settings.github_app_id or not private_key:
        print("ERROR: configure GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY (or _PATH).")
        return 1

    auth = GitHubAppAuth(
        app_id=settings.github_app_id,
        private_key=private_key,
        api_url=settings.github_api_url,
    )
    try:
        try:
            installations = auth.list_installations()
        except (GitHubAppError, httpx.HTTPError) as exc:
            print(f"ERROR: cannot list installations: {exc}")
            return 1

        print(f"App has {len(installations)} installation(s):")
        for installation in installations:
            print(
                f"  id={installation.id}  account={installation.account_login} "
                f"type={installation.account_type}"
            )
        print()

        clone_service = CloneService(timeout=30, max_repo_mb=50)
        exit_code = 0
        for repo_full_name in EXPECTED_SEED_REPOS:
            owner, name = repo_full_name.split("/", 1)
            try:
                installation = auth.resolve_installation_for_repo(owner, name)
            except (GitHubAppError, httpx.HTTPError) as exc:
                print(f"FAIL  {repo_full_name}: no installation ({exc})")
                print("  FIX: add this repository to the App installation at")
                print("       https://github.com/settings/installations")
                exit_code = 1
                continue

            try:
                repository = auth.get_repository(owner, name)
                print(f"  OK  {repo_full_name}: visible (branch={repository.default_branch})")
            except (GitHubAppError, httpx.HTTPError) as exc:
                print(f"FAIL  {repo_full_name}: installation resolves but metadata read failed")
                print("  FIX: verify the repo name and grant the App Contents: Read permission.")
                print(f"       error: {exc}")
                exit_code = 1
                continue

            checkout = None
            try:
                token = auth.installation_token(installation.id, repositories=(name,))
                checkout = clone_service.clone(owner, name, token=token.token)
                js_files = sum(1 for _ in checkout.path.rglob("*.js"))
                print(f"  OK  {repo_full_name}: cloned ({js_files} .js file(s))")
            except (CloneError, GitHubAppError, httpx.HTTPError) as exc:
                print(f"FAIL  {repo_full_name}: visible but clone failed")
                print("  FIX: re-authorize the App with Contents: Read for this repository.")
                print(f"       error: {exc}")
                exit_code = 1
            finally:
                if checkout is not None:
                    clone_service.cleanup(checkout)

        print()
        expected = set(EXPECTED_SEED_REPOS)
        for installation in installations:
            try:
                visible = {repo.full_name for repo in auth.list_repositories(installation.id)}
            except (GitHubAppError, httpx.HTTPError) as exc:
                print(f"WARN: could not list repositories for {installation.account_login}: {exc}")
                exit_code = 1
                continue
            extra = visible - expected
            if extra:
                print(f"INFO: {installation.account_login} also has: {', '.join(sorted(extra))}")
            missing = expected - visible
            if missing:
                print(
                    f"WARN: {installation.account_login} is missing: {', '.join(sorted(missing))}"
                )
                print("  FIX: add these repositories at")
                print(f"       https://github.com/settings/installations/{installation.id}")
                exit_code = 1

        return exit_code
    finally:
        auth.close()


if __name__ == "__main__":
    raise SystemExit(main())
