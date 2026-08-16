"""Headless Pi entry point: LCD face, voice, TTS, Crestron, and controls."""

import signal
import threading

import brain
import headless_control_server
import lcd
import voice_assistant


_stop_event = threading.Event()


def _stop(_signum, _frame) -> None:
    _stop_event.set()
    headless_control_server.stop_server()


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print("Luna headless Pi services starting")
    brain.start_crestron_server()
    lcd.show_face("thinking")
    lcd.start_animated_face_mode()
    voice_assistant.start()
    headless_control_server.start_server()
    print("Luna headless Pi services ready")
    _stop_event.wait()


if __name__ == "__main__":
    main()
