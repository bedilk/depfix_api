"""Turns a rejected edit into a sentence a human can act on.

``no_kept_edits`` used to be the entire explanation for the most common
failure in the pipeline, which made every diagnosis a blind bisect: was it
the model, the syntax validator, the test suite, or the test *reporter*?
That information all existed -- spread across ``FileEdit.verdict``,
``FileEdit.validation``, ``VerificationReport.attribution``, and the
typecheck diagnostics -- and none of it reached the operator.

This module is the one place that joins those four sources into
``verdict -> reason -> evidence -> hint``, so the orchestrator's outcome
detail, the CLI's per-file table, and the PR body all say the same thing.
Every hint names a concrete next action; a hint that would just restate the
reason is omitted rather than padded.
"""

from __future__ import annotations

from dataclasses import dataclass

from depfix.apply.models import EditVerdict, FileEdit
from depfix.core.pipeline import FixPipelineResult

_COMMITTABLE = frozenset({EditVerdict.KEPT, EditVerdict.APPLIED})


@dataclass(frozen=True)
class RejectionExplanation:
    relpath: str
    verdict: EditVerdict
    reason: str
    evidence: tuple[str, ...] = ()
    hint: str = ""

    def one_line(self) -> str:
        return f"{self.relpath}: {self.verdict.value} -- {self.reason}"

    def render(self, *, indent: str = "  ") -> str:
        lines = [f"{indent}{self.one_line()}"]
        lines += [f"{indent}    {item}" for item in self.evidence]
        if self.hint:
            lines.append(f"{indent}    hint: {self.hint}")
        return "\n".join(lines)


def explain_edit(edit: FileEdit, result: FixPipelineResult) -> RejectionExplanation | None:
    """Explain why ``edit`` isn't committable, or ``None`` if it is."""
    if edit.verdict in _COMMITTABLE:
        return None

    verification = result.verification

    if edit.verdict == EditVerdict.SKIPPED:
        if edit.error_message:
            return RejectionExplanation(
                edit.relpath,
                edit.verdict,
                "the edit was never written",
                evidence=(edit.error_message,),
                hint=(
                    "check the path exists in the checkout and is UTF-8 text; a generated "
                    "or vendored file may need a `paths.exclude` entry in .depfix.yml"
                ),
            )
        if not edit.changed:
            return RejectionExplanation(
                edit.relpath,
                edit.verdict,
                "the model returned the file unchanged",
                hint=(
                    "the call site may already be migrated, or the model didn't recognise it -- "
                    "re-run with a stronger model, or narrow the change's call_site_hints"
                ),
            )
        return RejectionExplanation(edit.relpath, edit.verdict, "the edit was never written")

    if edit.verdict == EditVerdict.REVERTED:
        if verification is not None and verification.attribution is not None:
            cases = verification.attribution.by_file.get(edit.relpath, [])
            if cases:
                return RejectionExplanation(
                    edit.relpath,
                    edit.verdict,
                    "this fix introduced a new test failure and was rolled back",
                    evidence=tuple(
                        f"failing: {c.identity}"
                        + (f" -- {c.message.splitlines()[0]}" if c.message else "")
                        for c in cases[:5]
                    ),
                    hint="re-run with --max-retries 2 to feed the failure back to the model",
                )
        added = verification.new_diagnostics if verification is not None else ()
        mine = [d for d in added if d.relpath == edit.relpath]
        if mine:
            return RejectionExplanation(
                edit.relpath,
                edit.verdict,
                "this fix introduced new TypeScript errors and was rolled back",
                evidence=tuple(d.one_line() for d in mine[:5]),
                hint="re-run with --max-retries 2, or check the migration guide's new response shape",
            )
        return RejectionExplanation(
            edit.relpath, edit.verdict, "this fix was rolled back by verification"
        )

    # SUSPECT: written, but nothing could confirm it.
    if edit.validation is not None and not edit.validation.is_valid:
        return RejectionExplanation(
            edit.relpath,
            edit.verdict,
            "the generated code failed the syntax/sanity check",
            evidence=tuple(filter(None, (edit.validation.error_message,))),
            hint="re-run with --max-retries 2, or use a stronger model for this repo",
        )

    if verification is not None:
        reason = verification.tier_reason or verification.skipped_reason
        evidence: list[str] = []
        if verification.skipped_reason:
            evidence.append(f"tests: {verification.skipped_reason}")
        if verification.after_fix is not None and verification.after_fix.used_fallback_parser:
            evidence.append(
                f"test output was unparseable (framework={verification.after_fix.framework.value}); "
                "only aggregate counts were recovered"
            )
        if verification.after_fix is not None and verification.after_fix.parse_error:
            evidence.append(f"parse error: {verification.after_fix.parse_error}")
        tc = verification.typecheck_after or verification.typecheck_baseline
        if tc is not None and tc.skipped_reason:
            evidence.append(f"typecheck: {tc.skipped_reason}")
        if verification.attribution is not None and verification.attribution.unattributed:
            evidence.append(
                f"{len(verification.attribution.unattributed)} new test failure(s) "
                "could not be attributed to any edited file"
            )
        return RejectionExplanation(
            edit.relpath,
            edit.verdict,
            reason or "nothing could confirm this fix",
            evidence=tuple(evidence),
            hint=_suspect_hint(verification),
        )

    return RejectionExplanation(edit.relpath, edit.verdict, "nothing could confirm this fix")


def _suspect_hint(verification) -> str:
    if verification.skipped_reason == "repo has no test script":
        return (
            "this repo has no test suite: add a tsconfig.json so `tsc --noEmit` can verify "
            "the change, or set `verify: false` in .depfix.yml to accept unverified edits"
        )
    if verification.after_fix is not None and verification.after_fix.used_fallback_parser:
        return (
            "depfix could not parse this repo's test output -- check the `test` script emits "
            "TAP/JSON (run `depfix init --offline` to probe the local runner)"
        )
    if verification.install is not None and not verification.install.ok:
        return (
            "dependencies could not be installed, so neither tests nor a typecheck could run; "
            "check the lockfile installs cleanly with --ignore-scripts"
        )
    return "run `depfix init --offline` to check the local test/typecheck toolchain"


def explain_rejections(result: FixPipelineResult) -> list[RejectionExplanation]:
    return [e for e in (explain_edit(edit, result) for edit in result.edits) if e is not None]


def format_rejections(result: FixPipelineResult) -> str:
    """A multi-line, self-explaining replacement for ``"no kept/committable
    edits"`` -- safe to put straight into a ``ChangeOutcome.detail``."""
    explanations = explain_rejections(result)
    if not explanations:
        if result.codemod_notes:
            return "no committable edits: " + " ".join(result.codemod_notes)
        return "no committable edits, and no edit reported a reason (this is a depfix bug)"
    header = f"no committable edits ({len(explanations)} file(s)):"
    return header + "\n" + "\n".join(e.render() for e in explanations)
