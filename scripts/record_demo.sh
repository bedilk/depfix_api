#!/usr/bin/env bash
# The full loop, in one take, for the 2.2 recording.
#
# Deterministic on purpose: it runs against the local verifiable fixture, so
# it needs no GitHub App and no network beyond the LLM call, and it produces
# the same beats every time you record it. Start the screen capture, run
# this, stop the capture.
#
#   GOOGLE_API_KEY=... bash scripts/record_demo.sh
set -euo pipefail

FIXTURE="tests/fixtures/openai_v3_verifiable"
WORK="$(mktemp -d -t depfix-demo-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

pause() { printf '\n'; sleep "${DEMO_PAUSE:-2}"; }
beat()  { printf '\n\033[1;34m── %s\033[0m\n\n' "$1"; sleep 1; }

beat "0. A repo written against openai-node v3"
cp -R "$FIXTURE" "$WORK/app"
sed -n '1,20p' "$WORK/app/src/chat.js"
pause

beat "1. Preflight — is this machine able to run depfix at all?"
depfix init --offline --keep-state
pause

beat "2. Watch — poll every provider feed for movement"
depfix watch --once --provider openai
pause

beat "3. Classify — turn detected events into structured breaking changes"
depfix classify
pause

beat "4. Scan — find the call sites in the repo"
depfix scan --path "$WORK/app" --provider openai --no-save
pause

beat "5. Fix + verify — generate the migration, then run the repo's OWN tests"
# The fixture ships a real node:test suite behind a local file: SDK shim, so
# this install hits no registry and the verdict below is a real test result.
depfix plan "$WORK/app" --provider openai --llm-provider "${LLM_PROVIDER:-gemini}" || true
pause

beat "6. The evidence"
echo "Plan artifacts:"
ls -1 .depfix/plans/ 2>/dev/null || echo "  (none — see the per-file reasons above)"
pause

beat "Done. Every verdict above came from the repo's own test suite."
