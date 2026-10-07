# Non-npm ecosystem candidates

Date: 2026-09-18

This is a scan-first shortlist for the next four ecosystems: Python, Java,
Go, and Rust. It intentionally cites each repository's own manifest and source
file. A matching dependency alone is not sufficient: Depfix must also find an
SDK import/call site.

## Readiness boundary

Depfix has a scanner for Python, Java, Go, and Rust. Java, Go, and Rust are
currently **scan-only**: Depfix may report affected declarations and imports,
but does not generate or verify a fix. Python is fix-and-verify capable.

`providers.yaml` currently maps the OpenAI, Stripe, and Anthropic SDKs for
`pypi`, but does not yet map an SDK package for `maven`, `go`, or `cargo`.
Consequently, only the two Python entries below are runnable today. Before
testing Java, Go, or Rust, add the corresponding provider package mapping;
otherwise the generic ecosystem scanner has no provider package to match.

```yaml
# Proposed additions to the existing Stripe/OpenAI provider declarations.
# Add only when the team chooses to make these targets supported.
sdk_packages:
  - name: com.stripe:stripe-java
    ecosystem: maven
  - name: github.com/stripe/stripe-go
    ecosystem: go
  - name: async-openai
    ecosystem: cargo
```

The first two belong to the `stripe` provider. The third is an OpenAI-compatible
community SDK, so it belongs to `openai` only if we explicitly decide that it
is a supported OpenAI client. This configuration change is not made by this
research note.

## Candidates

| Ecosystem | Candidate and evidence | Status | Exact scan command after clone |
|---|---|---|---|
| Python | [`stripe-samples/accept-a-payment`](https://github.com/stripe-samples/accept-a-payment): its [Python requirements](https://github.com/stripe-samples/accept-a-payment/blob/main/custom-payment-flow/server/python/requirements.txt) pin `stripe==14.2.0`; [server.py](https://github.com/stripe-samples/accept-a-payment/blob/main/custom-payment-flow/server/python/server.py) imports and calls Stripe. | Ready now; Python has a Stripe/PyPI mapping. | `depfix scan --path ./accept-a-payment/custom-payment-flow/server/python --provider stripe` |
| Python | [`stripe-samples/checkout-one-time-payments`](https://github.com/stripe-samples/checkout-one-time-payments): [requirements](https://github.com/stripe-samples/checkout-one-time-payments/blob/main/server/python/requirements.txt) pin Stripe; [server.py](https://github.com/stripe-samples/checkout-one-time-payments/blob/main/server/python/server.py) imports and uses it. | Ready now. | `depfix scan --path ./checkout-one-time-payments/server/python --provider stripe` |
| Java | [`stripe-samples/accept-a-payment`](https://github.com/stripe-samples/accept-a-payment): the [Java POM](https://github.com/stripe-samples/accept-a-payment/blob/main/custom-payment-flow/server/java/pom.xml) declares `com.stripe:stripe-java`; [Server.java](https://github.com/stripe-samples/accept-a-payment/blob/main/custom-payment-flow/server/java/src/main/java/com/stripe/sample/Server.java) imports it. | Needs the `maven` Stripe mapping above; scan-only. | `depfix scan --path ./accept-a-payment/custom-payment-flow/server/java --provider stripe` |
| Java | [`stripe-samples/checkout-one-time-payments`](https://github.com/stripe-samples/checkout-one-time-payments): [POM](https://github.com/stripe-samples/checkout-one-time-payments/blob/main/server/java/pom.xml) declares `stripe-java`; [Server.java](https://github.com/stripe-samples/checkout-one-time-payments/blob/main/server/java/src/main/java/com/stripe/sample/Server.java) imports `com.stripe` APIs. | Needs the `maven` Stripe mapping; scan-only. | `depfix scan --path ./checkout-one-time-payments/server/java --provider stripe` |
| Go | [`stripe-samples/accept-a-payment`](https://github.com/stripe-samples/accept-a-payment): [go.mod](https://github.com/stripe-samples/accept-a-payment/blob/main/custom-payment-flow/server/go/go.mod) requires `stripe-go`; [server.go](https://github.com/stripe-samples/accept-a-payment/blob/main/custom-payment-flow/server/go/server.go) imports it. | Needs the `go` Stripe mapping; scan-only. | `depfix scan --path ./accept-a-payment/custom-payment-flow/server/go --provider stripe` |
| Go | [`stripe-samples/checkout-one-time-payments`](https://github.com/stripe-samples/checkout-one-time-payments): [go.mod](https://github.com/stripe-samples/checkout-one-time-payments/blob/main/server/go/go.mod) requires `stripe-go`; [server.go](https://github.com/stripe-samples/checkout-one-time-payments/blob/main/server/go/server.go) imports checkout, price, and webhook packages. | Needs the `go` Stripe mapping; scan-only. | `depfix scan --path ./checkout-one-time-payments/server/go --provider stripe` |
| Rust | [`qingjian-team/qingjian`](https://github.com/qingjian-team/qingjian): [workspace manifest](https://github.com/qingjian-team/qingjian/blob/main/Cargo.toml) and [member manifest](https://github.com/qingjian-team/qingjian/blob/main/crates/qingjian-predict/Cargo.toml) declare `async-openai`; [chat client](https://github.com/qingjian-team/qingjian/blob/main/crates/qingjian-predict/src/chat_client.rs) constructs an OpenAI client. | Needs an explicit `cargo` OpenAI mapping; scan-only. It is a sizable workspace. | `depfix scan --path ./qingjian --provider openai` |
| Rust | [`rust-infra/tact`](https://github.com/rust-infra/tact): [workspace manifest](https://github.com/rust-infra/tact/blob/main/Cargo.toml) and [crate manifest](https://github.com/rust-infra/tact/blob/main/crates/tact/Cargo.toml) declare an `async-openai` dependency; its [OpenAI response adapter](https://github.com/rust-infra/tact/blob/main/crates/tact_llm/src/openai/responses/convert.rs) shows actual usage. | Needs an explicit `cargo` OpenAI mapping; scan-only. A robustness case, not the first Rust smoke test, because it uses an SDK fork for Responses. | `depfix scan --path ./tact --provider openai` |

## Recommended order

1. Clone and scan the two Python Stripe sample directories first. They are
   small enough for a clean baseline and require no provider configuration
   change.
2. Add the `maven` and `go` Stripe entries, accompanied by scanner tests, then
   scan the Java and Go sample directories.
3. Add `async-openai` only after deciding that a community OpenAI-compatible
   Rust SDK is in scope. Start with `qingjian`; reserve `tact` for a larger
   workspace test.

For a public repository scanned through the GitHub App rather than a local
clone, use `depfix scan --repo OWNER/REPO --provider PROVIDER`. The examples
above deliberately use `--path`: each recommended target is a language
subdirectory of a multi-language sample repository, which gives the ecosystem
detector an unambiguous manifest.
