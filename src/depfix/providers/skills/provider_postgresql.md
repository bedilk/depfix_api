# PostgreSQL (pg) scan skill

## SDK packages
- npm: `pg`, `pg-native`, `pg-pool`
- pypi: `psycopg`, `psycopg2`

## Import patterns to grep for (JS/TS)

```
require('pg')
require("pg")
const { Client, Pool } = require('pg')
import pg from 'pg'
import { Client, Pool } from 'pg'
```

## Binding names to follow

After an import, the code names the client. Common local names:
`client`, `pool`, `db`, `pgClient`, `pgPool`, `connection`.
grep for `<binding>.` once you know the local name.

## Call sites (this is what you report)
- `<binding>.query(...)` — the most common, expect many
- `<binding>.connect(...)`
- `<binding>.end(...)`
- `new Client(...)`, `new Pool(...)` — construction
- `pool.connect()` returning a client, then `client.query()`

## Python

```python
import psycopg  # v3
import psycopg2  # v2

psycopg.connect(...)
conn.cursor()
cursor.execute(...)
```

## Where to look first (efficient order)
1. `find_files("package.json")` — confirm `pg` is declared.
2. `resolve_installed_version("pg")` — record the version.
3. `grep("require\\(['\"]pg['\"]\\)|from ['\"]pg['\"]", "")` — find imports.
   In a monorepo, imports cluster in `.scripts/`, `src/`, `packages/*/src/`.
4. For each import, note the binding name, then
   `grep("<binding>\\.(query|connect|end)", "<dir of that file>")`.

## Do NOT
- Do not `list_dir` the whole tree. grep directly for the import pattern.
- Do not report `new Client(` from a non-pg import (e.g. an Elasticsearch
  or Redis `Client`). Confirm the binding traces to `require('pg')`.
