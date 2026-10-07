"""Agent-driven scanning: an LLM reads provider- and repo-specific skill
files, uses bounded read-only tools, and produces the same
RepoScanResult / ChangeEvent shape the deterministic scanners produce.

This is the opt-in alternative to the regex/AST scanners in
depfix.scanners.callsites and the HTTP-only feed pollers in
depfix.sources. It is turned on with SCAN_STRATEGY=agent; the
deterministic path stays on the sideline, unchanged, as the default.

Design contract, in order of importance:

1. **One agent per (provider, repo) pair.** No shared context between
   providers or repos. Every invocation is a clean, replayable unit.
2. **Skills are the primary source.** The agent reads three markdown
   files verbatim: a shared AGENT.md, the provider's skill file, and
   (optionally) the repo's own SCAN.md override. Nothing else steers it.
3. **Bounded, read-only tools.** The toolset extends FixToolset with
   feed-polling helpers. No shell, no writes, no network beyond the
   registered tools. Hard step cap.
4. **Output shape unchanged.** The agent returns exactly what the
   deterministic scanners produce, so classifier/assessor/orchestrator
   need no changes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from depfix.agent.scan_tools import ScanToolset, ToolResult
from depfix.classify.llm import LLMCompleter
from depfix.core.models import LLMCall
from depfix.obs.cost import CostLedger, CostStage
from depfix.providers.models import ProviderSpec
from depfix.scanners.models import (
    SCANNER_VERSION,
    CallSite,
    CallSiteKind,
    MatchConfidence,
    RepoScanResult,
    ScanTarget,
)
from depfix.sources.models import ChangeEvent, Severity, SourceKind, SpecChange, SpecChangeKind

logger = logging.getLogger(__name__)

_DEFAULT_MAX_STEPS = 25
_SKILLS_DIR_NAME = "skills"
_SHARED_SKILL = "AGENT.md"
_PROVIDER_SKILL_TEMPLATE = "provider_{provider_id}.md"
_REPO_SKILL_NAME = ".depfix/SCAN.md"


@dataclass
class ScanAgentTranscript:
    """One agent run's audit trail. Persisted so a scan is replayable."""

    tool_calls: list[tuple[str, str, bool]] = field(default_factory=list)
    steps_used: int = 0
    stopped_reason: str = ""
    llm_calls: list[LLMCall] = field(default_factory=list)
    # Verified old-API call sites the agent staged via record_call_site_anchor.
    # Copied from the toolset after the run; the orchestrator persists them.
    discovered_anchors: list[dict] = field(default_factory=list)
    # Agent-derived old→new API mappings; each entry is
    # {"old_symbol": str, "new_symbol": str, "notes": str}.
    api_mapping: list[dict] = field(default_factory=list)


class ScanAgent:
    """Bounded LLM agent that scans one (provider, repo) pair.

    Not safe to share across pairs — the transcript, tool call log, and
    ledger state are per-invocation.
    """

    def __init__(
        self,
        completer: LLMCompleter,
        *,
        max_steps: int = _DEFAULT_MAX_STEPS,
        skills_dir: Path | None = None,
        ledger: CostLedger | None = None,
    ) -> None:
        self._completer = completer
        self._max_steps = max_steps
        self._skills_dir = skills_dir or _default_skills_dir()
        self._ledger = ledger

    # -- public entry points ---------------------------------------------------

    def augment_baseline(
        self,
        provider: ProviderSpec,
        target: ScanTarget,
        toolset: ScanToolset,
        baseline: RepoScanResult,
        *,
        repo_full_name: str = "",
        judge_cap: int = 20,
    ) -> RepoScanResult:
        """Augment a deterministic baseline in hybrid mode.

        The deterministic scanner already found the well-formed call sites
        for free. This method's job is narrow and cheap:

        1. Resolve LOW-confidence sites the deterministic scanner could not
           confirm — promote real ones to MEDIUM, drop fabricated ones.
        2. Find raw-HTTP calls to the provider's API that the regex scanner
           can't see (only when the provider declares api_base_urls).

        When deterministic already found actionable sites and there is no
        ambiguity and no raw-HTTP anchors to chase, this returns immediately
        spending $0.
        """
        # Only METHOD_CALL, BARE_SYMBOL, and WRAPPER_IMPORT sites qualify for
        # the judge loop. ANCHOR, RAW_HTTP, and API_VERSION_PIN sites are not
        # judgeable — a raw-HTTP grep alone must never trigger the judge loop.
        _JUDGEABLE = frozenset(
            {CallSiteKind.METHOD_CALL, CallSiteKind.BARE_SYMBOL, CallSiteKind.WRAPPER_IMPORT}
        )
        high_medium = [s for s in baseline.call_sites if s.is_actionable]
        low_sites = [s for s in baseline.call_sites if not s.is_actionable and s.kind in _JUDGEABLE]
        needs_judge = bool(low_sites)
        needs_anchor = _has_raw_http_anchors(provider, target)

        if not needs_judge and not needs_anchor and high_medium:
            logger.info(
                "%s/%s: deterministic found %d actionable site(s), no ambiguity "
                "— skipping agent augmentation ($0 spent)",
                repo_full_name,
                provider.id,
                len(high_medium),
            )
            return baseline

        augmented = list(baseline.call_sites)
        # Non-judgeable non-actionable sites (RAW_HTTP, API_VERSION_PIN, …) are
        # preserved unchanged and appended after judging.
        non_judgeable_low = [
            s for s in baseline.call_sites if not s.is_actionable and s.kind not in _JUDGEABLE
        ]

        if needs_judge and low_sites:
            judge = CallSiteJudgeAgent(
                self._completer,
                max_steps=self._max_steps,
                skills_dir=self._skills_dir,
                ledger=self._ledger,
            )
            kept = judge.run(provider, low_sites[:judge_cap], toolset)
            augmented = high_medium + kept + low_sites[judge_cap:] + non_judgeable_low

        if needs_anchor:
            anchor_sites = self._find_raw_http_sites(provider, target, toolset, repo_full_name)
            for site in anchor_sites:
                if toolset.verify_line(site.filepath, site.line_number, site.line_content):
                    augmented.append(site)

        from depfix.scanners.models import dedupe_call_sites

        return dataclasses.replace(baseline, call_sites=dedupe_call_sites(augmented))

    def _find_raw_http_sites(
        self,
        provider: ProviderSpec,
        target: ScanTarget,
        toolset: ScanToolset,
        repo_full_name: str,
    ) -> list[CallSite]:
        """Grep for raw HTTP calls to the provider's known API base URLs."""
        sites: list[CallSite] = []
        api_urls = list(getattr(target, "api_base_urls", ())) + list(
            getattr(provider, "api_base_urls", ())
        )
        for url in api_urls:
            host = url.rstrip("/").split("://")[-1].split("/")[0]
            result = toolset.grep(pattern=host, max_results=20)
            if not result.ok:
                continue
            for line in result.content.splitlines():
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                filepath, lineno_str = parts[0], parts[1]
                try:
                    line_number = int(lineno_str)
                except ValueError:
                    continue
                sites.append(
                    CallSite(
                        filepath=filepath,
                        line_number=line_number,
                        column=0,
                        line_content=parts[2].strip(),
                        kind=CallSiteKind.RAW_HTTP,
                        confidence=MatchConfidence.LOW,
                        symbol=f"raw_http:{host}",
                        provider_id=provider.id,
                        evidence=f"raw HTTP call to {host}",
                    )
                )
        return sites

    def scan_repo(
        self,
        provider: ProviderSpec,
        target: ScanTarget,
        toolset: ScanToolset,
        *,
        repo_full_name: str = "",
    ) -> tuple[RepoScanResult, ScanAgentTranscript]:
        """Agent scans one checkout for one provider's call sites.

        Also discovers old-API call sites and maps them to their new
        equivalents. Verified old sites are staged in ``toolset.discovered_anchors``
        and copied into the transcript so the orchestrator can persist them.
        """
        skills = self._load_skills(provider.id, toolset.checkout_root)
        prompt = _repo_scan_prompt(provider, target, skills, toolset)
        transcript = ScanAgentTranscript()

        parsed = self._run_agent_loop(prompt, toolset, transcript, mode="repo")
        result = self._parse_repo_result(parsed, target, toolset, repo_full_name)

        # Copy agent-staged anchors and api_mapping into the transcript so
        # callers don't need a reference to the toolset.
        transcript.discovered_anchors = list(toolset.discovered_anchors)
        raw_mapping = parsed.get("api_mapping")
        if isinstance(raw_mapping, list):
            transcript.api_mapping = [
                m
                for m in raw_mapping
                if isinstance(m, dict) and m.get("old_symbol") and m.get("new_symbol")
            ]

        return result, transcript

    def poll_feed(
        self,
        provider: ProviderSpec,
        toolset: ScanToolset,
    ) -> tuple[list[ChangeEvent], ScanAgentTranscript]:
        """Agent polls one provider's feeds and returns new ChangeEvents."""
        skills = self._load_skills(provider.id, checkout_root=None)
        prompt = _feed_poll_prompt(provider, skills, toolset)
        transcript = ScanAgentTranscript()

        parsed = self._run_agent_loop(prompt, toolset, transcript, mode="feed")
        events = self._parse_feed_events(parsed, provider)
        return events, transcript

    # -- agent loop ------------------------------------------------------------

    def _run_agent_loop(
        self,
        prompt: str,
        toolset: ScanToolset,
        transcript: ScanAgentTranscript,
        *,
        mode: str,
    ) -> dict[str, Any]:
        """Drive the completer through bounded read-only tool calls until it
        emits a final JSON result (mode-appropriate) or we hit max_steps."""
        context: list[str] = []
        for step in range(self._max_steps):
            transcript.steps_used = step + 1
            response, call = self._complete(_build_turn_prompt(prompt, context, final=False))
            transcript.llm_calls.append(call)
            parsed = _parse_json_response(response)

            if _looks_like_final(parsed, mode):
                return parsed

            tool = parsed.get("tool")
            if not isinstance(tool, str):
                context.append(
                    "The previous response was invalid. Return either a tool "
                    "request or the final JSON result."
                )
                continue

            result = self._dispatch_tool(toolset, tool, parsed.get("arguments", {}))
            transcript.tool_calls.append(
                (tool, json.dumps(parsed.get("arguments", {}), sort_keys=True), result.ok)
            )
            context.append(f"TOOL {tool} RESULT (ok={result.ok}):\n{result.content}")

            if _is_stalling(transcript):
                transcript.stopped_reason = "stall detected (repeated tool calls)"
                break
        else:
            transcript.stopped_reason = "max_steps reached"

        # Final forced turn: no more tools allowed, must emit result JSON.
        response, call = self._complete(_build_turn_prompt(prompt, context, final=True))
        transcript.llm_calls.append(call)
        parsed = _parse_json_response(response)
        if not _looks_like_final(parsed, mode):
            raise RuntimeError(f"scan agent did not return a valid {mode} result JSON")
        return parsed

    def _dispatch_tool(self, toolset: ScanToolset, name: str, arguments: object) -> ToolResult:
        args = arguments if isinstance(arguments, dict) else {}
        if name == "read_file":
            return toolset.read_file(str(args.get("relpath", "")))
        if name == "list_dir":
            return toolset.list_dir(str(args.get("relpath", "")), depth=int(args.get("depth", 1)))
        if name == "grep":
            return toolset.grep(
                pattern=str(args.get("pattern", "")),
                relpath=str(args.get("relpath", "")),
                max_results=int(args.get("max_results", 50)),
            )
        if name == "find_files":
            return toolset.find_files(
                pattern=str(args.get("pattern", "")),
                max_results=int(args.get("max_results", 50)),
            )
        if name == "read_manifest":
            return toolset.read_manifest(str(args.get("relpath", "package.json")))
        if name == "resolve_installed_version":
            return toolset.resolve_installed_version(str(args.get("package", "")))
        if name == "poll_npm":
            return toolset.poll_npm(
                package=str(args.get("package", "")),
                dist_tag=str(args.get("dist_tag", "latest")),
            )
        if name == "poll_pypi":
            return toolset.poll_pypi(package=str(args.get("package", "")))
        if name == "poll_github_releases":
            return toolset.poll_github_releases(repo=str(args.get("repo", "")))
        if name == "fetch_openapi_spec":
            return toolset.fetch_openapi_spec(url=str(args.get("url", "")))
        if name == "fetch_url":
            return toolset.fetch_url(
                url=str(args.get("url", "")),
                max_bytes=int(args.get("max_bytes", 200_000)),
            )
        if name == "record_call_site_anchor":
            return toolset.record_call_site_anchor(
                symbol=str(args.get("symbol", "")),
                filepath=str(args.get("filepath", "")),
                line_number=int(args.get("line_number", 0)),
                line_content=str(args.get("line_content", "")),
            )
        return ToolResult.error(f"unknown tool: {name}")

    def _complete(self, prompt: str) -> tuple[str, LLMCall]:
        started = time.time()
        response = self._completer.complete(prompt, temperature=0.0)
        call = LLMCall(
            timestamp=datetime.now(),
            prompt=prompt,
            response=response.text,
            model=getattr(self._completer, "model", "unknown"),
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_estimate=response.cost_estimate,
            duration_ms=int((time.time() - started) * 1000),
        )
        if self._ledger is not None:
            self._ledger.record(
                CostStage.SCAN_AGENT,
                response.cost_estimate,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        return response.text, call

    # -- skill loading ---------------------------------------------------------

    def _load_skills(self, provider_id: str, checkout_root: Path | None) -> dict[str, str]:
        """Load the three skill files the agent gets to read verbatim.

        Any missing file becomes an empty string — the shared AGENT.md
        always exists, but per-provider and per-repo skills are optional.
        """
        skills: dict[str, str] = {}
        shared = self._skills_dir / _SHARED_SKILL
        skills["shared"] = shared.read_text(encoding="utf-8") if shared.is_file() else ""

        provider_skill = self._skills_dir / _PROVIDER_SKILL_TEMPLATE.format(provider_id=provider_id)
        skills["provider"] = (
            provider_skill.read_text(encoding="utf-8") if provider_skill.is_file() else ""
        )

        if checkout_root is not None:
            repo_skill = checkout_root / _REPO_SKILL_NAME
            skills["repo"] = repo_skill.read_text(encoding="utf-8") if repo_skill.is_file() else ""
        else:
            skills["repo"] = ""
        return skills

    # -- output parsing --------------------------------------------------------

    def _parse_repo_result(
        self,
        parsed: dict[str, Any],
        target: ScanTarget,
        toolset: ScanToolset,
        repo_full_name: str,
    ) -> RepoScanResult:
        """Rebuild a RepoScanResult from the agent's JSON verdict.

        Anything the agent claims that we can't verify against the
        toolset's read-only view is downgraded, not accepted at face value.
        Confidence tiers stay the same as the deterministic scanner so
        downstream policy is unchanged.
        """
        raw_sites = parsed.get("call_sites", []) or []
        raw_deps = parsed.get("dependencies", []) or []
        commit_sha = str(parsed.get("commit_sha") or toolset.commit_sha() or "")

        call_sites: list[CallSite] = []
        for entry in raw_sites:
            if not isinstance(entry, dict):
                continue
            filepath = str(entry.get("filepath") or "")
            line_number = int(entry.get("line_number") or 0)
            if not filepath or line_number <= 0:
                continue
            # Verify the line exists and contains what the agent claims.
            verified = toolset.verify_line(
                filepath, line_number, str(entry.get("line_content") or "")
            )
            if not verified:
                logger.info(
                    "agent claimed unverifiable site %s:%d — dropping", filepath, line_number
                )
                continue
            call_sites.append(
                CallSite(
                    filepath=filepath,
                    line_number=line_number,
                    column=int(entry.get("column") or 0),
                    line_content=str(entry.get("line_content") or ""),
                    kind=_coerce_kind(entry.get("kind")),
                    confidence=_coerce_confidence(entry.get("confidence")),
                    symbol=str(entry.get("symbol") or ""),
                    provider_id=target.provider_id,
                    evidence=str(entry.get("evidence") or "agent-identified"),
                )
            )

        dependencies = []
        for entry in raw_deps:
            if not isinstance(entry, dict):
                continue
            from depfix.scanners.models import DeclaredDependency

            dependencies.append(
                DeclaredDependency(
                    package=str(entry.get("package") or ""),
                    manifest_path=str(entry.get("manifest_path") or ""),
                    declared_range=entry.get("declared_range"),
                    resolved_version=entry.get("resolved_version"),
                    source=str(entry.get("source") or "agent"),
                )
            )

        from depfix.scanners.models import dedupe_call_sites

        return RepoScanResult(
            repo_full_name=repo_full_name,
            commit_sha=commit_sha,
            scanner_version=f"agent-{SCANNER_VERSION}",
            call_sites=dedupe_call_sites(call_sites),
            manifest_matches=[
                f"{d.package}@{d.resolved_version or d.declared_range or '?'} "
                f"({d.source}, {d.manifest_path})"
                for d in dependencies
            ],
            dependencies=dependencies,
            files_scanned=int(parsed.get("files_scanned") or 0),
            errors=list(parsed.get("errors") or []),
        )

    def _parse_feed_events(
        self, parsed: dict[str, Any], provider: ProviderSpec
    ) -> list[ChangeEvent]:
        """Rebuild ChangeEvents from the agent's JSON verdict.

        Every event must carry a source_url or verbatim evidence quote,
        matching the anti-hallucination gate the classify layer already
        applies to LLM-derived release notes.
        """
        raw_events = parsed.get("events", []) or []
        events: list[ChangeEvent] = []
        for entry in raw_events:
            if not isinstance(entry, dict):
                continue
            new_token = str(entry.get("new_token") or "").strip()
            if not new_token:
                continue
            source_url = entry.get("source_url")
            evidence = str(entry.get("evidence") or "")
            if not source_url and not evidence:
                logger.info("agent proposed event with no source_url or evidence — dropping")
                continue
            spec_changes = _parse_spec_changes(entry.get("spec_changes") or [])
            events.append(
                ChangeEvent(
                    provider_id=provider.id,
                    feed_key=str(entry.get("feed_key") or f"agent:{provider.id}:{new_token}"),
                    source_kind=_coerce_source_kind(entry.get("source_kind")),
                    source_url=source_url,
                    old_token=entry.get("old_token"),
                    new_token=new_token,
                    title=str(entry.get("title") or ""),
                    summary=str(entry.get("summary") or ""),
                    body=str(entry.get("body") or ""),
                    body_url=entry.get("body_url"),
                    severity=_coerce_severity(entry.get("severity")),
                    spec_changes=spec_changes,
                    raw={"agent": True, "evidence": evidence},
                )
            )
        return events


# -- prompt builders ---------------------------------------------------------


def _repo_scan_prompt(
    provider: ProviderSpec,
    target: ScanTarget,
    skills: dict[str, str],
    toolset: ScanToolset,
) -> str:
    sdk_packages = [pkg.name for pkg in provider.sdk_packages]
    npm_packages = [pkg.name for pkg in provider.sdk_packages if pkg.ecosystem == "npm"]
    github_repos = list(
        {f.config["github_repo"] for f in provider.feeds if f.config.get("github_repo")}
    )
    return f"""You are the depfix scan agent. Your job has TWO parts:

PART 1 — Discover the CURRENT (new) API surface from the provider's feeds.
PART 2 — Find OLD-API call sites in this repository, map them to the new API,
          and stage each verified old call site with record_call_site_anchor.

You have read-only access to the repository AND to a set of feed-polling tools.
You never make arbitrary network calls; only the registered tools are allowed.

## Provider info
- id: {provider.id}
- SDK packages: {sdk_packages}
- npm packages (for poll_npm): {npm_packages}
- GitHub repos (for poll_github_releases): {github_repos}
- Pre-classified feed symbols (may be empty on a fresh DB): {list(target.feed_symbols)}

## Shared instructions (AGENT.md)
{skills["shared"]}

## Provider-specific instructions
{skills["provider"] or "(none — use general SDK conventions for this ecosystem)"}

## Repository-specific overrides (.depfix/SCAN.md)
{skills["repo"] or "(none)"}

## Available tools
{_tool_manifest(mode="repo")}

## Workflow

### Step 1 — Fetch current API surface (new side)
Use poll_npm / poll_github_releases / fetch_url to read the provider's latest
release notes and migration guides. Identify what the NEW canonical API methods
look like (e.g. for OpenAI v4+: client.chat.completions.create, client.embeddings.create).
Focus on methods that changed across major versions.

### Step 2 — Find old-API call sites in the repo (old side)
Read the manifest to confirm the SDK is present and check its declared version.
Use grep to find import patterns and follow bindings to locate actual method calls.
For each old-API call you find (e.g. openai.createChatCompletion), read the file
to confirm the line and call record_call_site_anchor with the exact content.

### Step 3 — Map old → new
For every old symbol you anchored, state its new equivalent in api_mapping.
If a symbol has no new equivalent (removed with no replacement), set new_symbol to "".

### Rules
- record_call_site_anchor only for OLD-API calls that need migrating.
  Do NOT anchor already-migrated modern calls.
- verify_line is called inside record_call_site_anchor — if a line doesn't
  exist the anchor will be rejected. Only call it when you've seen the line.
- Do not invent API facts. Quote from the release notes or changelog.
- One hop of re-export is fine (downgrade confidence to "medium").

## How to respond
Each turn, return exactly ONE JSON object:

- To call a tool:
  {{"tool": "<name>", "arguments": {{...}}}}

- To finish:
  {{"tool": null, "result": {{
    "commit_sha": "...",
    "files_scanned": <int>,
    "call_sites": [{{
        "filepath": "src/x.js", "line_number": 12, "column": 4,
        "line_content": "openai.createChatCompletion({{...}})",
        "kind": "method_call",
        "confidence": "high",
        "symbol": "openai.createChatCompletion",
        "evidence": "why you're sure"
    }}, ...],
    "api_mapping": [{{
        "old_symbol": "openai.createChatCompletion",
        "new_symbol": "openai.chat.completions.create",
        "notes": "v3 → v4 rename; response shape also changed (no .data wrapper)"
    }}, ...],
    "dependencies": [{{
        "package": "openai",
        "manifest_path": "package.json",
        "declared_range": "3.3.0",
        "resolved_version": "3.3.0",
        "source": "lockfile"
    }}, ...],
    "errors": []
  }}}}

Confidence tiers:
- "high": direct SDK import + method call, unambiguous.
- "medium": one hop of indirection (wrapper module, re-export).
- "low": weak signal (raw HTTP URL, string match). Report but do not
  claim high confidence.

Include ALL old-API call sites in both call_sites (for the scan result) AND
stage them with record_call_site_anchor (for persistence). api_mapping is the
old→new reference that the fix pipeline will use to rewrite the code.
"""


def _feed_poll_prompt(provider: ProviderSpec, skills: dict[str, str], toolset: ScanToolset) -> str:
    return f"""You are the depfix feed agent. Your job: check {provider.name}'s
configured feeds for changes since the last recorded cursor, and report
any new events.

You have read-only access to registered feed endpoints and cached cursor
state through a small set of tools.

## Provider info
- id: {provider.id}
- Feeds: {[str(feed.kind.value) for feed in provider.feeds]}

## Shared instructions (AGENT.md)
{skills["shared"]}

## Provider-specific instructions
{skills["provider"] or "(none)"}

## Available tools
{_tool_manifest(mode="feed")}

## How to respond
Each turn, return exactly ONE JSON object:

- Tool call: {{"tool": "<name>", "arguments": {{...}}}}
- Final: {{"tool": null, "result": {{
    "events": [{{
        "feed_key": "npm:openai:latest",
        "source_kind": "npm_dist_tag",
        "source_url": "https://www.npmjs.com/package/openai/v/4.20.0",
        "old_token": "4.19.0",
        "new_token": "4.20.0",
        "title": "openai 4.19.0 -> 4.20.0",
        "summary": "npm dist-tag latest moved",
        "body": "verbatim release notes if you fetched them",
        "severity": "unknown",
        "spec_changes": [],
        "evidence": "quoted from the source, or empty if source_url covers it"
    }}, ...]
  }}}}

Every event must carry either source_url or an evidence quote. Do not
invent events. If a feed is unchanged, do not emit an event for it.
"""


def _build_turn_prompt(base: str, context: list[str], *, final: bool) -> str:
    joined = "\n\n".join(context) or "(no prior tool results)"
    if final:
        return f"""{base}

## Prior tool results
{joined}

## This is the final turn. No more tool calls are allowed.
Return the final JSON result object now.
"""
    return f"""{base}

## Prior tool results
{joined}

Return your next JSON turn.
"""


def _tool_manifest(*, mode: str) -> str:
    common = [
        "- read_file(relpath): read a repository-relative file.",
        "- list_dir(relpath, depth=1): list contents of a directory.",
        "- grep(pattern, relpath, max_results=50): regex search across source files.",
        "- find_files(pattern, max_results=50): glob for files by name.",
    ]
    repo_only = [
        "- read_manifest(relpath): parse a package.json/pyproject.toml/etc.",
        "- resolve_installed_version(package): read the lockfile-resolved version.",
        # Feed tools are also available in repo mode so the agent can fetch the
        # current API surface and derive old→new mappings in one pass.
        '- poll_npm(package, dist_tag="latest"): fetch npm registry metadata and recent versions.',
        "- poll_pypi(package): fetch PyPI metadata.",
        "- poll_github_releases(repo): fetch newest GitHub release notes (migration guides live here).",
        "- fetch_url(url, max_bytes=200000): fetch a bounded URL (changelog, docs page).",
        "- record_call_site_anchor(symbol, filepath, line_number, line_content): "
        "verify the line exists, then stage it as a discovered old-API call site. "
        "Call this for EVERY confirmed old-API usage found in the repo.",
    ]
    feed_only = [
        '- poll_npm(package, dist_tag="latest"): fetch npm registry info.',
        "- poll_pypi(package): fetch PyPI JSON.",
        "- poll_github_releases(repo): fetch newest GitHub releases.",
        "- fetch_openapi_spec(url): fetch and hash a spec.",
        "- fetch_url(url, max_bytes=200000): fetch a bounded URL for release notes.",
    ]
    tools = common + (repo_only if mode == "repo" else feed_only)
    return "\n".join(tools)


# -- hybrid helpers -----------------------------------------------------------


def _has_raw_http_anchors(provider: ProviderSpec, target: ScanTarget) -> bool:
    """pg is a wire protocol (no HTTP) so this returns False for it.
    OpenAI, Stripe etc. have api.openai.com/api.stripe.com declared."""
    return bool(getattr(target, "api_base_urls", ()) or getattr(provider, "api_base_urls", ()))


@dataclass
class _JudgeVerdicts:
    kept: list[CallSite] = field(default_factory=list)


class CallSiteJudgeAgent:
    """Given a SHORT list of LOW-confidence sites the deterministic scanner
    flagged but could not confirm, decide which are real.

    This is the cheap, high-value hybrid job: the input is a bounded list
    (not a whole repo), the agent reads at most a handful of files, and
    each verdict is grounded in a tool result. Cost per run: cents, not dollars.
    """

    _SKILL_NAME = "worker_judge.md"

    def __init__(
        self,
        completer: LLMCompleter,
        *,
        max_steps: int = _DEFAULT_MAX_STEPS,
        skills_dir: Path | None = None,
        ledger: CostLedger | None = None,
    ) -> None:
        self._completer = completer
        self._max_steps = max_steps
        self._skills_dir = skills_dir or _default_skills_dir()
        self._ledger = ledger

    def run(
        self,
        provider: ProviderSpec,
        low_sites: list[CallSite],
        toolset: ScanToolset,
    ) -> list[CallSite]:
        """Judge up to len(low_sites) LOW-confidence sites.

        Returns a list of sites confirmed real (promoted to MEDIUM).
        Sites the agent cannot confirm are dropped.
        """
        if not low_sites:
            return []

        skill_path = self._skills_dir / self._SKILL_NAME
        skill = skill_path.read_text(encoding="utf-8") if skill_path.is_file() else ""

        sites_json = json.dumps(
            [
                {
                    "filepath": s.filepath,
                    "line_number": s.line_number,
                    "line_content": s.line_content,
                    "symbol": s.symbol,
                }
                for s in low_sites
            ],
            indent=2,
        )
        prompt = _JUDGE_PROMPT.format(
            skill=skill,
            provider_id=provider.id,
            sites=sites_json,
            tools_readonly=_TOOLS_READONLY_MANIFEST,
        )

        transcript = ScanAgentTranscript()
        fake_toolset_agent = _JudgeToolAgent(self._completer, self._max_steps, self._ledger)
        parsed = fake_toolset_agent.run(prompt, toolset, transcript)

        keep_keys: set[tuple[str, int]] = {
            (str(v.get("filepath")), int(v.get("line_number", 0)))
            for v in parsed.get("verdicts", [])
            if isinstance(v, dict) and v.get("is_real") is True
        }

        kept: list[CallSite] = []
        for site in low_sites:
            if (site.filepath, site.line_number) in keep_keys:
                kept.append(dataclasses.replace(site, confidence=MatchConfidence.MEDIUM))
        return kept


class _JudgeToolAgent:
    """Minimal bounded agent loop for CallSiteJudgeAgent (no feed tools)."""

    def __init__(self, completer: LLMCompleter, max_steps: int, ledger: CostLedger | None) -> None:
        self._completer = completer
        self._max_steps = max_steps
        self._ledger = ledger

    def run(
        self,
        prompt: str,
        toolset: ScanToolset,
        transcript: ScanAgentTranscript,
    ) -> dict[str, Any]:
        context: list[str] = []
        for step in range(self._max_steps):
            transcript.steps_used = step + 1
            response, call = self._complete(prompt, context)
            transcript.llm_calls.append(call)
            parsed = _parse_json_response(response)

            # Final: {"tool": null, "result": {"verdicts": [...]}}
            if parsed.get("tool") is None and isinstance(parsed.get("result"), dict):
                return parsed.get("result", {})

            tool = parsed.get("tool")
            if not isinstance(tool, str):
                context.append("Invalid response. Return a tool call or the final verdict JSON.")
                continue

            args = parsed.get("arguments", {})
            if tool == "read_file":
                result = toolset.read_file(str(args.get("relpath", "")))
            elif tool == "grep":
                result = toolset.grep(
                    pattern=str(args.get("pattern", "")),
                    relpath=str(args.get("relpath", "")),
                )
            else:
                result = ToolResult.error(f"unknown tool for judge: {tool}")
            transcript.tool_calls.append((tool, json.dumps(args, sort_keys=True), result.ok))
            context.append(f"TOOL {tool} RESULT (ok={result.ok}):\n{result.content}")

        response, call = self._complete(prompt, context, final=True)
        transcript.llm_calls.append(call)
        parsed = _parse_json_response(response)
        return parsed.get("result", {})

    def _complete(
        self, base_prompt: str, context: list[str], *, final: bool = False
    ) -> tuple[str, LLMCall]:
        started = time.time()
        full = _build_turn_prompt(base_prompt, context, final=final)
        response = self._completer.complete(full, temperature=0.0)
        call = LLMCall(
            timestamp=datetime.now(),
            prompt=full,
            response=response.text,
            model=getattr(self._completer, "model", "unknown"),
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_estimate=response.cost_estimate,
            duration_ms=int((time.time() - started) * 1000),
        )
        if self._ledger is not None:
            self._ledger.record(
                CostStage.SCAN_AGENT,
                response.cost_estimate,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        return response.text, call


_TOOLS_READONLY_MANIFEST = "Available tools: read_file(relpath), grep(pattern, relpath)"

_JUDGE_PROMPT = """You are the depfix CallSiteJudgeAgent. The deterministic scanner found
these LOW-confidence potential {provider_id} call sites but could not
confirm them (deep indirection or ambiguous binding). Decide which are real.

## Skill
{skill}

## Candidate sites (verify each)
{sites}

## {tools_readonly}

For each candidate, read the file if needed to confirm the binding traces
back to a real {provider_id} SDK import. Return JSON each turn:
- Tool call: {{"tool": "read_file", "arguments": {{"relpath": "src/x.js"}}}}
- Final: {{"tool": null, "result": {{"verdicts": [
    {{"filepath": "src/x.js", "line_number": 42, "is_real": true,
      "reason": "binding traces to require('pg') at line 3"}},
    {{"filepath": "src/y.js", "line_number": 7, "is_real": false,
      "reason": "binding traces to ioredis, not {provider_id}"}}
  ]}}}}

Mark is_real=false for anything that traces to a DIFFERENT package.
Mark is_real=true ONLY when you can quote the import line confirming the SDK.
"""


# -- helpers ------------------------------------------------------------------


def _default_skills_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "providers" / _SKILLS_DIR_NAME


def _parse_json_response(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        stripped = stripped.rsplit("```", 1)[0]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        import re

        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if match is None:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _looks_like_final(parsed: dict[str, Any], mode: str) -> bool:
    if parsed.get("tool") is not None:
        return False
    result = parsed.get("result")
    if not isinstance(result, dict):
        return False
    if mode == "repo":
        return "call_sites" in result
    return "events" in result


def _is_stalling(transcript: ScanAgentTranscript) -> bool:
    """Repeated identical tool calls mean the agent is going nowhere."""
    if len(transcript.tool_calls) < 3:
        return False
    signatures = [(name, args) for name, args, _ok in transcript.tool_calls[-3:]]
    return len(set(signatures)) == 1


def _coerce_kind(value: object) -> CallSiteKind:
    try:
        return CallSiteKind(str(value))
    except ValueError:
        return CallSiteKind.METHOD_CALL


def _coerce_confidence(value: object) -> MatchConfidence:
    try:
        return MatchConfidence(str(value).lower())
    except ValueError:
        return MatchConfidence.LOW


def _coerce_severity(value: object) -> Severity:
    try:
        return Severity(str(value).lower())
    except ValueError:
        return Severity.UNKNOWN


def _coerce_source_kind(value: object) -> SourceKind:
    try:
        return SourceKind(str(value).lower())
    except ValueError:
        return SourceKind.GITHUB_RELEASE


def _parse_spec_changes(raw: list) -> list[SpecChange]:
    out: list[SpecChange] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(
                SpecChange(
                    kind=SpecChangeKind(str(entry.get("kind"))),
                    severity=_coerce_severity(entry.get("severity")),
                    subject=str(entry.get("subject") or ""),
                    pointer=str(entry.get("pointer") or ""),
                    detail=str(entry.get("detail") or ""),
                    direction=str(entry.get("direction") or "n/a"),
                    before=entry.get("before"),
                    after=entry.get("after"),
                )
            )
        except (ValueError, KeyError):
            continue
    return out
