#!/usr/bin/env bash
# Human-in-the-loop with a REAL person: a headed browser plus the operator console.
#
# The run pauses on the entitlement block. Open the console URL it prints, click
# "Take control", do the supervisor override yourself in the browser window the
# automation is driving, then click "Release & resume".
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
CUA=.venv/bin/cua
BASE=${MERIDIAN_BASE_URL:-http://127.0.0.1:8799}

$PY -m mockapp.app >/tmp/cua-mockapp.log 2>&1 &
APP_PID=$!
trap 'kill $APP_PID 2>/dev/null || true' EXIT
until curl -sf "$BASE/healthz" >/dev/null; do sleep 0.3; done

echo "override code for this demo: ${MERIDIAN_OVERRIDE_CODE:-see .env}"
exec $CUA replay -c member_savings_balance_lookup -i member_id=100999 \
    --label handoff_human --operator console --headed --base-url "$BASE"
