# table_detect.py
# OpenCV-based object detection for the table cam.
# Detects a target object by HSV colour range and exposes its normalised
# centre position for use by smart_table_take.
#
# Usage:
#   import table_detect
#   table_detect.set_detection_enabled(True)
#   # ... push frames from the table cam preview loop ...
#   annotated = table_detect.push_frame(frame_bgr)
#   det = table_detect.get_detected_object()   # dict or None
#   if det:
#       x, y = det["x"], det["y"]   # both 0.0-1.0 normalised

import threading
import time

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

import config

# ---------------------------------------------------------------------------
# Module-level state (protected by _lock)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_latest_detection = None     # dict or None
_latest_frame_t = 0.0
_detection_enabled = False
_stable_count = 0            # consecutive frames with a valid detection


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_detection_enabled(enabled: bool) -> None:
    """Enable or disable detection processing in push_frame()."""
    global _detection_enabled, _stable_count
    with _lock:
        _detection_enabled = bool(enabled)
        if not enabled:
            _stable_count = 0


def is_detection_enabled() -> bool:
    with _lock:
        return _detection_enabled


def get_detected_object():
    """Return the latest detection dict or None.

    Dict keys:
        x, y        normalised centre (0.0-1.0)
        w, h        normalised bounding-box size
        area_norm   contour area as fraction of frame area
        seen_t      time.time() when detected
        stable      True once detection has been confirmed for N frames
    """
    with _lock:
        return dict(_latest_detection) if _latest_detection is not None else None


def push_frame(frame_bgr):
    """Process a BGR frame in-place, update the latest detection, and return
    an annotated copy.  Call this from the table-cam preview display loop.
    Returns the frame unchanged if detection is disabled or cv2 is missing.
    """
    global _latest_detection, _latest_frame_t, _stable_count

    if cv2 is None or frame_bgr is None:
        return frame_bgr

    with _lock:
        enabled = _detection_enabled

    if not enabled:
        return frame_bgr

    # --- HSV colour range (configurable) ------------------------------------
    h_lo   = int(getattr(config, "TABLE_DETECT_H_LO",           10))
    h_hi   = int(getattr(config, "TABLE_DETECT_H_HI",           35))
    s_lo   = int(getattr(config, "TABLE_DETECT_S_LO",           80))
    s_hi   = int(getattr(config, "TABLE_DETECT_S_HI",          255))
    v_lo   = int(getattr(config, "TABLE_DETECT_V_LO",           60))
    v_hi   = int(getattr(config, "TABLE_DETECT_V_HI",          255))
    wrap   = bool(getattr(config, "TABLE_DETECT_HUE_WRAP",    False))
    min_af = float(getattr(config, "TABLE_DETECT_MIN_AREA_FRAC", 0.001))
    stable_n = int(getattr(config, "TABLE_DETECT_STABLE_FRAMES",     4))

    fh, fw = frame_bgr.shape[:2]
    frame_out = frame_bgr.copy()
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

    lower = np.array([h_lo, s_lo, v_lo], dtype=np.uint8)
    upper = np.array([h_hi, s_hi, v_hi], dtype=np.uint8)

    if wrap:
        # Hue wraps (e.g. red spans 0-10 and 170-179)
        lower2 = np.array([0,       s_lo, v_lo], dtype=np.uint8)
        upper2 = np.array([h_lo,    s_hi, v_hi], dtype=np.uint8)
        lower3 = np.array([h_hi,    s_lo, v_lo], dtype=np.uint8)
        upper3 = np.array([179,     s_hi, v_hi], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower2, upper2) | cv2.inRange(hsv, lower3, upper3)
    else:
        mask = cv2.inRange(hsv, lower, upper)

    # Morphological cleanup: remove noise, fill holes
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.dilate(mask, kernel, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_px = min_af * fw * fh
    valid = [(c, cv2.contourArea(c)) for c in contours if cv2.contourArea(c) >= min_px]

    now = time.time()
    if valid:
        best_c, best_area = max(valid, key=lambda t: t[1])
        bx, by, bw, bh = cv2.boundingRect(best_c)
        cx = (bx + bw / 2.0) / fw
        cy = (by + bh / 2.0) / fh

        with _lock:
            _stable_count = min(_stable_count + 1, stable_n)
            stable = _stable_count >= stable_n
            _latest_detection = {
                "x":         float(cx),
                "y":         float(cy),
                "w":         float(bw / fw),
                "h":         float(bh / fh),
                "area_norm": float(best_area / (fw * fh)),
                "seen_t":    now,
                "stable":    stable,
            }
            _latest_frame_t = now

        # Draw overlay
        color = (0, 200, 255) if stable else (100, 100, 255)
        px_c, py_c = int(cx * fw), int(cy * fh)
        cv2.rectangle(frame_out, (bx, by), (bx + bw, by + bh), color, 2)
        cv2.circle(frame_out, (px_c, py_c), 8, color, -1)
        label = f"OBJ ({cx:.2f},{cy:.2f})"
        if stable:
            label += " LOCKED"
        cv2.putText(frame_out, label, (bx, max(by - 8, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
    else:
        with _lock:
            _stable_count = max(_stable_count - 1, 0)
            if _stable_count == 0:
                _latest_detection = None
            _latest_frame_t = now

    return frame_out
