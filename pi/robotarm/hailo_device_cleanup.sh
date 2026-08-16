#!/bin/bash
# hailo_device_cleanup.sh
# This script checks for and kills any processes using the Hailo device, stops the hailort_service, and scans for the device.

set -e

echo "[1/5] Checking for running Hailo or Python processes..."
ps aux | grep -E 'hailo|python' | grep -v grep

read -p "Enter any suspicious PID(s) to kill, separated by spaces (or press Enter to skip): " pids
if [ ! -z "$pids" ]; then
  for pid in $pids; do
    echo "Killing PID $pid..."
    sudo kill -9 $pid || true
  done
else
  echo "No PIDs entered. Skipping kill step."
fi

echo "[2/5] Stopping hailort_service if running..."
sudo systemctl stop hailort_service || true

echo "[3/5] Checking Hailo device node..."
ls -l /dev/hailo0 || echo "/dev/hailo0 not found!"

echo "[4/5] Scanning for Hailo devices..."
hailortcli scan

echo "[5/5] If you still have issues, try rebooting: sudo reboot"
echo "Then run your app as the first and only Hailo process."
