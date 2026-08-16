#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 {status|start|stop|restart|enable|disable|logs}" >&2
  exit 1
}

cmd="${1:-}"
case "$cmd" in
  status)
    systemctl --user --no-pager --full status luna-webchat.service
    ;;
  start)
    systemctl --user start luna-webchat.service
    ;;
  stop)
    systemctl --user stop luna-webchat.service
    ;;
  restart)
    systemctl --user restart luna-webchat.service
    ;;
  enable)
    systemctl --user enable --now luna-webchat.service
    ;;
  disable)
    systemctl --user disable --now luna-webchat.service
    ;;
  logs)
    journalctl --user -u luna-webchat.service -n 100 --no-pager
    ;;
  *)
    usage
    ;;
esac
