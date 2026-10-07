#!/usr/bin/env bash
# Day-3 reality test: fork real repos, run plan -> apply, record what happened.
#
# The point is not to open PRs. The point is to produce plan artifacts that
# touch *application source*, because every run recorded so far has been
# manifest-only drift and the product claim is unmeasured until that changes.
#
# Writes one JSON line per (repo, change) to .depfix/day3/runs.jsonl, which
# scripts/collect_metrics.py reads. Every field it writes is observed, never
# assumed -- a step that fails records the failure rather than being skipped.
#
# Usage:
#   export GITHUB_TOKEN=...            # needs repo + fork scope
#   export DEPFIX_FORK_ORG=bedilk      # where forks land
#   bash scripts/day3_fork_runs.sh
#
# Add --open-pr to also push a branch and open a draft PR (task 3.2 evidence).

set -euo pipefail

FORK_ORG="${DEPFIX_FORK_ORG:-bedilk}"
REPOS_FILE="${DEPFIX_REPOS_FILE:-scripts/day3_repos.txt}"
WORKDIR="${DEPFIX_DAY3_WORKDIR:-.depfix/day3/checkouts}"
MANIFEST=".depfix/day3/runs.jsonl"
OPEN_PR=""

for arg in "$@"; do
  case "$arg" in
    --open-pr) OPEN_PR="--open-pr" ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

command -v gh >/dev/null || { echo "gh CLI is required" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
: "${GITHUB_TOKEN:?GITHUB_TOKEN must be set}"

mkdir -p "$WORKDIR" "$(dirname "$MANIFEST")"
: > "$MANIFEST"   # fresh run; the manifest is a record of THIS pass

mapfile -t REPOS < <(grep -vE '^\s*(#|$)' "$REPOS_FILE" || true)
if [ "${#REPOS[@]}" -eq 0 ]; then
  cat >&2 <<'EOF'
No target repos configured.

scripts/day3_repos.txt is intentionally empty -- it explains how to pick
targets honestly. Add 3-5 real repos that import a watched SDK, then re-run.
Until then the code-migration KEPT rate stays unmeasured, and
docs/DECISION.md stays BLOCKED. That is the correct state, not a bug.
EOF
  exit 1
fi

emit() {
  echo "$1" >> "$MANIFEST"
}

echo "=== Day-3 fork runs: ${#REPOS[@]} target(s), forking into $FORK_ORG ==="

for upstream in "${REPOS[@]}"; do
  name="${upstream##*/}"
  fork="$FORK_ORG/$name"
  checkout="$WORKDIR/$name"
  echo
  echo "--- $upstream -> $fork"

  # 1. Fork (idempotent: gh is a no-op if the fork exists)
  if ! gh repo fork "$upstream" --org "$FORK_ORG" --clone=false --remote=false 2>/dev/null; then
    echo "    fork failed (may already exist); continuing"
  fi

  # 2. Clone the fork, not upstream -- depfix must push somewhere it owns
  if [ ! -d "$checkout/.git" ]; then
    gh repo clone "$fork" "$checkout" -- --depth 50 || {
      emit "$(jq -nc --arg f "$fork" '{fork:$f, plan_status:"clone_failed"}')"
      continue
    }
  fi
  base_sha="$(git -C "$checkout" rev-parse HEAD)"

  # 3. Opt in. depfix refuses to touch a repo without .depfix.yml, by design.
  if [ ! -f "$checkout/.depfix.yml" ]; then
    cat > "$checkout/.depfix.yml" <<'YML'
version: 1
# Day-3 reality test: watch everything so we find out what's actually there.
providers:
  include: ["*"]
YML
    echo "    wrote .depfix.yml (uncommitted -- local opt-in for this run)"
  fi

  # 4. Plan. Capture status and stderr; a failure here is data.
  plan_log="$WORKDIR/$name.plan.log"
  if depfix plan --repo "$checkout" --json > "$WORKDIR/$name.plan.json" 2> "$plan_log"; then
    plan_status="ok"
  else
    plan_status="failed"
    echo "    plan failed -- see $plan_log"
  fi

  # 5. Apply each plan artifact this run produced for this repo
  changes=0
  while IFS= read -r plan_path; do
    [ -z "$plan_path" ] && continue
    changes=$((changes + 1))
    apply_log="$WORKDIR/$name.apply.$changes.log"
    if depfix apply --plan "$plan_path" --repo "$checkout" $OPEN_PR > "$apply_log" 2>&1; then
      apply_status="ok"
    else
      apply_status="failed"
    fi

    # Read the *artifact*, not the log -- the artifact is the source of truth
    kept=$(jq '[.. | objects | select(has("verdict")) | select(.verdict=="KEPT")] | length' \
             "$plan_path" 2>/dev/null || echo 0)
    edits=$(jq '[.. | objects | select(has("verdict"))] | length' \
             "$plan_path" 2>/dev/null || echo 0)
    cost=$(jq '[.. | objects | .total_cost? // empty] | first // 0' \
             "$plan_path" 2>/dev/null || echo 0)
    provider=$(jq -r '[.. | objects | .provider_id? // empty] | first // "unknown"' \
             "$plan_path" 2>/dev/null || echo unknown)
    change=$(jq -r '[.. | objects | .old_api? // empty] | first // "unknown"' \
             "$plan_path" 2>/dev/null || echo unknown)

    emit "$(jq -nc \
      --arg fork "$fork" --arg base "$base_sha" --arg provider "$provider" \
      --arg change "$change" --arg plan "$plan_status" --arg apply "$apply_status" \
      --arg path "$plan_path" \
      --argjson kept "${kept:-0}" --argjson edits "${edits:-0}" --argjson cost "${cost:-0}" \
      '{fork:$fork, base_sha:$base, provider:$provider, change:$change,
        plan_status:$plan, apply_status:$apply, plan_artifact:$path,
        kept:$kept, edits:$edits, cost:$cost}')"
  done < <(jq -r '.plans[]?.path // empty' "$WORKDIR/$name.plan.json" 2>/dev/null || true)

  if [ "$changes" -eq 0 ]; then
    emit "$(jq -nc --arg f "$fork" --arg p "$plan_status" \
      '{fork:$f, plan_status:$p, apply_status:"no_changes", kept:0, edits:0, cost:0}')"
    echo "    no actionable changes"
  fi
done

echo
echo "=== Recorded $(wc -l < "$MANIFEST") run(s) to $MANIFEST ==="
echo "Now refresh the tables:"
echo "    python scripts/collect_metrics.py --write"
echo "    python scripts/collect_metrics.py --check    # must pass before DECISION.md"
