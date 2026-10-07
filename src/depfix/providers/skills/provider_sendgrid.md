# SendGrid scan skill

## SDK packages
- npm: `@sendgrid/mail`, `@sendgrid/client`
- pypi: `sendgrid`

## Import patterns

```
require('@sendgrid/mail')
import sgMail from '@sendgrid/mail'
const sgMail = require('@sendgrid/mail')
```

## Call sites (report these)
- `sgMail.setApiKey(...)`
- `sgMail.send(...)`, `sgMail.sendMultiple(...)`
- `client.request(...)` (lower-level @sendgrid/client usage)

## Where to look first
1. `grep("@sendgrid/", "")` — scoped name is distinctive.
2. Email-sending code is usually in service files, background workers, or
   notification modules (`email.service.ts`, `mailer.js`, etc.).
