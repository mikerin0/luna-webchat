#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/luna-headless"
SERVICE_FILE="$SERVICE_DIR/luna-headless.service"
ENV_FILE="$CONFIG_DIR/luna.env"

mkdir -p "$SERVICE_DIR" "$CONFIG_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
# Optional bearer token for Ubuntu's Pi headless control requests.
LUNA_HEADLESS_TOKEN=
LUNA_HEADLESS_PORT=8004
EOF
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Luna headless Pi services
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=/usr/bin/env bash $SCRIPT_DIR/run_headless.sh
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now luna-headless.service
systemctl --user --no-pager --full status luna-headless.service | sed -n '1,22p'
