# Contributing to depfix

Thanks for your interest in contributing.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Workflow

1. Create a feature branch: `git checkout -b feature/short-name`
2. Make your changes in `src/depfix/`.
3. Add or update tests in `tests/unit/` (and `tests/integration/` if the change
   affects the live pipeline).
4. Run the local checks:
   ```bash
   make lint
   make typecheck
   make test
   ```
5. Commit — the pre-commit hook will re-run ruff / mypy / bandit.
6. Open a PR against `main`.

## Testing conventions

- **Unit tests** live in `tests/unit/` and must run without network access, without
  a real API key, and without external services. Use `unittest.mock.patch` to stub
  out `depfix.fixers.gemini.genai`.
- **Integration tests** live in `tests/integration/`, are marked with
  `@pytest.mark.integration`, and are skipped unless `GOOGLE_API_KEY` is set.
- Shared fixtures live in `tests/conftest.py`.
- Sample JS project used by fixtures: `tests/fixtures/openai_v3_project/`.

## Coding style

- `ruff` handles both linting and formatting. Do not run `black` or `isort`.
- Prefer explicit type hints on public functions.
- Keep functions small; the fixer / scanner / validator are intentionally
  independent — do not introduce cross-module imports outside `depfix.core`.

## Adding a new fixer or validator

- New LLM providers go under `src/depfix/fixers/` and should implement the same
  `generate_fix(file_usage, breaking_change) -> (fixed_code, confidence, LLMCall)`
  signature as `depfix.fixers.gemini.FixGenerator`.
- New language validators go under `src/depfix/validators/` and should return a
  `ValidationResult` from `depfix.core.models`.

## Releasing

Versioning is done in `pyproject.toml` and `src/depfix/__init__.py`. Tagged releases
(future) will trigger a publish workflow — for now, no packages are published.
