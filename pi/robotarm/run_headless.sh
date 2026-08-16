#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
VENV="${LUNA_PI_VENV_DIR:-/home/arm/hailo-venv}"
PYTHON_BIN="$VENV/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing Pi Python environment: $PYTHON_BIN" >&2
  exit 1
fi
exec "$PYTHON_BIN" headless_main.py
