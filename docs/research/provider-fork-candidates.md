# Provider fork candidates

Checked 2026-09-19. These are downstream applications/examples, not provider SDK
source repositories. GitHub metadata confirms each `bedilk` fork exists and is
not marked archived. The upstream repository pages and manifests are the
primary evidence to inspect before scanning.

| Provider/test focus | Upstream | Fork | Notes |
|---|---|---|---|
| Vercel AI + Supabase | [supabase-community/vercel-ai-chatbot](https://github.com/supabase-community/vercel-ai-chatbot) | [bedilk/depfix-vercel-ai-supabase](https://github.com/bedilk/depfix-vercel-ai-supabase) | Historical `ai`/Supabase application |
| Stripe Checkout | [stripe-samples/checkout-one-time-payments](https://github.com/stripe-samples/checkout-one-time-payments) | [bedilk/depfix-stripe-checkout](https://github.com/bedilk/depfix-stripe-checkout) | Official Node payment sample |
| Firebase JS | [firebase/quickstart-js](https://github.com/firebase/quickstart-js) | [bedilk/depfix-firebase-quickstart-js](https://github.com/bedilk/depfix-firebase-quickstart-js) | Feature-oriented Firebase examples |
| Anthropic | [anthropics/claude-quickstarts](https://github.com/anthropics/claude-quickstarts) | [bedilk/depfix-anthropic-quickstarts](https://github.com/bedilk/depfix-anthropic-quickstarts) | TypeScript/Node quickstarts |
| LangChain | [langchain-ai/langchain-nextjs-template](https://github.com/langchain-ai/langchain-nextjs-template) | [bedilk/depfix-langchain-nextjs-template](https://github.com/bedilk/depfix-langchain-nextjs-template) | Next.js integration template |
| Prisma | [prisma/nextjs-prisma-postgres-demo](https://github.com/prisma/nextjs-prisma-postgres-demo) | [bedilk/depfix-prisma-nextjs](https://github.com/bedilk/depfix-prisma-nextjs) | Prisma/Next.js demo |
| Clerk | [clerk/clerk-nextjs-onboarding-sample-app](https://github.com/clerk/clerk-nextjs-onboarding-sample-app) | [bedilk/depfix-clerk-onboarding](https://github.com/bedilk/depfix-clerk-onboarding) | Clerk onboarding sample |
| Octokit/GitHub Action | [mxschmitt/action-tmate](https://github.com/mxschmitt/action-tmate) | [bedilk/depfix-action-tmate](https://github.com/bedilk/depfix-action-tmate) | Compact GitHub Action |
| AWS S3 | [outsideris/citizen](https://github.com/outsideris/citizen) | [bedilk/depfix-citizen](https://github.com/bedilk/depfix-citizen) | Downstream S3 application |
| OpenAI | [gcui-art/album-ai](https://github.com/gcui-art/album-ai) | [bedilk/depfix-album-ai](https://github.com/bedilk/depfix-album-ai) | Small downstream OpenAI application |

These are not GitHub-archived repositories. They are useful test targets because
several contain historical or pinned SDK versions. Run `depfix scan` first and
inspect the detected manifest and call sites before enabling `plan` or `apply`.
The GitHub App must be installed on each fork separately.
