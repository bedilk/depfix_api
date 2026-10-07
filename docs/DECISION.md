# GO / NO-GO DECISION

## What this document is for

This file was written **before** the numbers existed, so that the numbers
couldn't be argued with afterwards. Its thresholds are a pre-commitment: a
promise about what evidence would change the plan, made at a moment when
nobody knew which way the evidence would fall.

That only works if one rule holds.

> ### The rule
>
> **Once a measurement exists, the thresholds below do not change.**
>
> If the KEPT rate lands at 58% and the threshold said 65%, the answer is
> "no-go, and here's the failure analysis" — not "65% was always a bit
> arbitrary." A threshold edited after seeing the data has no information in
> it, and neither does any decision made against it.
>
> If a threshold turns out to be *badly specified* (measuring the wrong
> thing, not merely inconvenient), say so explicitly, in writing, with the
> reasoning — and record both the old and new value. Silent revision is the
> failure mode this whole document exists to prevent.

`docs/RESULTS.md` holds the measurements. This file holds only the decision
rule and the verdict.

---

## Current verdict

### ⛔ BLOCKED — no verdict is admissible yet

**Not "no." Not "yes."** The gating measurement does not exist, so neither
answer is available.

| | |
|---|---|
| **Gating metric** | Code-migration KEPT rate |
| **Current value** | `n = 0` — no recorded run has edited application source code |
| **Root cause** | Every fleet run so far resolved to manifest-only dependency drift, handled by a deterministic codemod that never calls an LLM |
| **Unblocked by** | `bash scripts/day3_fork_runs.sh && python scripts/collect_metrics.py --write` |
| **Enforced by** | `python scripts/collect_metrics.py --check` — wired into CI; fails while `n = 0` |

Everything else on the Day-2/3 plan is either done or downstream of this one
number. See `docs/RESULTS.md` for the full status grid.

---

## Pre-committed thresholds

> ⚠️ **Verify these against git history before acting on them.** They are
> restated here for readability. If they differ from the original pre-commit,
> **the original wins** — reconstruct it with `git log -p -- docs/DECISION.md`
> and correct this table. Restating is allowed; revising is not.

### Primary gate — code-migration KEPT rate

The fraction of LLM-generated edits to application source that survive
verification with a `KEPT` verdict. Manifest-only edits **do not count**;
they are produced by a deterministic codemod and would inflate the number
while measuring nothing about the product claim.

| Band | Threshold | Decision |
|---|---|---|
| **GO** | ≥ 65% KEPT | The core loop works. Invest in breadth: more providers, more ecosystems, the PR-grouping schema change |
| **CONDITIONAL** | 40–64% KEPT | The loop works sometimes. Do not add breadth. Spend the next cycle on verification depth and confidence calibration until the rate moves or the ceiling is understood |
| **NO-GO** | < 40% KEPT | The premise is wrong as built. Stop, write the failure analysis, and consider whether the product is "report + suggest" rather than "fix + verify" |

**Minimum n before any band applies: 15 code-migration edits** across **≥ 3
distinct repos**. Below that it's noise, and a `GO` read off 4 edits is worse
than no read at all.

### Secondary gates

Any one of these failing while the primary gate passes means **CONDITIONAL,
not GO**.

| Metric | Threshold | Why it's a gate |
|---|---|---|
| False-KEPT rate | **0** confirmed | A `KEPT` verdict on a broken edit destroys trust permanently. One instance is a stop-the-line event |
| Scanner recall (`call_sites` eval) | ≥ 0.85 | Below this, depfix silently misses call sites and reports success |
| Cost per successful migration | ≤ $0.50 | Above this, a human reviewing the diff is cheaper |
| Secret leakage | **0** instances | Redaction is fail-closed by design. Any leak is a release blocker |
| Wall clock per change | ≤ 5 min p95 | Beyond this it can't run in CI |

---

## Failure-mode log

### F-1 — `release_notes` eval: 0/2 on two models

- **Observed:** identical failures on `gemini-3-flash-preview` and `gemini-2.5-flash`
- **Why it matters:** two models failing the same two cases the same way rules out the model — it's the classifier or the expectations
- **Hypothesis:** `classify/notes.py::_quote_present` rejects correct classifications whose quoted evidence was normalised (whitespace, smart quotes, markdown emphasis)
- **Bearing on the gate:** none directly. But it's a signal that evidence gates may be calibrated too tight
- **Status:** open

### F-2 — Every recorded run is manifest-only

- **Observed:** all plan artifacts on disk edit only dependency declarations; total LLM cost $0.00
- **Why it matters:** this *is* the block. The orchestrator and plumbing are exercised; the product claim isn't
- **Not a bug:** the runs did what they should — the targets had no pending API-surface change requiring a code edit
- **Status:** open — the fix is target selection (`scripts/day3_repos.txt`)

### F-3 — Six `fix_generation` eval cases are `verified: false`

- **Observed:** they run and assert, against recorded fixtures rather than a reviewed live-model run
- **Why it matters:** the green check reads as "the model works" and means "the harness works"
- **Status:** open — must be verified before any `GO`

### F-4 — Pure-agent scanning: 0 call sites for $3.16

- **Observed:** 967K tokens, $3.16, zero call sites found in a 9,536-file repo
- **Why it matters:** it settled the scan architecture — deterministic scan + bounded LLM adjudication (`scanners/judge.py`) is the right shape
- **Status:** **closed** — hybrid scan agent deliberately not built

### F-5 — PR flow never exercised end to end

- **Observed:** `--open-pr` is unit-tested against a mocked GitHub client only
- **Why it matters:** branch naming, body rendering, draft selection and the `uq_pull_request_fix_run` constraint have never run together against real GitHub
- **Status:** open — closes with the first `--open-pr` fork run

---

## What a NO-GO would actually mean

A `< 40%` KEPT rate would **not** mean the work was wasted. It would mean the
verification layer is the product and the fix layer is not. Everything that
survives a no-go:

- The evidence-based verification stack (test identity diff, attribution,
  typecheck fallback, characterisation)
- Watch → classify → scan: a high-precision "this change affects you, here,
  at these call sites" report has standalone value with no LLM required
- Plan/apply artifacts, the fleet orchestrator, the idempotency ledger, redaction

The product under a no-go is **"depfix tells you precisely what broke and
where, and proves it with evidence"** — not "depfix fixes it." Naming it in
advance is what keeps a no-go from being read as failure rather than a finding.

---

## Decision record

To be completed **only** when `python scripts/collect_metrics.py --check`
passes. Do not fill any field from memory or estimate.

```
Date:          ____________________
Decided by:    ____________________
Commit SHA:    ____________________

PRIMARY GATE
Code-migration KEPT rate: ____ % (n = ____ edits, ____ repos)
Minimum n satisfied (≥15, ≥3): [ ] yes  [ ] no  -> if no, STOP: gather more
Band: [ ] GO (≥65)  [ ] CONDITIONAL (40-64)  [ ] NO-GO (<40)

SECONDARY GATES
False-KEPT instances:                   ____ (threshold: 0)
Scanner recall (call_sites):            ____ (threshold: ≥0.85)
Cost per successful migration:         $____ (threshold: ≤$0.50)
Secret leakage instances:               ____ (threshold: 0)
Wall clock p95 per change:           ____s  (threshold: ≤300s)
All secondary gates passed: [ ] yes  [ ] no  -> if no, GO downgrades to CONDITIONAL

VERDICT  [ ] GO  [ ] CONDITIONAL  [ ] NO-GO

Thresholds unchanged since pre-commit: [ ] confirmed via git log -p
(if not: record old value, new value, and the reasoning — in this file)

Reasoning (2-3 sentences, including the strongest argument AGAINST this verdict):


Next cycle commits to:
```

The "strongest argument against" line is not decoration. A verdict recorded
without one is a verdict nobody stress-tested.
