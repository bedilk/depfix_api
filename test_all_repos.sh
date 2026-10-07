#!/bin/bash

RESULTS_FILE="detailed_test_results.txt"
echo "=== DEPFIX TEST RESULTS - $(date) ===" > $RESULTS_FILE
echo "" >> $RESULTS_FILE

test_repo() {
    local repo_full=$1
    local provider=$2
    local num=$3
    
    echo "" | tee -a $RESULTS_FILE
    echo "========================================" | tee -a $RESULTS_FILE
    echo "TEST $num/7: $repo_full ($provider)" | tee -a $RESULTS_FILE
    echo "========================================" | tee -a $RESULTS_FILE
    
    # INIT
    echo ">> INIT" | tee -a $RESULTS_FILE
    depfix init --offline --keep-state 2>&1 | grep -E "ready|database" | head -2 | tee -a $RESULTS_FILE
    
    # SCAN
    echo ">> SCAN" | tee -a $RESULTS_FILE
    local scan_output=$(depfix scan --repo $repo_full --provider $provider 2>&1)
    echo "$scan_output" > "scan_${num}.log"
    echo "$scan_output" | grep -E "files scanned|call sites|Package|version|Manifest" | tee -a $RESULTS_FILE
    
    # Extract detailed info
    local files=$(echo "$scan_output" | grep "files scanned" | grep -oE "files scanned: [0-9]+" | grep -oE "[0-9]+")
    local sites=$(echo "$scan_output" | grep "call sites:" | grep -oE "call sites: [0-9]+" | grep -oE "[0-9]+")
    local actionable=$(echo "$scan_output" | grep "actionable" | grep -oE "[0-9]+ actionable" | grep -oE "[0-9]+")
    
    echo "   Files: $files, Sites: $sites, Actionable: $actionable" | tee -a $RESULTS_FILE
    
    # PLAN
    echo ">> PLAN" | tee -a $RESULTS_FILE
    local plan_output=$(depfix plan --repo $repo_full --provider $provider \
        --llm-provider bedrock \
        --model us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
        --dry-run 2>&1)
    echo "$plan_output" > "plan_${num}.log"
    echo "$plan_output" | grep -E "breaking change|No breaking|cost|Cost" | tail -3 | tee -a $RESULTS_FILE
    
    echo "✓ Complete" | tee -a $RESULTS_FILE
}

# Test all repos
test_repo "bedilk/probot" "octokit" "1"
test_repo "bedilk/renovate" "octokit" "2"
test_repo "bedilk/bolt-js" "slack" "3"
test_repo "bedilk/sentry" "slack" "4"
test_repo "bedilk/AtlantaBot" "discord" "5"
test_repo "bedilk/guide" "discord" "6"
test_repo "bedilk/Rocket.Chat" "mongodb" "7"

echo "" | tee -a $RESULTS_FILE
echo "=== ALL TESTS COMPLETE ===" | tee -a $RESULTS_FILE
