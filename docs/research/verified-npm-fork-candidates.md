# Verified npm fork candidates for Depfix

Date: 2026-09-18

These five are public, active (not archived) downstream applications or
templates. They are not SDK source repositories. Each was checked against the
repository's GitHub metadata, its own npm manifest, and a source call site.
They are candidates to fork and test; no forks or pull requests were created
while preparing this note.

## Fork and test order

Start with the OpenAI project. Its pinned v3 SDK and `OpenAIApi` /
`createChatCompletion` call are a known, concrete v3-to-v4 migration target.
The other four validate detection and scanning first; a live `apply` must only
follow a reviewed, verified plan.

| Priority | Upstream | Depfix provider | Evidence | Fork command | First Depfix command |
|---:|---|---|---|---|---|
| 1 | [`warrenshiv/AIEventPlanner`](https://github.com/warrenshiv/AIEventPlanner) | `openai` | [`openai` `^3.2.1`](https://github.com/warrenshiv/AIEventPlanner/blob/main/package.json) and the legacy [`Configuration`, `OpenAIApi`, `createChatCompletion`](https://github.com/warrenshiv/AIEventPlanner/blob/main/pages/api/openai.js) call. | `gh repo fork warrenshiv/AIEventPlanner --fork-name depfix-openai-ai-event-planner --clone=false` | `depfix scan --repo bedilk/depfix-openai-ai-event-planner --provider openai` |
| 2 | [`stripe-samples/checkout-one-time-payments`](https://github.com/stripe-samples/checkout-one-time-payments) | `stripe` | The nested Node app declares [`stripe` `^20.2.0`](https://github.com/stripe-samples/checkout-one-time-payments/blob/main/server/node/package.json) and its server calls [`stripe.checkout.sessions.create`](https://github.com/stripe-samples/checkout-one-time-payments/blob/main/server/node/server.js). | `gh repo fork stripe-samples/checkout-one-time-payments --fork-name depfix-stripe-checkout --clone=false` | `depfix scan --repo bedilk/depfix-stripe-checkout --provider stripe` |
| 3 | [`anthropics/claude-quickstarts`](https://github.com/anthropics/claude-quickstarts) | `anthropic` | Its `customer-support-agent` app declares [`@anthropic-ai/sdk` `^0.27.1`](https://github.com/anthropics/claude-quickstarts/blob/main/customer-support-agent/package.json) and calls [`anthropic.messages.create`](https://github.com/anthropics/claude-quickstarts/blob/main/customer-support-agent/app/api/chat/route.ts). | `gh repo fork anthropics/claude-quickstarts --fork-name depfix-anthropic-quickstarts --clone=false` | `depfix scan --repo bedilk/depfix-anthropic-quickstarts --provider anthropic` |
| 4 | [`vercel/chatbot`](https://github.com/vercel/chatbot) | `vercel-ai` | The application declares [`ai` `7.0.15`](https://github.com/vercel/chatbot/blob/main/package.json) and calls [`streamText`](https://github.com/vercel/chatbot/blob/main/app/%28chat%29/api/chat/route.ts). | `gh repo fork vercel/chatbot --fork-name depfix-vercel-chatbot --clone=false` | `depfix scan --repo bedilk/depfix-vercel-chatbot --provider vercel-ai` |
| 5 | [`clerk/clerk-nextjs-onboarding-sample-app`](https://github.com/clerk/clerk-nextjs-onboarding-sample-app) | `clerk` | The sample declares [`@clerk/nextjs` `^6.12.8`](https://github.com/clerk/clerk-nextjs-onboarding-sample-app/blob/main/package.json) and uses [`auth()` from `@clerk/nextjs/server`](https://github.com/clerk/clerk-nextjs-onboarding-sample-app/blob/main/src/middleware.ts). | `gh repo fork clerk/clerk-nextjs-onboarding-sample-app --fork-name depfix-clerk-onboarding --clone=false` | `depfix scan --repo bedilk/depfix-clerk-onboarding --provider clerk` |

All five GitHub repositories reported `archived: false` on 2026-09-18. The
source citations above are the primary evidence for npm dependencies and SDK
call sites.

## Safe per-repository procedure

Install the GitHub App on the newly created fork before running Depfix. Then
run these commands one at a time, replacing the repository and provider with a
row from the table:

```bash
depfix scan --repo bedilk/depfix-openai-ai-event-planner --provider openai
depfix plan bedilk/depfix-openai-ai-event-planner --provider openai
depfix apply bedilk/depfix-openai-ai-event-planner
depfix pr list --repo bedilk/depfix-openai-ai-event-planner
```

`scan` confirms actual call sites, `plan` generates and verifies proposed
edits without a PR, and `apply` should be used only after reviewing that plan.
The Stripe sample has no real test script, so treat it as scan/plan-only until
you add a mocked test. The Vercel and Anthropic projects need their documented
environment variables for a full application test; absence of credentials
should not be bypassed by opening an unverified PR.
