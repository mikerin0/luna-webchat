#!/usr/bin/env bash
set -euo pipefail
cd /home/arm/robotarm
source /home/arm/hailo-rpi5-examples/venv_hailo_rpi_examples/bin/activate
python3 simple_cam_server.py
