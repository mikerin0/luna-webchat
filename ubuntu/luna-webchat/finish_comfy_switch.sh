#!/usr/bin/env bash
set -euo pipefail

CHECKPOINT_DIR="/home/ai/ComfyUI/models/checkpoints"
CHECKPOINT_FILE="v1-5-pruned-emaonly.ckpt"
CHECKPOINT_URL="https://huggingface.co/runwayml/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.ckpt"

cd /home/ai/luna-webchat

# Force the bridge to use SD1.5 explicitly.
sed -i 's|^export COMFY_CHECKPOINT=.*$|export COMFY_CHECKPOINT="v1-5-pruned-emaonly.ckpt"|' run_image_backend.sh

mkdir -p "$CHECKPOINT_DIR"
cd "$CHECKPOINT_DIR"

# Resume download safely until complete.
curl -L --fail -C - -o "$CHECKPOINT_FILE" "$CHECKPOINT_URL"

cd /home/ai/ComfyUI
pkill -f 'python.*main.py.*8188' || true
nohup /home/ai/ComfyUI/.venv/bin/python main.py --listen 0.0.0.0 --port 8188 > /tmp/comfyui.log 2>&1 &

for _ in $(seq 1 180); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8188/prompt || true)
  if [[ "$code" == "200" ]]; then
    break
  fi
  sleep 1
done

cd /home/ai/luna-webchat
pkill -f 'uvicorn image_backend:app' || true
nohup ./run_image_backend.sh > /tmp/luna-image.log 2>&1 &

for _ in $(seq 1 120); do
  code=$(curl -s -o /tmp/luna-image-health -w '%{http_code}' http://127.0.0.1:3020/health || true)
  if [[ "$code" == "200" ]]; then
    break
  fi
  sleep 1
done

curl -sS -w '\nHTTP_STATUS:%{http_code}\n' --max-time 900 -X POST \
  http://127.0.0.1:3010/api/generate-image \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a cinematic portrait of a friendly robot in a workshop"}' \
  > /tmp/comfy-final-chat-test.out || true

python3 - <<'PY'
import json, pathlib
p = pathlib.Path('/tmp/comfy-final-chat-test.out')
out = p.read_text(encoding='utf-8', errors='ignore') if p.exists() else ''
body = out.split('\nHTTP_STATUS:')[0].strip()
status = out.split('HTTP_STATUS:')[-1].strip() if 'HTTP_STATUS:' in out else 'unknown'
img_ok = False
try:
    j = json.loads(body)
    img_ok = bool(j.get('image_base64'))
except Exception:
    pass
print('FINAL_CHAT_HTTP=' + str(status))
print('FINAL_CHAT_HAS_IMAGE=' + str(img_ok))
PY
