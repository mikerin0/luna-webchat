#!/usr/bin/env python3
"""
Enumerate all available libcamera cameras and print their index, id, and path.
This helps determine the correct camera-index for the high cam.
"""
import subprocess
import re

def enumerate_libcamera_cameras():
    try:
        # Use libcamera-hello to list cameras (simulate --list-cameras if available)
        result = subprocess.run(["libcamera-hello", "--list-cameras"], capture_output=True, text=True)
        output = result.stdout + result.stderr
        print("\n[libcamera-hello --list-cameras output]:\n" + output)
        # Parse output for camera index and path
        camera_re = re.compile(r"\[(\d+)\]: (.+)")
        for line in output.splitlines():
            m = camera_re.match(line.strip())
            if m:
                idx, path = m.groups()
                print(f"Camera index {idx}: {path}")
    except Exception as e:
        print(f"Error enumerating cameras: {e}")

if __name__ == "__main__":
    enumerate_libcamera_cameras()
