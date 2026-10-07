# Onboarding a repository

Onboard in increasing order of blast radius. Do not skip a stage.

## 0. Prerequisites

- The GitHub App is installed on the repository or organisation.
- For automatic fleet discovery, a `.depfix.yml` is committed to the
  repository's default branch as the maintainer consent record. Naming a
  repository explicitly in `plan` or `apply` is itself opt-in; the file then
  remains optional configuration for provider/path filters and PR policy.

## 1. Rehearse — `--dry-run`

```sh
depfix plan OWNER/NAME --show-skips
```

This writes nothing to GitHub or the database. It confirms that the config
parses, a change is in scope, and the ledger would allow it to proceed.

## 2. Full run without a PR — `--no-pr`

```sh
depfix apply OWNER/NAME --no-pr
```

This clones, scans, fixes, and runs the repository test suite, but opens no
PR. The result is persisted as `FIXED_NO_PR`.

## 3. Live as drafts

Set `draft_pr: true` in `.depfix.yml`, then run:

```sh
depfix apply OWNER/NAME
```

Review and undraft a PR manually when you trust the result.

## 4. Live

Remove `draft_pr: true` after the draft stage is working well.

## Cron

The advisory lock is enabled by default. Give an hourly cron job a finite
window and emit JSON logs for aggregation:

```sh
depfix apply --max-duration 55 --log-format json
```

A duration or cost ceiling stops cleanly between work items and exits zero;
the next scheduled run resumes the remaining work.
