#!/usr/bin/env python3
"""Assemble RESULTS.md's measurement tables from artifacts already on disk.

This exists because hand-assembling numbers into a markdown table is how
measurement records rot: the table and the artifacts drift, and nobody can
tell which was right. So the tables in RESULTS.md are *generated*, and this
script is the only thing allowed to write them.

Three deliberate properties:

* **Reports coverage, not just averages.** "mean cost $0.04" is unreadable
  without "across 3 of 57 artifacts". Every aggregate carries its n and the
  fraction of artifacts that actually had the field.
* **Distinguishes zero from absent.** 57 artifacts at $0.00 because they were
  all manifest-only drift is a *finding*. 57 artifacts with no cost field is a
  *bug*. Collapsing both to "0" hides the difference that matters.
* **Refuses to synthesise the gating metric.** ``--check`` exits non-zero when
  the code-migration KEPT rate has n=0, so CI can enforce that DECISION.md
  cannot claim a verdict while its input is missing.

Usage:
    python scripts/collect_metrics.py                 # print tables
    python scripts/collect_metrics.py --write         # splice into docs/RESULTS.md
    python scripts/collect_metrics.py --check         # CI gate
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANS_DIR = REPO_ROOT / ".depfix" / "plans"
RESULTS_PATH = REPO_ROOT / "docs" / "RESULTS.md"
EVAL_REPORT = REPO_ROOT / ".depfix" / "eval" / "report.json"
FORK_MANIFEST = REPO_ROOT / ".depfix" / "day3" / "runs.jsonl"

BEGIN = "<!-- BEGIN GENERATED: collect_metrics.py -->"
END = "<!-- END GENERATED -->"

#: Anything under one of these is a dependency declaration, not application
#: code. The distinction is the whole point of this script: a manifest bump
#: costs $0 and proves nothing about the LLM's ability to migrate code.
MANIFEST_PATTERNS = (
    re.compile(r"(^|/)package(-lock)?\.json$"),
    re.compile(r"(^|/)(yarn|pnpm)-lock\.yaml$"),
    re.compile(r"(^|/)requirements[^/]*\.txt$"),
    re.compile(r"(^|/)(pyproject|Pipfile)\.toml$"),
    re.compile(r"(^|/)(poetry|Pipfile)\.lock$"),
    re.compile(r"(^|/)Gemfile(\.lock)?$"),
    re.compile(r"(^|/)go\.(mod|sum)$"),
    re.compile(r"(^|/)Cargo\.(toml|lock)$"),
)


def is_manifest(relpath: str) -> bool:
    return any(pattern.search(relpath) for pattern in MANIFEST_PATTERNS)


def dig(node: Any, *path: str) -> Any:
    """Walk a nested dict, returning None on any miss.

    Plan artifacts are versioned and the schema has moved; probing rather
    than indexing means an older artifact degrades to 'field absent' instead
    of crashing the whole collection.
    """
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def first_present(artifact: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = dig(artifact, *path)
        if value is not None:
            return value
    return None


@dataclass
class Aggregate:
    """A number plus how much of the corpus it actually covers."""

    values: list[float] = field(default_factory=list)
    absent: int = 0

    def observe(self, value: Any) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.values.append(float(value))
        else:
            self.absent += 1

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def total(self) -> float:
        return sum(self.values)

    @property
    def mean(self) -> float | None:
        return self.total / self.n if self.n else None

    @property
    def p50(self) -> float | None:
        if not self.values:
            return None
        ordered = sorted(self.values)
        return ordered[len(ordered) // 2]

    @property
    def p95(self) -> float | None:
        if not self.values:
            return None
        ordered = sorted(self.values)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

    def coverage(self, corpus: int) -> str:
        return f"{self.n}/{corpus}" if corpus else "0/0"


@dataclass
class PlanRecord:
    path: Path
    repo: str
    provider: str
    kind: str
    confidence: str
    edit_paths: list[str]
    cost: Any
    tokens: Any
    duration_ms: Any
    verdicts: Counter

    @property
    def touches_code(self) -> bool:
        """Did this plan edit anything that isn't a dependency declaration?

        A plan that only rewrote package.json tells you the orchestrator
        works; it tells you nothing about whether the model can migrate a
        call site, which is the claim depfix is actually making.
        """
        return any(not is_manifest(p) for p in self.edit_paths)


def load_plans(plans_dir: Path) -> tuple[list[PlanRecord], list[str]]:
    records: list[PlanRecord] = []
    problems: list[str] = []
    if not plans_dir.exists():
        return records, [f"{plans_dir} does not exist"]

    for path in sorted(plans_dir.glob("**/*.json")):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path.name}: unreadable ({exc})")
            continue
        if not isinstance(artifact, dict):
            problems.append(f"{path.name}: not a JSON object")
            continue

        edits = (
            first_present(artifact, ("edits",), ("result", "edits"), ("plan", "edits")) or []
        )
        edit_paths: list[str] = []
        verdicts: Counter = Counter()
        for edit in edits if isinstance(edits, list) else []:
            if not isinstance(edit, dict):
                continue
            relpath = edit.get("relpath") or edit.get("path") or edit.get("filepath")
            if isinstance(relpath, str):
                edit_paths.append(relpath)
            verdict = edit.get("verdict") or edit.get("status")
            if isinstance(verdict, str):
                verdicts[verdict.upper()] += 1

        records.append(
            PlanRecord(
                path=path,
                repo=str(
                    first_present(
                        artifact, ("repo_full_name",), ("repo",), ("result", "repo_full_name")
                    )
                    or "unknown"
                ),
                provider=str(
                    first_present(
                        artifact,
                        ("breaking_change", "provider_id"),
                        ("change", "provider_id"),
                        ("provider_id",),
                    )
                    or "unknown"
                ),
                kind=str(
                    first_present(
                        artifact, ("breaking_change", "kind"), ("change", "kind"), ("kind",)
                    )
                    or "unknown"
                ),
                confidence=str(
                    first_present(artifact, ("confidence",), ("result", "confidence"))
                    or "absent"
                ),
                edit_paths=edit_paths,
                cost=first_present(
                    artifact, ("total_cost",), ("result", "total_cost"), ("cost_estimate",)
                ),
                tokens=first_present(artifact, ("total_tokens",), ("result", "total_tokens")),
                duration_ms=first_present(
                    artifact, ("duration_ms",), ("result", "duration_ms")
                ),
                verdicts=verdicts,
            )
        )
    return records, problems


def load_eval(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return report if isinstance(report, dict) else None


def load_fork_runs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _money(value: float | None) -> str:
    return f"${value:.4f}" if value is not None else "—"


def _ms(value: float | None) -> str:
    return f"{value / 1000:.1f}s" if value is not None else "—"


def _blocked(reason: str, command: str) -> str:
    return f"**NOT MEASURED** — {reason}<br>_unblocked by:_ `{command}`"


def render(
    plans: list[PlanRecord],
    problems: list[str],
    eval_report: dict[str, Any] | None,
    fork_runs: list[dict[str, Any]],
) -> str:
    code_plans = [p for p in plans if p.touches_code]
    manifest_plans = [p for p in plans if not p.touches_code]

    lines: list[str] = [BEGIN, ""]

    # -- corpus composition --------------------------------------------------
    lines += [
        "### Plan artifacts on disk",
        "",
        f"Source: `.depfix/plans` — **{len(plans)}** artifact(s) parsed.",
        "",
        "| Class | Count | What it proves |",
        "| --- | ---: | --- |",
        f"| Manifest-only (dependency declaration edits) | {len(manifest_plans)} | "
        "orchestration, idempotency, PR plumbing |",
        f"| Code-migration (touches application source) | {len(code_plans)} | "
        "**the actual product claim** — LLM rewrites a call site and it survives verification |",
        "",
    ]
    if problems:
        lines += [
            f"{len(problems)} artifact(s) could not be parsed:",
            "",
            *[f"- `{p}`" for p in problems[:10]],
            "",
        ]

    # -- cost & latency (task 2.4) -------------------------------------------
    cost = Aggregate()
    tokens = Aggregate()
    duration = Aggregate()
    for plan in plans:
        cost.observe(plan.cost)
        tokens.observe(plan.tokens)
        duration.observe(plan.duration_ms)

    code_cost = Aggregate()
    for plan in code_plans:
        code_cost.observe(plan.cost)

    lines += [
        "### Cost and latency",
        "",
        "| Metric | n (coverage) | Total | Mean | p50 | p95 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
        f"| Cost, all plans | {cost.coverage(len(plans))} | {_money(cost.total)} | "
        f"{_money(cost.mean)} | {_money(cost.p50)} | {_money(cost.p95)} |",
        f"| Cost, code-migration only | {code_cost.coverage(len(code_plans))} | "
        f"{_money(code_cost.total)} | {_money(code_cost.mean)} | {_money(code_cost.p50)} | "
        f"{_money(code_cost.p95)} |",
        f"| Tokens, all plans | {tokens.coverage(len(plans))} | {tokens.total:,.0f} | "
        f"{tokens.mean or 0:,.0f} | {tokens.p50 or 0:,.0f} | {tokens.p95 or 0:,.0f} |",
        f"| Wall clock per plan | {duration.coverage(len(plans))} | {_ms(duration.total)} | "
        f"{_ms(duration.mean)} | {_ms(duration.p50)} | {_ms(duration.p95)} |",
        "",
    ]
    if cost.total == 0.0 and cost.n > 0:
        lines += [
            "> **Reading this honestly:** a total of $0.00 across "
            f"{cost.n} artifact(s) with the field present is not a missing "
            "measurement — it is the finding. Every recorded run was manifest-only "
            "drift, which is resolved by a deterministic codemod and never reaches "
            "an LLM. The cost of the thing depfix is actually selling is still "
            "unmeasured.",
            "",
        ]

    # -- verdict distribution ------------------------------------------------
    all_verdicts: Counter = Counter()
    code_verdicts: Counter = Counter()
    for plan in plans:
        all_verdicts.update(plan.verdicts)
    for plan in code_plans:
        code_verdicts.update(plan.verdicts)

    lines += ["### Edit verdict distribution", ""]
    if all_verdicts:
        total_edits = sum(all_verdicts.values())
        lines += ["| Verdict | All plans | Code-migration plans |", "| --- | ---: | ---: |"]
        for verdict in sorted(set(all_verdicts) | set(code_verdicts)):
            lines.append(
                f"| `{verdict}` | {all_verdicts.get(verdict, 0)} | "
                f"{code_verdicts.get(verdict, 0)} |"
            )
        lines += ["", f"Total edits recorded: {total_edits}.", ""]
    else:
        lines += ["No edit verdicts recorded in any artifact.", ""]

    # -- the gating metric (task 3.3) ----------------------------------------
    kept = code_verdicts.get("KEPT", 0)
    code_edits = sum(code_verdicts.values())
    lines += ["### Code-migration KEPT rate — the gating metric", ""]
    if code_edits:
        rate = kept / code_edits
        lines += [
            f"**{rate:.0%}** ({kept} KEPT / {code_edits} code edits across "
            f"{len(code_plans)} plan(s)).",
            "",
            "Compare against the pre-committed thresholds in `docs/DECISION.md`. "
            "Do not edit those thresholds now that this number exists.",
            "",
        ]
    else:
        lines += [
            _blocked(
                "no plan artifact contains an edit to application source code, so there "
                "is nothing to compute a KEPT rate over",
                "bash scripts/day3_fork_runs.sh",
            ),
            "",
        ]

    # -- real-repo fork runs (task 3.1) --------------------------------------
    lines += ["### Real-repo fork runs", ""]
    if fork_runs:
        lines += [
            "| Fork | Provider | Change | Plan | Apply | KEPT/edits | Cost |",
            "| --- | --- | --- | --- | --- | ---: | ---: |",
        ]
        for row in fork_runs:
            lines.append(
                f"| `{row.get('fork', '?')}` | {row.get('provider', '?')} | "
                f"{row.get('change', '?')} | {row.get('plan_status', '?')} | "
                f"{row.get('apply_status', '?')} | "
                f"{row.get('kept', 0)}/{row.get('edits', 0)} | "
                f"{_money(row.get('cost'))} |"
            )
        lines.append("")
    else:
        lines += [
            _blocked(
                "`.depfix/day3/runs.jsonl` is absent — no fork run has been recorded",
                "bash scripts/day3_fork_runs.sh",
            ),
            "",
        ]

    # -- eval corpus ---------------------------------------------------------
    lines += ["### Eval corpus", ""]
    if eval_report:
        lines += ["| Category | Cases | Passed | Score |", "| --- | ---: | ---: | ---: |"]
        categories = eval_report.get("categories") or {}
        if isinstance(categories, dict):
            for name, body in sorted(categories.items()):
                if not isinstance(body, dict):
                    continue
                lines.append(
                    f"| {name} | {body.get('total', '?')} | {body.get('passed', '?')} | "
                    f"{body.get('score', '?')} |"
                )
        lines += ["", f"Model: `{eval_report.get('model', 'unrecorded')}`.", ""]
    else:
        lines += [
            _blocked(
                "`.depfix/eval/report.json` is absent — the corpus exists "
                "(30 cases) but no scored run has been persisted",
                "depfix eval --report .depfix/eval/report.json",
            ),
            "",
        ]

    lines += [END]
    return "\n".join(lines)


def splice(markdown: str, generated: str) -> str:
    if BEGIN in markdown and END in markdown:
        head, _, rest = markdown.partition(BEGIN)
        _, _, tail = rest.partition(END)
        return f"{head}{generated}{tail}"
    return f"{markdown.rstrip()}\n\n{generated}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="splice into docs/RESULTS.md")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the code-migration KEPT rate has n=0 (CI gate)",
    )
    args = parser.parse_args()

    plans, problems = load_plans(PLANS_DIR)
    generated = render(plans, problems, load_eval(EVAL_REPORT), load_fork_runs(FORK_MANIFEST))

    if args.write:
        existing = RESULTS_PATH.read_text(encoding="utf-8") if RESULTS_PATH.exists() else ""
        RESULTS_PATH.write_text(splice(existing, generated), encoding="utf-8")
        print(f"wrote {RESULTS_PATH.relative_to(REPO_ROOT)}")
    else:
        print(generated)

    if args.check:
        code_edits = sum(sum(p.verdicts.values()) for p in plans if p.touches_code)
        if code_edits == 0:
            print(
                "\nFAIL: no code-migration edits recorded. The gating metric for "
                "docs/DECISION.md cannot be computed, so no go/no-go verdict is "
                "admissible. Run: bash scripts/day3_fork_runs.sh",
                file=sys.stderr,
            )
            return 1
        print(f"\nOK: {code_edits} code-migration edit(s) available to score.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
