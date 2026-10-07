# Stripe scan skill

## SDK packages
- npm: `stripe`
- pypi: `stripe`
- rubygems: `stripe`

## Import patterns (JS/TS)

```
require('stripe')
const Stripe = require('stripe')
import Stripe from 'stripe'
```

## Construction & bindings

```javascript
const stripe = new Stripe(secretKey, { apiVersion: '2020-08-27' })
const stripe = Stripe(secretKey)  // v2 legacy call form
```

Binding names: `stripe`, `stripeClient`.

## Call sites (report these)
- `stripe.charges.create(...)`, `stripe.charges.retrieve(...)`
- `stripe.paymentIntents.create(...)`
- `stripe.customers.create/retrieve/update/del/list(...)`
- `stripe.checkout.sessions.create/retrieve(...)`
- `stripe.subscriptions.create/update/retrieve(...)`
- `stripe.billingPortal.sessions.create(...)`
- `stripe.webhooks.constructEvent(...)`

## Version-pin anchors (report as api_version_pin, confidence medium)
- `apiVersion: '2020-08-27'` (dated literal near stripe construction)
- `Stripe-Version: 2020-08-27` (raw HTTP header)
Only report a dated version literal when the SAME file imports/constructs
Stripe — otherwise it's an unrelated config field.

## Where to look first
1. `grep("require\\(['\"]stripe['\"]\\)|from ['\"]stripe['\"]", "")`
2. Follow the binding, grep `<binding>\\.(charges|paymentIntents|customers|checkout|subscriptions|webhooks)`.

## Do NOT
- Do not report `apiVersion:` fields in Kubernetes-style config
  (`apiVersion: apps/v1`) — those are not Stripe versions.
