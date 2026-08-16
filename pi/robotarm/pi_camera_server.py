import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
import http.server

ROOT = Path('/tmp')
IMAGE_PATH = ROOT / 'robot_cam_latest.jpg'
PORT = int(os.getenv('CAM_PORT', '8003'))
CAPTURE_INTERVAL = float(os.getenv('CAM_CAPTURE_INTERVAL', '0.08'))

stop_event = threading.Event()


def capture_loop():
    while not stop_event.is_set():
        try:
            subprocess.run(
                [
                    'rpicam-jpeg',
                    '--camera', '0',
                    '--width', '640',
                    '--height', '360',
                    '--output', str(IMAGE_PATH),
                    '--timeout', '400',
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            time.sleep(1)
            continue
        time.sleep(CAPTURE_INTERVAL)


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, payload: dict[str, bool], status: int = 200) -> None:
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            accept_header = self.headers.get('Accept', '')
            wants_html = 'text/html' in accept_header or 'application/xhtml+xml' in accept_header
            image_ready = IMAGE_PATH.exists()
            if wants_html:
                body = (
                    f'<html><body>'
                    f'<h1>Camera server healthy</h1>'
                    f'<p>Status: OK</p>'
                    f'<p>Latest image: {"available" if image_ready else "not yet captured"}</p>'
                    f'</body></html>'
                ).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json({'ok': True, 'image_ready': image_ready})
            return

        if self.path != '/cam.jpg':
            self.send_response(404)
            self.end_headers()
            return

        if IMAGE_PATH.exists():
            data = IMAGE_PATH.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        return


if __name__ == '__main__':
    thread = threading.Thread(target=capture_loop, daemon=True)
    thread.start()

    httpd = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'cam server on {PORT}')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop_event.set()
        httpd.server_close()
