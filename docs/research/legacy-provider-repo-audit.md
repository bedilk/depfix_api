# Legacy provider repository audit

Checked 2026-09-19 using GitHub metadata and Depfix scans. Ollama model:
`qwen2.5-coder:7b`. No `apply` command or pull request was run.

## Repository status

The following proposed names were not found: `openai/openai-cookbook-js`,
`openai/whisper-jax`, `hwchase17/langchainjs-templates`,
`jerryjliu/llama_index-legacy`, `microsoft/semantic-kernel-samples-archive`,
`openai/openai-python-embedding-samples`, `stripe/stripe-node-examples`,
`stripe-samples/saving-card-without-payment`, `supabase/supabase-js-v1`,
`supabase/nextjs-auth-helpers`, `getsentry/sentry-javascript-v6-archive`, and
`anthropics/anthropic-quickstarts-legacy`.

Real repositories were forked or confirmed under `bedilk`:

| Fork | Upstream | Status |
|---|---|---|
| `depfix-openai-quickstart-node` | `openai/openai-quickstart-node` | scan: modern OpenAI v4, no old migration needed |
| `depfix-openai-gpt3` | `openai/gpt-3` | archived; scan found no supported source call sites |
| `depfix-openai-plugins-quickstart` | `openai/plugins-quickstart` | archived; no supported call sites |
| `depfix-nextjs-openai-doc-search` | `supabase-community/nextjs-openai-doc-search` | scan found OpenAI `^3.3.0` and actionable `createEmbedding` |
| `depfix-subscription-use-cases` | `stripe-samples/subscription-use-cases` | fork exists; GitHub App installation required |
| `depfix-stripe-checkout` | `stripe-samples/checkout-one-time-payments` | fork exists; GitHub App installation required |
| `depfix-raven-node` | `getsentry/raven-node` | archived; scan found no matching Sentry v6 call |
| `depfix-vercel-ai-supabase` | `vercel/chatbot`/related Vercel AI sample | GitHub App installation required |

The strongest current test target is `depfix-nextjs-openai-doc-search`. Its
scan found three actionable sites and the pinned OpenAI v3 manifest. The
focused Ollama plan reached the plan pipeline but did not produce a complete
summary, so no fix or PR is claimed.
