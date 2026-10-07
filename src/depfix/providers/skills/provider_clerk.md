# Clerk scan skill

## SDK packages
- npm: `@clerk/nextjs`, `@clerk/clerk-sdk-node`, `@clerk/backend`

## Import patterns

```
import { auth, currentUser, clerkClient } from '@clerk/nextjs'
import { auth, currentUser, clerkClient } from '@clerk/nextjs/server'
const { ClerkExpressRequireAuth } = require('@clerk/clerk-sdk-node')
import { createClerkClient } from '@clerk/backend'
```

## Call sites (report these)
- `auth()` — Next.js server-side auth helper
- `currentUser()` — get current user object
- `clerkClient.users.getUser(...)`, `clerkClient.users.getUserList(...)`
- `clerkClient.organizations.getOrganization(...)`
- `ClerkExpressRequireAuth()` (middleware)
- `withAuth(handler)` (Next.js pages router wrapper)

## Where to look first
1. `grep("@clerk/", "")` — scoped name is distinctive.
2. Server actions, API routes, and middleware are the most common locations.

## Do NOT
- Do not report `<ClerkProvider>` JSX usage as a call site — it's configuration.
- Only report actual function calls that make auth decisions or fetch user data.
