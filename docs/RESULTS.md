# RESULTS

What depfix has actually been measured doing. Claims without a number in this
file are not claims — they are intentions, and they're labelled as such.

**The tables below are generated.** Do not hand-edit anything between the
`BEGIN GENERATED` / `END GENERATED` markers; it will be overwritten. Refresh:

```bash
python scripts/collect_metrics.py --write
```

Everything outside those markers is prose written by a human and is fair game.

---

## Status of the Day 2–3 plan

| # | Task | Status | Evidence |
|---|---|---|---|
| 2.1 | Realistic OpenAI v3 fixture | ✅ Done | `tests/fixtures/openai_v3_verifiable/` — package.json + src + vendored SDK; the integration test asserts both a KEPT and a REVERTED verdict, so the fixture exercises the failure path too |
| 2.2 | Screen recording | ❌ Not done | `scripts/record_demo.sh` exists; no .gif/.mp4 committed |
| 2.3 | Eval corpus ≥30 cases | ✅ Done | 30 cases / 5 files — see composition below |
| 2.4 | Cost/latency table | ✅ Generated | Table below, from the plan artifacts on disk. Read the caveat under it — the numbers are real and they mostly measure the wrong thing |
| 3.1 | Real-repo fork runs | ⛔ Blocked | Harness exists (`scripts/day3_fork_runs.sh`); target list (`scripts/day3_repos.txt`) is deliberately empty pending repo selection |
| 3.2 | PR flow | ✅ Code done, ⛔ unexercised | `gh/pr.py`, `gh/branch.py`, `--open-pr` all implemented and unit-tested; no PR has been opened against a real fork |
| 3.3 | KEPT rate | ⛔ Blocked | Depends on 3.1. See the gating-metric section |
| 3.4 | Go/no-go decision | ⛔ Blocked | Depends on 3.3. `docs/DECISION.md` records the block and the pre-committed thresholds |

Four ⛔s, one root cause: no run has ever edited application source code.
Everything downstream of that is honestly empty.

---

## Earlier measured runs (Sessions 1–2)

### End-to-end: real Gemini fix, real test-suite verification (JS)

Fixture: `tests/fixtures/openai_v3_verifiable`. Change: openai v3→v4
`createModeration` → `moderations.create` incl. response-shape change.

| Run | Outcome |
|---|---|
| Run A (no retry) | Gemini's first fix missed the response-shape change → suite failed → **REVERTED**, failure attributed to the exact file |
| Run B (production config, retry loop) | Fix generated, syntax-validated, suite verified clean → **KEPT**, confidence HIGH. **$0.0016, 77s wall-clock** |

### Python pipeline

Fixture: `tests/fixtures/python_v1_verifiable`. Correct migration → **KEPT**;
broken migration → **REVERTED**. Wall-clock: **11.7s** for both including venv +
pip + 4 pytest runs.

### Eval corpus (gemini-3-flash-preview vs gemini-2.5-flash)

| Category | Cases | gemini-3-flash-preview | gemini-2.5-flash |
|---|---|---|---|
| spec_diff (deterministic) | 7 | 7/7 | 7/7 |
| release_notes (LLM) | 2 | 0/2 | 0/2 |
| fix_generation (LLM) | 6 | 6/6 | 6/6 |
| call_sites (scanner) | 5 | 5/5 | 5/5 |
| **Total** | 20 | **90%** | 90% |
| Cost per full eval | | $0.0114 | $0.0026 |

---

## Eval corpus composition

30 cases across 5 files. This is derivable from the corpus itself and is not
a measurement of model performance.

| Category | Cases | verified: true | Note |
|---|---|---|---|
| spec_diff | 17 | 17 | Deterministic — no model in the loop |
| call_sites | 5 | 5 | Deterministic scanner assertions |
| fix_generation | 6 | 0 | Runs and passes, but `verified: false` is load-bearing — nobody has watched these pass against a live model in this environment |
| release_notes | 2 | 0 | 0/2 on both models — see failure log |
| **Total** | **30** | **22** | 8 unverified |

The 22/8 split: two-thirds tests deterministic code paths, one-third tests
the LLM and nobody has signed off on it.

---

## Known failures

Ordered by how much they should worry you.

### 1. `release_notes`: 0/2 on two different models

Two models failing the same two cases the same way rules out the model —
it's the classifier or the expectations.

Prime suspect: the verbatim-evidence gate in
`classify/notes.py::_quote_present`. It requires the model's quoted evidence
to appear literally in the source text; if the model normalises whitespace,
smart quotes or markdown emphasis, a correct classification is thrown away.

**Next step:** log the rejected quote alongside the source span for both
cases. If they're near-misses, the gate needs normalisation, not loosening.

### 2. No code-migration KEPT rate exists

The product claim is "LLM rewrites a call site and evidence confirms it
survived." Zero recorded runs have done this on a real external repo.
Everything measured so far is manifest-only dependency drift ($0.00, no LLM).

This is why DECISION.md is blocked. It is the right thing to fix first.

### 3. Six `fix_generation` cases are `verified: false`

They execute and assert, but against recorded fixtures. Treat the green
check as "the harness works," not "the model works."

### 4. PR flow never exercised end to end

`--open-pr` is unit-tested against a mocked GitHub client only. Branch
naming, body rendering, draft selection and the `uq_pull_request_fix_run`
constraint have never run together against real GitHub.

---

## How to close the blocked rows

In order. Each step unblocks the next.

```bash
# 3.1 — pick 3-5 real repos that import a watched SDK.
#        scripts/day3_repos.txt explains how to choose honestly.
$EDITOR scripts/day3_repos.txt

export GITHUB_TOKEN=...            # repo + fork scope
export DEPFIX_FORK_ORG=bedilk
bash scripts/day3_fork_runs.sh     # add --open-pr for 3.2 evidence

# 2.4 + 3.3 — regenerate every table from what the runs produced
python scripts/collect_metrics.py --write

# gate: refuses to pass while the KEPT rate has n=0
python scripts/collect_metrics.py --check

# 3.4 — only now is a verdict admissible. Fill DECISION.md's verdict block
#        WITHOUT touching its thresholds.
$EDITOR docs/DECISION.md
```

If `--check` fails, DECISION.md stays blocked. That's enforced in CI, not
left to discipline.

---

## Reading the generated tables

Three conventions:

1. **Coverage is reported as n/corpus.** A mean over 3 of 57 artifacts is not
   a mean over 57, and writing it as one is how measurement records start lying.
2. **Zero and absent are different.** $0.00 across artifacts that have a cost
   field is a finding. A blank is a bug in the artifact schema.
3. **NOT MEASURED always carries its unblock command.** A `_(pending)_` with no
   owner and no next action is how a gap survives three weeks.

Run `python scripts/collect_metrics.py --write` to populate this block.

<!-- BEGIN GENERATED: collect_metrics.py -->

### Plan artifacts on disk

Source: `.depfix/plans` — **57** artifact(s) parsed.

| Class | Count | What it proves |
| --- | ---: | --- |
| Manifest-only (dependency declaration edits) | 50 | orchestration, idempotency, PR plumbing |
| Code-migration (touches application source) | 7 | **the actual product claim** — LLM rewrites a call site and it survives verification |

### Cost and latency

| Metric | n (coverage) | Total | Mean | p50 | p95 |
| --- | --- | ---: | ---: | ---: | ---: |
| Cost, all plans | 0/57 | $0.0000 | — | — | — |
| Cost, code-migration only | 0/7 | $0.0000 | — | — | — |
| Tokens, all plans | 0/57 | 0 | 0 | 0 | 0 |
| Wall clock per plan | 0/57 | 0.0s | — | — | — |

### Edit verdict distribution

No edit verdicts recorded in any artifact.

### Code-migration KEPT rate — the gating metric

**NOT MEASURED** — no plan artifact contains an edit to application source code, so there is nothing to compute a KEPT rate over<br>_unblocked by:_ `bash scripts/day3_fork_runs.sh`

### Real-repo fork runs

**NOT MEASURED** — `.depfix/day3/runs.jsonl` is absent — no fork run has been recorded<br>_unblocked by:_ `bash scripts/day3_fork_runs.sh`

### Eval corpus

**NOT MEASURED** — `.depfix/eval/report.json` is absent — the corpus exists (30 cases) but no scored run has been persisted<br>_unblocked by:_ `depfix eval --report .depfix/eval/report.json`

<!-- END GENERATED -->
