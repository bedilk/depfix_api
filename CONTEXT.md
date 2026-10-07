# Domain glossary

## Dependency drift

A repository's declared SDK version is behind the version observed from its
configured registry feed. Drift is a dependency-level fact; it does not imply
that any particular source call is broken.

## Automatic dependency update

A patch or minor dependency drift whose version constraints can be rewritten
unambiguously. It may proceed through the normal verification and PR flow.

## Vetted migration

A documented, provider-specific API migration with a concrete old and new
call shape. Major-version drift is reported and planned, but is only applied
when it is paired with a vetted migration.

## Security advisory

A published record that a specific range of a package's versions has a known
vulnerability, and (usually) which version fixes it. Sourced from a public
database (OSV, the GitHub Advisory Database). Distinct from drift: drift says
"a newer version exists"; an advisory says "your current version is unsafe."
_Avoid_: alert, CVE (a CVE is one identifier an advisory may carry, not the
advisory itself).

## Reachability

Whether a repository's own code actually calls the API a change or advisory
concerns. An advisory or breaking change is *reachable* when a scanned call
site touches the affected symbol, and *unreachable* when the package is
merely declared but never called in the relevant way. Unreachable findings
are real but low-priority.
_Avoid_: usage (too vague), coverage (means test-line execution here).

## Cooldown

A minimum age a newly published version must reach before depfix will adopt
it. A version published hours ago may be compromised; the cooldown is a
supply-chain safety gate, not a bookkeeping limit.

## Triage rule

A repository's standing instruction about a class of change: dismiss it,
snooze it until a condition holds, or let it proceed. Rules are proposed
(optionally by an LLM) and always confirmed by a human before they suppress
anything.
_Avoid_: filter, ignore (ignore is one specific rule action, not the rule).
