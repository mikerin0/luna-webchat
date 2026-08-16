# main.py
# Entry point for the robot arm application

import datetime
import os
import sys
import threading


class _TeeStream:
    def __init__(self, original, log_file):
        self._original = original
        self._log_file = log_file
        self._lock = threading.Lock()

    def write(self, data):
        if not data:
            return 0
        with self._lock:
            try:
                self._original.write(data)
                self._original.flush()
            except Exception:
                pass
            try:
                self._log_file.write(data)
                self._log_file.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        with self._lock:
            try:
                self._original.flush()
            except Exception:
                pass
            try:
                self._log_file.flush()
            except Exception:
                pass


def _setup_runtime_log():
    log_path = os.path.join(os.path.dirname(__file__), "runtime.log")
    log_file = open(log_path, "a", encoding="utf-8")
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file.write(f"\n===== Run started {ts} =====\n")
    log_file.flush()
    sys.stdout = _TeeStream(sys.stdout, log_file)
    sys.stderr = _TeeStream(sys.stderr, log_file)


_setup_runtime_log()

import gui
import lcd
import voice_assistant
import brain

if __name__ == "__main__":
    print("Robot Arm Main Program - launching GUI...")
    brain.start_crestron_server()
    lcd.show_face("thinking")
    voice_assistant.start()
    btn_win = gui.ButtonWindow()
    slider_win = gui.SliderWindow(btn_win)
    btn_win.mainloop()
