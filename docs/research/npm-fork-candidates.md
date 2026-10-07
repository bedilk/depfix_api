# npm fork candidates for Depfix testing

Date: 2026-09-17

This is a shortlist of downstream Node/npm applications, templates, and focused
examples that can be forked into `bedilk` for Depfix testing. It deliberately
excludes provider SDK source repositories: fixing an SDK's own source is not a
valid downstream-migration test. Each package claim links to the repository's
own `package.json` (or the Node subproject's manifest), which is the primary
source of truth.

Do a short preflight before installing the GitHub App or creating a PR:

```bash
gh repo fork OWNER/REPO --clone=false
depfix init bedilk/REPO
depfix scan --repo bedilk/REPO --provider PROVIDER
```

Fork only a few at a time. A successful scan is the admission criterion; an
SDK in `package.json` is not enough if the repository has no matching call
sites or its test suite cannot run locally.

| Priority | Candidate | Provider / manifest evidence | Why it is useful |
|---:|---|---|---|
| 1 | [`supabase-community/vercel-ai-chatbot`](https://github.com/supabase-community/vercel-ai-chatbot) | [`ai` `^2.1.6` and `@supabase/supabase-js` `^2.26.0`](https://github.com/supabase-community/vercel-ai-chatbot/blob/main/package.json) | Compact, historical AI SDK version and Supabase integration; strong migration target. |
| 2 | [`vercel/chatbot`](https://github.com/vercel/chatbot) | [uses the `ai` package](https://github.com/vercel/chatbot/blob/main/package.json) | Well-maintained full application for Vercel AI regression testing; needs external services, so start scan-only. |
| 3 | [`stripe-samples/checkout-one-time-payments`](https://github.com/stripe-samples/checkout-one-time-payments) | [Node implementation](https://github.com/stripe-samples/checkout-one-time-payments/tree/main/server/node) | Focused official Stripe payment flow; select the Node directory after forking. |
| 4 | [`stripe-samples/accept-a-payment`](https://github.com/stripe-samples/accept-a-payment) | [Payment Element Node implementation](https://github.com/stripe-samples/accept-a-payment/tree/main/payment-element) | Official Stripe migration target with isolated server variants; avoid running its whole multi-language suite. |
| 5 | [`firebase/quickstart-nodejs`](https://github.com/firebase/quickstart-nodejs) | [declares Firebase packages](https://github.com/firebase/quickstart-nodejs/blob/master/package.json) | Small, official Node Firebase examples with clear individual feature directories. |
| 6 | [`firebase/quickstart-js`](https://github.com/firebase/quickstart-js) | [Firebase JavaScript quickstarts](https://github.com/firebase/quickstart-js) | Official JS examples; fork and retain one feature directory to keep CI and scan scope small. |
| 7 | [`anthropics/claude-quickstarts`](https://github.com/anthropics/claude-quickstarts) | [TypeScript/Node quickstart source](https://github.com/anthropics/claude-quickstarts) | Practical Anthropic flows; choose a standalone Node example and mock the API in tests. |
| 8 | [`langchain-ai/langchain-nextjs-template`](https://github.com/langchain-ai/langchain-nextjs-template) | [LangChain/Next.js manifest](https://github.com/langchain-ai/langchain-nextjs-template/blob/main/package.json) | Realistic LangChain integration with an existing app structure; use mocked provider calls. |
| 9 | [`prisma/nextjs-prisma-postgres-demo`](https://github.com/prisma/nextjs-prisma-postgres-demo) | [Prisma client dependency](https://github.com/prisma/nextjs-prisma-postgres-demo/blob/main/package.json) | Contained Prisma/Next example; useful for scan and schema-aware fix verification. |
| 10 | [`prisma/prisma-examples`](https://github.com/prisma/prisma-examples) | [official example collection](https://github.com/prisma/prisma-examples) | Broad migration corpus. Fork only one example directory into a separate repo; do not test the whole collection as one unit. |
| 11 | [`clerk/clerk-nextjs-onboarding-sample-app`](https://github.com/clerk/clerk-nextjs-onboarding-sample-app) | [`@clerk/nextjs` manifest entry](https://github.com/clerk/clerk-nextjs-onboarding-sample-app/blob/main/package.json) | Small, focused Clerk application with ordinary Next.js call sites. |
| 12 | [`antiwork/shortest`](https://github.com/antiwork/shortest) | [`@clerk/nextjs` manifest entry](https://github.com/antiwork/shortest/blob/45a8dcfe89992b37ed2dfb1f76618048cff0f45c/package.json) | A downstream application using Clerk rather than a Clerk SDK repository. |
| 13 | [`vercel/ncc`](https://github.com/vercel/ncc) | [`@sentry/node` manifest entry](https://github.com/vercel/ncc/blob/cb1f1f058bfa7de4cb63f2411e14a724e714e260/package.json) | Mature, independently maintained Node project using Sentry; good scan-only then isolated-fix candidate. |
| 14 | [`disease-sh/API`](https://github.com/disease-sh/API) | [`@sentry/node` manifest entry](https://github.com/disease-sh/API/blob/cdea5fac227b88b6933012ddb4bc592ec21fde07/package.json) | Downstream API service, likely closer to production Sentry use than a minimal demo. |
| 15 | [`twilio-labs/call-gpt`](https://github.com/twilio-labs/call-gpt) | [Twilio package manifest](https://github.com/twilio-labs/call-gpt/blob/db0aecda0058f222bfc26ad626d831c365ecfcea/package.json) | Focused Twilio application; may also exercise OpenAI-related detection. Use mocked network tests. |
| 16 | [`sourcefuse/loopback4-notifications`](https://github.com/sourcefuse/loopback4-notifications) | [Twilio package manifest](https://github.com/sourcefuse/loopback4-notifications/blob/330b9641571a76d92de1fd56d961c308ceae3c7b/package.json) | Downstream notifications service with a clear SDK integration boundary. |
| 17 | [`JuanmaMenendez/website-change-monitor`](https://github.com/JuanmaMenendez/website-change-monitor) | [`@sendgrid/mail` manifest entry](https://github.com/JuanmaMenendez/website-change-monitor/blob/58b3a3bb2c9c4f20fcc1f15e460ff1f606a78f4ee7/package.json) | Small application likely to provide straightforward SendGrid call sites. |
| 18 | [`mxschmitt/action-tmate`](https://github.com/mxschmitt/action-tmate) | [`@octokit/rest` manifest entry](https://github.com/mxschmitt/action-tmate/blob/0e3058c2c8deab34606c16800925735f854f12e1/package.json) | Compact GitHub Action implementation; useful for Octokit scan/fix trials. |
| 19 | [`outsideris/citizen`](https://github.com/outsideris/citizen) | [`@aws-sdk/client-s3` manifest entry](https://github.com/outsideris/citizen/blob/6061a2531b9de45471a607c9d89536668838d1c9/package.json) | Downstream AWS S3 user with a direct SDK dependency. Confirm its tests before enabling PRs. |
| 20 | [`gcui-art/album-ai`](https://github.com/gcui-art/album-ai) | [`openai` manifest entry](https://github.com/gcui-art/album-ai/blob/fe5eb8838d9a53792375232b8ae90c3d1893eea3/package.json) | Small downstream OpenAI application; inspect for pinned old APIs before using it for a live PR test. |

## Recommended rollout

Use rows 1, 3, 5, 9, 11, 15, and 18 first. They cover seven providers while
keeping the repositories application-oriented. Treat all other rows as
scan-first candidates. Never enable `depfix apply` until `depfix plan` has a
verified artifact and the fork's own tests pass without external credentials.

The GitHub API was used only to check public repository and source metadata;
no repository was forked or modified while creating this report.
