#!/usr/bin/env bash
# Day 3: drive real forks through scan -> plan and emit the one number that
# decides Gate 1 -- the KEPT rate on code migrations in repos we didn't write.
#
# Deliberately NOT the manifest-drift number. A verified version bump is real
# work, but it is not evidence that the LLM can migrate a call site, and
# conflating the two is how a project talks itself into a launch.
#
#   bash scripts/real_repo_runs.sh bedilk/repo-a bedilk/repo-b bedilk/repo-c
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 OWNER/REPO [OWNER/REPO ...]" >&2
  exit 2
fi

RUN_ID="depfix-day3-$(date +%Y%m%d-%H%M%S)"
LOG_DIR="/tmp/${RUN_ID}"
mkdir -p "$LOG_DIR"
SUMMARY="$LOG_DIR/summary.tsv"
printf 'repo\tprovider\tfiles\tactionable\tkind\tverdict\tconfidence\tcost\n' > "$SUMMARY"

PROVIDERS="${DEPFIX_PROVIDERS:-openai stripe anthropic}"

echo "logs: $LOG_DIR"
depfix init --keep-state | tail -n 3

for repo in "$@"; do
  for provider in $PROVIDERS; do
    slug="${repo//\//_}__${provider}"
    scan_log="$LOG_DIR/${slug}.scan.log"
    plan_log="$LOG_DIR/${slug}.plan.log"

    echo "── $repo / $provider"
    if ! depfix scan --repo "$repo" --provider "$provider" > "$scan_log" 2>&1; then
      printf '%s\t%s\t-\t-\t-\tscan_failed\t-\t-\n' "$repo" "$provider" >> "$SUMMARY"
      continue
    fi

    files=$(grep -oE 'files scanned: [0-9]+' "$scan_log" | grep -oE '[0-9]+' | head -1 || echo 0)
    actionable=$(grep -oE '\([0-9]+ actionable\)' "$scan_log" | grep -oE '[0-9]+' | head -1 || echo 0)

    depfix plan --repo "$repo" --provider "$provider" > "$plan_log" 2>&1 || true

    # A plan artifact whose edits[] is empty is a manifest-only drift; one
    # with edits is an actual code migration. Only the latter counts toward
    # the KEPT rate.
    kind="none"; verdict="no_plan"; confidence="-"
    artifact=$(ls -t .depfix/plans/*"$(echo "$repo" | tr '/' '_')"* 2>/dev/null | head -1 || true)
    if [[ -n "$artifact" ]]; then
      edits=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1])).get('edits',[])))" "$artifact")
      confidence=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('confidence','-'))" "$artifact")
      if [[ "$edits" -gt 0 ]]; then kind="code_migration"; else kind="manifest_drift"; fi
      verdict="planned"
    elif grep -q 'no committable edits' "$plan_log"; then
      verdict="no_kept_edits"
      kind="code_migration_attempted"
    fi

    cost=$(grep -oE 'llm spend: \$[0-9.]+' "$plan_log" | grep -oE '[0-9.]+' | head -1 || echo "0.0000")
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$repo" "$provider" "$files" "$actionable" "$kind" "$verdict" "$confidence" "$cost" >> "$SUMMARY"
  done
done

echo
column -t -s $'\t' "$SUMMARY"
echo

attempted=$(awk -F'\t' '$5 ~ /^code_migration/ {n++} END {print n+0}' "$SUMMARY")
kept=$(awk -F'\t' '$5 == "code_migration" && $6 == "planned" {n++} END {print n+0}' "$SUMMARY")
echo "CODE-MIGRATION KEPT RATE: ${kept}/${attempted}"
echo "(manifest-only drifts are excluded on purpose -- they prove the writer, not the fixer)"
echo
echo "Paste the table above into docs/RESULTS.md, then fill in docs/DECISION.md."
