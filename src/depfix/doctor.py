"""Preflight checks used by :command:`depfix init`.

Every failure this checks for was first diagnosed the slow way -- by
running the real pipeline, getting an unexplained ``no_kept_edits``, and
bisecting. The Node reporter check is the archetype: a Node version whose
``--test`` output depfix couldn't parse made every fix in a repo with a
working test suite come back SUSPECT, and nothing in the failure pointed at
the reporter. That is a two-second check, not a debugging session.

The checks themselves are side-effect-free: no feeds are polled, no state
advances, and no model tokens are written. ``depfix init`` initializes the
local schema before running them.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from depfix.config import Settings
from depfix.providers.loader import ProviderConfigError, load_providers_file
from depfix.verify.parsers import parse_tap
from depfix.verify.sandbox import run_sandboxed


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str = ""
    hint: str = ""


def _ok(name: str, detail: str = "") -> Check:
    return Check(name, CheckStatus.OK, detail)


def _warn(name: str, detail: str, hint: str = "") -> Check:
    return Check(name, CheckStatus.WARN, detail, hint)


def _fail(name: str, detail: str, hint: str = "") -> Check:
    return Check(name, CheckStatus.FAIL, detail, hint)


def _binary_version(executable: str, *args: str) -> tuple[str | None, str]:
    path = shutil.which(executable)
    if path is None:
        return None, ""
    try:
        result = subprocess.run([path, *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return path, ""
    return path, (result.stdout or result.stderr).strip().splitlines()[0] if (
        result.stdout or result.stderr
    ) else ""


def check_git() -> Check:
    path, version = _binary_version("git", "--version")
    if path is None:
        return _fail("git", "not on PATH", "required to clone repos; install git")
    return _ok("git", version or path)


def check_node() -> Check:
    path, version = _binary_version("node", "--version")
    if path is None:
        return _fail(
            "node",
            "not on PATH",
            "required to syntax-check generated JS/TS fixes; install Node 20+",
        )
    return _ok("node", version or path)


def check_npm() -> Check:
    path, version = _binary_version("npm", "--version")
    if path is None:
        return _warn(
            "npm",
            "not on PATH",
            "without npm, no repo's test suite can run -- every fix will be verified by "
            "typecheck at best (MEDIUM confidence)",
        )
    return _ok("npm", version or path)


def check_ruby(settings: Settings) -> Check:
    ruby_path, ruby_version = _binary_version("ruby", "--version")
    bundle_path = shutil.which("bundle")
    if ruby_path is None:
        return _warn(
            "ruby",
            "not on PATH",
            "required only for Ruby fix generation (ruby -c syntax check + bundle install/test); "
            "scan-only Ruby works without it",
        )
    if bundle_path is None:
        return _warn(
            "ruby",
            f"{ruby_version}, but bundler missing",
            "install bundler (`gem install bundler`) to verify Ruby fixes against the repo's suite",
        )
    return _ok("ruby", f"{ruby_version}, bundler present")


def check_node_test_reporter() -> Check:
    """Probe that ``node --test --test-reporter=tap`` produces output
    :func:`depfix.verify.parsers.parse_tap` can actually read.

    This is the check that earns this whole module: Node's default
    ``--test`` reporter changed to a human-readable format in recent
    releases, and depfix silently degraded to the aggregate-count fallback
    parser -- which marks every edit SUSPECT. A version of Node that
    doesn't support ``--test-reporter`` at all fails the same way.
    """
    node = shutil.which("node")
    if node is None:
        return _fail("node --test reporter", "node not on PATH")

    with tempfile.TemporaryDirectory(prefix="depfix-doctor-") as tmp:
        probe = Path(tmp) / "probe.test.js"
        probe.write_text(
            "const test = require('node:test');\n"
            "const assert = require('node:assert');\n"
            "test('depfix doctor probe', () => { assert.ok(true); });\n",
            encoding="utf-8",
        )
        result = run_sandboxed(
            [node, "--test", "--test-reporter=tap", "probe.test.js"], cwd=tmp, timeout=60
        )

    if result.error or result.timed_out:
        return _fail(
            "node --test reporter",
            result.error or "probe timed out",
            "depfix cannot verify repos that use node:test on this machine",
        )
    cases = parse_tap(result.stdout)
    if not any(case.name for case in cases):
        return _fail(
            "node --test reporter",
            "`--test-reporter=tap` produced no parseable TAP",
            "upgrade/downgrade Node to a version supporting --test-reporter=tap; without it, "
            "node:test repos degrade to the aggregate parser and every edit is left SUSPECT",
        )
    return _ok("node --test reporter", f"tap parsed ({len(cases)} case(s))")


def check_typecheck_tool() -> Check:
    path = shutil.which("tsc")
    if path is None:
        return _warn(
            "tsc (global)",
            "not on PATH",
            "fine -- depfix prefers each repo's own node_modules/.bin/tsc; a global tsc is "
            "only a fallback for repos whose dependencies fail to install",
        )
    return _ok("tsc (global)", path)


def check_database(settings: Settings) -> list[Check]:
    from sqlalchemy import inspect, text

    from depfix.storage.db import get_engine

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        return [
            _fail(
                "database",
                f"{type(exc).__name__}: {exc}",
                "check DATABASE_URL, and that `docker compose up -d postgres` is running",
            )
        ]

    checks = [_ok("database", f"reachable ({engine.dialect.name})")]
    if engine.dialect.name != "postgresql":
        checks.append(
            _warn(
                "schema version",
                f"{engine.dialect.name} uses create_all, not migrations",
                "expected for local/dev SQLite; production must be Postgres",
            )
        )
        return checks

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
        tables = set(inspect(engine).get_table_names())
        if "alembic_version" not in tables:
            checks.append(
                _warn(
                    "schema version",
                    "not stamped yet" if tables else "empty database",
                    "run any depfix command (init_schema stamps and upgrades) or `make migrate`",
                )
            )
            return checks
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if current == head:
            checks.append(_ok("schema version", f"at head ({head})"))
        else:
            checks.append(
                _fail(
                    "schema version",
                    f"at {current}, head is {head}",
                    "run `make migrate` (or any depfix command) to upgrade",
                )
            )
    except Exception as exc:
        checks.append(_warn("schema version", f"could not determine: {exc}"))
    return checks


def check_github_app(settings: Settings) -> list[Check]:
    from depfix.gh import GitHubAppError

    if not settings.github_app_id:
        return [
            _warn(
                "github app",
                "GITHUB_APP_ID not set",
                "required for scan --repo / plan / apply",
            )
        ]
    private_key = settings.github_app_private_key
    if not private_key and settings.github_app_private_key_path:
        try:
            private_key = Path(settings.github_app_private_key_path).read_text(encoding="utf-8")
        except OSError as exc:
            return [
                _fail(
                    "github app",
                    f"could not read GITHUB_APP_PRIVATE_KEY_PATH: {exc}",
                    "check the path and its permissions",
                )
            ]
    if not private_key:
        return [
            _fail(
                "github app",
                "no private key configured",
                "set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH",
            )
        ]

    # Imported lazily so `init` still reports every other check on a box
    # with no App configured at all.
    from depfix.cli import _build_gh_auth

    gh_auth = _build_gh_auth(settings)
    if gh_auth is None:  # pragma: no cover - guarded above
        return [_fail("github app", "could not construct GitHubAppAuth")]
    try:
        installations = gh_auth.list_installations()
    except GitHubAppError as exc:
        return [
            _fail(
                "github app",
                str(exc),
                "a 401 here is almost always local clock skew or a key/app-id mismatch",
            )
        ]
    finally:
        gh_auth.close()

    if not installations:
        return [
            _warn(
                "github app",
                "authenticated, but installed on 0 accounts",
                "install the App on a repo or org you own",
            )
        ]
    return [
        _ok(
            "github app",
            f"{len(installations)} installation(s): "
            + ", ".join(i.account_login for i in installations[:5]),
        )
    ]


def check_llm(settings: Settings) -> Check:
    if settings.llm_provider == "bedrock":
        try:
            from depfix.classify.llm import BedrockCompleter

            c = BedrockCompleter(
                aws_access_key=settings.bedrock_access_key,
                aws_secret_key=settings.bedrock_secret_key,
                aws_region=settings.bedrock_region,
                aws_profile=settings.bedrock_profile,
                model=settings.bedrock_model,
            )
            c.complete("ping", temperature=0.1)
            return _ok(
                "llm (bedrock)",
                f"region={settings.bedrock_region}, model={settings.bedrock_model}",
            )
        except Exception as exc:
            return _fail(
                "llm (bedrock)",
                f"Bedrock unreachable: {exc}",
                "check AWS credentials/profile and BEDROCK_REGION",
            )

    if settings.llm_provider == "gemini":
        if not settings.google_api_key:
            return _fail(
                "llm (gemini)",
                "GOOGLE_API_KEY not set",
                "set it, or switch to LLM_PROVIDER=ollama for local inference",
            )
        return _ok("llm (gemini)", f"key present, model={settings.gemini_model}")

    url = settings.ollama_base_url.rstrip("/")
    try:
        response = httpx.get(f"{url}/api/tags", timeout=5.0)
        response.raise_for_status()
        tags = {entry.get("name", "") for entry in response.json().get("models", [])}
    except (httpx.HTTPError, ValueError) as exc:
        return _fail(
            "llm (ollama)",
            f"{url} unreachable: {exc}",
            "start Ollama (`ollama serve`) or switch to LLM_PROVIDER=gemini",
        )
    wanted = settings.ollama_model
    if wanted not in tags and not any(t.startswith(wanted) for t in tags):
        return _fail(
            "llm (ollama)",
            f"model {wanted!r} not pulled (have: {', '.join(sorted(tags)[:5]) or 'none'})",
            f"run `ollama pull {wanted}`",
        )
    return _ok("llm (ollama)", f"{url}, model={wanted}")


def check_providers(settings: Settings) -> Check:
    try:
        providers = load_providers_file(settings.providers_file)
    except (FileNotFoundError, ProviderConfigError) as exc:
        return _fail("providers.yaml", str(exc), "check PROVIDERS_FILE")
    enabled = [p for p in providers if p.enabled]
    return _ok("providers.yaml", f"{len(providers)} provider(s), {len(enabled)} enabled")


def check_observability(settings: Settings) -> Check:
    if not settings.sentry_dsn:
        return _warn(
            "sentry",
            "SENTRY_DSN not set",
            "uncaught exceptions in unattended runs will only reach stdout",
        )
    return _ok("sentry", f"configured (environment={settings.environment})")


def check_clone_dir(settings: Settings) -> Check:
    directory = Path(settings.clone_dir) if settings.clone_dir else Path(tempfile.gettempdir())
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".depfix-init-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return _fail("clone dir", f"{directory} not writable: {exc}", "set CLONE_DIR")
    return _ok("clone dir", str(directory))


def check_ecosystem_support() -> Check:
    from depfix.ecosystems import all_ids, fixable_ids

    fixable = sorted(fixable_ids())
    scan_only = sorted(set(all_ids()) - fixable_ids())
    return _ok(
        "ecosystem support",
        f"fix + verify: {', '.join(fixable)}; scan-only: {', '.join(scan_only)}",
    )


def run_checks(settings: Settings, *, skip_network: bool = False) -> list[Check]:
    """Every check, in the order a failure would actually block you."""
    checks: list[Check] = [
        check_git(),
        check_node(),
        check_npm(),
        check_ruby(settings),
        check_node_test_reporter(),
        check_typecheck_tool(),
        check_clone_dir(settings),
        check_providers(settings),
        check_ecosystem_support(),
    ]
    checks.extend(check_database(settings))
    if skip_network:
        checks.append(_warn("github app", "skipped (--offline)"))
        checks.append(_warn("llm", "skipped (--offline)"))
    else:
        checks.extend(check_github_app(settings))
        checks.append(check_llm(settings))
    checks.append(check_observability(settings))
    return checks


def worst_status(checks: list[Check]) -> CheckStatus:
    if any(c.status is CheckStatus.FAIL for c in checks):
        return CheckStatus.FAIL
    if any(c.status is CheckStatus.WARN for c in checks):
        return CheckStatus.WARN
    return CheckStatus.OK
