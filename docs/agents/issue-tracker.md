# Issue tracker: GitHub

Issues and specifications for this repository live in GitHub Issues. Use the
`gh` CLI for issue operations.

## Conventions

- Create: `gh issue create --title "..." --body "..."`.
- Read: `gh issue view <number> --comments`.
- List: `gh issue list --state open` with relevant labels.
- Comment: `gh issue comment <number> --body "..."`.
- Label: `gh issue edit <number> --add-label "..."`.
- Close: `gh issue close <number> --comment "..."`.

Infer the repository from the Git remote; `gh` does this automatically inside
this checkout.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Skill conventions

When a skill says to publish work to the issue tracker, create a GitHub issue.
When a skill says to fetch a ticket, run `gh issue view <number> --comments`.
