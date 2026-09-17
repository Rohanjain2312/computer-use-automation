#!/usr/bin/env bash
# One-time setup: virtualenv, dependencies, browser.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium

[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example — add ANTHROPIC_API_KEY before running discovery"; }

echo
echo "setup complete."
echo "  start the target app : .venv/bin/cua serve-app"
echo "  run the demo         : ./scripts/demo.sh"
