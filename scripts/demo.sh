#!/usr/bin/env bash
# The full vertical slice: discovery -> artifact -> replay -> error paths -> handoff.
#
# Needs ANTHROPIC_API_KEY in .env (the discovery step makes real model calls).
# Use scripts/replay.sh if you only want the no-model half.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
CUA=.venv/bin/cua
BASE=${MERIDIAN_BASE_URL:-http://127.0.0.1:8799}

echo "==> starting the MeridianCore mock console"
$PY -m mockapp.app >/tmp/cua-mockapp.log 2>&1 &
APP_PID=$!
trap 'kill $APP_PID 2>/dev/null || true' EXIT
until curl -sf "$BASE/healthz" >/dev/null; do sleep 0.3; done

echo
echo "==> 1/6  LLM-driven discovery (the only step that uses a model)"
$CUA discover --task tasks/member_savings_lookup.yaml --base-url "$BASE"

echo
echo "==> 2/6  deterministic replay, happy path"
$CUA replay -c member_savings_balance_lookup -i member_id=100731 --label happy --base-url "$BASE"

echo
echo "==> 3/6  business outcome: the member does not exist"
$CUA replay -c member_savings_balance_lookup -i member_id=999999 --label not_found --base-url "$BASE"

echo
echo "==> 4/6  caller contract violation: malformed input, rejected before the app is touched"
$CUA replay -c member_savings_balance_lookup -i member_id=oops --label bad_input --base-url "$BASE" || true

echo
echo "==> 5/6  hard failure: injected host error"
$CUA inject app_error --base-url "$BASE"
$CUA replay -c member_savings_balance_lookup -i member_id=100244 --label app_error --base-url "$BASE" || true
$CUA inject none --base-url "$BASE"

echo
echo "==> 6/6  escalation and same-session human takeover, then resume"
$CUA replay -c member_savings_balance_lookup -i member_id=100999 --label handoff \
    --operator scripted --base-url "$BASE"

echo
echo "==> capabilities an AI agent can now call"
$CUA catalog list
echo
echo "==> invoked the way an agent would: by name, typed in, typed out"
$CUA invoke member_savings_balance_lookup -i member_id=100244 --base-url "$BASE"
echo
echo "evidence written under evidence/"
