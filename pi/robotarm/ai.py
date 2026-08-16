# --- Hybrid: Modular entry point for main.py ---

import glob as _glob
import json
import os
import platform
import queue
import threading
import time

import table_detect

# Match working od.py behavior: set device type before Hailo/Gst initialization.
os.environ.setdefault("hailort_device_type", "hailo8")

# Ensure Hailo GStreamer plugin directories are discoverable before Gst.init().
_arch = platform.machine()
_gst_candidates = [
    f"/lib/{_arch}-linux-gnu/gstreamer-1.0",
    f"/usr/lib/{_arch}-linux-gnu/gstreamer-1.0",
]
_hailo_so_patterns = (
    f"/lib/{_arch}-linux-gnu/libgsthailotools.so",
    f"/lib/{_arch}-linux-gnu/*/libgsthailotools.so",
    f"/lib/{_arch}-linux-gnu/*/*/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/*/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/*/*/libgsthailotools.so",
    f"/lib/{_arch}-linux-gnu/libgsthailo.so",
    f"/lib/{_arch}-linux-gnu/*/libgsthailo.so",
    f"/lib/{_arch}-linux-gnu/*/*/libgsthailo.so",
    f"/usr/lib/{_arch}-linux-gnu/libgsthailo.so",
    f"/usr/lib/{_arch}-linux-gnu/*/libgsthailo.so",
    f"/usr/lib/{_arch}-linux-gnu/*/*/libgsthailo.so",
)
for _so in (_hit for _pat in _hailo_so_patterns for _hit in _glob.glob(_pat)):
    _so_dir = os.path.dirname(_so)
    if _so_dir not in _gst_candidates:
        _gst_candidates.append(_so_dir)

_gst_path = os.environ.get("GST_PLUGIN_PATH", "")
_gst_dirs = set(filter(None, _gst_path.split(":"))) if _gst_path else set()
_new_gst_dirs = [d for d in _gst_candidates if d not in _gst_dirs and os.path.isdir(d)]
if _new_gst_dirs:
    _prefix = ":".join(_new_gst_dirs)
    os.environ["GST_PLUGIN_PATH"] = f"{_prefix}:{_gst_path}" if _gst_path else _prefix

_app_instance = None
_frame_queue = queue.Queue(maxsize=2)
_gst_thread = None
_gst_loop = None
_gst_pipeline = None
_camera_stop_event = threading.Event()
_display_stop_event = threading.Event()
_active_mode = None
_pipeline_lock = threading.Lock()
_preview_enabled = True
_VIDEO_WINDOW_STATE_PATH = os.path.join(os.path.dirname(__file__), "video_window_state.json")


def _load_video_window_state():
    try:
        with open(_VIDEO_WINDOW_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_video_window_state(x, y, w, h):
    data = {
        "x": int(x),
        "y": int(y),
        "w": int(w),
        "h": int(h),
    }
    try:
        with open(_VIDEO_WINDOW_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[AI] Failed saving video window state: {e}")

import config


def _build_camera_pipeline():
    # Full AI overlay pipeline for high cam
    cam_name = "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a"
    width = getattr(config, "CAM_SENSOR_W", 1536)
    height = getattr(config, "CAM_SENSOR_H", 864)
    pipeline = (
        f'libcamerasrc camera-name="{cam_name}" ! '
        f'video/x-raw,format=NV12,width={width},height={height} ! '
        f'videoconvert ! tee name=t '
        f't. ! queue leaky=downstream max-size-buffers=1 ! '
        f'videoconvert ! videoscale ! video/x-raw,format=RGB,width={width},height={height} ! '
        f'appsink name=finger_sink emit-signals=false sync=false drop=true max-buffers=1 '
        f't. ! queue leaky=downstream max-size-buffers=10 ! '
        f'videoscale ! video/x-raw,width=640,height=640 ! '
        f'hailonet hef-path=/usr/local/hailo/resources/models/hailo8/yolov8m_pose.hef force-writable=true ! '
        f'hailofilter name=pose_filter so-path=/usr/local/hailo/resources/so/libyolov8pose_postprocess.so ! '
        f'hailotracker ! hailooverlay ! '
        f'videoconvert ! videoscale ! video/x-raw,format=RGB,width={width},height={height} ! '
        f'videoconvert ! appsink name=preview_sink emit-signals=false sync=false drop=true max-buffers=1'
    )
    return pipeline


def _graceful_stop_pipeline(pipe, Gst):
    if pipe is None:
        return
    try:
        pipe.send_event(Gst.Event.new_eos())
        bus = pipe.get_bus()
        if bus is not None:
            bus.timed_pop_filtered(
                int(0.25 * Gst.SECOND),
                Gst.MessageType.EOS | Gst.MessageType.ERROR,
            )
    except Exception:
        pass
    try:
        pipe.set_state(Gst.State.NULL)
        pipe.get_state(int(0.75 * Gst.SECOND))
    except Exception:
        pass


def _drain_frame_queue():
    while True:
        try:
            _frame_queue.get_nowait()
        except queue.Empty:
            break


def request_display_stop():
    """Request the preview window loop to stop cleanly."""
    _display_stop_event.set()
    try:
        _frame_queue.put_nowait(None)
    except queue.Full:
        pass


def clear_display_stop():
    """Allow the preview window loop to run again."""
    _display_stop_event.clear()


def set_preview_enabled(enabled: bool):
    """Toggle preview rendering while keeping camera/tracking running."""
    global _preview_enabled
    _preview_enabled = bool(enabled)
    if not _preview_enabled:
        try:
            cv2.destroyWindow("Camera Preview")
        except Exception:
            pass


def _enqueue_frame_from_sample(sample, Gst):
    if sample is None:
        return
    try:
        buf = sample.get_buffer()
        caps = sample.get_caps()
        struct = caps.get_structure(0)
        w = int(struct.get_value("width"))
        h = int(struct.get_value("height"))
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return
        try:
            frame_rgb = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            try:
                _frame_queue.put_nowait(frame_bgr)
            except queue.Full:
                pass
        finally:
            buf.unmap(mapinfo)
    except Exception as e:
        print(f"[AI] Preview sample decode error: {e}")


def _run_camera_pipeline(mode, pipeline_str, preview_sink_name="preview_sink"):
    global _gst_pipeline, _active_mode, _gst_loop
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst

    Gst.init(None)
    _active_mode = mode
    print(f"[AI] Launching pipeline ({mode}): {pipeline_str}")
    pipeline = Gst.parse_launch(pipeline_str)
    _gst_pipeline = pipeline

    # Attach pose callback on AI-enabled camera pipelines.
    if mode in ("HIGH_CAM", "TABLE_CAM"):
        hailofilter = pipeline.get_by_name("pose_filter")
        if hailofilter is not None:
            try:
                hailofilter.get_static_pad("src").add_probe(
                    Gst.PadProbeType.BUFFER, app_callback, None
                )
            except Exception as e:
                print(f"[AI] Could not attach hailofilter probe: {e}")
        else:
            print("[AI] pose_filter not found; wrist slider updates will be unavailable")

    preview_sink = pipeline.get_by_name(preview_sink_name)
    if preview_sink is None:
        print(f"[AI] Preview sink '{preview_sink_name}' not found for mode {mode}")

    pipeline.set_state(Gst.State.PLAYING)
    try:
        while not _camera_stop_event.is_set():
            if preview_sink is None:
                break
            sample = preview_sink.emit("try-pull-sample", int(0.05 * Gst.SECOND))
            if sample is not None:
                _enqueue_frame_from_sample(sample, Gst)
    except Exception as e:
        print(f"[AI] Camera worker error ({mode}): {e}")
    finally:
        _graceful_stop_pipeline(pipeline, Gst)
        _gst_pipeline = None
        _active_mode = None
        _gst_loop = None


def _stop_camera_locked(join_timeout=8.0):
    global _gst_thread, _gst_loop, _gst_pipeline
    _camera_stop_event.set()

    if _gst_loop:
        try:
            _gst_loop.quit()
        except Exception:
            pass
        _gst_loop = None
    if _gst_thread:
        try:
            _gst_thread.join(timeout=join_timeout)
        except Exception as e:
            print(f"[AI] Error joining GStreamer thread: {e}")
        if _gst_thread.is_alive():
            # Last resort: request immediate NULL-state transition if available,
            # then wait briefly again before aborting restart.
            if _gst_pipeline is not None:
                try:
                    _graceful_stop_pipeline(_gst_pipeline, Gst)
                except Exception as e:
                    print(f"[AI] Last-resort pipeline stop warning: {e}")
            try:
                _gst_thread.join(timeout=2.0)
            except Exception:
                pass
            if _gst_thread.is_alive():
                print("[AI] Camera thread did not stop in time; aborting restart to avoid device collision")
                return False
        _gst_thread = None
    _gst_pipeline = None

    # Give lower layers (libcamera/HailoRT) time to fully release handles.
    time.sleep(0.35)
    return True

def start_high_cam():
    global _gst_thread
    def run_gst():
        pipeline_str = _build_camera_pipeline()
        _run_camera_pipeline("HIGH_CAM", pipeline_str, "preview_sink")
    with _pipeline_lock:
        if not _stop_camera_locked():
            return False
        _drain_frame_queue()
        _camera_stop_event.clear()
        # Extra cooldown before recreating Hailo pipeline.
        time.sleep(0.75)
        _gst_thread = threading.Thread(target=run_gst, daemon=True)
        _gst_thread.start()
    return True


def _build_table_camera_pipeline():
    cam_name = "/base/axi/pcie@1000120000/rp1/i2c@88000/imx708@1a"
    width = getattr(config, "CAM_SENSOR_W", 1536)
    height = getattr(config, "CAM_SENSOR_H", 864)
    pipeline = (
        f'libcamerasrc camera-name="{cam_name}" ! '
        f'video/x-raw,format=NV12,width={width},height={height} ! '
        f'videoconvert ! tee name=t '
        f't. ! queue leaky=downstream max-size-buffers=1 ! '
        f'videoconvert ! videoscale ! video/x-raw,format=RGB,width={width},height={height} ! '
        f'appsink name=finger_sink emit-signals=false sync=false drop=true max-buffers=1 '
        f't. ! queue leaky=downstream max-size-buffers=10 ! '
        f'videoscale ! video/x-raw,width=640,height=640 ! '
        f'hailonet hef-path=/usr/local/hailo/resources/models/hailo8/yolov8m_pose.hef force-writable=true ! '
        f'hailofilter name=pose_filter so-path=/usr/local/hailo/resources/so/libyolov8pose_postprocess.so ! '
        f'hailotracker ! hailooverlay ! '
        f'videoconvert ! videoscale ! video/x-raw,format=RGB,width={width},height={height} ! '
        f'videoconvert ! appsink name=table_preview_sink emit-signals=false sync=false drop=true max-buffers=1'
    )
    return pipeline


def start_table_cam():
    global _gst_thread
    def run_gst():
        pipeline_str = _build_table_camera_pipeline()
        _run_camera_pipeline("TABLE_CAM", pipeline_str, "table_preview_sink")
    with _pipeline_lock:
        if not _stop_camera_locked():
            return False
        _drain_frame_queue()
        _camera_stop_event.clear()
        time.sleep(0.1)
        _gst_thread = threading.Thread(target=run_gst, daemon=True)
        _gst_thread.start()
    return True

def stop_high_cam():
    with _pipeline_lock:
        return _stop_camera_locked()




# ai.py - Hailo Pose Estimation Integration (Official Pipeline)
import os
import cv2
import numpy as np
import hailo
from collections import deque
import config
import brain as robot_brain
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
from hailo_apps.hailo_app_python.core.gstreamer.gstreamer_app import app_callback_class
from hailo_apps.hailo_app_python.apps.pose_estimation.pose_estimation_pipeline import GStreamerPoseEstimationApp

os.environ.setdefault("GLOG_minloglevel", "2")
try:
    import mediapipe as mp
except Exception:
    mp = None

Gst.init(None)

COCO_SKELETON = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 11), (6, 12),
    (11, 12), (11, 13), (13, 15),
    (12, 14), (14, 16),
    (5, 7), (7, 9),
    (6, 8), (8, 10),
]
RIGHT_HAND_IDX = config.KEYPOINTS.get("right_hand", 10)
_tracking_lock = threading.Lock()
_tracking_lr_gain = 1.0
_tracking_ud_gain = 1.0
_tracking_servo_center = 1500
_tracking_servo_span = 350
_tracking_servo_min = 900
_tracking_servo_max = 2100
_tracking_servo_speed = int(getattr(config, "TRACKING_BASE_SPEED_MS", 300))
_tracking_send_period_s = 0.30
_tracking_combo_time_ms = int(getattr(config, "TRACKING_ARM_COMBO_MS", 3600))
_tracking_x_invert = bool(getattr(config, "TRACKING_X_INVERT", False))
_tracking_x_center_norm = float(getattr(config, "TRACKING_X_CENTER_NORM", 0.5))
_tracking_x_center_deadband = float(getattr(config, "TRACKING_X_CENTER_DEADBAND", 0.03))
_last_tracking_send_t = 0.0
_last_base_pos = None
_last_arm_combo = None
_last_right_wrist_norm = (0.5, 0.5)
_last_right_wrist_raw_norm = (0.5, 0.5)
_last_right_wrist_seen_t = 0.0
_last_ud_pct = 50.0
_last_low_ud_seen_t = 0.0
_last_top_ud_seen_t = 0.0
_auto_switch_requested_mode = None
_auto_switch_enabled = bool(getattr(config, "AUTO_SWITCH_TO_TABLE_ENABLED", True))
_auto_switch_disappear_s = float(getattr(config, "AUTO_SWITCH_DISAPPEAR_S", 0.40))
_auto_switch_low_ud_pct = float(getattr(config, "AUTO_SWITCH_LOW_UD_PCT", 15.0))
_auto_switch_low_memory_s = float(getattr(config, "AUTO_SWITCH_LOW_MEMORY_S", 2.2))
_auto_switch_low_fallback_ud_pct = float(getattr(config, "AUTO_SWITCH_LOW_FALLBACK_UD_PCT", 20.0))
_auto_switch_edge_missing_enabled = bool(getattr(config, "AUTO_SWITCH_EDGE_COUNTS_AS_MISSING", True))
_auto_switch_edge_missing_y = float(getattr(config, "AUTO_SWITCH_EDGE_MISSING_Y", 0.97))
_auto_switch_top_ud_pct = float(getattr(config, "AUTO_SWITCH_TOP_UD_PCT", 85.0))
_auto_switch_top_memory_s = float(getattr(config, "AUTO_SWITCH_TOP_MEMORY_S", 2.2))
_auto_switch_top_fallback_ud_pct = float(getattr(config, "AUTO_SWITCH_TOP_FALLBACK_UD_PCT", 80.0))
_auto_switch_edge_top_missing_enabled = bool(getattr(config, "AUTO_SWITCH_EDGE_TOP_COUNTS_AS_MISSING", True))
_auto_switch_edge_top_missing_y = float(getattr(config, "AUTO_SWITCH_EDGE_TOP_MISSING_Y", 0.03))
_auto_switch_rearm_s = float(getattr(config, "AUTO_SWITCH_REARM_S", 1.2))
_auto_switch_rearm_block_until = 0.0
_brain = robot_brain.RobotBrain()
_mp_hands = None
_mp_hands_init_started = False
_hand_gesture_engine = "none"
_last_hand_gesture_state = "NONE"
_last_hand_gesture_seen_t = 0.0
_last_fist_seen_t = 0.0
_last_fist_centered_seen_t = 0.0
_last_fist_center_norm = (0.5, 0.5)
_last_one_seen_t = 0.0
_last_two_seen_t = 0.0
_last_hand_process_t = 0.0
_last_hand_gesture_score = 0.0
_gesture_window_size = max(3, int(getattr(config, "GESTURE_HISTORY_SIZE", 5)))
_gesture_history = deque(maxlen=_gesture_window_size)

class UserAppCallback(app_callback_class):
    pass


def set_tracking_lr_scale(value):
    global _tracking_lr_gain
    with _tracking_lock:
        _tracking_lr_gain = max(0.0, min(2.0, float(value)))


def set_tracking_ud_scale(value):
    global _tracking_ud_gain
    with _tracking_lock:
        _tracking_ud_gain = max(0.0, min(2.0, float(value)))


def get_tracking_scales():
    with _tracking_lock:
        return _tracking_lr_gain, _tracking_ud_gain


def get_right_wrist_norm():
    with _tracking_lock:
        return _last_right_wrist_norm, _last_right_wrist_seen_t


def get_closed_fist_centered_recent(max_age_s: float = 0.40):
    now = time.time()
    with _tracking_lock:
        seen_t = float(_last_fist_centered_seen_t)
        centered_recent = (now - seen_t) <= max(0.05, float(max_age_s))
        return bool(centered_recent), seen_t, tuple(_last_fist_center_norm)


def get_one_finger_recent(max_age_s: float = 0.50):
    now = time.time()
    with _tracking_lock:
        seen_t = float(_last_one_seen_t)
        recent = (now - seen_t) <= max(0.05, float(max_age_s))
        return bool(recent), seen_t


def get_two_finger_recent(max_age_s: float = 0.50):
    now = time.time()
    with _tracking_lock:
        seen_t = float(_last_two_seen_t)
        recent = (now - seen_t) <= max(0.05, float(max_age_s))
        return bool(recent), seen_t


def get_hand_gesture_status() -> dict:
    now = time.time()
    with _tracking_lock:
        return {
            "engine": str(_hand_gesture_engine),
            "state": str(_last_hand_gesture_state),
            "last_seen_s": max(0.0, now - float(_last_hand_gesture_seen_t)),
            "last_fist_s": max(0.0, now - float(_last_fist_seen_t)),
            "last_fist_centered_s": max(0.0, now - float(_last_fist_centered_seen_t)),
            "last_one_s": max(0.0, now - float(_last_one_seen_t)),
            "last_two_s": max(0.0, now - float(_last_two_seen_t)),
            "score": float(_last_hand_gesture_score),
            "history_size": int(_gesture_window_size),
            "fist_center": tuple(_last_fist_center_norm),
            "mediapipe_available": bool(mp is not None),
            "model_complexity": int(getattr(config, "GESTURE_MP_MODEL_COMPLEXITY", 1)),
        }


def _sync_gesture_history_config():
    global _gesture_window_size, _gesture_history
    size = max(3, int(getattr(config, "GESTURE_HISTORY_SIZE", 5)))
    if size == _gesture_window_size:
        return
    _gesture_window_size = size
    _gesture_history = deque(list(_gesture_history), maxlen=size)


def _score_expected(actual: dict, expected: dict) -> float:
    """Return a 0..10 match score for expected finger up/down states.
    expected value: True/False/None (None = don't care)."""
    score = 0.0
    weight = 0.0
    for finger, exp in expected.items():
        if exp is None:
            continue
        weight += 1.0
        if bool(actual.get(finger, False)) == bool(exp):
            score += 1.0
    if weight <= 0.0:
        return 0.0
    return (score / weight) * 10.0


def _init_hands_detector_worker():
    global _mp_hands, _hand_gesture_engine
    try:
        _mp_hands = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=1,
            model_complexity=max(0, min(1, int(getattr(config, "GESTURE_MP_MODEL_COMPLEXITY", 1)))),
            min_detection_confidence=float(getattr(config, "GESTURE_MP_MIN_DET_CONF", 0.55)),
            min_tracking_confidence=float(getattr(config, "GESTURE_MP_MIN_TRACK_CONF", 0.55)),
        )
        _hand_gesture_engine = "mediapipe_hands"
        print("[AI] MediaPipe Hands initialized")
    except Exception as e:
        print(f"[AI] MediaPipe Hands unavailable: {e}")
        _mp_hands = None
        _hand_gesture_engine = "error"


def _get_hands_detector():
    global _mp_hands, _hand_gesture_engine, _mp_hands_init_started
    if not bool(getattr(config, "GESTURE_USE_MEDIAPIPE_HANDS", True)):
        _hand_gesture_engine = "disabled"
        return None
    if _mp_hands is not None:
        return _mp_hands
    if mp is None:
        _hand_gesture_engine = "unavailable"
        return None
    if not _mp_hands_init_started:
        _mp_hands_init_started = True
        _hand_gesture_engine = "warming"
        threading.Thread(target=_init_hands_detector_worker, daemon=True).start()
    return None


def _classify_finger_count_gesture(hand_landmarks, handedness_label=None):
    lm = hand_landmarks.landmark
    y_margin = 0.02
    thumb_x_margin = 0.02

    index_up = lm[8].y < (lm[6].y - y_margin)
    middle_up = lm[12].y < (lm[10].y - y_margin)
    ring_up = lm[16].y < (lm[14].y - y_margin)
    pinky_up = lm[20].y < (lm[18].y - y_margin)

    handed = str(handedness_label or "").strip().lower()
    if handed == "right":
        thumb_up = lm[4].x < (lm[3].x - thumb_x_margin)
    elif handed == "left":
        thumb_up = lm[4].x > (lm[3].x + thumb_x_margin)
    else:
        thumb_up = abs(lm[4].x - lm[3].x) > thumb_x_margin

    actual = {
        "thumb": bool(thumb_up),
        "index": bool(index_up),
        "middle": bool(middle_up),
        "ring": bool(ring_up),
        "pinky": bool(pinky_up),
    }

    expected_map = {
        "FIST": {"thumb": False, "index": False, "middle": False, "ring": False, "pinky": False},
        "ONE": {"thumb": None, "index": True, "middle": False, "ring": False, "pinky": False},
        "TWO": {"thumb": None, "index": True, "middle": True, "ring": False, "pinky": False},
        "THREE": {"thumb": None, "index": True, "middle": True, "ring": True, "pinky": False},
        "FOUR": {"thumb": False, "index": True, "middle": True, "ring": True, "pinky": True},
        "FIVE": {"thumb": True, "index": True, "middle": True, "ring": True, "pinky": True},
    }

    best_name = "NONE"
    best_score = 0.0
    for name, expected in expected_map.items():
        score = _score_expected(actual, expected)
        if score > best_score:
            best_score = score
            best_name = name

    threshold = float(getattr(config, "GESTURE_SCORE_THRESHOLD", 8.5))
    if best_score < threshold:
        return "NONE", float(best_score)
    return str(best_name), float(best_score)


def _update_hand_gesture_state_from_frame(frame_rgb, now):
    global _last_hand_process_t
    global _last_hand_gesture_state, _last_hand_gesture_seen_t, _last_hand_gesture_score
    global _last_fist_seen_t, _last_fist_centered_seen_t, _last_fist_center_norm, _last_one_seen_t, _last_two_seen_t

    if _active_mode != "HIGH_CAM":
        return

    if not bool(getattr(config, "GESTURE_EVENTS_ENABLED", True)):
        return

    min_interval = max(0.05, float(getattr(config, "GESTURE_PROCESS_INTERVAL_S", 0.14)))
    with _tracking_lock:
        _sync_gesture_history_config()
        if (now - _last_hand_process_t) < min_interval:
            return
        _last_hand_process_t = now

    hands = _get_hands_detector()
    if hands is None:
        return

    # Keep callback light: run hand inference on a downscaled frame.
    try:
        h, w = frame_rgb.shape[:2]
        max_dim = max(160, int(getattr(config, "GESTURE_MP_INPUT_MAX_DIM", 320)))
        largest = max(h, w)
        if largest > max_dim:
            scale = float(max_dim) / float(largest)
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            hand_frame = cv2.resize(frame_rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
        else:
            hand_frame = frame_rgb
    except Exception:
        hand_frame = frame_rgb

    try:
        result = hands.process(hand_frame)
    except Exception:
        return

    if not result or not result.multi_hand_landmarks:
        with _tracking_lock:
            _gesture_history.append("NONE")
            _last_hand_gesture_state = "NONE"
            _last_hand_gesture_score = 0.0
        return

    handedness_label = None
    if getattr(result, "multi_handedness", None):
        try:
            raw_label = result.multi_handedness[0].classification[0].label
            if bool(getattr(config, "GESTURE_MP_FLIP_HANDEDNESS", True)):
                if raw_label == "Right":
                    handedness_label = "Left"
                elif raw_label == "Left":
                    handedness_label = "Right"
                else:
                    handedness_label = raw_label
            else:
                handedness_label = raw_label
        except Exception:
            handedness_label = None

    hand = result.multi_hand_landmarks[0]
    state, score = _classify_finger_count_gesture(hand, handedness_label)

    wrist = hand.landmark[0]
    cx = float(getattr(config, "GESTURE_CENTER_X", 0.50))
    cy = float(getattr(config, "GESTURE_CENTER_Y", 0.50))
    tx = max(0.01, float(getattr(config, "GESTURE_CENTER_X_TOL", 0.10)))
    ty = max(0.01, float(getattr(config, "GESTURE_CENTER_Y_TOL", 0.12)))
    wx = float(wrist.x)
    wy = float(wrist.y)
    centered = (abs(wx - cx) <= tx) and (abs(wy - cy) <= ty)

    with _tracking_lock:
        _gesture_history.append(str(state))
        min_count = max(2, int(getattr(config, "GESTURE_STABLE_MIN_COUNT", 3)))
        stable_state = "NONE"
        counts = {}
        for s in _gesture_history:
            counts[s] = counts.get(s, 0) + 1
        for s, c in counts.items():
            if s != "NONE" and c >= min_count:
                if stable_state == "NONE" or c > counts.get(stable_state, 0):
                    stable_state = s

        _last_hand_gesture_state = str(stable_state)
        _last_hand_gesture_score = float(score)
        if stable_state != "NONE":
            _last_hand_gesture_seen_t = now
        _last_fist_center_norm = (wx, wy)
        if stable_state == "ONE":
            _last_one_seen_t = now
        if stable_state == "TWO":
            _last_two_seen_t = now
        if stable_state == "FIST":
            _last_fist_seen_t = now
            if centered:
                _last_fist_centered_seen_t = now


def consume_auto_switch_request() -> str | None:
    global _auto_switch_requested_mode
    with _tracking_lock:
        mode = _auto_switch_requested_mode
        _auto_switch_requested_mode = None
        return mode


def consume_auto_switch_to_table_request() -> bool:
    # Backward-compatible helper used by older callers.
    return consume_auto_switch_request() == "TABLE_CAM"


def reset_auto_switch_state_for_mode(mode: str):
    """Reset/arm auto-switch state when camera mode changes.
    Prevents stale low/disappear state from immediately retriggering."""
    global _auto_switch_requested_mode, _auto_switch_rearm_block_until
    global _last_right_wrist_seen_t, _last_low_ud_seen_t, _last_top_ud_seen_t, _last_ud_pct
    now = time.time()
    with _tracking_lock:
        _auto_switch_requested_mode = None
        _last_right_wrist_seen_t = now
        _last_low_ud_seen_t = 0.0
        _last_top_ud_seen_t = 0.0
        _last_ud_pct = 50.0
        _auto_switch_rearm_block_until = now + max(0.0, _auto_switch_rearm_s)


def _compute_auto_switch_target(now: float) -> str | None:
    if not _auto_switch_enabled:
        return None
    if not robot_brain.is_wrist_tracking_enabled():
        return None
    if now < _auto_switch_rearm_block_until:
        return None

    mode = str(_active_mode)
    wrist_disappeared = (now - _last_right_wrist_seen_t) >= _auto_switch_disappear_s
    if not wrist_disappeared:
        return None

    if mode == "HIGH_CAM":
        was_low_recently = (now - _last_low_ud_seen_t) <= _auto_switch_low_memory_s
        fallback_low_met = _last_ud_pct <= _auto_switch_low_fallback_ud_pct
        return "TABLE_CAM" if (was_low_recently or fallback_low_met) else None

    if mode == "TABLE_CAM":
        was_top_recently = (now - _last_top_ud_seen_t) <= _auto_switch_top_memory_s
        fallback_top_met = _last_ud_pct >= _auto_switch_top_fallback_ud_pct
        return "HIGH_CAM" if (was_top_recently or fallback_top_met) else None

    return None


def arm_auto_switch_if_needed() -> bool:
    """Set auto-switch request when low-wrist+disappear conditions are met.
    This is safe to call from GUI polling as a fallback if callback timing is uneven.
    Returns True when request is (or already was) armed."""
    global _auto_switch_requested_mode
    now = time.time()
    with _tracking_lock:
        target_mode = _compute_auto_switch_target(now)
        if target_mode and (_auto_switch_requested_mode is None):
            _auto_switch_requested_mode = target_mode
        return _auto_switch_requested_mode is not None


def get_auto_switch_status() -> dict:
    """Return a snapshot of each auto-switch prerequisite for GUI diagnostics."""
    now = time.time()
    with _tracking_lock:
        tracking_enabled = robot_brain.is_wrist_tracking_enabled()
        high_cam_active = (_active_mode == "HIGH_CAM")
        table_cam_active = (_active_mode == "TABLE_CAM")
        wrist_missing_s = max(0.0, now - _last_right_wrist_seen_t)
        wrist_disappeared = wrist_missing_s >= _auto_switch_disappear_s
        low_recent_s = max(0.0, now - _last_low_ud_seen_t)
        was_low_recently = low_recent_s <= _auto_switch_low_memory_s
        fallback_low_met = _last_ud_pct <= _auto_switch_low_fallback_ud_pct
        top_recent_s = max(0.0, now - _last_top_ud_seen_t)
        was_top_recently = top_recent_s <= _auto_switch_top_memory_s
        fallback_top_met = _last_ud_pct >= _auto_switch_top_fallback_ud_pct
        mode_is_high = high_cam_active
        mode_is_table = table_cam_active
        edge_ok = (was_low_recently or fallback_low_met) if mode_is_high else (was_top_recently or fallback_top_met)
        not_already_requested = _auto_switch_requested_mode is None
        rearm_blocked = now < _auto_switch_rearm_block_until
        computed_target = _compute_auto_switch_target(now)
        should_switch = (
            _auto_switch_enabled
            and (mode_is_high or mode_is_table)
            and tracking_enabled
            and (not rearm_blocked)
            and wrist_disappeared
            and edge_ok
            and not_already_requested
            and (computed_target is not None)
        )
        return {
            "enabled": bool(_auto_switch_enabled),
            "high_cam_active": bool(high_cam_active),
            "table_cam_active": bool(table_cam_active),
            "tracking_enabled": bool(tracking_enabled),
            "rearm_blocked": bool(rearm_blocked),
            "wrist_disappeared": bool(wrist_disappeared),
            "was_low_recently": bool(was_low_recently),
            "fallback_low_met": bool(fallback_low_met),
            "was_top_recently": bool(was_top_recently),
            "fallback_top_met": bool(fallback_top_met),
            "low_ok": bool(edge_ok),
            "not_already_requested": bool(not_already_requested),
            "should_switch": bool(should_switch),
            "request_armed": bool(_auto_switch_requested_mode is not None),
            "requested_mode": _auto_switch_requested_mode,
            "computed_target": computed_target,
            "last_ud_pct": float(_last_ud_pct),
            "wrist_missing_s": float(wrist_missing_s),
            "low_recent_s": float(low_recent_s),
            "top_recent_s": float(top_recent_s),
            "disappear_threshold_s": float(_auto_switch_disappear_s),
            "low_threshold_pct": float(_auto_switch_low_ud_pct),
            "low_fallback_pct": float(_auto_switch_low_fallback_ud_pct),
            "low_memory_s": float(_auto_switch_low_memory_s),
            "top_threshold_pct": float(_auto_switch_top_ud_pct),
            "top_fallback_pct": float(_auto_switch_top_fallback_ud_pct),
            "top_memory_s": float(_auto_switch_top_memory_s),
        }


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _lerp(a, b, t):
    return a + ((b - a) * t)


def _remap_x_to_calibrated_center(x_norm):
    """Remap normalized X so configured center maps to 0.5."""
    center = _clamp(float(_tracking_x_center_norm), 0.05, 0.95)
    x = _clamp(float(x_norm), 0.0, 1.0)

    if x <= center:
        left_span = max(center, 1e-6)
        remapped = 0.5 * (x / left_span)
    else:
        right_span = max(1.0 - center, 1e-6)
        remapped = 0.5 + (0.5 * ((x - center) / right_span))

    deadband = _clamp(float(_tracking_x_center_deadband), 0.0, 0.25)
    if abs(remapped - 0.5) <= deadband:
        remapped = 0.5

    return _clamp(remapped, 0.0, 1.0)


def _arm_targets_from_ud_pct(ud_pct):
    """Map up/down percent (0..100) to synchronized targets for servos 3/4/5.
        Compressed calibration endpoints:
            top=100: s5=1121, s4=2170, s3=2021
            low=0:   s5=2400, s4=1480, s3=1344
    """
    p = _clamp(float(ud_pct), 0.0, 100.0)
    t = p / 100.0
    s5 = int(round(_lerp(2400, 1121, t)))
    s4 = int(round(_lerp(1480, 2170, t)))
    s3 = int(round(_lerp(1344, 2021, t)))
    return s3, s4, s5


def _update_right_wrist_tracking(x_norm, y_norm):
    global _last_tracking_send_t, _last_base_pos, _last_arm_combo
    global _last_right_wrist_norm, _last_right_wrist_raw_norm, _last_right_wrist_seen_t, _last_ud_pct
    global _last_low_ud_seen_t, _last_top_ud_seen_t
    now = time.time()

    x_raw = _clamp(float(x_norm), 0.0, 1.0)
    y_raw = _clamp(float(y_norm), 0.0, 1.0)

    # Use raw normalized x by default; optional invert handles mirrored camera feeds.
    x_norm = (1.0 - x_raw) if _tracking_x_invert else x_raw
    x_norm = _remap_x_to_calibrated_center(x_norm)
    y_norm = y_raw
    ud_pct = (1.0 - y_norm) * 100.0

    edge_bottom_missing = _auto_switch_edge_missing_enabled and (y_norm >= _auto_switch_edge_missing_y)
    edge_top_missing = _auto_switch_edge_top_missing_enabled and (y_norm <= _auto_switch_edge_top_missing_y)
    edge_is_missing = edge_bottom_missing or edge_top_missing

    with _tracking_lock:
        _last_right_wrist_raw_norm = (x_raw, y_raw)
        _last_right_wrist_norm = (x_norm, y_norm)
        # Treat extreme edge detections as effectively missing for switch logic.
        if not edge_is_missing:
            _last_right_wrist_seen_t = now
        _last_ud_pct = ud_pct
        if ud_pct <= _auto_switch_low_ud_pct:
            _last_low_ud_seen_t = now
        if ud_pct >= _auto_switch_top_ud_pct:
            _last_top_ud_seen_t = now

    if not robot_brain.is_wrist_tracking_enabled():
        return

    if now - _last_tracking_send_t < _tracking_send_period_s:
        return

    # Left/Right wrist slider mapping (inverted direction):
    # 0% -> 2000, 50% -> 1500, 100% -> 1000.
    base_pos = int(round(1000 + ((1.0 - x_norm) * 1000)))
    base_pos = _clamp(base_pos, 1000, 2000)

    # Up/Down mapping shared with GUI slider semantics:
    # y_norm=0 (top) => 100%, y_norm=1 (bottom) => 0%.
    ud_pct = (1.0 - y_norm) * 100.0
    s3, s4, s5 = _arm_targets_from_ud_pct(ud_pct)
    arm_combo = (s3, s4, s5)

    base_changed = (_last_base_pos != base_pos)
    arm_changed = (_last_arm_combo != arm_combo)

    if not base_changed and not arm_changed:
        return

    try:
        if arm_changed:
            robot_brain.send_multi_servo_command(
                {5: s5, 4: s4, 3: s3},
                _tracking_combo_time_ms,
            )
            _last_arm_combo = arm_combo
        if base_changed:
            robot_brain.send_servo_command(6, base_pos, _tracking_servo_speed)
        _last_base_pos = base_pos
        _last_tracking_send_t = now
    except Exception as e:
        print(f"[AI] Wrist tracking servo update failed: {e}")

def _draw_pose_skeleton(frame, points):
    h, w = frame.shape[:2]
    for i, j in COCO_SKELETON:
        if i < len(points) and j < len(points):
            pi, pj = points[i], points[j]
            if hasattr(pi, 'x') and hasattr(pi, 'y') and hasattr(pj, 'x') and hasattr(pj, 'y'):
                x1, y1 = int(pi.x() * w), int(pi.y() * h)
                x2, y2 = int(pj.x() * w), int(pj.y() * h)
                cv2.line(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
    for idx, p in enumerate(points):
        if hasattr(p, 'x') and hasattr(p, 'y'):
            x, y = int(p.x() * w), int(p.y() * h)
            color = (0, 0, 255) if idx == RIGHT_HAND_IDX else (255, 255, 255)
            cv2.circle(frame, (x, y), 5, color, -1)

def app_callback(pad, info, user_data):
    global _auto_switch_requested_mode
    buffer = info.get_buffer()
    if buffer is None:
        return Gst.PadProbeReturn.OK
    roi = hailo.get_roi_from_buffer(buffer)
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
    wrist_found = False
    now = time.time()
    with _tracking_lock:
        last_wrist_x, last_wrist_y = _last_right_wrist_raw_norm
    best_wrist = None
    best_dist2 = None
    try:
        format, width, height = None, None, None
        try:
            caps = pad.get_current_caps()
            struct = caps.get_structure(0)
            format = struct.get_value('format')
            width = struct.get_value('width')
            height = struct.get_value('height')
        except Exception:
            pass
        if format and width and height:
            success, mapinfo = buffer.map(Gst.MapFlags.READ)
            if success:
                try:
                    frame = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((height, width, 3))
                    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    _update_hand_gesture_state_from_frame(frame, now)
                    for detection in detections:
                        if detection.get_label() == "person":
                            landmarks = detection.get_objects_typed(hailo.HAILO_LANDMARKS)
                            if landmarks:
                                points = landmarks[0].get_points()
                                _draw_pose_skeleton(frame_bgr, points)
                                if RIGHT_HAND_IDX < len(points):
                                    right_x = points[RIGHT_HAND_IDX].x()
                                    right_y = points[RIGHT_HAND_IDX].y()
                                    if (not np.isfinite(right_x)) or (not np.isfinite(right_y)):
                                        continue
                                    if (right_x < 0.0) or (right_x > 1.0) or (right_y < 0.0) or (right_y > 1.0):
                                        continue
                                    wrist_found = True
                                    rx, ry = int(right_x * width), int(right_y * height)
                                    cv2.circle(frame_bgr, (rx, ry), 10, (0, 255, 255), 3)
                                    cv2.putText(frame_bgr, "Right Hand", (rx+10, ry), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,255), 2)
                                    dist2 = ((right_x - last_wrist_x) ** 2) + ((right_y - last_wrist_y) ** 2)
                                    if (best_dist2 is None) or (dist2 < best_dist2):
                                        best_dist2 = dist2
                                        best_wrist = (right_x, right_y)
                    # Use one stable wrist candidate per frame to avoid random person-order churn.
                    if best_wrist is not None:
                        _update_right_wrist_tracking(best_wrist[0], best_wrist[1])
                finally:
                    buffer.unmap(mapinfo)

        # Auto-switch rule (both directions):
        # HIGH_CAM -> TABLE_CAM when wrist goes low then disappears,
        # TABLE_CAM -> HIGH_CAM when wrist goes high then disappears.
        if (not wrist_found) and (_active_mode in ("HIGH_CAM", "TABLE_CAM")):
            with _tracking_lock:
                target_mode = _compute_auto_switch_target(now)
                if target_mode and (_auto_switch_requested_mode is None):
                    _auto_switch_requested_mode = target_mode
                    print(f"[AI] Auto switch requested: wrist edge then disappeared -> {target_mode}")
    except Exception as e:
        print(f"[ERROR] Pose overlay: {e}")
    return Gst.PadProbeReturn.OK
# Main thread: call this to display frames and handle window events
def run_high_cam_window_loop(window_name="Hailo Pose Estimation"):
    window_ready = False
    state = _load_video_window_state()
    default_w = int(state.get("w", getattr(config, "VIDEO_WINDOW_DEFAULT_W", 960)))
    default_h = int(state.get("h", getattr(config, "VIDEO_WINDOW_DEFAULT_H", 540)))
    default_x = int(state.get("x", getattr(config, "VIDEO_WINDOW_DEFAULT_X", 340)))
    default_y = int(state.get("y", getattr(config, "VIDEO_WINDOW_DEFAULT_Y", 20)))
    last_empty_log = 0.0
    while not _display_stop_event.is_set():
        try:
            frame = _frame_queue.get(timeout=1)
        except queue.Empty:
            continue
        if frame is None:
            continue
        if not _preview_enabled:
            if window_ready:
                try:
                    cv2.destroyWindow(window_name)
                except Exception:
                    pass
                window_ready = False
            continue
        if not window_ready:
            try:
                cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(window_name, default_w, default_h)
                cv2.moveWindow(window_name, default_x, default_y)
                window_ready = True
            except Exception as e:
                print(f"[AI] Could not initialize preview window: {e}")
        # When the table cam is active, run object detection overlay.
        with _tracking_lock:
            current_mode = _active_mode
        if current_mode == "TABLE_CAM":
            frame = table_detect.push_frame(frame)
        cv2.imshow(window_name, frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            request_display_stop()
            break
    try:
        if window_ready:
            try:
                x, y, w, h = cv2.getWindowImageRect(window_name)
                _save_video_window_state(x, y, w, h)
            except Exception:
                pass
        cv2.destroyWindow(window_name)
    except Exception:
        pass

if __name__ == "__main__":
    user_data = UserAppCallback()
    # Force video source to /dev/video0 for Pi Camera
    import sys
    sys.argv += ["--input", "/dev/video1"]
    app = GStreamerPoseEstimationApp(app_callback, user_data)
    app.run()



