#!/bin/bash
# Depfix Repository Testing Script
# Features:
# - Logs stored in /tmp
# - Automatic cleanup after completion
# - Individual init-scan-plan cycle for each repo
# - Comprehensive results table

set -e

# Create temp directory for logs
TEST_ID="depfix-test-$(date +%s)"
LOG_DIR="/tmp/${TEST_ID}"
mkdir -p "$LOG_DIR"

echo "=== DEPFIX TEST SUITE ==="
echo "Log directory: $LOG_DIR"
echo "Started: $(date)"
echo ""

# Array of repositories to test
declare -a REPOS=(
    "bedilk/probot:octokit"
    "bedilk/renovate:octokit"
    "bedilk/bolt-js:slack"
    "bedilk/sentry:slack"
    "bedilk/AtlantaBot:discord"
    "bedilk/guide:discord"
    "bedilk/Rocket.Chat:mongodb"
)

# Results file
RESULTS_FILE="$LOG_DIR/test_results.txt"
echo "DEPFIX TEST RESULTS - $(date)" > "$RESULTS_FILE"
echo "========================================" >> "$RESULTS_FILE"
echo "" >> "$RESULTS_FILE"

# Test counter
TOTAL_REPOS=${#REPOS[@]}
CURRENT=0

# Function to test a single repo
test_repo() {
    local repo_spec=$1
    local repo_name=$(echo $repo_spec | cut -d: -f1)
    local provider=$(echo $repo_spec | cut -d: -f2)
    local short_name=$(echo $repo_name | cut -d/ -f2)

    CURRENT=$((CURRENT + 1))

    echo "========================================"
    echo "TEST ${CURRENT}/${TOTAL_REPOS}: ${repo_name} (${provider})"
    echo "========================================"
    echo ""

    # Log files in temp directory
    local scan_log="$LOG_DIR/scan_${CURRENT}_${short_name}.log"
    local plan_log="$LOG_DIR/plan_${CURRENT}_${short_name}.log"

    # Record to results file
    echo "----------------------------------------" >> "$RESULTS_FILE"
    echo "TEST ${CURRENT}/${TOTAL_REPOS}: ${repo_name} (${provider})" >> "$RESULTS_FILE"
    echo "----------------------------------------" >> "$RESULTS_FILE"

    # Step 1: INIT
    echo "Step 1: INIT"
    depfix init --offline --keep-state 2>&1 | grep -E "ready|OK|database" | head -5
    echo ""

    # Step 2: SCAN
    echo "Step 2: SCAN"
    depfix scan --repo "$repo_name" --provider "$provider" 2>&1 | tee "$scan_log" | tail -30

    # Extract scan metrics
    FILES_SCANNED=$(grep "files scanned:" "$scan_log" | tail -1 | awk '{print $3}' || echo "N/A")
    CALL_SITES=$(grep "call sites:" "$scan_log" | tail -1 | awk '{print $6}' || echo "N/A")
    ACTIONABLE=$(grep "call sites:" "$scan_log" | tail -1 | sed 's/.*(\(.*\) actionable).*/\1/' || echo "N/A")

    echo "  → Files: $FILES_SCANNED, Call sites: $CALL_SITES, Actionable: $ACTIONABLE" >> "$RESULTS_FILE"
    echo ""

    # Step 3: PLAN
    echo "Step 3: PLAN"
    depfix plan --repo "$repo_name" --provider "$provider" \
      --llm-provider bedrock \
      --model us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
      --dry-run 2>&1 | tee "$plan_log" | tail -20

    # Extract plan results
    BREAKING_CHANGES=$(grep "breaking change(s) created" "$plan_log" | tail -1 | awk '{print $3}' || echo "0")

    echo "  → Breaking changes: $BREAKING_CHANGES" >> "$RESULTS_FILE"
    echo "  → Scan log: $scan_log" >> "$RESULTS_FILE"
    echo "  → Plan log: $plan_log" >> "$RESULTS_FILE"
    echo "" >> "$RESULTS_FILE"
    echo ""
}

# Run tests for all repos
for repo_spec in "${REPOS[@]}"; do
    test_repo "$repo_spec"
done

# Generate summary
echo "========================================" | tee -a "$RESULTS_FILE"
echo "ALL TESTS COMPLETE" | tee -a "$RESULTS_FILE"
echo "========================================" | tee -a "$RESULTS_FILE"
echo "Completed: $(date)" | tee -a "$RESULTS_FILE"
echo "Log directory: $LOG_DIR" | tee -a "$RESULTS_FILE"
echo "" | tee -a "$RESULTS_FILE"

# Display results summary
echo "=== TEST SUMMARY ==="
cat "$RESULTS_FILE"
echo ""
echo "=== LOG FILES ==="
ls -lh "$LOG_DIR"

# Ask user if they want to keep logs
echo ""
echo "========================================"
echo "Test logs are stored in: $LOG_DIR"
echo "========================================"
read -p "Delete log files? (y/N): " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Cleaning up log files..."
    rm -rf "$LOG_DIR"
    echo "✓ Log files removed"
else
    echo "✓ Log files preserved at: $LOG_DIR"
fi

echo ""
echo "=== TEST SUITE COMPLETE ==="
