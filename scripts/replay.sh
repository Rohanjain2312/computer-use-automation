#!/usr/bin/env bash
# Everything except discovery — runs with no model access and no API key.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
CUA=.venv/bin/cua
BASE=${MERIDIAN_BASE_URL:-http://127.0.0.1:8799}
CAP=${1:-member_savings_balance_lookup}

$PY -m mockapp.app >/tmp/cua-mockapp.log 2>&1 &
APP_PID=$!
trap 'kill $APP_PID 2>/dev/null || true' EXIT
until curl -sf "$BASE/healthz" >/dev/null; do sleep 0.3; done

echo "==> happy path";        $CUA replay -c "$CAP" -i member_id=100244 --label happy --base-url "$BASE"
echo "==> business outcome";  $CUA replay -c "$CAP" -i member_id=999999 --label not_found --base-url "$BASE"
echo "==> invalid input";     $CUA replay -c "$CAP" -i member_id=oops --label bad_input --base-url "$BASE" || true
echo "==> injected failure";  $CUA inject app_error --base-url "$BASE"
$CUA replay -c "$CAP" -i member_id=100244 --label app_error --base-url "$BASE" || true
$CUA inject none --base-url "$BASE"
echo "==> human handoff";     $CUA replay -c "$CAP" -i member_id=100999 --label handoff --operator scripted --base-url "$BASE"
