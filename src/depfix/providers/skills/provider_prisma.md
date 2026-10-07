# Prisma scan skill

## SDK packages
- npm: `@prisma/client`, `prisma`

## Import patterns

```
import { PrismaClient } from '@prisma/client'
const { PrismaClient } = require('@prisma/client')
```

## Construction & bindings

```javascript
const prisma = new PrismaClient()
const db = new PrismaClient({ log: ['query'] })
```

Binding names: `prisma`, `db`, `client`.

## Call sites (report these)
- `prisma.<model>.findUnique(...)`, `prisma.<model>.findMany(...)`
- `prisma.<model>.create(...)`, `prisma.<model>.update(...)`, `prisma.<model>.delete(...)`
- `prisma.<model>.upsert(...)`
- `prisma.$queryRaw(...)`, `prisma.$executeRaw(...)`
- `prisma.$transaction([...])`
- `prisma.$connect()`, `prisma.$disconnect()`

## Where to look first
1. `grep("@prisma/client", "")` — finds both import and package.json references.
2. In a monorepo, PrismaClient is often re-exported from a `packages/db/` or
   `packages/prisma/` workspace. Follow the re-export one hop.

## Do NOT
- Do not scan `node_modules/@prisma/` — only user code calling PrismaClient.
- Do not report schema file `.prisma` references — they are not call sites.
