"""Characterization tests: a behaviour oracle for repos with no test suite.

This is the weakest-to-obtain oracle and the one with a trap sharp enough to
justify a long docstring, because getting it wrong quietly defeats the whole
point of the system.

**You cannot generate a test from the fixed code and use it to validate that
fix.** A test the model wrote by looking at the migrated code will pass the
migrated code -- that is the model grading its own homework, and it will
endorse a wrong migration as happily as a right one. Every design choice
below exists to make that impossible:

1. Fixtures are generated *before the fix exists*, from the vendor's
   migration guide and the pre-fix source -- never from the fix. The model
   supplies only *inputs and mocks*, never assertions.
2. depfix supplies the oracle: "the subject module returns the same value
   before and after the migration". Behaviour preservation is our
   judgement, not the model's.
3. The fixture must pass three falsifiability gates before its verdict is
   trusted at all:

   * **baseline** -- old code + old mock must PASS. If it fails, the fixture
     describes something other than this module, so it is discarded.
   * **discrimination** -- old code + *new* mock must FAIL. A fixture the
     unmigrated code still passes under the new API proves nothing about the
     migration; it is vacuous and discarded. This is a mutation test on the
     oracle itself.
   * **contact** -- the subject must actually have loaded the mocked package,
     or the fixture exercised nothing relevant.

4. Only then: fixed code + new mock must PASS, and its snapshot must equal
   the baseline snapshot. A confirmed result earns ``MEDIUM`` and never
   ``HIGH`` -- an LLM-authored fixture is not a test the repo's maintainers
   wrote and reviewed.

Scope is deliberately CommonJS JavaScript. Interception uses
``Module._load``, which does not see ESM ``import``; and TypeScript repos
already have a stronger, cheaper oracle in :mod:`depfix.verify.typecheck`.
That split -- TS to typecheck, CJS JS to characterization -- is the intended
coverage story, not a gap.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from depfix.core.models import BreakingChange
from depfix.obs.cost import CostLedger, CostStage
from depfix.redaction import redact_text
from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES, run_sandboxed

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from depfix.classify.llm import LLMCompleter

_SUPPORTED_SUFFIXES = frozenset({".js", ".cjs", ".mjs"})

# The harness is a template with four sentinels substituted per probe. The
# oracle -- "did the subject return the same stable value?" -- lives here in
# depfix's own code, not in anything the model produced. The model only fills
# __DEPFIX_MOCKS__ and __DEPFIX_EXERCISE__.
_HARNESS = r"""// depfix characterization harness -- generated per run, never committed.
const Module = require('node:module');
const fs = require('node:fs');

const MOCKS = __DEPFIX_MOCKS__;

let moduleLoads = 0;
const originalLoad = Module._load;
Module._load = function (request) {
  if (Object.prototype.hasOwnProperty.call(MOCKS, request)) {
    moduleLoads += 1;
    return MOCKS[request];
  }
  return originalLoad.apply(this, arguments);
};

function stable(value) {
  if (value === null || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map(stable);
  return Object.keys(value).sort().reduce((acc, key) => {
    acc[key] = stable(value[key]);
    return acc;
  }, {});
}

(async () => {
  let payload;
  try {
    const subjectPath = __DEPFIX_SUBJECT__;
    const subject = subjectPath.endsWith('.mjs') ? await import(subjectPath) : require(subjectPath);
    const value = await (async () => { __DEPFIX_EXERCISE__ })();
    payload = { ok: true, value: stable(value === undefined ? null : value), moduleLoads };
  } catch (err) {
    payload = { ok: false, error: String((err && err.message) || err), moduleLoads };
  }
  fs.writeFileSync(__DEPFIX_OUT__, JSON.stringify(payload));
  process.exit(payload.ok ? 0 : 1);
})();
"""

# Fenced code blocks inside this prompt are built from an escaped backtick so
# no literal triple-backtick appears in this source file.
_FENCE = "\u0060\u0060\u0060"

_PROMPT = (
    "You are writing test *fixtures* for a dependency migration. You are NOT "
    "writing assertions -- a separate harness supplies those.\n\n"
    "Package: {package} ({old_version} -> {new_version})\n"
    "Old API: {old_api}\n"
    "New API: {new_api}\n"
    "Migration guide: {migration_guide}\n"
    "{examples}\n\n"
    "Subject module (CommonJS), at {relpath}:\n"
    + _FENCE
    + "javascript\n"
    + "{source}\n"
    + _FENCE
    + "\n\n"
    "Return ONLY a JSON object, with no prose and no markdown fences:\n"
    "{{\n"
    '  "mock_old": "<a JavaScript expression evaluating to a mock of the '
    "'{package}' module as it behaved at {old_version}>\",\n"
    '  "mock_new": "<the same mock, with the surface it has at {new_version}>",\n'
    '  "exercise": "<a JavaScript statement body that calls one exported '
    "function of `subject` with fixed literal inputs and RETURNS its result; "
    'use `return await ...`>"\n'
    "}}\n\n"
    "Requirements:\n"
    "- Both mocks are self-contained expressions: no imports, no I/O, no randomness.\n"
    "- Both mocks return the SAME logical data, differing only in the shape the "
    "SDK version dictates (for example, a `.data` wrapper present in one and "
    "absent in the other).\n"
    "- `exercise` is deterministic: literal inputs only, no dates, no network.\n"
    "- `mock_new` must NOT also support the old surface -- it is the new surface only.\n"
)


@dataclass
class Probe:
    """One harness execution."""

    label: str
    passed: bool = False
    module_loads: int = 0
    snapshot: str = ""
    error: str = ""


@dataclass
class CharacterizationReport:
    """Outcome of the whole protocol for one file."""

    ran: bool = False
    skipped_reason: str = ""
    relpath: str = ""
    #: True only when every falsifiability gate held and the fixed code
    #: reproduced the baseline snapshot.
    confirmed: bool = False
    rejected_reason: str = ""
    probes: list[Probe] = field(default_factory=list)
    cost_usd: float = 0.0

    def probe(self, label: str) -> Probe | None:
        return next((p for p in self.probes if p.label == label), None)


@dataclass(frozen=True)
class _Fixtures:
    mock_old: str
    mock_new: str
    exercise: str


class Characterizer:
    """Generates and runs characterization tests for one checkout.

    Holds an :class:`~depfix.classify.llm.LLMCompleter` rather than a fixer:
    it needs prose-to-JSON completion, not code editing, and reusing the
    classify seam means ``--llm-provider`` already selects the backend.
    """

    def __init__(
        self,
        completer: LLMCompleter,
        *,
        timeout: float = 120.0,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        ledger: CostLedger | None = None,
    ) -> None:
        self._completer = completer
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._ledger = ledger

    def supports(self, relpath: str) -> bool:
        return Path(relpath).suffix in _SUPPORTED_SUFFIXES

    def characterize(
        self,
        *,
        root: Path,
        relpath: str,
        original_source: str,
        change: BreakingChange,
        apply_fix: Callable[[], object],
        revert_fix: Callable[[], object],
    ) -> CharacterizationReport:
        """Run the full protocol for one file.

        ``apply_fix``/``revert_fix`` move the *real* file between its fixed
        and original content: the subject has to sit at its true path for
        relative ``require``s to resolve, so a temp copy is not an option.
        The caller owns the file's final state -- this method always leaves
        the fix applied on return, and never decides to keep or revert it.
        """
        report = CharacterizationReport(relpath=relpath)

        if shutil.which("node") is None:
            report.skipped_reason = "node is not on PATH"
            return report
        if not self.supports(relpath):
            report.skipped_reason = (
                f"characterization supports CommonJS .js/.cjs only (got "
                f"{Path(relpath).suffix!r}); TypeScript is verified by `tsc --noEmit` instead"
            )
            return report

        try:
            fixtures, cost = self._generate_fixtures(relpath, original_source, change)
        except Exception as exc:
            report.skipped_reason = f"fixture generation failed: {exc}"
            return report
        report.cost_usd = cost
        report.ran = True

        harness_dir = root / ".depfix-characterize"
        harness_dir.mkdir(parents=True, exist_ok=True)
        try:
            revert_fix()
            baseline = self._run_probe(
                "old-code+old-mock",
                root,
                harness_dir,
                relpath,
                fixtures.mock_old,
                fixtures.exercise,
            )
            report.probes.append(baseline)
            if not baseline.passed:
                report.rejected_reason = (
                    "the generated fixture does not pass against the unmodified code, so it "
                    f"describes something other than this module's behaviour ({baseline.error})"
                )
                return report
            if baseline.module_loads == 0:
                report.rejected_reason = (
                    f"the subject never loaded {change.package!r}, so this fixture exercises "
                    "nothing related to the migration"
                )
                return report

            discriminator = self._run_probe(
                "old-code+new-mock",
                root,
                harness_dir,
                relpath,
                fixtures.mock_new,
                fixtures.exercise,
            )
            report.probes.append(discriminator)
            if discriminator.passed:
                # The oracle can't tell migrated from unmigrated code.
                # Trusting it would be indistinguishable from trusting nothing.
                report.rejected_reason = (
                    "vacuous fixture: the unmigrated code still passes against the new-API "
                    "mock, so this test cannot detect whether the migration happened"
                )
                return report

            apply_fix()
            confirmation = self._run_probe(
                "fixed-code+new-mock",
                root,
                harness_dir,
                relpath,
                fixtures.mock_new,
                fixtures.exercise,
            )
            report.probes.append(confirmation)
            if not confirmation.passed:
                report.rejected_reason = (
                    f"the migrated code fails against the new-API mock ({confirmation.error})"
                )
                return report
            if confirmation.snapshot != baseline.snapshot:
                report.rejected_reason = (
                    "the migrated code returns a different value than the original "
                    f"(before: {baseline.snapshot[:200]}, after: {confirmation.snapshot[:200]})"
                )
                return report

            report.confirmed = True
            return report
        finally:
            # Never leave the fix reverted -- the caller expects it applied.
            apply_fix()
            shutil.rmtree(harness_dir, ignore_errors=True)

    # -- internals -------------------------------------------------------------

    def _generate_fixtures(
        self, relpath: str, source: str, change: BreakingChange
    ) -> tuple[_Fixtures, float]:
        examples = ""
        if change.examples:
            before, after = change.examples[0]
            examples = f"Example before:\n{before}\nExample after:\n{after}"
        prompt = _PROMPT.format(
            package=change.package,
            old_version=change.old_version,
            new_version=change.new_version,
            old_api=change.old_api,
            new_api=change.replacement,
            migration_guide=change.migration_guide or "(none supplied)",
            examples=examples,
            relpath=relpath,
            source=redact_text(source)[:12000],
        )
        response = self._completer.complete(prompt, temperature=0.0)
        if self._ledger is not None:
            self._ledger.record(
                CostStage.CHARACTERIZATION,
                response.cost_estimate,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        payload = _parse_json_object(response.text)
        for key in ("mock_old", "mock_new", "exercise"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"fixture response is missing {key!r}")
        if payload["mock_old"].strip() == payload["mock_new"].strip():
            # Identical mocks make the discrimination gate impossible to fail,
            # which would defeat the entire protocol.
            raise ValueError("mock_old and mock_new are identical")
        return (
            _Fixtures(payload["mock_old"], payload["mock_new"], payload["exercise"]),
            response.cost_estimate,
        )

    def _run_probe(
        self,
        label: str,
        root: Path,
        harness_dir: Path,
        relpath: str,
        mock: str,
        exercise: str,
    ) -> Probe:
        subject = (root / relpath).resolve()
        out_path = harness_dir / f"{label}.json"
        harness_path = harness_dir / f"{label}.harness.js"

        harness = _HARNESS
        harness = harness.replace("__DEPFIX_MOCKS__", mock)
        harness = harness.replace("__DEPFIX_OUT__", json.dumps(str(out_path)))
        harness = harness.replace("__DEPFIX_SUBJECT__", json.dumps(str(subject)))
        harness = harness.replace("__DEPFIX_EXERCISE__", exercise)
        harness_path.write_text(harness, encoding="utf-8")

        result = run_sandboxed(
            ["node", str(harness_path)],
            cwd=root,
            timeout=self._timeout,
            max_output_bytes=self._max_output_bytes,
        )
        probe = Probe(label=label)
        if result.error or result.timed_out:
            probe.error = result.error or "harness timed out"
            return probe
        if not out_path.is_file():
            probe.error = (result.stderr or result.stdout or "harness wrote no result").strip()[
                :400
            ]
            return probe
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            probe.error = f"unreadable harness output: {exc}"
            return probe
        probe.passed = bool(payload.get("ok"))
        probe.module_loads = int(payload.get("moduleLoads", 0) or 0)
        probe.error = str(payload.get("error", ""))
        probe.snapshot = json.dumps(payload.get("value"), sort_keys=True)
        return probe


def _parse_json_object(text: str) -> dict:
    stripped = text.strip()
    fence = "\u0060\u0060\u0060"
    if stripped.startswith(fence):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        stripped = stripped.rsplit(fence, 1)[0]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if match is None:
            raise ValueError("fixture response was not JSON") from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("fixture response was not a JSON object")
    return parsed
