# depfix — revised development plan

Supersedes the previous 7-week "npm poller → PR" plan. Rewritten after strategic
review; see `## Why this plan changed` at the bottom for the reasoning.

## Ship criterion

> An unattended, correct PR for an **HTTP API change** that no existing tool
> (Renovate, Dependabot) could have detected.

Not "a correct PR for a semver bump." Renovate already does that. If we can't
answer *"why doesn't Renovate just add an LLM?"* in one sentence, we don't have
a product.

## Wedge

**Versioned SaaS APIs that ship an SDK.** Flagship: Stripe and OpenAI (TS/JS
only). Then Anthropic, Twilio, Supabase, Plaid.

Why this specific intersection:

- **SDK import gives a grep anchor** — detection is tractable.
- **API version is a real change signal** — dated versions, changelog posts,
  OpenAPI spec diffs. There is no `package.json` line that says
  `Stripe-Version: 2023-10-16`, so no registry watcher touches it today.
- **Blast radius is revenue, not utility functions.** A silent Stripe or
  OpenAI break is a P0 — this is a pain people will pay to remove.

Flagship demo is **OpenAI Node SDK v3 → v4**. Kill the lodash fixture; nobody
has been paged by a lodash upgrade.

## Business model — build one, sell the other

- **Consumer-side (build first):** self-serve, bottoms-up. Proves the tech and
  accumulates a corpus of how real codebases use each provider.
- **Provider-side (sell):** Stripe/OpenAI/etc. pay us; we distribute fixes to
  their customers. Single sale, many repos, vendor has both changelog and
  motivation.

Non-obvious linkage: running the consumer side is what makes the provider pitch
land — *"400 repos in our index break on this change; here are the three call
patterns you didn't consider."* Don't build the provider console for v1, but
keep the data model able to answer that question.

## Revised weeks

### Week 1 — Change sources (generalize, don't specialize)

- Build `sources/` where a `Provider` has N change feeds:
  GitHub Releases, `CHANGELOG.md`, RSS/Atom, an HTML changelog page,
  an OpenAPI spec URL.
- **Stripe and OpenAI publish OpenAPI specs — spec diffing gives structured
  breaking changes for free**, which no changelog parser will match. Same week
  of work as the npm-only poller, saves a Week 4 rewrite.
- Ship with the flagship provider (OpenAI or Stripe) wired end to end.
- Retire the npm-only poller as *one* source among many, not the trunk.

### Week 2 — Eval corpus + change classifier

- **Corpus first.** 20–30 real breaking changes with known-good fixes, mined
  from migration guides and the commits that closed them. Automated scoring
  harness. **This is the asset that compounds** — every prompt/model/retry
  change gets a number.
- Then: `notes / spec-diff → BreakingChange` classifier.
- Delete the demo-shaped code in `cli.py` that assumes an npm event trigger.

### Week 3 — Repo access + call-site scanner (needs the whole week)

- GitHub App tokens (least privilege — see Trust posture below), shallow clone.
- Hard part: **finding API call sites**. Not `import stripe` — `stripe.charges.create`,
  thin internal wrappers, raw `fetch` to `api.stripe.com`, config-driven version
  pins.
- **Measure recall against the corpus.** If < ~85%, that's the trigger to
  bring in tree-sitter. Don't gate the whole plan on AST work up front; do
  gate it on numbers.

### Week 4 — Fix generation + validation

- Apply patches in place (not "output diffs to stdout").
- **Validation stack — explicit engineering time:**
  1. Type-check against the new SDK's `.d.ts` / new OpenAPI spec. Cheap,
     catches most shape changes.
  2. Run the repo's test script. **State plainly** in the PR body that repo
     tests almost always mock the provider — they will pass while production
     burns.
  3. Provider sandbox / test-mode keys where they exist (Stripe test mode is
     the gift). Recorded fixtures replayed against the new spec.
- For v1: type-check + tests + evidence in the PR body, and let the human
  close the last-mile gap deliberately. Do not pretend the mocked test suite
  proves anything.
- Score every run against the Week-2 corpus.

### Week 5 — Retry loop + PR

- Feed validator errors back to the LLM; bounded retries.
- Branch, commit, open PR.
- **PR body is the product surface:** the change, the source link (spec diff
  or changelog excerpt), the migration guide excerpt, what was verified and
  what wasn't, confidence score, and every call site touched.

### Week 6 — Orchestration

- Event → analyze → clone → scan → fix → validate → PR.
- `.depfix.yml` in target repos.
- Idempotency (don't re-open a PR for the same change twice).

### Week 7 — Design partners + demo artifact

- Three repos we don't control.
- Record the demo we actually apply with: **unattended detection of a real
  provider change → PR → merged by someone else.** That's the artifact for the
  YC application.

## Trust posture (design in now, expensive to retrofit)

Design partners will ask on call one:

- Least-privilege GitHub App permissions.
- Diffs strictly scoped to matched call sites. No unrelated edits.
- Never read secrets; never send secret material to the LLM.
- Explicit statement of what leaves the repo and what the LLM provider sees.

## Cut list

Keep the previous cuts. Two amendments:

- **tree-sitter:** moves from *"defer indefinitely"* to *"gated on Week 3
  recall numbers."*
- **Dashboard:** stays cut for v1. But the **provider-side blast-radius view**
  is the eventual product surface, so don't foreclose it in the data model.

## Why this plan changed

The old plan built a better Renovate: npm poller, version event, semver diff,
PR. Every one of those artifacts is a thing Dependabot / Renovate already do,
at scale, for free, with GitHub distribution. If we ship exactly that, the
first YC question is *"why doesn't Renovate just add an LLM?"* and there's no
answer.

The real gap is HTTP APIs:

- No manifest entry to diff (nothing in `package.json` pins `Stripe-Version`).
- No semver signal — changes arrive as dated API versions, changelog posts,
  deprecation emails.
- Blast radius is revenue.
- **Nobody automates this today.**

The old detection layer assumed a registry event as the trigger. For APIs
there is no registry event. That is the architectural fork, and it hits at
Week 1 Day 5. Better to take it now than to rewrite in Week 4.
