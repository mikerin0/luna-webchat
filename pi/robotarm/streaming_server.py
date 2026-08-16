import os
import subprocess
import threading
import time
import http.server
import socketserver
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("STREAM_PORT", "8001"))

class MJPEGHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b{ok: true})
            return

        if self.path != "/stream.mjpg":
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()

        while True:
            try:
                img_path = ROOT / "latest.jpg"
                if img_path.exists():
                    data = img_path.read_bytes()
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                time.sleep(0.1)
            except Exception:
                break

    def log_message(self, format, *args):
        return

class ThreadedTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

server = ThreadedTCPServer(("0.0.0.0", PORT), MJPEGHandler)
print(f"streaming server on {PORT}")
server.serve_forever()
