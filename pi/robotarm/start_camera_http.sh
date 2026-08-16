#!/usr/bin/env bash
set -euo pipefail
PORT="${STREAM_PORT:-8002}"
CAMERA_NAME="${STREAM_CAMERA_NAME:-/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a}"
source /home/arm/hailo-rpi5-examples/venv_hailo_rpi_examples/bin/activate
/usr/bin/gst-launch-1.0 -v libcamerasrc camera-name="$CAMERA_NAME" ! video/x-raw,width=1536,height=864,framerate=10/1 ! videoconvert ! jpegenc ! multipartmux boundary=frame ! tcpserversink host=0.0.0.0 port=$PORT
