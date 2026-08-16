#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -r requirements.txt

CERT_DIR="$SCRIPT_DIR/.certs"
CERT_FILE="$CERT_DIR/luna-cert.pem"
KEY_FILE="$CERT_DIR/luna-key.pem"
PRIMARY_IP="$(hostname -I | awk '{print $1}')"

mkdir -p "$CERT_DIR"

if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then
  echo "Generating self-signed cert for localhost and $PRIMARY_IP ..."
  openssl req \
    -x509 \
    -nodes \
    -newkey rsa:2048 \
    -sha256 \
    -days 825 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,DNS:127.0.0.1,IP:127.0.0.1,IP:$PRIMARY_IP" \
    -addext "extendedKeyUsage=serverAuth" \
    -addext "basicConstraints=CA:FALSE" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE"
fi

exec uvicorn app:app \
  --host 0.0.0.0 \
  --port 3010 \
  --ssl-keyfile "$KEY_FILE" \
  --ssl-certfile "$CERT_FILE"
