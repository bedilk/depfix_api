# OpenAI scan skill

## SDK packages
- npm: `openai`
- pypi: `openai`
- rubygems: `ruby-openai`

## Import shapes to look for

### JavaScript / TypeScript
```javascript
import OpenAI from 'openai'; // v4+
const OpenAI = require('openai'); // v4+ CJS
const { Configuration, OpenAIApi } = require('openai'); // v3 legacy
```

Client construction:
```javascript
const client = new OpenAI({ apiKey }); // v4
const openai = new OpenAIApi(new Configuration(...)); // v3
```

### Python
```python
from openai import OpenAI  # v1+
import openai

openai.api_key = ...  # v0 legacy
```

## Call sites to look for (v4 canonical)
- `client.chat.completions.create(...)`
- `client.completions.create(...)`
- `client.embeddings.create(...)`
- `client.moderations.create(...)`
- `client.images.generate(...)`

## Legacy v3 call sites (still very common)
- `openai.createChatCompletion(...)`
- `openai.createCompletion(...)`
- `openai.createEmbedding(...)`
- `openai.createModeration(...)`

If you find v3 call sites, that itself is useful evidence — flag them.

## Anchors (fallback signals)
- URL: `api.openai.com`
- Header: `OpenAI-Version` (rare; OpenAI mostly versions via SDK)

## Common wrapper patterns
Many teams re-export the client from a `lib/openai.ts` or `client.js`.
One hop is expected — downgrade to `medium` confidence, don't reject.

## Feeds
- npm: `openai` package, dist-tag `latest`
- pypi: `openai` project
- github releases: `openai/openai-node`, `openai/openai-python`
- openapi: `https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml`

## What "breaking" means for OpenAI
- A method rename (`createChatCompletion` → `chat.completions.create`).
- A response shape change (v3's `.data` wrapper was removed in v4).
- A parameter removal or a new required parameter.
- Model deprecation (older `text-davinci-*`, `gpt-3.5-turbo-0301`,
  etc.). These are docs-only announcements — search release notes.
