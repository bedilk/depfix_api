# depfix — Honest Business Assessment

*Compiled 2026-09-17 from four parallel research passes (competitor landscape, pricing, market viability, codebase audit). Sources cited in the appendices of each section.*

---

## 0. The verdict up front

**This is not a $100B idea. It is also not "just another open-source solution" — it is better engineered than most of the category, but it sits directly in the blast radius of features the platforms are shipping for free.**

The calibrated view:

- **Realistic best case:** a **$5–30M acqui-hire** by a platform (GitHub/GitLab, a CI vendor, an AI-coding company, or a security-platform roll-up like FOSSA). This is literally the precedent: both prior exits in this exact niche — Dependabot (→ GitHub, 2019) and Renovate (→ Mend, 2019) — were undisclosed, feature-level acquisitions. The closest direct analog to depfix, **EdgeBit** (YC-backed, built almost exactly this pipeline for JS/TS), was acquired by FOSSA in **September 2025, within ~1 year of its $3.1M seed**.
- **Likely case:** a niche tool with modest paid adoption — low hundreds of thousands to low single-digit millions ARR — serving teams that want a sharper, more automated experience than Dependabot/Renovate and don't yet trust a general agent unsupervised.
- **Worst case:** functionally obsoleted in 12–18 months as GitHub's April 2026 "assign Dependabot alerts to an AI agent" feature and its equivalents mature — free, bundled, already installed where the repos live.

The window to matter is open but closing. What determines the outcome is in §7.

> **Addendum (same day):** a follow-up research pass on the broader "all-services-in / integration reliability" framing (§2b) found that the *vision* targets genuinely unclaimed whitespace beyond EdgeBit's package-scoped lane — no one chains the full pipeline for third-party SaaS APIs today. That raises the ceiling of the niche-SaaS branch and sharpens the acquisition story, but it raises the engineering bar too; the outcome distribution above still stands.

---

## 1. What we have now

**Product:** `depfix` v0.2.0 (self-classified Alpha). ~18.6k lines of Python, ~7.7k lines of unit tests (442 test functions), 17 commits, ~1 month of visible history, single developer.

**The pipeline is real, not vaporware.** End to end and actually wired:

1. **Watch** — polls npm dist-tags, GitHub releases, and OpenAPI specs from `providers.yaml` (20 providers; changelogs/RSS declared but unimplemented) into Postgres.
2. **Classify** — OpenAPI spec diffs classified deterministically (no LLM, confidence 1.0); prose release notes classified by LLM behind a **verbatim-quote anti-hallucination gate** (a proposed change is dropped if its evidence can't be found in the source text).
3. **Scan** — call-site detection across a local checkout or a repo shallow-cloned via a least-privilege GitHub App token, with HIGH/MEDIUM/LOW confidence per usage.
4. **Fix** — LLM-generated edits (Gemini or local Ollama), applied transactionally to a disposable copy.
5. **Verify** — installs the target repo's own deps and runs its **real test suite** before/after each edit; new failures are attributed to a specific file (import graph → filename convention → sole-candidate elimination) and only the guilty edit is reverted; bounded retry loop feeds the concrete failure back to the LLM.
6. **PR** — verified edits pushed to a deterministic branch, PR opened with evidence links and an honest confidence tier; idempotent across reruns.

Plus a Terraform-style `init`/`plan`/`apply` flow (immutable plan artifacts with base-SHA staleness checks; `apply` never re-calls the LLM) and a fleet orchestrator with per-repo opt-in (`.depfix.yml`), an idempotency ledger, advisory locks, cost budgets, and failure isolation.

**Genuine engineering strengths** (audit's words: "above the bar for Alpha"):
- The verify/attribution/confidence-tier design is the best trust-engineering in the codebase — it models "how much can we prove," not "how good does the fix look," and auto-drafts low-confidence PRs.
- Least-privilege GitHub App tokens (per-scan, single-repo, contents:read, ~1hr), env-allowlisted sandbox for running hostile `npm test`, hash-pinned lockfile, non-root Docker, Alembic migrations that handle legacy schemas.
- Fail-closed repo config parser; `--force` can never bypass a tenant's own `.depfix.yml`.

**What it is not:**
- **CLI + cron only.** `api/` and `workers/` are one-line placeholder stubs; Redis is provisioned but unused. No web UI, no dashboard — operators read Postgres with `psql`.
- **Single-tenant, totally.** No tenant/org/customer concept anywhere in schema, config, or CLI. No auth, no accounts, no billing.
- **JS/TS only**, Gemini/Ollama only. Of 20 providers, only openai/stripe/anthropic have tuned call-site anchors and only openai/stripe have OpenAPI feeds (the high-fidelity path); the other 17 ride version-bump signals, which are weak proxies for "breaking."
- **Trust gap between docs and code:** the plan docs promise "never send secret material to the LLM," but `fixers/gemini.py` sends **entire file contents** to Gemini with no redaction. Sandbox is process-level, not container/VM. README documents a CI workflow (`.github/workflows/ci.yml`) that does not exist in the tree; `.env.example` referenced but absent; a 6,362-line raw chat transcript (`changes.txt`) and real run artifacts (`output/`) are committed at the repo root; `pyproject.toml` URLs are placeholders. A technical buyer notices all of this in the first ten minutes of diligence.
- **Naming collision:** an unrelated OSS project `agent0ai/depfix` (Python dependency isolation) launched under the same name in August 2026.

**Honest one-liner:** a genuinely well-engineered single-operator CLI prototype of the right pipeline — with zero product surface, zero tenancy, and a data-privacy promise it doesn't yet keep.

---

## 2. Who else is doing this

### Closest competitors

| Who | What | Status |
|---|---|---|
| **EdgeBit** | *The* closest match: watched releases, call-graph call-site detection, AI fix investigation, ran the repo's real CI, auto-PRs with risk routing. JS/TS. | **Acquired by FOSSA, Sept 2025** (~1 yr after $3.1M seed); now a feature ("fossabot") inside FOSSA's SCA platform, not a standalone company. |
| **GitHub Dependabot + AI agent** | Since **April 2026**, Dependabot alerts are assignable to Copilot/Claude/Codex, which inspect usage, open a PR, and iterate on test failures. Security-alert-triggered, opt-in, gated behind Code Security + Copilot plans. | Free-ish/bundled, owns distribution (846k+ repos on Dependabot, ~15M merged Dependabot PRs in 2024). The commoditization clock. |
| **Infield.ai** | Dependency-upgrade risk scoring + upgrade sequencing (Ruby/JS/Python). Deliberately does **not** generate fixes or run tests — humans/white-glove do the work. | $3M seed (2024), ~5 people, not a breakout after ~3 years. |
| **AWS Transform** | Agentic modernization; added **Node.js/npm version upgrades and AWS SDK v2→v3 migration in 2026**. Invoked, not event-driven. | Active, expanding. (Amazon Q code transformation being retired into it.) |
| **bumpgen** (OSS) | TS-only; attaches to Dependabot/Renovate PRs, AST + type-diff + GPT-4 fixes. Reactive only, ~45% success, ~146 stars. | Proof the technique is public knowledge. |
| **Snyk Agent Fix / Copilot Autofix** | Real agentic LLM autofix loops (Autofix even re-verifies before PR) — but triggered by **security alerts/CodeQL patterns**, not SDK breaking changes. | Structurally similar loop, different trigger and problem. |
| **Moderne/OpenRewrite, Codemod.com, ast-grep, jscodeshift** | Transformation *engines* — human-authored/selected recipes, no upstream watching, no autonomous trigger. Moderne's tech is already embedded in Amazon Q, MS Copilot, Broadcom tooling. | Moderne $50M raised; Codemod early; Grit.io absorbed into Honeycomb (2025). |
| **Dependabot / Renovate / Snyk / Mend / Socket** | Version bumps, alerts, semver heuristics, crowd-sourced merge confidence. **None scan call sites or fix code as core behavior.** | The free floor every buyer already has. |

Academic state of the art has caught up too: FSE 2025 "Automatically Fixing Dependency Breaking Changes," Byam (2025), DepRepair + DepBench benchmark (2026), Google's FSE 2025 paper (74% of change lines in 595 real internal migrations LLM-generated), Amazon's claimed 4,500 dev-years / $260M saved on Java upgrades. The problem is legible, published, and being industrialized by the biggest players.

### What's actually differentiated

No single stage is novel — watching exists (Renovate), call-site analysis exists (OpenRewrite/bumpgen), LLM fixing exists (Snyk/Copilot/Codemod), verify-then-PR exists partially (Copilot Autofix, EdgeBit). The differentiation is that **almost nobody still operating independently chains all six stages, cross-signal (dist-tags + releases + OpenAPI diffs), with fixes validated against the target's own real test suite.** EdgeBit did, and got bought. That's the whole strategic picture in one sentence: the pipeline is validated, and validated pipelines in this niche get absorbed, not IPO'd.


### 2b. "But we're not just GitHub deps — we watch *all* the services." Does that change the picture?

**Yes, meaningfully — a dedicated research pass on the "integration reliability" landscape confirms the seam is real and currently unoccupied.** EdgeBit is the right comparison for what's *shipped* (repo-level JS/TS fixes delivered as GitHub PRs, mostly on package-release signals); it is *not* the right comparison for what's *designed* (watching heterogeneous third-party service APIs — specs, changelogs, deprecations — across a company's whole integration estate). Verified findings, late 2026:

**Nobody chains the full pipeline for third-party SaaS APIs.** Watch third-party specs → classify breaking → find call sites in consumer code → LLM-fix → verify with tests → PR: aggressive search found no product, OSS project, or paper that does all six for networked APIs (academic API-evolution work is decades deep but library-scoped, and even there only ~27% of migrations proved fully automatable in one empirical study). The closest analog anywhere is **mendapi** — OSS, "Dependabot for every API you depend on," ~20 hardcoded providers, pre-1.0, **1 GitHub star**, and deliberately no test-verification step (the exact step depfix leads with).

**The three adjacent categories are structurally unable to fill the seam:**
- **Unified-API companies** (Merge $75M raised, Apideck, Nango, Kombo $30M, Paragon…) literally sell "never worry about integrations breaking" — proof the pain is monetizable — but only where vendors are interchangeable (HRIS, CRM, ATS, accounting). Nobody routes Stripe payment intents or OpenAI completions through an abstraction proxy; they have implicitly ceded exactly depfix's provider list.
- **Provider-side SDK companies consolidated away instead of moving consumer-side:** Stainless → Anthropic (May 2026, ~$300M reported/unconfirmed; hosted product wound down, leaving its customers *without* "SDK stays synced with the API" — APIMatic is openly courting the displaced); liblab → Postman (Nov 2025); Speakeasy and Fern show no consumer-side migration tooling.
- **API-diff tooling is provider-side or dead:** Optic acquired by Atlassian (2024) and shelved (repo archived Jan 2026); Bump.sh/Treblle serve API *owners*; oasdiff documents a "monitor a third-party API you don't control" workflow — the closest documented match to depfix's own use case — but stops at diff-and-alert, as do the small consumer watchers (ApiNotes.io, APIDrift, an Apify "Breaking-Change Impact Radar" actor).

**Who holds the most pieces:** Postman (Live Insights traffic-based drift detection from the Akita acquisition + liblab SDK gen) — unassembled. Watchlist: mendapi if it gains traction; Apideck/Merge if they extend "we monitor breaking changes" beyond interchangeable-vendor categories; APIMatic as a migration-services vendor post-Stainless.

**The pain is documented — but bought as consulting today, not product** *(this evidence set is from training knowledge, flagged for spot-checking)*: Stripe's dated API versions and migration sprints; Twilio sunsets and A2P 10DLC deadlines; Google Maps' 2018 pricing/API shock; Salesforce's rolling API retirements (SIs sell the upgrade work as standard billable engagements — the clearest "API version upgrade as a service" in existence); PSD2/Open Banking churn (which spawned Plaid/TrueLayer/Tink precisely where abstraction *was* possible); Slack's classic-app sunset; OpenAI's own endpoint/model retirements.

**Why the whitespace is unoccupied — four hard problems the framing inherits:**
1. **Most vendors publish no (or unreliable) OpenAPI spec.** The deterministic-diff edge — depfix's best idea — applies to a minority of the "dozens of services"; everything else falls back to prose changelogs and LLM classification.
2. **Raw REST calls are nearly invisible to static scanning.** `fetch("https://api.vendor.com/v2/…")` with a hand-built URL gives a scanner almost nothing compared to `import Stripe from "stripe"`. This is materially harder than the dependency-bot problem.
3. **Static analysis misses runtime-constructed and feature-flagged calls.** The strongest version of this product likely needs an optional traffic/proxy component (the Akita/Live Insights approach) — a roadmap question, not a detail.
4. **The flagship verify step can pass trivially exactly where it matters most.** Customer test suites usually *mock* external APIs, so "the test suite still passes" may prove nothing about the fix working against the real, changed API. Nobody else has solved this either — but it's depfix's headline claim, so it's depfix's problem to solve (contract tests, recorded-traffic replay, or sandbox-account smoke calls).

**And the shipped-today caveat stands:** 2 of 20 providers have OpenAPI feeds wired; remediation is still repo-scoped JS/TS via a GitHub App. The broader framing is a *design truth* and a *roadmap claim*, not yet a *product truth*.

**Net effect on the verdict:** the "all-services-in" framing is not marketing repositioning — it names a real seam between three well-funded categories that can't fill it (one research thread even surfaced YC's 2026 Request for Startups reportedly flagging this exact space — unverified). It upgrades the strategic story from "better Dependabot" to "the integration-reliability layer nobody built," strengthens the data-asset thesis (the corpus becomes cross-provider *API change intelligence*, spec + changelog + fix-outcome), and makes the AI-provider wedge (OpenAI/Anthropic/Stripe — vendors that *do* publish specs and churn fastest) the right proving ground. What it does **not** do is lower the bar: the four problems above are why the seam is empty, and the outcome distribution in §0 is unchanged until at least one of them is demonstrably solved.


---

## 3. The final destination

What "shipped to customers" looks like if we go all the way — a **verified, event-driven code-maintenance service**:

- **Continuous, not invoked:** upstream releases trigger it; customers wake up to a verified, evidence-linked, confidence-labeled PR — the thing general agents structurally don't do (they wait to be asked).
- **Trust as the product:** proven merge rates, per-provider accuracy stats, honest confidence tiers, immutable plan artifacts for human review, container-grade sandboxing, secret redaction, and a policy-enforceable "code never leaves the VPC" mode (Ollama/self-hosted inference as a contractual guarantee, not an option).
- **A data asset:** a continuously-curated, labeled corpus of (provider, version, breaking change, fix pattern, real-world merge outcome) across hundreds of SDKs. This — not the pipeline code — is the only thing in this category that has ever justified a premium (Socket's malware corpus → $1B valuation). It is also the thing an acquirer can't rebuild quickly.
- **Distribution where repos live:** one-click GitHub Marketplace install (depfix-owned App, per-tenant installations), dashboard, plans, billing.
- **Multi-tenant SaaS + enterprise mode:** SOC2, SSO/RBAC, audit trail, DPA with Gemini (or enforced local inference), Helm/Terraform self-host, SLAs.
- Eventually: beyond JS/TS (Python next — the AI-SDK churn wedge is polyglot), and more OpenAPI-grade high-fidelity feeds instead of version-bump proxies.

**The realistic strategic destination, stated honestly:** build the above far enough to prove merge-rate data and accumulate the breaking-change corpus, and become *the obvious acquisition* for GitHub/GitLab/FOSSA-class buyers — or, if traction outruns the platforms, a niche SaaS at $1–20M ARR. Planning for a standalone $100B outcome would require a compliance-mandated budget category (like security scanning) that "fixes breakage faster" simply is not.

---

## 4. What must be done to get there

### Phase 1 — Credibility + design partners (weeks; ~0 cost beyond time)
1. **Close the trust gap first:** implement secret/credential redaction before any code goes into an LLM prompt (`fixers/gemini.py::_build_prompt`); make "what leaves the repo" an auditable, enforced statement. This is design-partner question #1.
2. **Repo hygiene for diligence:** restore or stop documenting CI; add real `.github/workflows/ci.yml`; delete/relocate `changes.txt` and committed `output/` artifacts; fix placeholder URLs; add `.env.example`.
3. **Rename.** `depfix` is already taken by an unrelated OSS project (Aug 2026). Fix before any public motion.
4. **Minimal visibility:** a `depfix status` table or read-only page so a partner's eng lead doesn't need `psql`.
5. **Widen the eval corpus** (~20 cases today) around the providers your 3 pilot companies actually use; publish the numbers. Merge-rate data is your entire future negotiating position.
6. Turn `docs/onboarding.md`'s dry-run → no-PR → draft → live ladder into a runbook a non-founder can execute.
7. **Recruit 3 design partners** — mid-size JS/TS-heavy teams using OpenAI/Stripe/Anthropic SDKs (the three providers with tuned anchors). Free, in exchange for merge-rate data and logos.

### Phase 2 — Self-serve SaaS (months; requires funding or serious runway)
1. **Multi-tenancy end to end** — tenant id on every table, per-tenant credentials/config isolation.
2. **Web app** — signup/login, connect repos, review plans/diffs, approve PRs, history. The largest missing surface; the `api/` stub becomes real.
3. **GitHub Marketplace listing** — depfix-owned App, 3-click install, kills the bring-your-own-App barrier.
4. **Real queue/workers** — Redis is already provisioned; the one-shot-cron model caps out at tens of repos.
5. **Billing + metering** — per-run cost primitives already exist; roll up per tenant.
6. **Add a non-Google fixer** (Anthropic/OpenAI/Azure) — many buyers can't or won't send code to Gemini.

### Phase 3 — Enterprise (quarters; only if Phase 2 shows pull)
1. SOC2 (Type I in progress minimum), real audit logging, RBAC, SSO/SAML.
2. **Enforced-local-inference mode** — an org-level policy that *forbids* cloud LLMs, built on the existing Ollama path; Helm/Terraform for full in-VPC deployment.
3. Container/VM-grade sandboxing for the verify step (gVisor/Firecracker) — AppSec will probe "hostile `npm test`" on call one.
4. DPA + subprocessor list covering the LLM provider.
5. Language breadth (Python, then Java/Go) behind a real abstraction — big-tech stacks are never pure JS/TS.
6. SLA/status page/incident process on top of the existing Sentry + structured logging.

---

## 5. Pricing

Comparables (verified late-2026): Snyk $25–105/dev/mo; Socket $25–50/dev/mo; Mend Renovate Enterprise ~$150–400/dev/yr blended; GitHub Code Security $30/committer/mo; Copilot Enterprise ~$60/user/mo effective; Cursor Teams $40–120/user/mo; Devin $2–2.25/ACU; CodeRabbit $24–72/user/mo + $0.25/file; Codemod Team $1,000/mo flat; Moderne/Infield enterprise-negotiated, unpublished. Dependabot/Renovate: **$0, bundled — the free floor.**

**Recommended structure:**

- **Launch (Option A): per-repo tiers** — the unit of work is the repo, not the seat.
  - Starter: **$49–99/repo/mo** (self-serve, capped fix-PRs/mo)
  - Growth: **$150–250/repo/mo** (unlimited verified fixes, priority, integrations)
  - Enterprise: custom, ~$75–150/repo/mo at 100+ repos, annual, SSO/SOC2/self-host.
- **Add later (Option B): outcome pricing** — $500–2,000/mo platform fee + **$25–100 per merged, test-passing fix PR**, tiered down with volume. Anchor: a senior dev at $125–250/hr spending 1–2 hrs per manual fix = $125–500 of avoided labor per fix — price at a 3–10× discount to that. No incumbent prices this way; it's the differentiator, but only sellable once merge-rate data exists, and it must be capped/forecast to avoid Greptile-style bill-shock backlash.
- **Largest accounts (Option C): Moderne-style annual contracts** scaled by codebase footprint — illustrative **$150–400K/yr for a 500-repo JS/TS enterprise**.

**Market size (order-of-magnitude only):** software supply-chain security platforms ~$5.5B (2025) → ~$10B (2030); enterprise AI coding agents ~$10–11B run-rate (2026). Bottom-up SAM for "JS/TS automated breaking-change remediation sold to GitHub-Enterprise-grade orgs": **roughly $250M–1.5B/yr** (~15–19k orgs × $15–100K ACV). Note what that arithmetic says: even owning the entire wedge is a sub-$2B revenue category — that alone rules out $100B; expansion beyond JS/TS and beyond "upgrades" would be required for anything bigger.

---

## 6. Why not $100B — the structural case, honestly

1. **The platforms are absorbing the wedge.** GitHub shipped watch→classify→fix→verify on Dependabot alerts in April 2026, bundled where the repos already live. AWS Transform added npm migrations in 2026. Moderne's engine is already inside Amazon Q and MS Copilot.
2. **General agents commoditize the mechanism.** Claude Code / Copilot agent / Cursor with a good prompt already do "upgrade this dep and fix breakage" credibly (Google: 74% of migration change lines LLM-generated internally; Amazon: 4,500 dev-years saved). What they don't do is *watch and self-trigger* — that's the surviving wedge, and it's narrower than the pitch deck version.
3. **It's a productivity budget, not a compliance budget.** Snyk peaked at $8.5B and is worth well under half that; the only up-only story (Socket, $1B) is built on a proprietary *security* dataset feeding compliance mandates. "Fix breakage faster" competes with the same budget line as Copilot seats.
4. **Category history:** two exits (Dependabot, Renovate), both modest feature acquisitions, both products then given away free. The closest clone (EdgeBit) exited to FOSSA inside a year. The on-thesis startup (Infield) is still 5 people three years in.

**The three factors that decide which outcome we get:**
1. **A data/trust asset the platforms don't have** — the labeled breaking-change + fix-outcome corpus. Without it, this is a UX wrapper on a capability being absorbed.
2. **Distribution/usage before the free platform feature matures** — install base and merge-rate case studies are what turn "acqui-hire maybe" into "acquisition definitely." The clock started April 2026.
3. **Picking a lane general agents are structurally weak at** — event-driven verified remediation for a specific high-churn, high-value ecosystem (AI-provider SDKs is actually a good one: OpenAI/Anthropic/Stripe churn fast and the tuned anchors already exist) rather than "any SDK, any language."

---

## 7. Recommendation

Run it as a **12-month, milestone-gated bet on the acqui-hire/niche-SaaS band**, not a $100B raise-and-blitz:

1. **Now–6 weeks:** Phase 1. Rename, close the trust gap, hygiene, eval corpus, 3 design partners on the AI-SDK wedge (OpenAI/Anthropic/Stripe). Cost ≈ time.
2. **Gate 1 (month 3):** do design partners merge ≥50% of verified PRs and voluntarily keep it running? If no — open-source it, keep it as a portfolio piece, stop investing. If yes:
3. **Months 3–9:** Phase 2 (tenancy, dashboard, Marketplace, queue). Start charging Option A. Accumulate the corpus and publish accuracy numbers loudly — the corpus and the merge-rate data *are* the company.
4. **Gate 2 (month 9–12):** paying repos growing and platform features still behind on accuracy? → raise or push toward $1M ARR niche SaaS. Platforms caught up? → sell the team + corpus while both still clear the bar. Either branch of that gate is a *good* outcome for a one-month-old solo project; only refusing to pick one is bad.

The engineering is real — the confidence-tier/attribution/plan-apply work is better than most of the funded competition shipped at seed stage. That earns the right to run the experiment. It does not repeal the category's history: in this niche, excellent pipelines get *acquired*, datasets get *valued*, and everything else gets *bundled away for free*.
