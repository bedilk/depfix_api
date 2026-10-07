.PHONY: help install install-dev lock test test-unit test-integration lint format typecheck security clean run eval eval-full eval-compare classify scan repos migrate migration docker-build docker-up docker-down

PYTHON ?= python
PIP ?= $(PYTHON) -m pip

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime dependencies
	$(PIP) install -e .

install-dev:  ## Install with dev extras (tests, lint, mypy)
	$(PIP) install -e ".[dev]"
	pre-commit install || true

lock:  ## Regenerate requirements.lock after changing project dependencies
	pip-compile --generate-hashes --output-file=requirements.lock pyproject.toml

test:  ## Run all tests (excluding integration/e2e)
	pytest -m "not integration and not e2e"

test-unit:  ## Run unit tests only
	pytest tests/unit -m "not integration and not e2e"

test-integration:  ## Run integration tests (requires GOOGLE_API_KEY)
	pytest tests/integration -m integration

lint:  ## Ruff lint
	ruff check src tests

format:  ## Ruff format
	ruff format src tests

format-check:  ## Ruff format check (no writes)
	ruff format --check src tests

typecheck:  ## mypy type check
	mypy src

security:  ## Bandit security lint
	bandit -q -c pyproject.toml -r src -x tests

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +

run:  ## Run the depfix CLI (pass ARGS="--help")
	depfix $(ARGS)

eval:  ## Run the eval corpus without an LLM (deterministic spec_diff cases only; CI gate)
	depfix eval --no-llm --fail-under 1.0 --save .depfix-cache/eval-results/local.json

eval-full:  ## Run the full eval corpus, including LLM-backed release_notes/fix_generation cases
	depfix eval --fail-under 1.0 --save .depfix-cache/eval-results/local.json

eval-compare:  ## Run the full eval corpus and fail on any per-case regression vs. the last saved report
	depfix eval --compare .depfix-cache/eval-results/local.json --save .depfix-cache/eval-results/local.json

classify:  ## Classify pending change events into breaking changes
	depfix classify

scan:  ## Scan a repo/path for provider call sites (pass ARGS="--repo owner/name --provider openai")
	depfix scan $(ARGS)

repos:  ## List repos visible to the GitHub App installation(s)
	depfix repos $(ARGS)

migrate:  ## Apply Alembic migrations up to head (URL comes from DATABASE_URL/.env, not alembic.ini)
	alembic -c alembic.ini upgrade head

migration:  ## Create a new blank Alembic revision (pass MSG="add foo column")
	alembic -c alembic.ini revision -m "$(MSG)"

docker-build:  ## Build the depfix Docker image
	docker build -t depfix:local .

docker-up:  ## Start the full stack (postgres + redis + app)
	docker compose up -d --build

docker-down:  ## Stop the stack
	docker compose down
