# Sentry scan skill

## SDK packages
- npm: `@sentry/node`, `@sentry/browser`, `@sentry/react`, `@sentry/nextjs`
- pypi: `sentry-sdk`

## Import patterns

```
require('@sentry/node')
import * as Sentry from '@sentry/node'
import Sentry from '@sentry/node'
```

Also: `@sentry/browser`, `@sentry/react`, `@sentry/nextjs` — all share the `Sentry.*` namespace.

## Call sites
- `Sentry.init(...)`
- `Sentry.captureException(...)`, `Sentry.captureMessage(...)`
- `Sentry.setUser/setTag/setContext(...)`
- `new Sentry.Integrations.Http(...)` (v7 legacy — flag it)
- `Sentry.httpIntegration(...)` (v8 replacement)
- `NodeClient`, `Scope`, `getDefaultIntegrations`

## Where to look first
1. `grep("@sentry/", "")` — the scoped name is distinctive.
2. `Sentry.init` is almost always in an instrumentation/setup file
   (`instrument.js`, `sentry.server.config.ts`, `sentry.client.config.ts`).
