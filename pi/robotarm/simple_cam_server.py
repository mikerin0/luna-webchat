import os
import subprocess
import threading
import time
import http.server
from pathlib import Path

ROOT = Path("/tmp")
IMAGE_PATH = ROOT / "robot_cam_latest.jpg"
PORT = int(os.getenv("CAM_PORT", "8010"))


def capture_frame() -> None:
    subprocess.run([
        "/bin/bash",
        "-lc",
        "source /home/arm/hailo-rpi5-examples/venv_hailo_rpi_examples/bin/activate && rpicam-jpeg --camera 0 --width 1536 --height 864 --output /tmp/robot_cam_latest.jpg --timeout 1000 >/dev/null 2>&1",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def capture_loop() -> None:
    while True:
        try:
            capture_frame()
        except Exception:
            pass
        time.sleep(0.2)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b{ok:true})
            return
        if self.path != "/cam.jpg":
            self.send_response(404)
            self.end_headers()
            return
        if IMAGE_PATH.exists():
            data = IMAGE_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        return


capture_frame()
threading.Thread(target=capture_loop, daemon=True).start()
httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
print(f"cam server on {PORT}")
httpd.serve_forever()
