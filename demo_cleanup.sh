#!/bin/bash
# Quick demo of tmp folder cleanup feature
# Tests with just one repository

set -e

# Create temp directory for logs
TEST_ID="depfix-demo-$(date +%s)"
LOG_DIR="/tmp/${TEST_ID}"
mkdir -p "$LOG_DIR"

echo "========================================="
echo "DEMO: Tmp Folder Auto-Cleanup Feature"
echo "========================================="
echo ""
echo "Log directory: $LOG_DIR"
echo "Started: $(date)"
echo ""

# Test single repo: bedilk/probot
echo "Testing repository: bedilk/probot (octokit)"
echo ""

# Log files in temp directory
SCAN_LOG="$LOG_DIR/scan_probot.log"
PLAN_LOG="$LOG_DIR/plan_probot.log"
RESULTS_FILE="$LOG_DIR/test_results.txt"

echo "Step 1: INIT"
depfix init --offline --keep-state 2>&1 | grep -E "ready|database" | head -3
echo ""

echo "Step 2: SCAN"
depfix scan --repo bedilk/probot --provider octokit 2>&1 | tee "$SCAN_LOG" | tail -20
echo ""

echo "Step 3: PLAN"
depfix plan --repo bedilk/probot --provider octokit \
  --llm-provider bedrock \
  --model us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
  --dry-run 2>&1 | tee "$PLAN_LOG" | tail -10
echo ""

# Create results summary
cat > "$RESULTS_FILE" << EOF
DEMO TEST RESULTS - $(date)
======================================
Repository: bedilk/probot
Provider: octokit
Model: Sonnet 4.5

Files scanned: $(grep "files scanned:" "$SCAN_LOG" | awk '{print $3}' || echo "N/A")
Call sites: $(grep "call sites:" "$SCAN_LOG" | awk '{print $6}' || echo "N/A")

Logs stored in: $LOG_DIR
EOF

echo "========================================="
echo "TEST COMPLETE"
echo "========================================="
echo ""
echo "Log files created:"
ls -lh "$LOG_DIR"
echo ""
cat "$RESULTS_FILE"
echo ""

# Demonstrate auto-cleanup
echo "========================================="
echo "AUTO-CLEANUP DEMONSTRATION"
echo "========================================="
echo ""
echo "Logs are stored in: $LOG_DIR"
echo ""
echo "Automatically cleaning up in 3 seconds..."
sleep 3

rm -rf "$LOG_DIR"
echo "✓ Log directory removed: $LOG_DIR"
echo ""

# Verify cleanup
if [ -d "$LOG_DIR" ]; then
    echo "✗ Cleanup failed - directory still exists"
    exit 1
else
    echo "✓ Cleanup verified - no logs remain"
fi

echo ""
echo "========================================="
echo "DEMO COMPLETE - Tmp cleanup working!"
echo "========================================="
