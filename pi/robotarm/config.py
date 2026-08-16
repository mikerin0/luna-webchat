# Camera backend for GStreamer source: "libcamera" (default) or "v4l2"
CAMERA_BACKEND = "libcamera"
# Pi Camera (high cam, port 1) V4L2 device
# Set to "/dev/video0" or "/dev/video1" depending on which is the high cam
PI_CAMERA_V4L2_DEVICE = "/dev/video1"  # Change to "/dev/video1" if needed
# Table cam (port 0) V4L2 device
ARDUCAM_V4L2_DEVICE = "/dev/video0"    # table cam
# Wide FOV capture resolution (IMX708 native 16:9)
CAM_SENSOR_W = 1536
CAM_SENSOR_H = 864
""" 
Camera Index Mapping (from rpicam-hello --list-cameras):
	0 : table cam  (/base/axi/pcie@1000120000/rp1/i2c@88000/imx708@1a)
	1 : high cam   (/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a)
"""
# Pi Camera on port 1 (high camera, used for pose tracking via Hailo)
PI_CAMERA_INDEX = 1  # high cam
# Second IMX708 on port 0 (table / manipulation view)
ARDUCAM_INDEX = 0    # table cam
# Camera backend for GStreamer source: "libcamera" (default) or "v4l2"
CAMERA_BACKEND = "libcamera"
# Pi Camera (high cam, port 1) V4L2 device
PI_CAMERA_V4L2_DEVICE = "/dev/video0"  # high cam

# Camera index for high cam (matches rpicam-hello --camera 1)
PI_CAMERA_INDEX = 1
# Table cam (port 0) V4L2 device
ARDUCAM_V4L2_DEVICE = "/dev/video0"    # table cam
# Wide FOV capture resolution (IMX708 native 16:9)
CAM_SENSOR_W = 1536
CAM_SENSOR_H = 864
# config.py - Project configuration for robotarm

# Camera device for high cam
PI_CAMERA_DEVICE = "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a"
FRAME_W = 640
FRAME_H = 360

# Overlay and tracking parameters (defaults, update as needed)
ARM_X_CENTER = 0.20
ARM_X_RANGE = 0.10
ARM_Y_RANGE = 0.15
ARM_Z_DEFAULT = 0.15
TRACKING_ALPHA = 0.15
ARM_Y_DEFAULT = 0.0

# Wrist tracking X-axis orientation.
# False = use wrist x as-is, True = mirror left/right (for mirrored camera feeds).
TRACKING_X_INVERT = False

# Wrist tracking X center calibration (normalized 0.0..1.0).
# Use this when the detected wrist appears horizontally offset from the visual center.
# Example: if your wrist is in screen center but slider reads ~25, set to 0.25.
TRACKING_X_CENTER_NORM = 0.25
# Small deadband around calibrated center to reduce servo jitter.
TRACKING_X_CENTER_DEADBAND = 0.03

# Auto switch from HIGH_CAM to TABLE_CAM after wrist goes low then disappears.
AUTO_SWITCH_TO_TABLE_ENABLED = True
AUTO_SWITCH_DISAPPEAR_S = 0.30
# Slider semantics: low wrist is near 0% (not 100%).
AUTO_SWITCH_LOW_UD_PCT = 30.0
AUTO_SWITCH_LOW_MEMORY_S = 2.2
AUTO_SWITCH_LOW_FALLBACK_UD_PCT = 35.0
# Treat wrist detections at extreme bottom edge as effectively missing.
AUTO_SWITCH_EDGE_COUNTS_AS_MISSING = True
AUTO_SWITCH_EDGE_MISSING_Y = 0.97
# Reverse direction (TABLE_CAM -> HIGH_CAM): trigger when wrist goes high then disappears.
AUTO_SWITCH_TOP_UD_PCT = 70.0
AUTO_SWITCH_TOP_MEMORY_S = 2.2
AUTO_SWITCH_TOP_FALLBACK_UD_PCT = 65.0
# Treat wrist detections at extreme top edge as effectively missing.
AUTO_SWITCH_EDGE_TOP_COUNTS_AS_MISSING = True
AUTO_SWITCH_EDGE_TOP_MISSING_Y = 0.03
# Delay before auto-switch can re-arm after manually entering HIGH_CAM.
AUTO_SWITCH_REARM_S = 1.2


# Model path for Hailo pose inference (YOLOv8m-pose)
POSE_MODEL_PATH = "/usr/local/hailo/resources/models/hailo8/yolov8m_pose.hef"
# Post-processing .so for pose (must match model)
SO_PATH = "/usr/local/hailo/resources/so/libyolov8pose_postprocess.so"

# COCO keypoints mapping for pose estimation
KEYPOINTS = {
	"nose": 0,
	"left_eye": 1,
	"right_eye": 2,
	"left_ear": 3,
	"right_ear": 4,
	"left_shoulder": 5,
	"right_shoulder": 6,
	"left_elbow": 7,
	"right_elbow": 8,
	"left_wrist": 9,
	"right_wrist": 10,
	"left_hip": 11,
	"right_hip": 12,
	"left_knee": 13,
	"right_knee": 14,
	"left_ankle": 15,
	"right_ankle": 16,
	"right_hand": 10  # Alias for right wrist
}

# --- Wrist tracking servo speeds ---
# time_ms for the base-rotation servo (servo 6) when tracking.
# Lower = faster response but jerkier. 200-400 is a good range.
TRACKING_BASE_SPEED_MS = 500
# time_ms for the combined arm servos (3, 4, 5) when tracking.
# Higher value smooths the vertical sweep. 2000-5000 is a good range.
TRACKING_ARM_COMBO_MS = 5000

# --- Gripper microswitch safety (servo 1 close-stop) ---
# Physical wiring from old code: BCM GPIO 17 signal, GND return.
GRIPPER_SWITCH_PIN_BCM = 17       # Raspberry Pi BCM GPIO number
GRIPPER_SWITCH_PULL_UP = True     # Internal pull-up enabled
GRIPPER_SWITCH_PRESSED_STATE = 0  # GPIO reads LOW when microswitch is triggered
# Hard safety limit: claw can never close beyond this pulse.
GRIPPER_HARD_CLOSE_MAX = 2270
# Close motion runs in short increments so the switch can stop the gripper early.
GRIPPER_CLOSE_STEP_US = 35        # microseconds (pulse units) per close step
GRIPPER_CLOSE_STEP_TIME_MS = 70   # milliseconds per step move
# Debounce microswitch to avoid false early-trigger at different close rates.
GRIPPER_SWITCH_CONFIRM_READS = 2
GRIPPER_SWITCH_CONFIRM_INTERVAL_MS = 8
# If True, claw close also treats a stable raw GPIO state change from its
# start-of-close baseline as a stop trigger.
# Set False to avoid stopping too early from spring/lever vibration.
GRIPPER_SWITCH_TRIGGER_ON_CHANGE = False
# Optional tiny extra squeeze after confirmed switch trigger for better grip.
GRIPPER_TRIGGER_EXTRA_CLOSE_US = 15
GRIPPER_TRIGGER_EXTRA_CLOSE_TIME_MS = 120

# --- Claw cycle timing (used by GUI Claw Cycle toggle) ---
# Faster values for the repeating cycle without affecting normal open/close buttons.
CLAW_CYCLE_OPEN_TIME_MS = 300
CLAW_CYCLE_CLOSE_STEP_US = 60
CLAW_CYCLE_CLOSE_STEP_TIME_MS = 35
CLAW_CYCLE_PAUSE_S = 0.08

# --- Text-to-speech (Bluetooth speaker) ---
# Uses Piper offline TTS with UK male voice preference.
TTS_ENABLED = True
TTS_PIPER_BIN = "/home/arm/piper/piper/piper"
TTS_MODEL_CANDIDATES = [
	"/home/arm/piper/en_US-libritts_r-medium.onnx",
	"/home/arm/piper/en_GB-alan-medium.onnx",
	"/home/arm/piper/en_GB-cockney-medium.onnx",
	"/home/arm/piper/en_GB-cockney-low.onnx",
	"/home/arm/piper/en_GB-alba-medium.onnx",
	"/home/arm/piper/en_US-lessac-medium.onnx",
]
TTS_SAMPLE_RATE = 22050
TTS_ALSA_DEVICE = "pulse"
# Add short silence at start to prevent first-word clipping on Bluetooth sinks.
# 500 ms gives most BT adapters enough time to fully wake before speech starts.
TTS_BT_LEAD_IN_MS = 500

# Optional Rhubarb Lip Sync integration for animated LCD mouth visemes.
LIPSYNC_RHUBARB_ENABLED = True
LIPSYNC_RHUBARB_BIN_CANDIDATES = [
	"/usr/bin/rhubarb",
	"/usr/local/bin/rhubarb",
	"rhubarb",
]
LIPSYNC_RHUBARB_RECOGNIZER = "phonetic"
LIPSYNC_RHUBARB_TIMEOUT_S = 8.0

# --- Voice assistant (USB mic + OpenAI) ---
VOICE_ASSISTANT_ENABLED = True
# ALSA device for USB mic (TONOR TC-777 is card 2 on this Pi).
VOICE_MIC_ALSA_DEVICE = "plughw:2,0"
VOICE_MIC_SAMPLE_RATE = 16000
# Length of each recorded audio chunk sent to Whisper.
VOICE_RECORD_CHUNK_S = 4.0
# OpenAI-compatible endpoint and models (can point to local OpenWebUI).
# Leave empty to use the default OpenAI cloud endpoint.
VOICE_OPENAI_BASE_URL = "http://172.31.31.106:3000/api/v1"
VOICE_WHISPER_MODEL = "whisper-1"
VOICE_GPT_MODEL = "llama-me:latest"
# Higher temperature makes responses less robotic and less templated.
VOICE_GPT_TEMPERATURE = 0.9
# Number of conversation turns retained as context.
VOICE_MAX_CONTEXT_TURNS = 10
# VAD endpointing knobs (webrtcvad):
# mode: 0=least aggressive, 3=most aggressive speech detection.
VOICE_VAD_MODE = 2
# Frame size in ms. webrtcvad supports 10/20/30.
VOICE_VAD_FRAME_MS = 30
# Consecutive speech frames required before recording starts.
VOICE_VAD_START_FRAMES = 4
# End utterance after this much consecutive silence.
VOICE_VAD_END_SILENCE_MS = 900
# Minimum and maximum utterance duration bounds.
VOICE_MIN_UTTERANCE_MS = 450
VOICE_MAX_UTTERANCE_S = 12.0
# RMS energy threshold (0–32767).  Chunks below this level are treated as
# silence and never sent to Whisper.  Raise if background noise causes false
# triggers; lower if you have a very quiet voice.
VOICE_ENERGY_THRESHOLD = 400.0
# Seconds of additional mic suppression after TTS finishes playing.
# Prevents the room echo / BT reverb tail from being heard as a new utterance.
VOICE_POST_SPEECH_MUTE_S = 1.5
# Start muted by default so assistant does not begin listening until you unmute.
VOICE_MIC_START_MUTED = True
# System prompt for GPT.
VOICE_SYSTEM_PROMPT = (
        "You are Luna, the voice of a friendly robot companion. "
        "The human user is Mike. Always remember: your name is Luna and his name is Mike. "
        "Speak naturally and conversationally, like talking with a friend. "
        "Avoid assistant cliches such as 'How can I help you today?'. "
        "Keep responses short: 1 to 3 sentences."
)

# --- Ultrasonic auto-take ---
ULTRASONIC_TRIGGER_PIN_BCM = 23
ULTRASONIC_ECHO_PIN_BCM = 24
# Slider-only smoothing factor (0.0-1.0). Lower values are steadier.
ULTRASONIC_SLIDER_SMOOTH_ALPHA = 0.22
AUTO_TAKE_ENABLED_STARTUP = False
AUTO_TAKE_TRIGGER_DISTANCE_CM = 14.0
AUTO_TAKE_CLEAR_DISTANCE_CM = 20.0
AUTO_TAKE_STABLE_TIME_S = 0.25
AUTO_TAKE_POLL_INTERVAL_S = 0.08
AUTO_TAKE_REQUIRE_HAND_VISIBLE = True
AUTO_TAKE_HAND_MAX_AGE_S = 0.9
AUTO_TAKE_DISPLAY_TEXT = "TAKE"
AUTO_TAKE_DISPLAY_TIME_S = 1.0
AUTO_TAKE_OPEN_TIME_MS = 250
AUTO_TAKE_EXTEND_TIME_MS = 900
AUTO_TAKE_SETTLE_TIME_S = 0.6
AUTO_TAKE_RETURN_TIME_MS = 1200
AUTO_TAKE_EXTEND_POSITIONS = {
	3: 1827,
	4: 1568,
	5: 1878,
}

# --- Gesture-triggered events (true fist via MediaPipe Hands) ---
# Gesture model: MediaPipe Hands (finger landmarks) for true fist detection.
GESTURE_EVENTS_ENABLED = True
GESTURE_HAND_MAX_AGE_S = 0.40
GESTURE_CENTER_X = 0.50
GESTURE_CENTER_Y = 0.50
GESTURE_CENTER_X_TOL = 0.10
GESTURE_CENTER_Y_TOL = 0.12
GESTURE_CENTER_HOLD_S = 1.25
GESTURE_LOCK_TOP_CAM_S = 20.0
GESTURE_DONE_OVERLAY_S = 10.0
GESTURE_DONE_TEXT = "done"
GESTURE_USE_MEDIAPIPE_HANDS = True
# MediaPipe Hands: 1 is the higher-accuracy model (0 is faster/lower accuracy).
GESTURE_MP_MODEL_COMPLEXITY = 1
GESTURE_MP_MIN_DET_CONF = 0.55
GESTURE_MP_MIN_TRACK_CONF = 0.55
GESTURE_MP_FLIP_HANDEDNESS = True
GESTURE_PROCESS_INTERVAL_S = 0.14
GESTURE_MP_INPUT_MAX_DIM = 320
GESTURE_SCORE_THRESHOLD = 8.5
GESTURE_HISTORY_SIZE = 5
GESTURE_STABLE_MIN_COUNT = 3
# Optional safety fallback if hand-landmark detector is unavailable.
GESTURE_ALLOW_WRIST_PROXY_FALLBACK = True
GESTURE_COMMAND_ONE_MAX_AGE_S = 0.45
GESTURE_COMMAND_TWO_MAX_AGE_S = 0.45

# --- Crestron TCP server ---
CRESTRON_SERVER_ENABLED = True
CRESTRON_SERVER_HOST = "0.0.0.0"
CRESTRON_PORT = 50005
CRESTRON_ACCEPT_TIMEOUT_S = 1.0
CRESTRON_CLIENT_TIMEOUT_S = 1.0
CRESTRON_MAX_LINE_BYTES = 2048

# --- AI API Tools (poses, faces, and gripper) ---
# Enable AI tools to allow voice assistant to call poses, change faces, and control gripper.
# This allows GPT to interact with robot arm movements and expressions.
VOICE_AI_TOOLS_ENABLED = False  # Keep off for normal conversation; enable only when you want robot actions

# ---------------------------------------------------------------------------
# Table object detection (table cam, OpenCV HSV colour range)
# ---------------------------------------------------------------------------
# Hue range 0-179 (OpenCV uses half-angle). Default: orange/yellow objects.
TABLE_DETECT_H_LO = 10
TABLE_DETECT_H_HI = 35
TABLE_DETECT_S_LO = 80    # Saturation lower bound (0-255)
TABLE_DETECT_S_HI = 255
TABLE_DETECT_V_LO = 60    # Value (brightness) lower bound (0-255)
TABLE_DETECT_V_HI = 255
# Set True for hues that wrap around 0 (e.g. red: spans ~0-10 AND ~170-179).
TABLE_DETECT_HUE_WRAP = False
# Minimum contour area as a fraction of total frame area to count as an object.
TABLE_DETECT_MIN_AREA_FRAC = 0.001
# Consecutive frames with a valid detection required before marking as "stable".
TABLE_DETECT_STABLE_FRAMES = 4
# Maximum age (seconds) of a detection before smart_table_take treats it as lost.
TABLE_DETECT_MAX_AGE_S = 0.5

# ---------------------------------------------------------------------------
# Smart table take (brain.smart_table_take)
# ---------------------------------------------------------------------------
# Servo 6 (base) mapping: object at left edge of frame (x_norm=0) → this pos,
# object at right edge (x_norm=1) → right pos.
SMART_TAKE_BASE_LEFT  = 2000   # servo 6 pulse when object is at frame left
SMART_TAKE_BASE_RIGHT = 1000   # servo 6 pulse when object is at frame right
# Invert X so table-cam left/right matches physical arm direction.
SMART_TAKE_BASE_X_INVERT = False

# How long to allow before aborting if no stable object is detected.
SMART_TAKE_DETECT_TIMEOUT_S = 5.0
# Time (ms) for the base-alignment move.
SMART_TAKE_BASE_ALIGN_MS = 700
# Time (ms) to wait after base move for mechanical settling.
SMART_TAKE_BASE_SETTLE_MS = 300

# Pre-grasp arm position (arm raised, claw open, positioned over table).
SMART_TAKE_PRE_GRASP_MS = 1200   # time_ms for the move
SMART_TAKE_PRE_GRASP_S3 = 2079
SMART_TAKE_PRE_GRASP_S4 = 2170
SMART_TAKE_PRE_GRASP_S5 = 1840

# Descent: servo 5 moves from pre-grasp value toward the table in increments.
SMART_TAKE_DESCENT_STEP_US = 25    # servo 5 pulse units per step
SMART_TAKE_DESCENT_STEP_MS = 280   # time_ms per step
SMART_TAKE_DESCENT_MAX_STEPS = 20  # abort if still not close after this many steps
# Ultrasonic distance (cm) at which descent stops and gripper closes.
# HC-SR04 min reliable range ~2 cm; auto-take triggers at 14 cm — 10 cm is a safe stop.
SMART_TAKE_CLOSE_DISTANCE_CM = 10.0
# Ultrasonic distance (cm) below which we consider the object still held (lift verify).
SMART_TAKE_HELD_DISTANCE_CM = 8.0
# Time (ms) for the lift move back to home after gripping.
SMART_TAKE_LIFT_MS = 1700
# Hard upper limit for servo 5 during descent (pulse units).
# table-take successfully grips at 1937; 2060 gives extra headroom.
SMART_TAKE_DESCENT_S5_MAX = 2060
# Minimum detection area (frame fraction) required to treat a candidate as pickable.
SMART_TAKE_MIN_AREA_NORM = 0.002
# If False, Smart Take will not auto-open the claw after a miss verdict.
SMART_TAKE_AUTO_OPEN_ON_MISS = False

# Smart-take claw close tuning.
SMART_TAKE_GRIP_STEP_US = 45
SMART_TAKE_GRIP_STEP_TIME_MS = 70
SMART_TAKE_GRIP_SWITCH_CONFIRM_READS = 2
SMART_TAKE_GRIP_SWITCH_CONFIRM_INTERVAL_MS = 8
SMART_TAKE_GRIP_TRIGGER_EXTRA_CLOSE_US = 28
SMART_TAKE_GRIP_TRIGGER_EXTRA_CLOSE_TIME_MS = 140
