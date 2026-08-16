# poses.py
# Stores absolute poses and direct servo commands

POSES = {
    # Sequential, one-servo-at-a-time command lists:
    # (servo_id, absolute_position, time_ms)
    #
    # Additional supported step syntaxes:
    # - synchronized up-to-3-servo move:
    #   {"servos": {sid: pos, sid: pos, ...}, "time_ms": 800}
    # - face change step:
    #   {"face": "happy", "time_ms": 300}
    "home": [
        # Move servo 5 (shoulder) to safe high position first so servo 3
        # (wrist) can reach its target without mechanical interference.
    #    (5, 1800, 1500),
        (1, 1500, 200),
        (2, 1500, 200),
        (3, 2021, 1000),
        (4, 2170, 1000),
        (6, 1500, 1000),
        # Finally bring shoulder down to the actual home position.
        (5, 1121, 2500),
    ],
    "sleep": [
        (6, 1500, 1000),
        (1, 1500, 200),
        (2, 1500, 200),
        (3, 2021, 100),
        (4, 2170, 1000),
        (5, 1800, 1000),
    ],
    "nod": [
        (1, 2200, 500),
        (3, 1850, 500),
        (3, 2080, 500),
        (3, 1850, 500),
        (3, 2080, 500),
        (3, 2021, 500),
        (1, 1500, 500),
    ],
    "nod-once": [
        (1, 2200, 500),
        (3, 1850, 500),
        (3, 2080, 500),
        (3, 2021, 500),
        (1, 1500, 500),
        {"face": "happy", "time_ms": 180},
    ],
    # "No" gesture in conversation mode.
    # Includes a face-change example before movement.
    "no": [
        {"face": "sad", "time_ms": 220},
        # Stabilize arm posture before the gesture.
        {"servos": {5: 1121, 4: 2170, 3: 2021}, "time_ms": 1200},
        # Sweep left.
        {"servos": {6: 1300, 2: 1650}, "time_ms": 450},
        # Sweep right.
        {"servos": {6: 1700, 2: 1350}, "time_ms": 450},
        # Sweep left again.
        {"servos": {6: 1300, 2: 1650}, "time_ms": 450},
        # Return to neutral and recover face.
        {"servos": {6: 1500, 2: 1500}, "time_ms": 500},
        {"face": "happy", "time_ms": 180},
    ],
    "take": [
        {"face": "thinking", "time_ms": 200},
        # Return to a known starting position.
#        {"pose": "home"},
        {"pose": "nod-once"},
        # Extend arm toward the object.
        {"servos": {1: 1500, 3: 1827, 4: 1568, 5: 1878}, "time_ms": 1000},
        # Settle before gripping.
        {"wait_ms": 1000},
        # Close claw; stops automatically when microswitch triggers.
        {"close_claw": True, "step_time_ms": 200},
        # Lift arm with the object.
        {"servos": {3: 2021, 4: 2170, 5: 1121}, "time_ms": 1000},
        {"face": "happy", "time_ms": 200},
    ],
    "table-take": [
        # Move to known start pose over the table.
        {"servos": {1: 1500, 2: 1500, 3: 2021, 4: 2170, 5: 1121, 6: 1500}, "time_ms": 1400},
        # Rotate base toward the designated pickup spot.
        #(6, 1000, 900),
        # Lower/extend wrist and shoulder together.
        {"servos": {3: 2079, 5: 1840}, "time_ms": 1000},
        # Final slow shoulder approach.
        (5, 1937, 1800),
        # Short settle helps reduce switch bounce and pickup misses.
        {"wait_ms": 250},
        # Close claw until microswitch confirms contact (gentler + stronger debounce).
        {
            "close_claw": True,
            "step_us": 20,
            "step_time_ms": 130,
            "switch_confirm_reads": 3,
            "switch_confirm_interval_ms": 10,
            "trigger_extra_close_us": 8,
            "trigger_extra_close_time_ms": 100,
        },
        # Return to the initial known pose while holding the object.
        {"servos": {2: 1500, 3: 2021, 4: 2170, 5: 1121, 6: 1500}, "time_ms": 1700},
    ],
    "roll": [
        # Start keyframe.
        {"servos": {1: 1500, 2: 1500, 3: 2021, 4: 2170, 5: 1121, 6: 1500}, "time_ms": 800},
        # Midpoint between keyframe 1 and 2.
        {"servos": {1: 1854, 2: 1500, 3: 1885, 4: 2405, 5: 1121, 6: 1257}, "time_ms": 700},
        # Keyframe 2.
        {"servos": {1: 2209, 2: 1500, 3: 1749, 4: 2641, 5: 1121, 6: 1015}, "time_ms": 800},
        # Midpoint between keyframe 2 and 3.
        {"servos": {1: 2209, 2: 1500, 3: 1525, 4: 2641, 5: 1325, 6: 1267}, "time_ms": 700},
        # Keyframe 3.
        {"servos": {1: 2209, 2: 1500, 3: 1302, 4: 2641, 5: 1529, 6: 1520}, "time_ms": 800},
        # Midpoint between keyframe 3 and 4.
        {"servos": {1: 2209, 2: 1500, 3: 1500, 4: 2641, 5: 1300, 6: 1646}, "time_ms": 700},
        # Keyframe 4.
        {"servos": {1: 2209, 2: 1500, 3: 1749, 4: 2641, 5: 1121, 6: 1772}, "time_ms": 800},
        # Midpoint between keyframe 4 and 5.
        {"servos": {1: 1854, 2: 1500, 3: 1661, 4: 2405, 5: 1121, 6: 1636}, "time_ms": 700},
        # Final keyframe.
        {"servos": {1: 1500, 2: 1500, 3: 2021, 4: 2170, 5: 1121, 6: 1500}, "time_ms": 800},
    ],
    "reach": [
        # Start keyframe.
        {"servos": {1: 1500, 2: 1500, 3: 2021, 4: 2170, 5: 1121, 6: 1500}, "time_ms": 1500},
        {"servos": {2: 2044, 2: 2044, 3: 1380, 4: 1918, 5: 2034, 6: 840}, "time_ms": 1500},
        {"servos": {2: 500, 2: 1500, 3: 1380, 4: 1918, 5: 2034, 6: 2024}, "time_ms": 1500},
        {"servos": {2: 1500, 2: 1500, 3: 2021, 4: 2170, 5: 1121, 6: 1500}, "time_ms": 1500},
        (1, 1500, 500),
        (1, 2200, 500),
        (1, 1500, 500),
    ],
    "w": [
        # Start keyframe.
        {"servos": {1: 1500, 2: 1500, 3: 2021, 4: 2170, 5: 1121, 6: 1500}, "time_ms": 1500},
        {"servos": {2: 2044, 2: 2044, 3: 1380, 4: 1918, 5: 2034, 6: 840}, "time_ms": 1500},
        {"servos": {2: 801, 2: 1500, 3: 1380, 4: 1918, 5: 2034, 6: 2024}, "time_ms": 1500},
        {"servos": {2: 1500, 2: 1500, 3: 2021, 4: 2170, 5: 1121, 6: 1500}, "time_ms": 1500},
        (1, 1500, 500),
        (1, 2200, 500),
        (1, 1500, 500),   
    ]
}

def get_pose(name):
    return list(POSES.get(str(name).lower(), []))
