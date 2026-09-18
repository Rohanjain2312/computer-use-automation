#!/usr/bin/env bash
# Prove deterministic replay consults no model. See scripts/prove_no_llm.py.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
BASE=${MERIDIAN_BASE_URL:-http://127.0.0.1:8799}

$PY -m mockapp.app >/tmp/cua-mockapp.log 2>&1 &
APP_PID=$!
trap 'kill $APP_PID 2>/dev/null || true' EXIT
until curl -sf "$BASE/healthz" >/dev/null; do sleep 0.3; done

echo "============================================================"
echo " 1/2  REPLAY under the seals — must SUCCEED"
echo "============================================================"
$PY scripts/prove_no_llm.py replay 2>&1 | grep -vE "^  \[ *[0-9]+\]"
replay_rc=${PIPESTATUS[0]}

echo
echo "============================================================"
echo " 2/2  CONTROL: DISCOVERY under the same seals — must FAIL"
echo "============================================================"
$PY scripts/prove_no_llm.py discover 2>&1 | grep -E "seal|SEAL|raised|PASS|FAIL"
control_rc=${PIPESTATUS[0]}

# Evidence check: discovery writes a model trace, replay never does.
echo
traced=$(find evidence/replay -name model_trace.jsonl 2>/dev/null | wc -l | tr -d ' ')
echo "replay runs that wrote a model trace: $traced  (expected 0)"

echo
if [ "$replay_rc" -eq 0 ] && [ "$control_rc" -eq 0 ] && [ "$traced" -eq 0 ]; then
  echo "RESULT: replay consults no model."
  exit 0
fi
echo "RESULT: verification FAILED (replay=$replay_rc control=$control_rc traces=$traced)"
exit 1
