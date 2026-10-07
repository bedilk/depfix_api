# depfix — Dependency Fix Agent

AI-powered tool that automatically fixes breaking dependency changes in your codebase.

When a package ships a breaking change, `depfix`:

1. **Detects** usages of the affected API across your codebase
2. **Generates** fixes using Google Gemini (or a local Ollama model)
3. **Validates** the fixes for syntax correctness (via Node.js)
4. **Verifies** kept fixes by running the target repo's own test suite before/after during `plan` and `apply`
5. **Outputs** unified diffs that can be applied as patches

## Requirements

- Python **3.11+**
- Node.js **20+** (used to syntax-check generated JS/TS fixes)
- npm (used by `depfix plan`/`apply` to install a target repo's dependencies and run its real test
  suite when verifying a fix). During `scan`, Depfix also detects the repository's npm, Yarn, or
  pnpm client. Missing repository-pinned Yarn/pnpm versions are installed into `~/.depfix/`
  rather than globally, ready for later verification.
- A Google Generative AI API key ([get one here](https://aistudio.google.com/app/apikey)), or a
  local [Ollama](https://ollama.com/) install for `--llm-provider ollama`

## Quick start

```bash
# Clone
git clone <repo-url>
cd dependency-fix-agent

# Create a venv and install (editable, with dev extras)
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Configure environment
cp .env.example .env
# Edit .env and set GOOGLE_API_KEY=...

# Run the CLI
depfix --help
depfix run --codebase ./tests/fixtures/openai_v3_project --change-file my_change.json --show-diffs
```

## Provider detection

`depfix scan --repo OWNER/REPO --provider ID` is the normal end-to-end
inspection command. Before it clones and scans the repository, it polls that
provider's feeds and classifies new events. It reads the checkout's manifests
and lockfiles, finds provider SDK/API call sites, and saves that evidence with
the checkout commit SHA. It then compares every classified provider change to
that exact scan and records an explainable decision: `actionable`,
`current`, `no_call_sites`, or `version_not_affected`. When a feed contains
release-note or spec evidence for an API that the repository actually uses,
Depfix may create a feed-proven migration candidate; its old API must match an
observed symbol and its evidence must be verbatim from the feed. Each saved
scan updates the catalog's managed `repository_migrations` section with every
resolved high/medium-confidence repository symbol, its repository and commit
SHA, resolved SDK package/version, the latest feed version, and one of two
decisions: `actionable` (the SDK is behind a feed version or the old API is
used) or `non_actionable`. A reason explains the comparison. The hand-authored
catalog describes provider feeds; it is not a fallback source of changes to
fix. `plan` re-scans and still requires a classified, feed-proven change before
editing, so a broad provider anchor, a stale observation, or version metadata
alone can never authorize a model call or PR. The deterministic evaluation
corpus runs last.

`depfix watch` remains available for scheduled fleet-wide polling — it polls
every feed declared in `providers.yaml` — npm dist-tags,
PyPI versions, GitHub releases, and OpenAPI specs today; changelogs and RSS are declared but
not yet implemented — and records changes in the configured database. Local
commands use a persistent `depfix.db` SQLite file by default; set
`DATABASE_URL` to Postgres for shared or concurrent deployments. Like the old poller it
replaces, it's designed to be driven by cron or systemd: one shot per
invocation, no in-process scheduler.

```bash
# Optional: use Postgres instead of the default local SQLite database.
docker compose up -d postgres

# Configure the local process to use it.
export DATABASE_URL=postgresql+psycopg://depfix:depfix@localhost:5432/depfix

# Edit the provider/feed list.
$EDITOR providers.yaml

# Run a watch pass.
depfix watch --once

# Only watch specific providers, or preview without writing to the DB.
depfix watch --once --provider stripe --provider openai
depfix watch --once --dry-run --show-changes

# 4. See what changed.
docker compose exec postgres psql -U depfix -d depfix \
    -c "SELECT provider_id, feed_key, old_token, new_token, severity FROM change_event ORDER BY detected_at DESC LIMIT 20;"
docker compose exec postgres psql -U depfix -d depfix \
    -c "SELECT kind, severity, subject FROM spec_change WHERE severity = 'breaking' ORDER BY id DESC LIMIT 20;"
```

Schedule the watcher with a crontab entry:

```cron
*/15 * * * * cd /srv/depfix && /srv/depfix/.venv/bin/depfix watch --once >> /var/log/depfix.log 2>&1
```

## Classifying detected changes

The scan workflow classifies its provider's unclassified `change_event` rows
automatically. `depfix classify` remains available for bulk/backfill work.
It turns unclassified events (normally written by `depfix watch`) into
structured `BreakingChange` rows. Events carrying a
structural OpenAPI diff (`spec_changes`) are classified deterministically —
no LLM, no hallucination risk, always `confidence=1.0`. Events with only
prose release notes are classified by an LLM, gated behind a verbatim-quote
anti-hallucination check; a proposed change is dropped outright if its
`evidence` can't be found in the source text.

An npm dist-tag move is also recorded deterministically as **dependency
drift**, even when there are no release notes. Drift is distinct from an API
migration: a same-major patch/minor update can be planned as a manifest-only
change and verified without an LLM; a major or unparseable update is reported
but requires a vetted, provider-specific migration before `apply` can open a
PR.

```bash
depfix classify                 # classify pending events (LLM if configured)
depfix classify --no-llm        # only the deterministic spec-diff path
depfix classify --limit 50      # process at most 50 events this run
depfix classify --output-dir output/changes  # export each newly created row as <ID>.json
```

The database row is the source of truth. The JSON export is an audit/replay
artifact, not a file that must be placed in the repository being fixed. Use
the printed ID with `--from-event <ID>` for normal plan/apply runs.

For deterministic end-to-end testing without waiting for a real upstream
release, [examples/seed-providers.yaml](examples/seed-providers.yaml) declares
explicit `fixture_change` feeds. They are test-only inputs to `watch`, kept in
Depfix rather than in the target repository; do not add them to production
`providers.yaml`.

`depfix run` can then consume a classified row directly:

```bash
depfix run --codebase ./my-app --from-event 42 --show-diffs
```

## Evaluating the pipeline

Every `scan` ends by running the deterministic evaluation after the repository
scan. It scores Depfix's scanner/classifier/fixer behavior against a YAML
corpus; it does not claim to know undiscovered call sites in the repository
just scanned. `depfix eval` remains available when you need its advanced
corpus, reporting, comparison, or LLM options.

`depfix eval` scores the classifier and fixer against a YAML corpus
(`src/depfix/evals/corpus/`). Each case is tagged `verified: true|false` —
unverified cases still run but are reported separately, so a passing eval
never silently counts a case nobody has actually confirmed against a live
model.

```bash
depfix eval --no-llm                     # deterministic spec_diff cases only (CI-safe)
depfix eval                              # full corpus, including LLM-backed cases
depfix eval --category spec_diff         # just one category
depfix eval --save report.json           # persist a JSON report
depfix eval --compare report.json        # fail on any case that regressed pass->fail
```

## Scanning a repo for call sites

`depfix scan` first detects and classifies provider changes, then finds usages
of that provider's SDK across a codebase — either a
local directory or a remote repo cloned on the fly via a GitHub App
installation — and reports/persists the call sites, manifest/lockfile version
evidence, and one feed-change decision per classified migration, with a
confidence level (`HIGH`/`MEDIUM`/`LOW`) reflecting how directly each usage
traces back to the SDK import.

```bash
# Scan a local checkout. Depfix detects upstream changes, compares their old
# API symbols with this checkout, and saves the commit-specific result.
depfix scan --path ./my-app --provider openai --no-save

# This equivalent legacy-friendly form also works. --old-api narrows the
# report to a particular old API namespace.
depfix scan --repo ./my-app --package openai --old-api openai.Completion --no-save

# Narrow the scan to the symbols touched by one classified breaking change.
depfix scan --path ./my-app --provider openai --from-event 42

# Scan a remote repo by cloning it via the GitHub App (needs GITHUB_APP_ID +
# a private key configured -- see below).
depfix scan --repo acme/widgets --provider stripe

# A normal scan always includes the deterministic regression corpus.
depfix scan --repo acme/widgets --provider stripe

# See what repos the App installation(s) can see.
depfix repos
```

To scan remote repos, register a GitHub App with `contents: read` permission,
install it on the target repo(s)/org, and set:

| Variable | Purpose |
| --- | --- |
| `GITHUB_APP_ID` | Numeric GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY` | PEM private key, inline (wins over the path below if both are set) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Path to a `.pem` file with the private key |
| `CLONE_DIR` | Base directory for shallow clones (defaults to the system temp dir) |
| `CLONE_MAX_REPO_MB` | Reject a clone once its working tree exceeds this size (default `500`) |

`depfix scan` mints a short-lived, least-privilege installation token per
scan and shallow-clones (`--depth=1`) into a temp directory that's removed
after the scan regardless of outcome.

For JavaScript repositories, scan also prepares the package manager that the
checkout declares. npm is checked from `PATH`; a missing pinned Yarn or pnpm
client is installed with `npm --ignore-scripts` into
`~/.depfix/package-managers/`, outside both the target repository and your
global toolchain. `plan` and `apply` repeat this preparation defensively, so
they remain usable when run without a preceding scan. Bun is detected but not
auto-installed because its vendor installer is an arbitrary shell script.

### Safe GitHub App test repositories

Install the App only on repositories you own or administer. For public
upstream projects, fork them into your own GitHub account or organization
first; an App installation does not grant access to somebody else's
repository. Start with `depfix scan --no-save` and use the pipeline only in a
dedicated test fork containing an explicit `.depfix.yml`.

The default provider set covers 20 active TypeScript/JavaScript SDKs. Useful
upstream fork candidates are [OpenAI's Node SDK](https://github.com/openai/openai-node),
[Anthropic's TypeScript SDK](https://github.com/anthropics/anthropic-sdk-typescript),
[Supabase JS](https://github.com/supabase/supabase-js), [Vercel AI SDK](https://github.com/vercel/ai),
and [Sentry JavaScript](https://github.com/getsentry/sentry-javascript). These
forks are useful for safe scan experiments; for end-to-end fix and PR tests,
use a small repository you control with deliberately outdated SDK call sites.

## Ecosystem support

depfix understands the top-10 language ecosystems at two honest tiers
(single authority: `src/depfix/ecosystems/`):

| Tier | Ecosystems | What you get |
| --- | --- | --- |
| **Fix + verify** | JavaScript/TypeScript (npm), Python (PyPI), Go, Ruby | Full pipeline: call-site scan, LLM fix, syntax validation, dependency install + the repo's own test suite run before/after each edit. Python installs into a throwaway in-checkout `.depfix-venv` and runs pytest with a JUnit reporter. |
| **Scan-only** | Java, Kotlin, Rust, PHP, C#/.NET, Swift | Repo detection, SDK dependency-declaration scanning across each ecosystem's manifests/lockfiles (go.mod, Cargo.toml/lock, Gemfile/lock, composer.json/lock, pom.xml/gradle, csproj, Package.swift/resolved, requirements/pyproject/poetry), and import-level call-site flagging. `plan` reports unsupported ecosystems rather than generating an edit nobody can verify locally. |

Dependency-drift automation is end-to-end for npm and PyPI: `depfix watch`
discovers a registry version move, `classify` records it deterministically,
and a same-major patch/minor drift becomes a verified manifest-only change.
With `LOCKFILE_REGENERATE=true`, npm and Poetry, uv, and Pipenv projects also
refresh their existing lockfile. pip-tools is deliberately not auto-refreshed:
its `requirements.txt` is generated from `requirements.in`, so Depfix reports
the required manual `requirements.in` change and `pip-compile` step rather
than risking a reverted verified bump. The remaining scan-only ecosystems stay detect/report-only
until their registry adapters, manifest writers, lockfile refreshers, and
verifiers are implemented. Depfix says this explicitly; it will not
manufacture a cross-ecosystem version change.

A provider declares which registries its SDKs live in per package
(`providers.yaml` → `sdk_packages: [{name: stripe, ecosystem: npm}, {name:
stripe, ecosystem: pypi}]`); the scanner routes each package to the right
ecosystem's matcher. Import-pattern confidence is honest about guesswork:
patterns derived mechanically from the package name (Python modules, Go
module paths, Maven group ids) report HIGH, convention-based namespace
guesses (Composer, NuGet, SwiftPM) report MEDIUM.

## Terraform-style workflow

Use the following workflow for an explicit, reviewable run. `init` initializes
the local database, loads the provider configuration, and checks the configured
GitHub App, LLM choice, and basic `git`/Node/npm availability. `plan` runs the
actual scan -> generate -> verify workflow in a disposable checkout,
but writes no database state, branch, or PR. When it has verified committable
edits, it writes an immutable plan artifact containing the exact diffs and base
commit SHA. `apply --plan-file` consumes that exact artifact: it does not call
the LLM again and refuses a stale repository/file base.

```bash
# Prepare the local database and integrations once per machine or environment.
depfix init

# Review the verified proposal. This cannot create a PR. It prints a path
# such as output/plans/<run-id>-<change>.json.
depfix plan acme/widgets --from-event 42 \
  --llm-provider ollama --ollama-model qwen2.5-coder:7b

# Create a PR from precisely that reviewed artifact. No LLM call occurs.
depfix apply acme/widgets --from-event 42 \
  --plan-file output/plans/<run-id>-<change>.json
```

During `plan` (and any `--dry-run` pass), depfix also runs a **local
impact check** before generating any fix: it runs the repo's test suite
against the current dependency tree, bumps the affected dependency to the
post-change version (manifest + regenerated lockfile, reverted afterwards),
runs the suite again, and reports which tests newly fail. A confirmed
break is quoted in the plan output as evidence; a clean run is reported as
exactly what it is -- "this repo's own tests don't observe the break"
(mocked externals prove nothing), never "safe to skip". Disable with
`PLAN_IMPACT_CHECK_ENABLED=false`.

Use `--force` only for intentional controlled retries (for example a seed
fixture that is in retry cooldown). A plan with no artifact reports the
per-file terminal verdict, such as `reverted`, `suspect`, or `skipped`, and
must be corrected/replanned before it can be applied.

`apply` still honours the target repository's `.depfix.yml`: `enabled`,
provider/path filters, verification policy, and `open_pr`/`draft_pr` settings
remain safety gates. For every named repository, the command itself is the
explicit opt-in; the configuration-file gate protects only automatic fleet
discovery.

## Common tasks

```bash
make install-dev         # install + dev extras + pre-commit hooks
make test                # run unit tests (no LLM calls)
make test-integration    # end-to-end tests (needs GOOGLE_API_KEY)
make lint                # ruff check
make format              # ruff format
make typecheck           # mypy on src/
make security            # bandit on src/
make run ARGS="--help"   # depfix --help
make eval                 # depfix eval --no-llm (CI gate)
make eval-full            # depfix eval with LLM-backed cases included
make eval-compare         # depfix eval --compare against the last saved report
make classify             # depfix classify
make scan ARGS="--path ./my-app --provider openai --no-save"
make repos                 # depfix repos
make run ARGS="plan acme/widgets --provider openai"
make docker-up            # postgres + redis + app via docker compose
```

## Project layout

```
dependency-fix-agent/
├── src/depfix/               # main package
│   ├── core/                 # models + agent orchestrator
│   ├── ecosystems/           # the multi-ecosystem seam: 10 specs, detection, manifests, import scanning
│   ├── scanners/             # codebase scanning (manifest + call-site + repo orchestration)
│   ├── fixers/               # LLM-based fix generation (Gemini, Ollama)
│   ├── validators/           # syntax / semantic validation
│   ├── apply/                 # WorkspaceEditor -- transactional in-place file edits
│   ├── verify/                 # sandboxed install/test-run verification of a candidate fix
│   ├── retry/                 # bounded SUSPECT/REVERTED retry loop, fed by verify's attribution
│   ├── sources/               # change feeds (npm dist-tag, GitHub, OpenAPI diff)
│   ├── classify/              # ChangeEvent -> BreakingChange (deterministic + LLM)
│   ├── evals/                 # YAML corpus + scoring harness (`depfix eval`)
│   ├── gh/                   # GitHub App auth, branch/commit + PR creation (Git Data API)
│   ├── clone/                 # shallow-clone / local-copy checkout service
│   ├── repoconfig/            # per-repo opt-in `.depfix.yml` loader/models
│   ├── orchestrator/          # plan/apply policy + runner
│   ├── api/                  # (placeholder) HTTP surface
│   ├── workers/              # (placeholder) background jobs
│   ├── registry/             # npm registry HTTP client
│   ├── storage/              # SQLAlchemy schema + session factory + fix_store/attempt_store
│   ├── watcher/               # one-shot feed poller (`depfix watch`)
│   ├── config.py             # Pydantic Settings — env-driven config
│   └── cli.py                # `depfix` entrypoint
├── tests/
│   ├── unit/                            # fast, no external services
│   ├── integration/                     # test_fix_pipeline.py needs node+npm; the rest hit a real LLM API (opt-in via marker)
│   ├── fixtures/openai_v3_project       # sample JS codebase (openai-node v3), for `depfix run`
│   └── fixtures/openai_v3_verifiable    # self-contained shim + real node:test suite, for verification tests
├── pyproject.toml            # PEP 621 metadata, ruff/mypy/pytest config
├── requirements.txt          # runtime deps
├── requirements-dev.txt      # dev/test deps
├── Dockerfile
├── docker-compose.yml        # postgres + redis + app
├── Makefile
└── .github/workflows/ci.yml  # lint → test → docker build
```

## Configuration

All configuration is read from environment variables (or a local `.env` file). See
[`.env.example`](.env.example) for the full list. Key variables:

| Variable          | Default              | Purpose                                  |
| ----------------- | -------------------- | ---------------------------------------- |
| `GOOGLE_API_KEY`  | *(required)*         | Google Generative AI credential          |
| `GEMINI_MODEL`    | `gemini-2.5-flash`   | Which Gemini model to call               |
| `DATABASE_URL`    | `sqlite:///depfix.db` | Persistent local SQLite database; set a Postgres DSN for shared/concurrent deployments |
| `REDIS_URL`       | unset                | Redis URL (used by future workers)       |
| `NPM_REGISTRY_URL`| `registry.npmjs.org` | npm registry base URL (override for tests) |
| `CLASSIFY_MIN_CONFIDENCE` | `0.5`        | Drop LLM-derived (release-notes) changes below this confidence |
| `EVAL_CORPUS_DIR` | packaged corpus      | Directory of `*.yaml` eval corpus files  |
| `EVAL_RESULTS_DIR`| `./.depfix-cache/eval-results` | Where `depfix eval --save` writes JSON reports |
| `GITHUB_APP_ID`   | unset                | GitHub App ID for `depfix scan --repo`/`depfix repos` |
| `GITHUB_APP_PRIVATE_KEY` / `GITHUB_APP_PRIVATE_KEY_PATH` | unset | GitHub App private key, inline or by path |
| `CLONE_DIR`       | system temp dir      | Base directory for shallow clones        |
| `CLONE_MAX_REPO_MB` | `500`               | Reject a clone once its working tree exceeds this size |
| `SCAN_MAX_FILES`  | `15000`              | Files scanned per repo (hard ceiling: 15000) |
| `SCAN_PREPARE_PACKAGE_MANAGER` | `true` | Prepare repository-pinned Yarn/pnpm during scan in the isolated Depfix cache |
| `PACKAGE_MANAGER_INSTALL_TIMEOUT` | `90` | Seconds allowed to provision a package-manager runtime |
| `FIX_VALIDATE_SYNTAX` | `true`           | Run syntax/sanity checks before writing an edit to disk |
| `VERIFY_ENABLED`  | `true`               | Whether `depfix plan`/`apply` runs the repo's own test suite to confirm each edit |
| `VERIFY_INSTALL_TIMEOUT` | `300`         | Wall-clock cap (seconds) for the dependency install step |
| `VERIFY_TEST_TIMEOUT` | `300`            | Wall-clock cap (seconds) for one baseline or after-fix test run |
| `VERIFY_IGNORE_SCRIPTS` | `true`         | Pass `--ignore-scripts` to the install command |
| `VERIFY_MAX_OUTPUT_BYTES` | `2000000`    | Truncate captured install/test output past this size |
| `RETRY_MAX_ROUNDS` | `2`                 | Bounded re-attempts for SUSPECT/REVERTED edits |
| `REPO_CONFIG_FILENAME` | `.depfix.yml`   | Per-repo opt-in pipeline config file the orchestrator looks for at the repo root |
| `PIPELINE_REQUIRE_CONFIG_FILE` | `true`  | Refuse to touch a repo with no `REPO_CONFIG_FILENAME` at all |
| `PIPELINE_MAX_OPEN_PRS` | `5`            | Per-repo cap on simultaneously open depfix-authored PRs |
| `PIPELINE_MAX_ATTEMPTS` | `3`            | Per-(repo, change) cap on non-terminal orchestrator attempts before giving up |
| `PIPELINE_RETRY_COOLDOWN_HOURS` | `6.0`  | Minimum time between two orchestrator attempts at the same (repo, change) |
| `PIPELINE_MAX_CHANGES_PER_REPO` | `3`    | Hard cap on distinct breaking changes processed per `plan`/`apply` run |
| `LOG_LEVEL`       | `INFO`               | Root logging level                       |
| `SENTRY_DSN`      | unset                | Sentry DSN for error reporting           |

## Testing

Unit tests are pure-Python and never call out to the network:

```bash
pytest tests/unit -m "not integration and not e2e"
```

Integration tests exercise the full pipeline against the real Gemini API. They are
skipped automatically unless `GOOGLE_API_KEY` is set:

```bash
GOOGLE_API_KEY=... pytest tests/integration -m integration
```

`tests/integration/test_fix_pipeline.py` is the exception: it runs `FixPipeline`
against a real `Verifier` (real `npm install` + `node --test`) but never calls an
LLM, so it's deliberately unmarked and runs under plain `pytest`/`make test` --
skipped automatically unless both `node` and `npm` are on `PATH`.

## Docker

```bash
docker compose up -d --build
docker compose exec app depfix --help
```

The compose stack starts Postgres and Redis alongside the app container. The image
uses a multi-stage build with a non-root runtime user and includes Node.js + npm --
required not just for the JS syntax validator, but for `depfix plan`/`apply` verification
step, which installs a target repo's real dependencies and runs its real test suite.
Debian's `nodejs` apt package doesn't bundle npm (and its separate `npm` package pulls
in ~300 unrelated JS library packages), so the runtime stage installs the upstream
Node.js binary tarball instead.

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and PR to `main`:

1. **lint** — `ruff check`, `ruff format --check`, `mypy`, `bandit`
2. **test** — `pytest` on Python 3.11 and 3.12 with coverage
3. **eval** — `depfix eval --no-llm --fail-under 0.80`, gating on the deterministic
   spec-diff corpus (LLM-backed cases are skipped, not failed, without an API key)
4. **build-docker** — smoke build the Dockerfile (waits on both `test` and `eval`)

Coverage XML and the eval JSON report are uploaded as build artifacts.

## Design decisions

- **Package name** — `depfix` (short, lowercase, unique on PyPI)
- **Layout** — `src/`-layout to avoid accidental imports of the wrong copy during dev
- **Dependency management** — `pip` + `requirements*.txt` (no Poetry/PDM)
- **Config** — Pydantic Settings, single `get_settings()` factory (`lru_cache`d)
- **Model default** — `gemini-2.5-flash` (1.5 family is no longer available on new API keys)
- **Linter/formatter** — `ruff` replaces flake8, black, isort
- **Test runner** — `pytest`, with markers `integration` and `e2e` to gate live calls
- **No hosted deploy target yet** — image builds in CI but is not pushed anywhere

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Source Available — free for non-commercial use. Commercial use requires a license.
See [LICENSE](LICENSE) for full terms.
