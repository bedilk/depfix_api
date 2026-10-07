#!/usr/bin/env bash
# scripts/test_pipeline.sh
# Usage:
#   bash scripts/test_pipeline.sh sentry:bedilk/depfix-test-sentry-v6-app
#   bash scripts/test_pipeline.sh sentry,anthropic:bedilk/depfix-test-sentry-v6-app
#   bash scripts/test_pipeline.sh openai:bedilk/a sentry:bedilk/b
#
# Format: provider[,provider,...]:owner/repo
#
# Portable across macOS (BSD grep) and Linux: all log parsing is done in
# Python, never with `grep -P`.

set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "Usage: bash scripts/test_pipeline.sh provider[,provider]:owner/repo [...]"
    exit 1
fi

PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
PROVIDERS_FILE="${PROVIDERS_FILE:-providers.yaml}"

# ---------------------------------------------------------------------------
# ensure_provider: if a provider id is missing from providers.yaml, discover
# its feeds (npm, pypi, rubygems, github releases, security advisories, etc.)
# and append a full entry so depfix can scan it.
# ---------------------------------------------------------------------------
DISCOVER_SCRIPT="${TMPDIR:-/tmp}/depfix-discover-$$.py"
cat > "$DISCOVER_SCRIPT" <<'DISCOVER_PY'
"""Auto-discover a provider and append it to providers.yaml.

Usage: python discover.py <provider_id> <providers_yaml_path>

Discovery strategy (no LLM, no network except npm/pypi/rubygems registry):
  1. Check npm registry for a package matching the provider id (or common
     scoped patterns like @<id>/sdk, @<id>/node, <id>-js, etc.).
  2. Check PyPI for a matching package.
  3. Check RubyGems for a matching gem.
  4. Infer github_repo from the npm or pypi metadata.
  5. Generate feed entries: dist tags, github releases, security advisories,
     ts_exports_diff, and changelog if discoverable.
  6. Append the new provider block to providers.yaml.
"""
import json, re, sys, urllib.request, urllib.error, yaml
from pathlib import Path

def fetch_json(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "depfix-discover/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return None

def npm_lookup(name):
    return fetch_json(f"https://registry.npmjs.org/{name}")

def pypi_lookup(name):
    return fetch_json(f"https://pypi.org/pypi/{name}/json")

def rubygems_lookup(name):
    return fetch_json(f"https://rubygems.org/api/v1/gems/{name}.json")

def extract_github_repo(urls):
    """Extract owner/repo from a list of URL strings."""
    for url in urls:
        if not url:
            continue
        m = re.search(r"github\.com[/:]([^/]+/[^/.#]+)", str(url))
        if m:
            repo = m.group(1).rstrip("/")
            if repo.endswith(".git"):
                repo = repo[:-4]
            return repo
    return None

def discover(provider_id):
    """Return a provider dict ready for providers.yaml, or None."""
    sdk_packages = []
    feeds = []
    github_repos = set()
    name = provider_id.replace("-", " ").title()

    # --- npm discovery ---
    npm_candidates = [
        provider_id,
        f"@{provider_id}/sdk",
        f"@{provider_id}/node",
        f"@{provider_id}/{provider_id}",
        f"{provider_id}-js",
        f"@{provider_id}/core",
    ]
    npm_pkg = None
    npm_data = None
    for candidate in npm_candidates:
        data = npm_lookup(candidate)
        if data and "name" in data:
            npm_pkg = data["name"]
            npm_data = data
            break

    if npm_pkg:
        sdk_packages.append({"name": npm_pkg, "ecosystem": "npm"})
        repo_url = (npm_data.get("repository") or {})
        if isinstance(repo_url, dict):
            repo_url = repo_url.get("url", "")
        gh = extract_github_repo([str(repo_url), npm_data.get("homepage", "")])
        if gh:
            github_repos.add(gh)
        feeds.append({"kind": "npm_dist_tag", "package": npm_pkg, "dist_tag": "latest",
                       **({"github_repo": gh} if gh else {})})
        feeds.append({"kind": "security_advisory", "package": npm_pkg, "ecosystem": "npm"})
        feeds.append({"kind": "ts_exports_diff", "package": npm_pkg})

    # --- pypi discovery ---
    pypi_candidates = [provider_id, provider_id.replace("-", "_"), f"{provider_id}-sdk",
                       f"{provider_id}-python", f"py{provider_id}"]
    pypi_pkg = None
    pypi_data = None
    for candidate in pypi_candidates:
        data = pypi_lookup(candidate)
        if data and "info" in data:
            pypi_pkg = data["info"]["name"]
            pypi_data = data
            break

    if pypi_pkg:
        sdk_packages.append({"name": pypi_pkg, "ecosystem": "pypi"})
        info = pypi_data["info"]
        urls_to_check = [
            info.get("home_page", ""),
            info.get("project_url", ""),
            info.get("package_url", ""),
        ]
        for pu in (info.get("project_urls") or {}).values():
            urls_to_check.append(pu)
        gh = extract_github_repo(urls_to_check)
        if gh:
            github_repos.add(gh)
        feeds.append({"kind": "pypi_dist_tag", "package": pypi_pkg,
                       **({"github_repo": gh} if gh else {})})
        feeds.append({"kind": "security_advisory", "package": pypi_pkg, "ecosystem": "pypi"})

    # --- rubygems discovery ---
    gem_candidates = [provider_id, f"{provider_id}-ruby", f"ruby-{provider_id}",
                      f"{provider_id}-sdk"]
    gem_pkg = None
    gem_data = None
    for candidate in gem_candidates:
        data = rubygems_lookup(candidate)
        if data and "name" in data:
            gem_pkg = data["name"]
            gem_data = data
            break

    if gem_pkg:
        sdk_packages.append({"name": gem_pkg, "ecosystem": "rubygems"})
        gh = extract_github_repo([gem_data.get("source_code_uri", ""),
                                   gem_data.get("homepage_uri", "")])
        if gh:
            github_repos.add(gh)
        feeds.append({"kind": "rubygems_dist_tag", "gem": gem_pkg,
                       **({"github_repo": gh} if gh else {})})

    if not sdk_packages:
        return None

    # --- github releases for discovered repos ---
    for gh in sorted(github_repos):
        feeds.append({"kind": "github_release", "repo": gh})

    # Use the prettiest name from npm or the id
    if npm_data:
        name = npm_data.get("description", name)
        if len(name) > 50:
            name = npm_pkg.replace("@", "").replace("/", " ").title()

    provider = {
        "id": provider_id,
        "name": name,
        "api_version_scheme": "semver",
        "sdk_packages": sdk_packages,
        "feeds": feeds,
    }
    return provider

def main():
    if len(sys.argv) < 3:
        print("Usage: discover.py <provider_id> <providers_yaml>", file=sys.stderr)
        sys.exit(1)

    provider_id = sys.argv[1].strip()
    yaml_path = Path(sys.argv[2])

    # Check if already present
    data = yaml.safe_load(yaml_path.read_text()) or {}
    existing_ids = {p["id"] for p in (data.get("providers") or [])}
    if provider_id in existing_ids:
        print(f"[discover] {provider_id} already in {yaml_path}", file=sys.stderr)
        sys.exit(0)

    print(f"[discover] {provider_id} not in {yaml_path}, auto-discovering...", file=sys.stderr)
    provider = discover(provider_id)
    if provider is None:
        print(f"[discover] ERROR: could not find any packages for '{provider_id}' "
              "on npm, PyPI, or RubyGems", file=sys.stderr)
        sys.exit(2)

    # Append to providers.yaml
    data.setdefault("providers", []).append(provider)
    yaml_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True))

    pkgs = ", ".join(f"{p['ecosystem']}:{p['name']}" for p in provider["sdk_packages"])
    feed_kinds = ", ".join(f["kind"] for f in provider["feeds"])
    print(f"[discover] Added {provider_id}: packages=[{pkgs}] feeds=[{feed_kinds}]", file=sys.stderr)

if __name__ == "__main__":
    main()
DISCOVER_PY

ensure_provider() {
    local provider="$1"
    "$PYTHON" "$DISCOVER_SCRIPT" "$provider" "$PROVIDERS_FILE"
}
# rich wraps at 80 columns when stdout is not a TTY; widen it so single
# summary lines are never split across two lines in the log files.
export COLUMNS="${COLUMNS:-250}"
export NO_COLOR=1

LOG_DIR=$(mktemp -d "${TMPDIR:-/tmp}/depfix-test.XXXXXX")
SUMMARY="$LOG_DIR/summary.tsv"
DETAIL="$LOG_DIR/detail.md"
PARSER="$LOG_DIR/parse_logs.py"

cat > "$PARSER" <<'PY'
"""Extract metrics from a depfix scan log + plan log. Prints one TSV row
(or a markdown detail block with --detail). Never fails: missing values
become '—' / 0 so one bad log can't break the summary."""
import re, sys

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return ANSI.sub("", fh.read())
    except OSError:
        return ""

def first(pattern, text, default="", group=1, last=False):
    hits = re.findall(pattern, text)
    if not hits:
        return default
    hit = hits[-1] if last else hits[0]
    return hit[group - 1] if isinstance(hit, tuple) else hit

def table_rows(text, title):
    """Rows of a rich table (cells split on │) that follows `title`."""
    idx = text.find(title)
    if idx < 0:
        return []
    rows = []
    for line in text[idx:].splitlines()[1:]:
        if "│" not in line:
            if rows and not line.strip().startswith(("┃", "━", "─", "┡", "└", "┏", "╇")):
                break
            continue
        cells = [c.strip() for c in line.strip().strip("│").split("│")]
        rows.append(cells)
    # drop header rows (rich renders headers with ┃, but be defensive)
    return [r for r in rows if r and r[0] not in ("Package", "Old API", "File")]

def errors(text):
    return [l.strip() for l in text.splitlines()
            if l.strip().startswith(("Error", "Pipeline failed", "Change detection failed"))]

repo, provider, scan_path, plan_path = sys.argv[1:5]
detail = "--detail" in sys.argv
scan, plan = read(scan_path), read(plan_path)

files = first(r"files scanned:\s*(\d+)", scan, "0", last=True)
sites = first(r"call sites:\s*(\d+)", scan, "0", last=True)
actionable = first(r"\((\d+) actionable\)", scan, "0", last=True)
breaking = first(r"(\d+) new breaking change", scan, "0")

drift = table_rows(scan, "Dependency Drift Summary")
drift_from = drift[0][1] if drift and len(drift[0]) > 1 else "—"
drift_to = drift[0][2] if drift and len(drift[0]) > 2 else "—"
drift_risk = first(r"Version drift \(([a-z-]+)", plan, "") or (
    drift[0][3] if drift and len(drift[0]) > 3 else "—")

matches = table_rows(scan, "Upstream Change Match")
api_matches = sum(1 for r in matches if any(c == "actionable" for c in r))

scan_cost = float(first(r"LLM cost:\s*\$([0-9.]+)", scan, "0") or 0)
plan_cost = float(first(r"LLM spend this run:\s*\$([0-9.]+)", plan, "0") or 0)

artifact = "Yes" if re.search(
    r"Plan artifact:|Upgrade plan:|Version drift \([^)]*PR planned", plan) else "No"
proposed = "; ".join(
    l.strip() for l in plan.splitlines()
    if re.search(r"would upgrade|would process|Version drift|Upgrade plan:|no committable edits", l)
)[:300] or "—"

errs = errors(scan) + errors(plan)
status = "ERROR: " + errs[0][:150] if errs else "ok"

if not detail:
    print("\t".join(map(str, [
        repo, provider, files, sites, actionable, breaking, drift_from, drift_to,
        drift_risk, api_matches, f"${scan_cost + plan_cost:.4f}", artifact, proposed,
    ])))
else:
    print(f"## {repo} ({provider})\n")
    print("| Metric | Value |\n| --- | --- |")
    for k, v in [("Files scanned", files), ("Call sites (total)", sites),
                 ("Actionable sites", actionable), ("Breaking changes", breaking),
                 ("Drift", f"{drift_from} → {drift_to} ({drift_risk})"),
                 ("API matches", api_matches),
                 ("**Total cost**", f"**${scan_cost + plan_cost:.4f}**"),
                 ("PR artifact?", artifact), ("Proposed changes", proposed)]:
        print(f"| {k} | {v} |")
    for label, text in (("Scan log tail", scan), ("Plan log tail", plan)):
        tail = "\n".join(text.splitlines()[-40:])
        print(f"\n<details><summary>{label}</summary>\n\n```\n{tail}\n```\n</details>\n")
PY

printf 'repo\tprovider\tfiles_scanned\tcall_sites\tactionable\tbreaking_changes\tdrift_from\tdrift_to\tdrift_risk\tapi_matches\ttotal_cost\tartifact\tproposed_changes\n' > "$SUMMARY"
{
    echo "# depfix pipeline test — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo
    echo "Log directory: \`$LOG_DIR\`"
    echo
} > "$DETAIL"

run_one() {
    local provider="$1" repo="$2"
    local short="${repo##*/}"
    local scan_log="$LOG_DIR/${short}.${provider}.scan.log"
    local plan_log="$LOG_DIR/${short}.${provider}.plan.log"

    echo "──────────────────────────────────────"
    echo "  $repo ($provider)"
    echo "──────────────────────────────────────"

    ensure_provider "$provider"

    depfix init --offline --keep-state 2>&1 | tail -2

    echo "[scan] $repo / $provider"
    depfix scan --repo "$repo" --provider "$provider" > "$scan_log" 2>&1 \
        || echo "  (scan exited $?) — see $scan_log"

    echo "[plan] $repo / $provider"
    depfix plan "$repo" --provider "$provider" --force > "$plan_log" 2>&1 \
        || echo "  (plan exited $?) — see $plan_log"

    local row
    row=$("$PYTHON" "$PARSER" "$repo" "$provider" "$scan_log" "$plan_log")
    printf '%s\n' "$row" >> "$SUMMARY"
    "$PYTHON" "$PARSER" "$repo" "$provider" "$scan_log" "$plan_log" --detail >> "$DETAIL"

    echo "[done] $(printf '%s' "$row" | cut -f1,3,4,5,12 | tr '\t' ' ')"
    echo
}

# Reassemble all args into one string then parse with Python.
# This handles spaces around commas regardless of how the shell split the args:
#   bash test_pipeline.sh slack, postgresql , redis:bedilk/repo
#   bash test_pipeline.sh postgresql,    redis,  google-cloud:bedilk/repo  sentry:bedilk/other
joined="$*"
while IFS=$'\t' read -r providers repo; do
    [[ -z "$repo" ]] && continue
    IFS=',' read -ra provider_list <<< "$providers"
    for provider in "${provider_list[@]}"; do
        [[ -z "$provider" ]] && continue
        run_one "$provider" "$repo"
    done
done < <("$PYTHON" -c "
import re, sys
raw = sys.argv[1]
# Find every :owner/repo anchor.  Work backwards from each to collect its providers.
anchors = list(re.finditer(r'([\w][\w-]*)\s*:\s*([\w.-]+/[\w._-]+)', raw))
for i, m in enumerate(anchors):
    repo = m.group(2)
    # The region holding this target's providers runs from the end of the
    # previous anchor (or string start) to this anchor's start.
    region_start = anchors[i-1].end() if i > 0 else 0
    region = raw[region_start:m.start()]
    # Collect: strip, split on commas/spaces, keep non-empty tokens
    tokens = [t.strip() for t in re.split(r'[,\s]+', region) if t.strip()]
    # The last token before the colon is already captured as m.group(1)
    providers = tokens + [m.group(1)]
    print(','.join(providers) + '\t' + repo)
" "$joined")

echo
echo "═══════════════════════════════════════════"
echo "  SUMMARY"
echo "═══════════════════════════════════════════"
column -t -s $'\t' "$SUMMARY"
echo
{
    echo "## Summary"
    echo
    head -1 "$SUMMARY" | sed 's/	/ | /g; s/^/| /; s/$/ |/'
    head -1 "$SUMMARY" | awk -F'\t' '{s="|"; for(i=1;i<=NF;i++) s=s" --- |"; print s}'
    tail -n +2 "$SUMMARY" | sed 's/	/ | /g; s/^/| /; s/$/ |/'
} >> "$DETAIL"

echo "Full detail report: $DETAIL"
echo "Raw logs: $LOG_DIR"
