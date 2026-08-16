"""Small HTTP control surface for the Pi headless robot services."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import brain
import lcd
import voice_assistant

HOST = os.getenv("LUNA_HEADLESS_HOST", "0.0.0.0")
PORT = int(os.getenv("LUNA_HEADLESS_PORT", "8004"))
TOKEN = os.getenv("LUNA_HEADLESS_TOKEN", "").strip()
_server: ThreadingHTTPServer | None = None


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not TOKEN:
        return True
    return handler.headers.get("Authorization", "") == f"Bearer {TOKEN}"


def _status() -> dict[str, Any]:
    return {
        "ok": True,
        "voice_started": bool(getattr(voice_assistant, "_started", False)),
        "mic_muted": bool(voice_assistant.is_muted()),
        "speaking": bool(brain.is_speaking()),
        "face": lcd.get_current_face_name(),
        "emotion": lcd.get_emotion(),
        "arm_power": brain.get_servo_power_status(timeout_s=1.5),
    }


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, _status())
            return
        if self.path == "/status":
            if not _authorized(self):
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            self._send(200, _status())
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not _authorized(self):
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        if self.path != "/control":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "invalid JSON"})
            return

        action = str(payload.get("action", "")).strip().lower()
        if action == "mute":
            voice_assistant.set_muted(True)
        elif action == "unmute":
            voice_assistant.set_muted(False)
        elif action == "face":
            emotion = str(payload.get("emotion", "neutral"))
            if not lcd.set_emotion(emotion):
                self._send(400, {"ok": False, "error": "invalid emotion"})
                return
        elif action == "text":
            lcd.show_text(str(payload.get("text", "")), float(payload.get("duration", 2.0)))
        elif action == "arm_power_on":
            if not brain.power_on_and_rest():
                self._send(502, {"ok": False, "error": "relay unreachable or rest pose failed"})
                return
        elif action == "arm_power_off":
            if not brain.set_servo_power(False):
                self._send(502, {"ok": False, "error": "relay unreachable"})
                return
        elif action == "arm_power_toggle":
            current = brain.get_servo_power_status(timeout_s=2.0)
            turning_on = current is not True
            ok = brain.power_on_and_rest() if turning_on else brain.set_servo_power(False)
            if not ok:
                self._send(502, {"ok": False, "error": "relay unreachable or rest pose failed"})
                return
        else:
            self._send(400, {"ok": False, "error": "unsupported action"})
            return
        self._send(200, _status())

    def log_message(self, *_args: Any) -> None:
        return


def start_server() -> ThreadingHTTPServer:
    global _server
    if _server is not None:
        return _server
    _server = ThreadingHTTPServer((HOST, PORT), _Handler)
    threading.Thread(target=_server.serve_forever, daemon=True, name="HeadlessControlHTTP").start()
    print(f"[Headless] Control server listening on {HOST}:{PORT}")
    return _server


def stop_server() -> None:
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None


if __name__ == "__main__":
    start_server()
    threading.Event().wait()
