# MVP in 3 Days — Execution Plan

*Written 2026-09-17. Owner: Tohir + Claude (pair). Reviewer: Bedil — leave comments inline or in PROGRESS.md.*

**Context:** full market/competitor/pricing research and a codebase audit are done — see
[`docs/research/BUSINESS_ASSESSMENT.md`](research/BUSINESS_ASSESSMENT.md). Short version: the
pipeline already works end-to-end as a CLI; the whitespace ("watch ALL service providers →
verified auto-fix") is real and unoccupied; the risks are known. The MVP is therefore **not
"build the product"** — it is **"make the existing loop demonstrable on real repos and get
honest numbers out of it."**

## Scope decisions (locked for these 3 days)

- **One wedge:** OpenAI SDK (spec feed + tuned call-site anchors already wired; fastest-churning provider). Stripe as stretch.
- **One metric:** % of generated fixes that survive test verification (KEPT rate), plus scan recall on the eval corpus.
- **One deliverable:** a recorded end-to-end run (watch → classify → scan → fix → verify → PR) + 2–3 real repos through the loop + a numbers table.
- **Explicitly cut:** multi-tenancy, dashboard, web API, Marketplace listing, rename, SOC2, non-Google LLM, new languages. All post-MVP.

## Day 1 — Trust + hygiene (the demo-breakers)

| # | Task | Why | Done when |
|---|---|---|---|
| 1.1 | **Secret redaction** before any code enters an LLM prompt (`fixers/gemini.py::_build_prompt`, same for Ollama): scrub API keys, tokens, `.env`-style values, high-entropy strings | Docs promise it; code doesn't do it. First question any tester asks. | Unit test proves a planted `sk-...` key never reaches the prompt |
| 1.2 | Restore **CI**: `.github/workflows/ci.yml` (lint → unit tests → `eval --no-llm`) matching what the README already claims | README documents CI that doesn't exist — instant credibility hit | Green run on GitHub Actions |
| 1.3 | Repo hygiene: delete `changes.txt` + committed `output/` artifacts, add `.env.example`, fix placeholder URLs in `pyproject.toml`, gitignore `output/` and `.DS_Store` | 10-minute-diligence items | Clean tree |
| 1.4 | `depfix status` — one CLI table: pending change_events, classified changes, fix runs + verdicts, open PRs | Testers must see state without psql | Command works against the docker-compose Postgres |
| 1.5 | Environment up: venv, deps, Postgres via docker compose, one `watch --once` pass against real feeds | Proves the loop runs on this machine today | Rows in `change_event` |

## Day 2 — The demo + the numbers

| # | Task | Done when |
|---|---|---|
| 2.1 | Make `tests/fixtures/openai_v3_project` a realistic small app (real-looking routes/services using openai-node v3 patterns, with a real test suite) | `depfix fix` produces KEPT edits verified by the fixture's own tests |
| 2.2 | Full-loop recording: `watch → classify → scan → fix → verify → PR` end to end, screen-recorded | A shareable video/gif — this is the pitch asset |
| 2.3 | Eval corpus: +10 real OpenAI/Stripe breaking changes (from their changelogs) with verified labels | `depfix eval` reports accuracy on ≥30 cases; number written down |
| 2.4 | Cost/latency capture: LLM cost + wall-clock per fix from the existing per-run cost tracking | A table in PROGRESS.md |

## Day 3 — Reality test

| # | Task | Done when |
|---|---|---|
| 3.1 | Fork 2–3 real public repos that use the OpenAI SDK with dated call sites; run `scan --no-save`, then `fix` on the forks | Scan + fix results recorded per repo |
| 3.2 | If GitHub App creds are ready: `--open-pr` on our own fork to demo the full PR flow. If not: `--output ./patches` and show diffs | PR screenshot or patch files |
| 3.3 | Write `RESULTS.md`: call sites found/missed, KEPT/REVERTED/SUSPECT counts, cost per fix, honest failure notes | The numbers exist, whatever they are |
| 3.4 | Decide from data: KEPT rate ≥50% on real repos → recruit pilot teams next week. Below → the failure modes are the next work list | Decision written down |

## Blockers / decisions needed (Bedil / Tohir)

1. **LLM:** is there a `GOOGLE_API_KEY` we can use, or do we run local Ollama (`qwen2.5-coder:7b` — weaker fixes, zero cost, code never leaves the machine)? Ideally both, compared.
2. **GitHub App:** does one exist (APP_ID + private key)? Needed only for remote-repo scan and `--open-pr`; local-path mode covers most of the MVP without it.
3. **Rename:** `depfix` name is taken by an unrelated OSS project (Aug 2026). Not a 3-day blocker, but decide before anything public.

## Post-MVP (weeks 2–4, only if Day-3 numbers are good)

Pilot with 2–3 friendly teams (dry-run → draft-PR ladder per `docs/onboarding.md`) → then, in order: multi-tenancy, dashboard, Marketplace App, queue workers, billing. Pricing, market sizing, and the strategic gates are all in the assessment doc.
