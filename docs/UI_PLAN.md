# Developer UI Plan — Browse Depfix Specs & Release Notes

*Written 2026-09-17. Proposal, not yet built — `src/depfix/api/` is currently a one-line
placeholder (see `docs/decisions.md`, "Explicitly deferred: HTTP API"). This plan turns it
into the read surface a developer actually wants: "what changed upstream, and why does
depfix think it's breaking."*

## Why this doc exists

Today the only way to see a detected breaking change is `psql` against `breaking_change` /
`spec_change` rows, or scrolling `depfix classify --output-dir` JSON exports. That's fine for
the CLI operator; it's a wall for a developer who just wants to know "why did I get a PR" or
"is there a Stripe change coming that affects me." The data already exists and is well-shaped
for this — `BreakingChangeRow` (`src/depfix/storage/schema.py:185`) already carries
`description`, `migration_guide`, `source_url`, `evidence`, `confidence`, `old_api`/`new_api`,
and links back to `SpecChangeRow` for the structural diff. **This is a read-layer project, not
a new data model.**

## The core object: a "Spec"

A **Spec** = one `BreakingChangeRow`, rendered as a developer would want to read a changelog
entry:

```
┌─────────────────────────────────────────────────────────────┐
│ ● BREAKING   stripe   2024-04-10 → 2024-06-20                │
│ PaymentIntent.confirmation_method removed                     │
├─────────────────────────────────────────────────────────────┤
│ Confidence: ██████████ 1.0 (deterministic, spec_diff)         │
│                                                                 │
│ What changed                                                   │
│   confirmation_method was removed from PaymentIntent.          │
│   [old_api / new_api diff, syntax-highlighted]                 │
│                                                                 │
│ Migration guide                                                 │
│   [migration_guide markdown]                                   │
│                                                                 │
│ Evidence                                                        │
│   "removed the deprecated confirmation_method field"            │
│   — quoted verbatim from source, links to source_url            │
│                                                                 │
│ Affects your repos                                              │
│   acme/checkout-service   3 call sites   PR #142 (KEPT, HIGH)  │
│   acme/billing-worker     1 call site    not yet scanned       │
└─────────────────────────────────────────────────────────────┘
```

This one card is the entire product surface for v1. Everything else is navigation to/from it.

## Pages (v1 scope)

1. **Feed** (`/`) — reverse-chronological list of Specs across all watched providers, filterable
   by provider, severity (`breaking`/`deprecated`/`safe`), and source (`spec_diff` vs
   `release_notes`). This is `SELECT * FROM breaking_change ORDER BY created_at DESC` with
   joins — no new computation.
2. **Spec detail** (`/specs/:id`) — the card above, plus the linked `SpecChangeRow` structural
   diff when `source = spec_diff`, and every `CallSiteRow`/`FixRunRow`/`PullRequestRow` this
   change touched, across every repo.
3. **Provider page** (`/providers/:id`) — one provider's timeline of Specs + its
   `FeedState` (last polled, feed health) — answers "is depfix actually watching Stripe
   correctly."
4. **Repo page** (`/repos/:id`) — the operational view a design partner actually asked for in
   the 3-day MVP plan (`docs/MVP_3DAY_PLAN.md`, item 1.4, `depfix status`): pending changes,
   fix-run history with verdicts (KEPT/REVERTED/SUSPECT/SKIPPED), confidence tiers, open PRs.
   **This page is the web version of that CLI command — build the API endpoint once, serve
   both.**

Deliberately cut from v1: auth/accounts (single-tenant today, per the audit), editing/approving
anything from the UI (plan/apply stays CLI-only until multi-tenancy exists — see
`docs/research/BUSINESS_ASSESSMENT.md` §4), search, notifications/webhooks.

## Data flow (no new pipeline, just a read path)

```
Postgres (existing schema)
    │
    ▼
FastAPI read-only routers in src/depfix/api/   (turns the "future" stub real)
    │  /api/specs, /api/specs/:id, /api/providers/:id, /api/repos/:id
    │  each endpoint is a thin SQLAlchemy query + Pydantic serializer —
    │  reuses src/depfix/storage/schema.py models directly, no duplication
    ▼
Static SPA (or server-rendered templates for v1 — see below) fetches JSON
```

**Recommendation: skip the SPA for v1.** A Jinja2-rendered FastAPI app (server-side HTML,
zero build step, zero frontend framework decision) covers all four pages above and can ship in
the time a build pipeline alone would take to set up. Reach for a React/Next SPA only once the
Repo page needs live-updating state (fix runs in progress) — that's a v2 problem, not v1.

## What this requires from the rest of the codebase

- `src/depfix/api/__init__.py` stops being a stub: add FastAPI, four read routers, Pydantic
  schemas mirroring the SQLAlchemy rows already in `storage/schema.py`.
- Nothing in `core/`, `orchestrator/`, `verify/`, or `classify/` changes — this is additive by
  design, which is exactly the "adapter, not a rewrite" shape the codebase-design skill argues
  for (see `.agents/skills/codebase-design/SKILL.md`): the UI is a new adapter on an existing
  seam (the storage schema), not a reason to touch the pipeline.
- One new dependency: `fastapi` + `uvicorn` (already implied by `docker-compose.yml`'s `app`
  service, which currently runs nothing HTTP).
- `make run-api` / a `depfix serve` CLI subcommand to launch it locally against the same
  `DATABASE_URL` everything else uses.

## Phased build

| Phase | Scope | Effort |
|---|---|---|
| **P0** | `/api/specs` + `/api/specs/:id` JSON endpoints only, no HTML. Proves the read layer works and unblocks `depfix status` (CLI) reusing the same query functions. | ~1 day |
| **P1** | Feed + Spec-detail HTML pages (Jinja2, no auth, local-only). This is the "developer can see the spec with its release note" deliverable the request asked for. | ~2 days |
| **P2** | Provider page + Repo page (the `depfix status` web equivalent). | ~1–2 days |
| **P3** | Deploy behind basic auth for design partners to see their own repo's feed (still single-tenant — one shared login, not per-org accounts). | ~1 day |
| **P4 (post-MVP, needs multi-tenancy)** | Per-org accounts, SSO, the full dashboard from `docs/research/BUSINESS_ASSESSMENT.md` Phase 2. | weeks |

P0–P2 fit inside the existing single-tenant architecture and don't block or get blocked by the
3-day MVP plan — they can run as a parallel track once Day 1–2 of that plan (secret redaction,
CI, hygiene) is done, since this UI would otherwise be the next thing shown to a design partner.

## Open questions for Bedil

1. Jinja2 server-rendered (fast to ship, ugly-by-default) vs. a minimal SPA (nicer, slower to
   start) — the plan above assumes Jinja2 for v1; flag if a nicer front end matters more than
   speed for the pilot demo.
2. Does the design-partner pilot need this before or after the 3-day MVP's Day 3 real-repo
   test? Recommendation: after — Day 3's KEPT-rate numbers are the more urgent proof point.
