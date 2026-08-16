#!/usr/bin/env bash
set -euo pipefail
cd /home/ai/luna-webchat
if [[ ! -d .venv-image ]]; then
  python3 -m venv .venv-image
fi
source .venv-image/bin/activate
pip install -r requirements-image.txt
export COMFY_URL="${COMFY_URL:-http://127.0.0.1:8188}"
export COMFY_CHECKPOINT="DreamShaper_8_pruned.safetensors"
export COMFY_WIDTH="${COMFY_WIDTH:-512}"
export COMFY_HEIGHT="${COMFY_HEIGHT:-512}"
export COMFY_STEPS="${COMFY_STEPS:-2}"
export COMFY_CFG="${COMFY_CFG:-1.0}"
export COMFY_TIMEOUT_SECONDS="${COMFY_TIMEOUT_SECONDS:-420}"
exec uvicorn image_backend:app --host 0.0.0.0 --port 3020
