# MongoDB scan skill

## SDK packages
- npm: `mongodb`, `mongoose`
- pypi: `pymongo`

## Import patterns (JS/TS)

```
require('mongodb')
const { MongoClient, ObjectId } = require('mongodb')
import { MongoClient } from 'mongodb'
```

## Bindings & call sites

```javascript
const client = new MongoClient(uri)
client.connect()
client.db(name)
db.collection(name)
collection.findOne/find/insertOne/updateOne/deleteOne(...)
```

Also: `new ObjectId(...)`, `.toHexString()`, `ReadPreference`.

## Where to look first
1. `grep("require\\(['\"]mongodb['\"]\\)|from ['\"]mongodb['\"]", "")`
2. Follow the `MongoClient`/`db`/`collection` bindings.

## Do NOT
- `mongoose` is a different (higher-level) package. Only report `mongoose`
  call sites if `mongoose` is in the target sdk_packages.
- Do not report `MongoClient` from a test mock — check the import.
