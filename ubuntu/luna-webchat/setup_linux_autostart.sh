#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/luna-webchat"
SERVICE_FILE="$SERVICE_DIR/luna-webchat.service"
ENV_FILE="$CONFIG_DIR/luna.env"

mkdir -p "$SERVICE_DIR" "$CONFIG_DIR"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Luna Webchat HTTPS Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=/usr/bin/env bash $REPO_DIR/run_https.sh
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
# Luna runtime environment
# Set this to match WINDOWS_AGENT_TOKEN on Windows agent host.
# Example: LUNA_WINDOWS_AGENT_TOKEN=replace-with-random-token
LUNA_WINDOWS_AGENT_TOKEN=

# Optional override if Windows agent host/port changes.
LUNA_WINDOWS_AGENT_URL=http://172.31.31.11:8787
EOF
  echo "Created $ENV_FILE"
else
  echo "Keeping existing $ENV_FILE"
fi

systemctl --user daemon-reload
systemctl --user enable --now luna-webchat.service

echo
echo "Autostart enabled for user service: luna-webchat.service"
echo "Status:"
systemctl --user --no-pager --full status luna-webchat.service | sed -n '1,20p'
echo
echo "If you want this to start at boot before login, run once:"
echo "  sudo loginctl enable-linger $USER"
