"""Opens (or reuses) a PR for a depfix branch, with a body that links back
to the breaking change's real evidence fields -- no invented metadata, just
what's already on :class:`~depfix.core.models.BreakingChange` and
:class:`~depfix.core.pipeline.FixPipelineResult`.
"""

from __future__ import annotations

import logging
import re

import httpx

from depfix.apply.models import EditOrigin, EditVerdict, FileEdit
from depfix.core.models import BreakingChange, ChangeKind
from depfix.core.pipeline import FixPipelineResult
from depfix.gh.app import GitHubAppError
from depfix.gh.models import PullRequest

logger = logging.getLogger(__name__)

DEFAULT_GITHUB_API_URL = "https://api.github.com"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ATmention_RE = re.compile(r"@(?=\w)")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _sanitize_md(text: str | None) -> str:
    """Strip HTML tags, @-mentions, and markdown images from upstream text.

    Prevents social-engineering attacks via injected markdown in PR bodies.
    """
    if not text:
        return text or ""
    text = _HTML_TAG_RE.sub("", text)
    text = _MD_IMAGE_RE.sub("", text)
    text = _ATmention_RE.sub("@​", text)  # zero-width space breaks @-mention rendering
    return text


def pr_title_for(change: BreakingChange) -> str:
    """Short, informative title -- same "old -> new" shape used elsewhere
    (e.g. commit messages), so a PR list and a commit log read the same
    way."""
    return f"depfix: migrate {change.old_api} -> {change.replacement}"


def build_pr_body(
    change: BreakingChange,
    result: FixPipelineResult,
    *,
    edits: list[FileEdit] | None = None,
    escalated: bool = False,
    plan_digest: str = "",
    grouped_members: list | None = None,
    plan_impact_summary: str = "",
    drift_review: bool | None = None,
    removals: list | None = None,
) -> str:
    """Markdown PR body: change summary, migration guide, source evidence
    (when present), a per-file table of edits, and the verification
    summary -- every field used here is real, read directly off ``change``
    and ``result``.

    ``edits`` defaults to ``result.edits`` (every candidate edit, including
    ones that were reverted or never committed) for backward compatibility,
    but callers that already filtered down to the actually-committable set
    (see :func:`committable_edits`) should pass that same list here --
    otherwise the "Files changed" table lists files the PR doesn't actually
    touch.
    """
    if edits is None:
        edits = list(result.edits)
    lines = [
        f"## {change.package}: `{change.old_api}` -> `{change.replacement}`",
        "",
        f"- **Package:** {change.package} ({change.old_version} -> {change.new_version})",
        f"- **Kind:** {change.kind.value}",
        f"- **Confidence:** `{result.confidence.value}`",
        f"- **Description:** {_sanitize_md(change.description)}",
    ]
    if change.migration_guide:
        lines += ["", "### Migration guide", "", _sanitize_md(change.migration_guide)]
    if change.evidence:
        lines += ["", "### Evidence", "", f"> {_sanitize_md(change.evidence)}"]
    if change.source_url:
        lines += ["", f"Source: {_sanitize_md(change.source_url)}"]
    if change.kind is ChangeKind.SECURITY_ADVISORY:
        lines += [
            "",
            "### ⚠️ Security advisory",
            "",
            f"This PR addresses a known vulnerability ({_sanitize_md(change.description).split(':')[0] if ':' in change.description else 'see description'}). "
            "Review the advisory references and upgrade guidance before merging.",
        ]
    needs_review = drift_review if drift_review is not None else _is_major_dependency_review(change)
    if needs_review:
        from depfix.codemods.lockfile import describe_drift

        lines += [
            "",
            "### Human review required",
            "",
            f"This is a registry-confirmed dependency update spanning "
            f"**{describe_drift(change)}**. Depfix opened this as a draft after "
            "validation and verification; review the provider's migration guidance "
            "before merging.",
        ]

    lines += [
        "",
        "### Files changed",
        "",
        "| File | Verdict | Author | Usages fixed |",
        "| --- | --- | --- | --- |",
    ]
    for edit in edits:
        lines.append(
            f"| `{edit.relpath}` | {edit.verdict.value} | {_author_label(edit)} | {edit.usages_fixed} |"
        )
    if any(edit.derivation_trace for edit in edits):
        lines += ["", "### How each fix was derived", ""]
        for edit in edits:
            if edit.derivation_trace:
                lines += [f"**`{edit.relpath}`**: {_sanitize_md(edit.derivation_trace)}", ""]

    lines += ["", "### Confidence", "", result.confidence.summary]
    if result.confidence_reason:
        lines.append(f"_{_sanitize_md(result.confidence_reason)}._")

    if plan_impact_summary:
        lines += ["", "### Local impact check", "", plan_impact_summary]

    verification = result.verification
    lines += ["", "### Verification"]
    if verification is None or not verification.ran:
        reason = verification.skipped_reason if verification is not None else "not configured"
        lines.append(f"Test-suite verification did not run ({reason}).")
    else:
        lines.append(f"New test failures introduced: {verification.new_failure_count}.")
    if verification is not None and verification.typechecked:
        lines.append(
            f"`tsc --noEmit` introduced {len(verification.new_diagnostics)} new diagnostic(s)."
        )
    if verification is not None and verification.static_after is not None:
        tool = verification.static_after.tool or "static check"
        added = len(verification.new_static_diagnostics)
        lines.append(f"`{tool}` introduced {added} new diagnostic(s).")
    if verification is not None and verification.coverage is not None:
        lines.append(f"Coverage: {verification.coverage.summary()}.")
    if verification is not None and verification.contract is not None:
        lines.append(f"Request contract: {verification.contract.summary()}.")
    if verification is not None and verification.flaky_test_identities:
        flaky = ", ".join(verification.flaky_test_identities[:5])
        lines.append(
            f"_{len(verification.flaky_test_identities)} test(s) failed once but did not "
            f"reproduce on re-run and were treated as flaky: {flaky}._"
        )

    # Always-run stage results (smoke, characterization) — shown when present.
    # Skipped stages are visible here so reviewers know WHY a stage didn't run.
    if verification is not None and getattr(verification, "stage_results", ()):  # type: ignore[arg-type]
        from depfix.verify.models import StageStatus

        stage_lines = [sr.as_pr_line() for sr in verification.stage_results]
        has_skips = any(sr.status == StageStatus.SKIPPED for sr in verification.stage_results)
        lines += ["", "### Verification stages", ""]
        lines.extend(stage_lines)
        if has_skips:
            lines += [
                "",
                "> ⚠️ One or more stages could not produce a full signal (see `skipped:` "
                "reasons above). Enable `characterization_strict=True` or `smoke_strict=True` "
                "in your repo config to make these stages block the PR instead of warn.",
            ]

    lines.append("")
    if result.codemod_notes:
        lines += ["", "### Codemod notes", *[f"- {note}" for note in result.codemod_notes[:20]]]
    if grouped_members:
        lines += [
            "",
            "### Grouped changes",
            "",
            "| Change | Confidence | Files |",
            "| --- | --- | --- |",
        ]
        for member_change, member_result, member_edits in grouped_members:
            lines.append(
                f"| {member_change.package}: `{member_change.old_api}` → "
                f"`{member_change.replacement}` "
                f"| `{member_result.confidence.value}` | {len(member_edits)} |"
            )
        lines += [
            "",
            "_This PR groups several related changes. Each row's confidence "
            "tier is independent; review lower-confidence rows carefully._",
        ]

    removal_section = _removal_lines(removals or [])
    if removal_section:
        lines += removal_section

    lines.append("")
    lines.append("_Opened automatically by [depfix](https://github.com/bedilk/dependency_check)._")
    if escalated:
        lines.append("_A fallback model was used after the primary produced no committable edits._")
    if plan_digest:
        lines.append(f"_Applied from plan artifact `{plan_digest}`._")
    lines += ["", _pr_state_trailer(change=change, result=result, plan_digest=plan_digest)]
    return "\n".join(lines)


def _removal_lines(removals) -> list[str]:
    blocking = [r for r in removals if r.blocks_merge]
    upcoming = [r for r in removals if r.reachable and r.impact == "upcoming"]
    if not blocking and not upcoming:
        return []
    label = {
        "breaks_on_upgrade": "❌ breaks with this upgrade",
        "already_removed": "❌ already gone in the installed version",
    }
    lines = ["", "### ⚠️ Removed APIs still called by this repository", ""]
    if blocking:
        lines += [
            "These methods no longer exist upstream. **depfix did not rewrite them** — "
            "migrate these call sites before merging.",
            "",
            "| Removed API | Removed in | Status | Call sites | What to use instead |",
            "| --- | --- | --- | --- | --- |",
        ]
        for r in blocking:
            sites = ", ".join(f"`{s}`" for s in r.sites[:5]) + (
                f" (+{len(r.sites) - 5} more)" if len(r.sites) > 5 else ""
            )
            match_note = " _(suffix match)_" if r.match == "suffix" else ""
            guidance = _sanitize_md(r.guidance or "—").replace("|", "\\|")[:160]
            lines.append(
                f"| `{r.old_api}`{match_note} | {r.removed_in or '?'} "
                f"| {label.get(r.impact, r.impact)} | {sites} | {guidance} |"
            )
    if upcoming:
        lines += [
            "",
            f"<details><summary>Scheduled removals in later versions ({len(upcoming)})</summary>",
            "",
        ]
        lines += [
            f"- `{r.old_api}` removed in {r.removed_in}: {', '.join(r.sites[:3])}" for r in upcoming
        ]
        lines += ["", "</details>"]
    return lines


def build_upgrade_pr_body(
    artifact,
    *,
    escalated: bool = False,
    plan_digest: str = "",
) -> str:
    """PR body for a combined upgrade: version bump + API migrations in one PR."""
    plan_pkg = artifact.upgrade_package
    span = f"{artifact.upgrade_from or '?'} → {artifact.upgrade_to}"
    lines = [
        f"## Upgrade `{plan_pkg}` {span}",
        "",
        f"- **Package:** {plan_pkg}",
        f"- **From:** {artifact.upgrade_from or '?'}",
        f"- **To:** {artifact.upgrade_to}",
        f"- **Confidence:** `{artifact.confidence}`",
    ]

    if artifact.steps:
        lines += [
            "",
            "### Steps",
            "",
            "| # | Kind | Summary | Files |",
            "| --- | --- | --- | --- |",
        ]
        for i, step in enumerate(artifact.steps, 1):
            relpaths = ", ".join(f"`{r}`" for r in step.relpaths[:5])
            if len(step.relpaths) > 5:
                relpaths += f" (+{len(step.relpaths) - 5} more)"
            lines.append(f"| {i} | {step.kind} | {step.summary} | {relpaths} |")

    if artifact.edits:
        lines += [
            "",
            "### Files changed",
            "",
            "| File | Confidence | Usages fixed |",
            "| --- | --- | --- |",
        ]
        for edit in artifact.edits:
            lines.append(f"| `{edit.relpath}` | {edit.confidence:.0%} | {edit.usages_fixed} |")

    if artifact.member_dedupe_keys:
        lines += [
            "",
            f"### Bundled migrations ({len(artifact.member_dedupe_keys)})",
            "",
            "This PR bundles the version bump with API migrations that fall inside "
            f"the {span} window. Each migration was verified as part of the upgrade unit.",
        ]

    removal_section = _removal_lines(list(artifact.removals))
    if removal_section:
        lines += removal_section

    if artifact.notes:
        lines += ["", "### Notes", ""]
        lines.extend(f"- {n}" for n in artifact.notes)

    lines.append("")
    lines.append("_Opened automatically by [depfix](https://github.com/bedilk/dependency_check)._")
    if escalated:
        lines.append("_A fallback model was used after the primary produced no committable edits._")
    if plan_digest:
        lines.append(f"_Applied from plan artifact `{plan_digest}`._")
    lines += [
        "",
        f"<!-- depfix:v1 upgrade={plan_pkg} from={artifact.upgrade_from or '?'} to={artifact.upgrade_to} confidence={artifact.confidence} -->",
    ]
    return "\n".join(lines)


def _author_label(edit: FileEdit) -> str:
    if edit.origin is EditOrigin.CODEMOD:
        return f"codemod `{edit.codemod_id}`" if edit.codemod_id else "codemod"
    if edit.origin is EditOrigin.PLAN:
        return "plan replay"
    return "model"


def _is_major_dependency_review(change: BreakingChange) -> bool:
    if change.kind.value != "dependency_version_bump":
        return False
    from depfix.codemods.lockfile import dependency_drift_policy

    return dependency_drift_policy(change) == "review_pr"


_PR_TRAILER_PREFIX = "<!-- depfix:v1"


def _pr_state_trailer(
    *, change: BreakingChange, result: FixPipelineResult, plan_digest: str
) -> str:
    parts = [
        _PR_TRAILER_PREFIX,
        f"change={change.dedupe_key[:16]}",
        f"provider={change.provider_id}",
        f"confidence={result.confidence.value}",
    ]
    if plan_digest:
        parts.append(f"plan={plan_digest}")
    return " ".join([*parts, "-->"])


def parse_pr_state_trailer(body: str) -> dict[str, str] | None:
    for line in reversed(body.splitlines()):
        line = line.strip()
        if line.startswith(_PR_TRAILER_PREFIX):
            fields = line[len(_PR_TRAILER_PREFIX) :].removesuffix("-->").strip().split()
            parsed = {
                key: value
                for token in fields
                if "=" in token
                for key, _, value in [token.partition("=")]
            }
            return parsed or None
    return None


def kept_edits(result: FixPipelineResult) -> list[FileEdit]:
    """Only test-confirmed edits belong in a PR -- ``SUSPECT`` edits never
    ran against the real test suite (or failed to), so they stay out."""
    return [edit for edit in result.edits if edit.verdict == EditVerdict.KEPT]


def committable_edits(
    result: FixPipelineResult,
    *,
    allow_unverified: bool = False,
    allow_review_suspects: bool = False,
) -> list[FileEdit]:
    """Edits safe to push to a branch.

    Defaults to the same test-confirmed-only set as :func:`kept_edits`. When
    ``allow_unverified=True`` (a repo with ``verify: false`` in its
    ``.depfix.yml`` -- see :mod:`depfix.repoconfig`, which has no test suite
    to confirm against), ``APPLIED`` edits are committable too since they
    were never run through :class:`~depfix.verify.verifier.Verifier` in the
    first place. ``SUSPECT`` edits are excluded either way -- a fix whose
    syntax couldn't even be validated has no business in a PR.
    """
    verdicts = {EditVerdict.KEPT, EditVerdict.APPLIED} if allow_unverified else {EditVerdict.KEPT}
    edits = [edit for edit in result.edits if edit.verdict in verdicts]
    if allow_review_suspects:
        # Deterministic registry-drift PRs may proceed when the repository's
        # test reporter/typecheck is unavailable. A suspect edit is admitted
        # only when syntax validation itself succeeded/was unnecessary; this
        # covers unavailable verification tooling, never malformed source.
        edits.extend(
            edit
            for edit in result.edits
            if edit.verdict is EditVerdict.SUSPECT
            and (edit.validation is None or edit.validation.is_valid)
        )
    return edits


def open_pull_request(
    *,
    owner: str,
    repo: str,
    token: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    draft: bool = False,
    api_url: str = DEFAULT_GITHUB_API_URL,
    http_timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> PullRequest:
    """Open a PR from ``head_branch`` onto ``base_branch``. If one already
    exists for that head/base (GitHub returns 422), look it up and return it
    with ``already_existed=True`` instead of failing -- idempotent re-runs
    for the same deterministic branch (see :func:`depfix.gh.branch.branch_name_for`)
    shouldn't error just because a previous run already opened the PR.
    """
    api_url = api_url.rstrip("/")
    owned_client = client is None
    http_client = client or httpx.Client(timeout=http_timeout)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = http_client.post(
            f"{api_url}/repos/{owner}/{repo}/pulls",
            headers=headers,
            json={
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
                "draft": draft,
            },
        )
        if resp.status_code == 422:
            existing = http_client.get(
                f"{api_url}/repos/{owner}/{repo}/pulls",
                headers=headers,
                params={"head": f"{owner}:{head_branch}", "base": base_branch, "state": "open"},
            )
            _raise_for_status(existing)
            results = existing.json()
            if not results:
                raise GitHubAppError(
                    f"GitHub rejected PR creation for {head_branch} -> {base_branch} as "
                    f"unprocessable (422), but no existing open PR was found: {resp.text[:300]}"
                )
            return _pull_request_from_dict(results[0], already_existed=True)
        _raise_for_status(resp)
        return _pull_request_from_dict(resp.json(), already_existed=False)
    finally:
        if owned_client:
            http_client.close()


def _pull_request_from_dict(item: dict, *, already_existed: bool) -> PullRequest:
    return PullRequest(
        number=item["number"],
        html_url=item["html_url"],
        head_branch=item["head"]["ref"],
        base_branch=item["base"]["ref"],
        already_existed=already_existed,
    )


def apply_pr_labels(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    labels: list[str],
    token: str,
    api_url: str = DEFAULT_GITHUB_API_URL,
    http_timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> None:
    """Best-effort, idempotent label attachment for an existing PR."""
    if not labels:
        return
    owned_client = client is None
    http_client = client or httpx.Client(timeout=http_timeout)
    try:
        response = http_client.post(
            f"{api_url.rstrip('/')}/repos/{owner}/{repo}/issues/{pr_number}/labels",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"labels": labels},
        )
        if response.status_code >= 400:
            logger.warning(
                "failed to apply labels %s to %s/%s#%d: %d %s",
                labels,
                owner,
                repo,
                pr_number,
                response.status_code,
                response.text[:200],
            )
    except httpx.HTTPError as exc:
        logger.warning("label call to %s/%s#%d failed: %s", owner, repo, pr_number, exc)
    finally:
        if owned_client:
            http_client.close()


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise GitHubAppError(
            f"GitHub rejected the token (401 Unauthorized) for {resp.request.url}."
        )
    if resp.status_code == 404:
        raise GitHubAppError(f"GitHub resource not found: {resp.request.url}")
    if resp.status_code == 422:
        raise GitHubAppError(
            f"GitHub rejected the request as unprocessable (422): {resp.text[:300]}"
        )
    if resp.status_code >= 400:
        raise GitHubAppError(f"GitHub API error {resp.status_code}: {resp.text[:300]}")
