#!/usr/bin/env bash
# Full test suite. Browser-backed tests need chromium (scripts/setup.sh installs it).
# No model is called: every test is deterministic.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m pytest "$@"
