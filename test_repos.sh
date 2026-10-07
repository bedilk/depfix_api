#!/bin/bash

# Test repos sequentially with depfix scan and plan
# Using Claude Sonnet 4.5 via Bedrock

MODEL="us.anthropic.claude-sonnet-4-5-20250929-v1:0"
RESULTS_DIR="test_results_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

test_repo() {
    local owner=$1
    local name=$2
    local provider=$3
    local output_file="$RESULTS_DIR/${owner//\//_}_${name}.log"
    
    echo "========================================" | tee -a "$output_file"
    echo "Testing: $owner/$name (provider: $provider)" | tee -a "$output_file"
    echo "Started: $(date)" | tee -a "$output_file"
    echo "========================================" | tee -a "$output_file"
    
    # Scan the repo
    echo "Running depfix scan..." | tee -a "$output_file"
    depfix scan --repo "$owner/$name" --provider "$provider" \
        --llm-provider bedrock \
        --model "$MODEL" \
        2>&1 | tee -a "$output_file"
    
    local scan_status=$?
    echo "Scan exit code: $scan_status" | tee -a "$output_file"
    
    if [ $scan_status -eq 0 ]; then
        echo "Scan succeeded. Running depfix plan..." | tee -a "$output_file"
        
        # Plan the fixes
        depfix plan --repo "$owner/$name" \
            --llm-provider bedrock \
            --model "$MODEL" \
            --dry-run \
            2>&1 | tee -a "$output_file"
        
        local plan_status=$?
        echo "Plan exit code: $plan_status" | tee -a "$output_file"
    else
        echo "Scan failed, skipping plan" | tee -a "$output_file"
    fi
    
    echo "Completed: $(date)" | tee -a "$output_file"
    echo "" | tee -a "$output_file"
}

# Test all repos
test_repo "probot" "probot" "octokit"
test_repo "renovatebot" "renovate" "octokit"
test_repo "slackapi" "bolt-js" "slack"
test_repo "getsentry" "sentry" "slack"
test_repo "Androz2091" "AtlantaBot" "discord"
test_repo "discordjs" "guide" "discord"
test_repo "RocketChat" "Rocket.Chat" "mongodb"

echo "All tests complete. Results in: $RESULTS_DIR"
