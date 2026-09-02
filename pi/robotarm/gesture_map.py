"""Gesture clip definitions and intent-to-gesture mapping for conversational
arm behaviors.

Each gesture clip is a list of keyframes:
    {"delta": {servo_id: signed_pulse_delta, ...}, "time_ms": nominal_ms, "face": "happy"}

Deltas are relative to poses.NEUTRAL_POSE and are scaled by the "energy"
parameter passed to brain.gesture(); "face" is optional and triggers an LCD
expression change alongside the movement. All amplitude/time values below are
small, conservative defaults.

TODO: calibrate amplitudes and timings against the physical arm's real range
of motion and desired expressiveness; these are safe starting points, not
tuned choreography.
"""

GESTURE_CLIPS = {
    "GESTURE_WAVE_SMALL": [
        {"delta": {6: -120}, "time_ms": 260},
        {"delta": {6: 120}, "time_ms": 260},
        {"delta": {6: -120}, "time_ms": 260},
        {"delta": {6: 0}, "time_ms": 260},
    ],
    "GESTURE_EXPLAIN_A": [
        {"delta": {5: -80, 3: 60}, "time_ms": 350},
        {"delta": {5: 80, 3: -60}, "time_ms": 350},
        {"delta": {5: 0, 3: 0}, "time_ms": 350},
    ],
    "GESTURE_EXPLAIN_B": [
        {"delta": {4: -90, 6: 80}, "time_ms": 350},
        {"delta": {4: 90, 6: -80}, "time_ms": 350},
        {"delta": {4: 0, 6: 0}, "time_ms": 350},
    ],
    "GESTURE_THINK": [
        {"delta": {1: 60, 3: -40}, "time_ms": 500, "face": "thinking"},
        {"delta": {1: 0, 3: 0}, "time_ms": 600},
    ],
    "GESTURE_YES": [
        {"delta": {3: -90}, "time_ms": 260},
        {"delta": {3: 90}, "time_ms": 260},
        {"delta": {3: -90}, "time_ms": 260},
        {"delta": {3: 0}, "time_ms": 260, "face": "happy"},
    ],
    "GESTURE_NO": [
        {"delta": {6: -150}, "time_ms": 320, "face": "sad"},
        {"delta": {6: 150}, "time_ms": 320},
        {"delta": {6: -150}, "time_ms": 320},
        {"delta": {6: 0}, "time_ms": 320},
    ],
    "GESTURE_APOLOGY": [
        {"delta": {5: 60, 4: -60}, "time_ms": 400, "face": "sad"},
        {"delta": {1: 40}, "time_ms": 350},
        {"delta": {5: 0, 4: 0, 1: 0}, "time_ms": 500},
    ],
}

# intent -> single clip name, or a list of clip names to alternate between
# on successive calls (used for "explain" so it doesn't repeat identically).
INTENT_TO_GESTURE = {
    "greet": "GESTURE_WAVE_SMALL",
    "explain": ["GESTURE_EXPLAIN_A", "GESTURE_EXPLAIN_B"],
    "think": "GESTURE_THINK",
    "confirm": "GESTURE_YES",
    "deny": "GESTURE_NO",
    "apology": "GESTURE_APOLOGY",
}
