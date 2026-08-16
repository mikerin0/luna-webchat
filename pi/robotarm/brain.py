# brain.py
# Controls movement of the robot arm

import threading
import time
import urllib.request
import os
import queue
import subprocess
import importlib
import socket
import json
import tempfile

import config
import poses

try:
    import serial
except Exception:
    serial = None

SHELLY_RELAY_IP = "172.31.31.166"
SERIAL_PORT = "/dev/ttyAMA10"
SERIAL_BAUD = 9600
SERIAL_TIMEOUT_S = 1.0

_SERVO_ID_MIN = 1
_SERVO_ID_MAX = 6
_SERVO_POS_MIN = 500
_SERVO_POS_MAX = 2500
_SERVO_CLAW_ID = 1
_SERVO_CLAW_MIN = 1500
_SERVO_CLAW_MAX = int(getattr(config, "GRIPPER_HARD_CLOSE_MAX", 2270))

_io_lock = threading.Lock()
_serial_conn = None
_serial_error_logged = False
_tracking_mode_lock = threading.RLock()
_wrist_tracking_enabled = False
_live_servo_follow_enabled = False
_mode_transition_lock = threading.Lock()
_last_commanded_positions = {sid: None for sid in range(_SERVO_ID_MIN, _SERVO_ID_MAX + 1)}

# --- Speech output state ---
_tts_queue = queue.Queue(maxsize=24)
_tts_worker_started = False
_tts_worker_lock = threading.Lock()
_tts_speaking = threading.Event()  # set while audio is being rendered or played

# --- Gripper claw state ---
_gripper_motion_lock = threading.Lock()
_gripper_pos_est = _SERVO_CLAW_MIN   # tracks estimated claw position
_gripper_switch_ready = False
_gpio_mod = None
_claw_abort = threading.Event()       # set to cancel an in-progress close
_last_close_stopped_by_switch = False

# --- Crestron TCP server state ---
_crestron_server_thread = None
_crestron_server_stop = threading.Event()
_crestron_conn = None
_crestron_conn_lock = threading.Lock()
_crestron_handlers = {}


def register_crestron_command(command: str, handler):
    """Register a handler for an incoming Crestron command."""
    cmd = str(command or "").strip().upper()
    if not cmd:
        return
    if callable(handler):
        _crestron_handlers[cmd] = handler


def unregister_crestron_command(command: str):
    cmd = str(command or "").strip().upper()
    if not cmd:
        return
    _crestron_handlers.pop(cmd, None)


def _set_crestron_conn(conn):
    global _crestron_conn
    with _crestron_conn_lock:
        old = _crestron_conn
        _crestron_conn = conn
    if old is not None and old is not conn:
        try:
            old.close()
        except Exception:
            pass


def send_to_crestron(command: str) -> bool:
    """Send one command line to the currently connected Crestron client."""
    payload = str(command or "").strip()
    if not payload:
        return False
    with _crestron_conn_lock:
        conn = _crestron_conn
    if conn is None:
        print("[Brain] Crestron send skipped (no active client)")
        return False
    try:
        conn.sendall(f"{payload}\n".encode("utf-8", errors="ignore"))
        print(f"[Brain] -> Crestron: {payload}")
        return True
    except Exception as exc:
        print(f"[Brain] Crestron send failed: {exc}")
        _set_crestron_conn(None)
        return False


def _dispatch_crestron_command(command: str):
    cmd = str(command or "").strip().upper()
    if not cmd:
        return
    print(f"[Brain] <- Crestron: {cmd}")

    handler = _crestron_handlers.get(cmd)
    if callable(handler):
        try:
            handler(cmd)
            return
        except Exception as exc:
            print(f"[Brain] Crestron handler error for {cmd}: {exc}")
            return

    if cmd == "PING":
        send_to_crestron("PONG")
        return
    if cmd == "HOME":
        run_pose("home")
        return
    if cmd in ("SLEEP", "SLEEP_ARM"):
        sleep_arm()
        return
    if cmd in ("WAKE", "WAKE_ARM"):
        set_servo_power(True)
        time.sleep(0.4)
        run_pose("home")
        return
    if cmd in ("TAKE", "TAKE_ITEM", "HANDOFF"):
        run_pose("take")
        return

    print(f"[Brain] Crestron command ignored: {cmd}")


def _crestron_server_loop():
    host = str(getattr(config, "CRESTRON_SERVER_HOST", "0.0.0.0"))
    port = int(getattr(config, "CRESTRON_PORT", 50005))
    accept_timeout = max(0.2, float(getattr(config, "CRESTRON_ACCEPT_TIMEOUT_S", 1.0)))
    client_timeout = max(0.1, float(getattr(config, "CRESTRON_CLIENT_TIMEOUT_S", 1.0)))
    buf_limit = max(128, int(getattr(config, "CRESTRON_MAX_LINE_BYTES", 2048)))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((host, port))
            server.listen(2)
            server.settimeout(accept_timeout)
            print(f"[Brain] Crestron TCP server listening on {host}:{port}")

            while not _crestron_server_stop.is_set():
                conn = None
                try:
                    conn, addr = server.accept()
                    conn.settimeout(client_timeout)
                    _set_crestron_conn(conn)
                    print(f"[Brain] Crestron connected: {addr}")
                    buffer = ""

                    while not _crestron_server_stop.is_set():
                        try:
                            chunk = conn.recv(1024)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buffer += chunk.decode("utf-8", errors="ignore")
                        if len(buffer) > buf_limit:
                            buffer = buffer[-buf_limit:]
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            _dispatch_crestron_command(line.strip())

                except socket.timeout:
                    continue
                except Exception as exc:
                    print(f"[Brain] Crestron server error: {exc}")
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    _set_crestron_conn(None)

    except Exception as exc:
        print(f"[Brain] Crestron server start failed: {exc}")


def start_crestron_server() -> bool:
    """Start background TCP server for Crestron client connections."""
    global _crestron_server_thread
    if not bool(getattr(config, "CRESTRON_SERVER_ENABLED", True)):
        print("[Brain] Crestron server disabled by config")
        return False
    if _crestron_server_thread is not None and _crestron_server_thread.is_alive():
        return True
    _crestron_server_stop.clear()
    _crestron_server_thread = threading.Thread(target=_crestron_server_loop, daemon=True)
    _crestron_server_thread.start()
    return True


def stop_crestron_server():
    """Stop background TCP server and close any active Crestron client socket."""
    global _crestron_server_thread
    _crestron_server_stop.set()
    _set_crestron_conn(None)
    th = _crestron_server_thread
    if th is not None and th.is_alive():
        try:
            th.join(timeout=2.0)
        except Exception:
            pass
    _crestron_server_thread = None


def set_servo_power(on: bool) -> bool:
    """Turn the servo power relay on or off via Shelly HTTP API.
    Returns True if the request succeeded, False otherwise."""
    action = "on" if on else "off"
    url = f"http://{SHELLY_RELAY_IP}/relay/0?turn={action}"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            print(f"[Brain] Servo power relay turned {action} (HTTP {resp.status})")
            return True
    except Exception as exc:
        print(f"[Brain] Shelly relay error ({action}): {exc}")
        return False


def _clamp_servo_position(servo_id: int, position: int) -> int:
    if int(servo_id) == _SERVO_CLAW_ID:
        return max(_SERVO_CLAW_MIN, min(_SERVO_CLAW_MAX, int(position)))
    return max(_SERVO_POS_MIN, min(_SERVO_POS_MAX, int(position)))


def _ensure_serial_open() -> bool:
    global _serial_conn, _serial_error_logged
    if _serial_conn is not None:
        return True
    if serial is None:
        if not _serial_error_logged:
            print("[Brain] pyserial not available; servo commands disabled")
            _serial_error_logged = True
        return False
    try:
        _serial_conn = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=SERIAL_TIMEOUT_S)
        print(f"[Brain] Serial connected on {SERIAL_PORT} @ {SERIAL_BAUD}")
        return True
    except Exception as exc:
        if not _serial_error_logged:
            print(f"[Brain] Serial open failed ({SERIAL_PORT}): {exc}")
            _serial_error_logged = True
        return False


def send_servo_command(servo_num: int, position: int, time_ms: int = 800) -> bool:
    """Send one absolute LSC-6 servo command packet.
    Returns True when packet is written successfully."""
    sid = int(servo_num)
    if sid < _SERVO_ID_MIN or sid > _SERVO_ID_MAX:
        print(f"[Brain] Invalid servo id: {sid}")
        return False

    t_ms = max(20, int(time_ms))
    pos = _clamp_servo_position(sid, int(position))

    packet = bytearray([
        0x55, 0x55, 0x08, 0x03, 0x01,
        t_ms & 0xFF,
        (t_ms >> 8) & 0xFF,
        sid,
        pos & 0xFF,
        (pos >> 8) & 0xFF,
    ])

    with _io_lock:
        if not _ensure_serial_open():
            return False
        try:
            _serial_conn.write(packet)
            _last_commanded_positions[sid] = pos
            return True
        except Exception as exc:
            print(f"[Brain] Servo write failed (id={sid}): {exc}")
            return False


def send_multi_servo_command(positions: dict, time_ms: int = 800) -> bool:
    """Send one synchronized multi-servo LSC-6 command packet.
    positions: {servo_id: absolute_position}
    """
    if not isinstance(positions, dict) or not positions:
        return False

    t_ms = max(20, int(time_ms))
    items = []
    for sid, pos in positions.items():
        sid_i = int(sid)
        if sid_i < _SERVO_ID_MIN or sid_i > _SERVO_ID_MAX:
            continue
        items.append((sid_i, _clamp_servo_position(sid_i, int(pos))))

    if not items:
        return False

    # Data payload format:
    # [count, time_l, time_h, sid1, pos1_l, pos1_h, sid2, pos2_l, pos2_h, ...]
    data = [len(items), t_ms & 0xFF, (t_ms >> 8) & 0xFF]
    for sid_i, pos_i in items:
        data.extend([sid_i, pos_i & 0xFF, (pos_i >> 8) & 0xFF])

    length = 2 + len(data)  # command byte + data length accounting used by LSC protocol
    packet = bytearray([0x55, 0x55, length, 0x03] + data)

    with _io_lock:
        if not _ensure_serial_open():
            return False
        try:
            _serial_conn.write(packet)
            for sid_i, pos_i in items:
                _last_commanded_positions[sid_i] = pos_i
            return True
        except Exception as exc:
            print(f"[Brain] Multi-servo write failed: {exc}")
            return False


def read_servo_position(servo_num: int, fast: bool = True):
    """Read current physical servo position.
    Returns integer pulse value or None on failure."""
    sid = int(servo_num)
    if sid < _SERVO_ID_MIN or sid > _SERVO_ID_MAX:
        return None

    # Packet: 55 55 04 15 01 <sid>
    packet = bytearray([0x55, 0x55, 0x04, 0x15, 0x01, sid])
    retries = 1 if fast else 3
    read_timeout_s = 0.08 if fast else SERIAL_TIMEOUT_S

    with _io_lock:
        if not _ensure_serial_open():
            return None
        original_timeout = _serial_conn.timeout
        try:
            _serial_conn.timeout = read_timeout_s
            for _ in range(retries):
                try:
                    _serial_conn.reset_input_buffer()
                except Exception:
                    pass
                _serial_conn.write(packet)
                time.sleep(0.025)
                # Read a small burst and parse by header so partial framing
                # does not force a full failure.
                response = _serial_conn.read(32)
                if len(response) >= 8:
                    start = response.find(b"\x55\x55")
                    if start >= 0 and (start + 8) <= len(response):
                        frame = response[start:start + 8]
                        if frame[3] == 0x15:
                            return int(frame[6]) | (int(frame[7]) << 8)
        except Exception as exc:
            print(f"[Brain] Servo read failed (id={sid}): {exc}")
            return None
        finally:
            try:
                _serial_conn.timeout = original_timeout
            except Exception:
                pass
    return None


def get_all_servo_positions(fast: bool = True):
    """Return dict of servo_id -> position information.
    Fields per servo: {'position': int|None, 'source': 'read'|'commanded'|'none'}"""
    positions = {}
    for sid in range(_SERVO_ID_MIN, _SERVO_ID_MAX + 1):
        actual = read_servo_position(sid, fast=fast)
        if actual is not None:
            positions[sid] = {"position": int(actual), "source": "read"}
            continue
        commanded = _last_commanded_positions.get(sid)
        if commanded is not None:
            positions[sid] = {"position": int(commanded), "source": "commanded"}
        else:
            positions[sid] = {"position": None, "source": "none"}
    return positions


def get_last_commanded_positions():
    return dict(_last_commanded_positions)


def reload_poses() -> bool:
    """Reload poses.py at runtime so edited poses are available immediately."""
    global poses
    try:
        poses = importlib.reload(poses)
        pose_count = len(getattr(poses, "POSES", {}) or {})
        print(f"[Brain] Poses reloaded ({pose_count} pose definitions)")
        return True
    except Exception as exc:
        print(f"[Brain] Failed to reload poses: {exc}")
        return False


def parse_servo_command(command):
    """Parse either (servo, pos, time_ms) or 'servo:pos:time_ms'."""
    if isinstance(command, (tuple, list)) and len(command) == 3:
        return int(command[0]), int(command[1]), int(command[2])
    if isinstance(command, str):
        parts = [p.strip() for p in command.replace(",", ":").split(":") if p.strip()]
        if len(parts) == 3:
            return int(parts[0]), int(parts[1]), int(parts[2])
    raise ValueError(f"Invalid servo command format: {command}")


def parse_pose_step(command):
    """Parse one pose step.

    Supported formats:
    1) Single-servo (existing):
       - (servo_id, position, time_ms)
       - "servo_id:position:time_ms"

    2) Multi-servo synchronized step (up to 3 servos):
       - {"servos": {sid: pos, ...}, "time_ms": 800}
       - ({sid: pos, ...}, time_ms)
       - ([(sid, pos), (sid, pos), ...], time_ms)

     3) Face step:
         - {"face": "happy", "time_ms": 300}
         - ("face", "happy", 300)

    Returns:
      ("single", sid, pos, time_ms)
      OR ("multi", positions_dict, time_ms)
      OR ("face", face_name, time_ms)
    """
    # Face tuple form: ("face", "happy", 300)
    if isinstance(command, (tuple, list)) and len(command) == 3:
        if str(command[0]).strip().lower() == "face":
            face_name = str(command[1]).strip().lower()
            time_ms = int(command[2])
            if not face_name:
                raise ValueError(f"Invalid face step: {command}")
            return "face", face_name, time_ms

    # Dict form: {"servos": {...}, "time_ms": ...}  or special step dicts
    if isinstance(command, dict):
        if "face" in command:
            face_name = str(command.get("face", "")).strip().lower()
            time_ms = int(command.get("time_ms", 0))
            if not face_name:
                raise ValueError(f"Invalid face step: {command}")
            return "face", face_name, time_ms
        if "close_claw" in command:
            close_kwargs = {}
            if command.get("step_us") is not None:
                close_kwargs["step_us"] = int(command["step_us"])
            if command.get("step_time_ms") is not None:
                close_kwargs["step_time_ms"] = int(command["step_time_ms"])
            if command.get("switch_confirm_reads") is not None:
                close_kwargs["switch_confirm_reads"] = int(command["switch_confirm_reads"])
            if command.get("switch_confirm_interval_ms") is not None:
                close_kwargs["switch_confirm_interval_ms"] = int(command["switch_confirm_interval_ms"])
            if command.get("trigger_extra_close_us") is not None:
                close_kwargs["trigger_extra_close_us"] = int(command["trigger_extra_close_us"])
            if command.get("trigger_extra_close_time_ms") is not None:
                close_kwargs["trigger_extra_close_time_ms"] = int(command["trigger_extra_close_time_ms"])
            return "close_claw", close_kwargs
        if "wait_ms" in command:
            return "wait", int(command["wait_ms"])
        if "pose" in command:
            return "sub_pose", str(command["pose"])
        servos = command.get("servos")
        time_ms = command.get("time_ms", 800)
        if not isinstance(servos, dict) or not servos:
            raise ValueError(f"Invalid multi-servo dict step: {command}")
        positions = {int(k): int(v) for k, v in servos.items()}
        return "multi", positions, int(time_ms)

    # Tuple/list pair forms: ({sid:pos,...}, time_ms) OR ([(sid,pos), ...], time_ms)
    if isinstance(command, (tuple, list)) and len(command) == 2:
        first, time_ms = command
        if isinstance(first, dict):
            positions = {int(k): int(v) for k, v in first.items()}
            if not positions:
                raise ValueError(f"Invalid empty multi-servo step: {command}")
            return "multi", positions, int(time_ms)
        if isinstance(first, (tuple, list)):
            positions = {}
            for pair in first:
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    raise ValueError(f"Invalid servo pair in multi-servo step: {pair}")
                sid, pos = int(pair[0]), int(pair[1])
                positions[sid] = pos
            if not positions:
                raise ValueError(f"Invalid empty multi-servo list step: {command}")
            return "multi", positions, int(time_ms)

    # String "close_claw" shorthand
    if isinstance(command, str) and command.strip().lower() == "close_claw":
        return "close_claw", {}

    # Fallback to existing single-servo syntax.
    sid, pos, t_ms = parse_servo_command(command)
    return "single", sid, pos, t_ms


def run_command(command) -> bool:
    """Run one servo command in 'servo:position:time_ms' format."""
    sid, pos, t_ms = parse_servo_command(command)
    ok = send_servo_command(sid, pos, t_ms)
    # Wait for that move to complete before any next command.
    time.sleep(max(0.0, t_ms / 1000.0))
    return ok


def run_pose(name: str) -> bool:
    """Run a named pose sequentially with single/multi-servo and face steps."""
    sequence = poses.get_pose(name)
    if not sequence:
        print(f"[Brain] Pose '{name}' not found or empty")
        return False

    print(f"[Brain] Running pose '{name}' with {len(sequence)} commands")
    all_ok = True
    for cmd in sequence:
        parsed = parse_pose_step(cmd)
        kind = parsed[0]
        t_ms = 0
        ok = True
        if kind == "single":
            _, sid, pos, t_ms = parsed
            ok = send_servo_command(sid, pos, t_ms)
        elif kind == "multi":
            _, positions, t_ms = parsed
            ok = send_multi_servo_command(positions, t_ms)
        elif kind == "face":
            _, face_name, t_ms = parsed
            try:
                import lcd
                lcd.show_face(face_name)
            except Exception as exc:
                print(f"[Brain] Face step failed ({face_name}): {exc}")
                ok = False
        elif kind == "close_claw":
            _, close_kwargs = parsed
            ok = close_claw(**(close_kwargs or {}))
        elif kind == "wait":
            _, wait_ms = parsed
            time.sleep(wait_ms / 1000.0)
        elif kind == "sub_pose":
            _, sub_name = parsed
            ok = run_pose(sub_name)
        else:
            print(f"[Brain] Unknown pose step kind '{kind}': {cmd}")
            ok = False
        all_ok = all_ok and ok
        time.sleep(max(0.0, t_ms / 1000.0))
    return all_ok


def set_wrist_tracking_enabled(enabled: bool):
    global _wrist_tracking_enabled
    with _tracking_mode_lock:
        _wrist_tracking_enabled = bool(enabled)


def set_live_servo_follow_enabled(enabled: bool):
    global _live_servo_follow_enabled
    with _tracking_mode_lock:
        _live_servo_follow_enabled = bool(enabled)


def is_wrist_tracking_enabled() -> bool:
    with _tracking_mode_lock:
        return _wrist_tracking_enabled


def is_live_servo_follow_enabled() -> bool:
    with _tracking_mode_lock:
        return _live_servo_follow_enabled


def enable_wrist_tracking_mode() -> bool:
    """Power up and move to HOME pose, then enable wrist tracking."""
    with _mode_transition_lock:
        with _tracking_mode_lock:
            if _wrist_tracking_enabled:
                return True
            set_servo_power(True)
            # Give the controller time to boot after relay turns on.
            time.sleep(1.0)
            run_pose("home")
            set_wrist_tracking_enabled(True)
            # Keep live follow disabled until explicitly enabled later.
            set_live_servo_follow_enabled(False)
        return True


def disable_wrist_tracking_mode() -> bool:
    """Disable wrist tracking, move to SLEEP pose, then power down."""
    with _mode_transition_lock:
        with _tracking_mode_lock:
            if not _wrist_tracking_enabled:
                # Keep state coherent even if UI asks for OFF repeatedly.
                set_live_servo_follow_enabled(False)
                set_servo_power(False)
                return True
            set_wrist_tracking_enabled(False)
            set_live_servo_follow_enabled(False)
            run_pose("sleep")
            set_servo_power(False)
        return True


def stop_wrist_tracking_to_home() -> bool:
    """Stop wrist tracking and move to HOME pose (keep servo power on)."""
    with _mode_transition_lock:
        with _tracking_mode_lock:
            set_wrist_tracking_enabled(False)
            set_live_servo_follow_enabled(False)
            run_pose("home")
        return True


def sleep_arm() -> bool:
    """Move to SLEEP pose and shut off Shelly servo power."""
    with _mode_transition_lock:
        with _tracking_mode_lock:
            set_wrist_tracking_enabled(False)
            set_live_servo_follow_enabled(False)
            # Ensure controller is awake long enough to execute the sleep pose.
            set_servo_power(True)
            time.sleep(1.5)  # Extended wait for relay + controller readiness
            pose_ok = bool(run_pose("sleep"))
            if not pose_ok:
                # Keep power on so caller can attempt an immediate fallback sleep move.
                print("[Brain] sleep_arm: sleep pose failed; leaving power ON for fallback")
                return False
            # Small post-move settle so servo command can physically complete.
            time.sleep(0.35)
            set_servo_power(False)
        return True


# ---------------------------------------------------------------------------
# Ultrasonic distance helper — shared, serialised, cached
# ---------------------------------------------------------------------------
# Single lock so gui.py's 150 ms slider poll and smart_table_take never
# claim the same lgpio pins simultaneously.
_ultrasonic_hw_lock = threading.Lock()
_ultrasonic_cache   = {"cm": None, "t": 0.0}


def read_ultrasonic_cm(timeout_s: float = 0.04) -> float | None:
    """Serialised single HC-SR04 read.  Updates shared cache.
    Returns distance in cm, or None on timeout / error."""
    trig_pin = int(getattr(config, "ULTRASONIC_TRIGGER_PIN_BCM", 23))
    echo_pin = int(getattr(config, "ULTRASONIC_ECHO_PIN_BCM", 24))
    with _ultrasonic_hw_lock:
        try:
            import lgpio
            h = lgpio.gpiochip_open(0)
            try:
                lgpio.gpio_claim_output(h, trig_pin)
                lgpio.gpio_claim_input(h, echo_pin)
                lgpio.gpio_write(h, trig_pin, 0)
                time.sleep(0.000002)
                lgpio.gpio_write(h, trig_pin, 1)
                time.sleep(0.000010)
                lgpio.gpio_write(h, trig_pin, 0)
                t0 = time.time()
                while lgpio.gpio_read(h, echo_pin) == 0:
                    if time.time() - t0 > timeout_s:
                        return None
                pulse_start = time.time()
                while lgpio.gpio_read(h, echo_pin) == 1:
                    if time.time() - pulse_start > timeout_s:
                        return None
                pulse_end = time.time()
                dist = round((pulse_end - pulse_start) * 17150.0, 2)
                _ultrasonic_cache["cm"] = dist
                _ultrasonic_cache["t"]  = time.time()
                return dist
            finally:
                try:
                    lgpio.gpio_free(h, trig_pin)
                except Exception:
                    pass
                try:
                    lgpio.gpio_free(h, echo_pin)
                except Exception:
                    pass
                lgpio.gpiochip_close(h)
        except Exception:
            return None


def get_ultrasonic_cache(max_age_s: float = 1.5) -> float | None:
    """Return the latest cached distance (cm), or None if stale / unavailable."""
    if _ultrasonic_cache["cm"] is None:
        return None
    if (time.time() - _ultrasonic_cache["t"]) > max_age_s:
        return None
    return float(_ultrasonic_cache["cm"])


def _object_x_to_servo6(x_norm: float) -> int:
    """Map a normalised table-cam X position (0=left, 1=right) to servo 6."""
    base_left  = int(getattr(config, "SMART_TAKE_BASE_LEFT",  2000))
    base_right = int(getattr(config, "SMART_TAKE_BASE_RIGHT", 1000))
    invert     = bool(getattr(config, "SMART_TAKE_BASE_X_INVERT", False))
    x = float(x_norm)
    if invert:
        x = 1.0 - x
    x = max(0.0, min(1.0, x))
    pos = int(round(base_left + x * (base_right - base_left)))
    return max(min(pos, max(base_left, base_right)), min(base_left, base_right))


def smart_table_take(status_cb=None) -> bool:
    """Autonomously locate an object on the table and pick it up.

    Uses table_detect (OpenCV HSV detection on the table cam preview) for
    X-axis alignment and the HC-SR04 ultrasonic sensor to guide the descent.
    Returns True on successful pickup, False on timeout or failure.
    """
    import table_detect  # local import; no circular dependency

    def _status(msg: str):
        print(f"[Brain] smart_table_take: {msg}")
        if status_cb is not None:
            try:
                status_cb(msg)
            except Exception:
                pass

    # --- Config ----------------------------------------------------------
    detect_timeout_s = float(getattr(config, "SMART_TAKE_DETECT_TIMEOUT_S", 5.0))
    base_align_ms    = int(getattr(config,   "SMART_TAKE_BASE_ALIGN_MS",    700))
    base_settle_ms   = int(getattr(config,   "SMART_TAKE_BASE_SETTLE_MS",   300))
    pre_grasp_ms     = int(getattr(config,   "SMART_TAKE_PRE_GRASP_MS",    1200))
    s3_pre           = int(getattr(config,   "SMART_TAKE_PRE_GRASP_S3",    2079))
    s4_pre           = int(getattr(config,   "SMART_TAKE_PRE_GRASP_S4",    2170))
    s5_pre           = int(getattr(config,   "SMART_TAKE_PRE_GRASP_S5",    1840))
    descent_step_us  = int(getattr(config,   "SMART_TAKE_DESCENT_STEP_US",   25))
    descent_step_ms  = int(getattr(config,   "SMART_TAKE_DESCENT_STEP_MS",  280))
    max_steps        = int(getattr(config,   "SMART_TAKE_DESCENT_MAX_STEPS", 20))
    close_dist_cm    = float(getattr(config, "SMART_TAKE_CLOSE_DISTANCE_CM", 10.0))
    held_dist_cm     = float(getattr(config, "SMART_TAKE_HELD_DISTANCE_CM",   8.0))
    lift_ms          = int(getattr(config,   "SMART_TAKE_LIFT_MS",           1700))
    S5_MAX           = int(getattr(config,   "SMART_TAKE_DESCENT_S5_MAX",   2060))
    min_area_norm    = float(getattr(config, "SMART_TAKE_MIN_AREA_NORM", 0.002))
    auto_open_on_miss = bool(getattr(config, "SMART_TAKE_AUTO_OPEN_ON_MISS", False))
    grip_step_us     = int(getattr(config,   "SMART_TAKE_GRIP_STEP_US",      30))
    grip_step_ms     = int(getattr(config,   "SMART_TAKE_GRIP_STEP_TIME_MS", 95))
    grip_reads       = int(getattr(config,   "SMART_TAKE_GRIP_SWITCH_CONFIRM_READS", 2))
    grip_int_ms      = int(getattr(config,   "SMART_TAKE_GRIP_SWITCH_CONFIRM_INTERVAL_MS", 8))
    grip_extra_us    = int(getattr(config,   "SMART_TAKE_GRIP_TRIGGER_EXTRA_CLOSE_US", 18))
    grip_extra_ms    = int(getattr(config,   "SMART_TAKE_GRIP_TRIGGER_EXTRA_CLOSE_TIME_MS", 140))

    # Step 1: Power on, open claw, move to home --------------------------------
    _status("Powering up")
    set_servo_power(True)
    time.sleep(0.5)
    open_claw(time_ms=400)
    run_pose("home")
    time.sleep(1.0)

    # Step 2: Enable detection and wait for stable lock -------------------------
    _status("Looking for object")
    table_detect.set_detection_enabled(True)
    deadline = time.time() + detect_timeout_s
    det = None
    while time.time() < deadline:
        d = table_detect.get_detected_object()
        if (
            d is not None
            and d.get("stable")
            and float(d.get("area_norm", 0.0)) >= min_area_norm
            and (time.time() - d["seen_t"]) < float(
            getattr(config, "TABLE_DETECT_MAX_AGE_S", 0.5)
            )
        ):
            det = d
            break
        time.sleep(0.05)

    if det is None:
        _status("No object detected — aborting")
        table_detect.set_detection_enabled(False)
        run_pose("home")
        return False

    x_norm = det["x"]
    _status(f"Object found at x={x_norm:.2f}")

    # Step 3: Align base servo to detected X -----------------------------------
    servo6_target = _object_x_to_servo6(x_norm)
    _status(f"Aligning base -> servo6={servo6_target}")
    send_servo_command(6, servo6_target, base_align_ms)
    time.sleep((base_align_ms + base_settle_ms) / 1000.0)

    # Refresh detection after base move (object may have shifted in frame)
    d2 = table_detect.get_detected_object()
    if (
        d2 is not None
        and d2.get("stable")
        and float(d2.get("area_norm", 0.0)) >= min_area_norm
        and (time.time() - d2["seen_t"]) < 0.5
    ):
        servo6_target = _object_x_to_servo6(d2["x"])
        send_servo_command(6, servo6_target, base_align_ms // 2)
        time.sleep((base_align_ms // 2 + base_settle_ms) / 1000.0)

    # Step 4: Move arm to pre-grasp height -------------------------------------
    _status("Moving to pre-grasp position")
    send_multi_servo_command({3: s3_pre, 4: s4_pre, 5: s5_pre}, pre_grasp_ms)
    time.sleep(pre_grasp_ms / 1000.0 + 0.1)

    # Step 5: Ultrasonic-guided descent ----------------------------------------
    _status("Descending toward object")
    s5_current = s5_pre
    for step in range(max_steps):
        dist = read_ultrasonic_cm()
        dist_str = f"{dist:.1f}" if dist is not None else "None"
        _status(f"Step {step + 1}/{max_steps}: dist={dist_str} cm, s5={s5_current}")
        if dist is not None and dist <= close_dist_cm:
            _status(f"Close distance reached ({dist_str} cm) -- gripping")
            break
        if s5_current >= S5_MAX:
            _status(f"S5_MAX={S5_MAX} reached (dist={dist_str} cm) -- attempting grip anyway")
            break
        s5_current = min(S5_MAX, s5_current + descent_step_us)
        send_servo_command(5, s5_current, descent_step_ms)
        time.sleep(descent_step_ms / 1000.0)
    else:
        _status("Descent loop finished -- attempting grip anyway")

    # Step 6: Close claw -------------------------------------------------------
    _status("Closing claw")
    time.sleep(0.2)
    close_claw(
        step_us=grip_step_us,
        step_time_ms=grip_step_ms,
        switch_confirm_reads=grip_reads,
        switch_confirm_interval_ms=grip_int_ms,
        trigger_extra_close_us=grip_extra_us,
        trigger_extra_close_time_ms=grip_extra_ms,
    )
    switch_grab = did_last_close_stop_on_switch()
    if switch_grab:
        _status("Gripper switch triggered during close")
    else:
        _status("Gripper switch not confirmed during close")
    time.sleep(0.15)

    # Step 7: Lift -------------------------------------------------------------
    _status("Lifting")
    send_multi_servo_command({3: 2021, 4: 2170, 5: 1121}, lift_ms)
    time.sleep(lift_ms / 1000.0 + 0.1)

    # Step 8: Verify grip via ultrasonic ---------------------------------------
    dist_after = read_ultrasonic_cm()
    dist_after_str = f"{dist_after:.1f}" if dist_after is not None else "None"
    if dist_after is not None and dist_after <= held_dist_cm:
        _status(f"Grip verified (dist={dist_after_str} cm)")
        success = True
    elif switch_grab:
        _status(
            f"Ultrasonic uncertain (dist={dist_after_str} cm) but switch triggered; keeping claw closed"
        )
        success = True
    else:
        _status(f"No object detected after lift (dist={dist_after_str} cm)")
        if auto_open_on_miss:
            _status("Configured to auto-open claw on miss")
            open_claw(time_ms=400)
        else:
            _status("Keeping claw closed on miss (SMART_TAKE_AUTO_OPEN_ON_MISS=False)")
        success = False

    # Step 9: Return to neutral carry pose (keep claw state) -------------------
    table_detect.set_detection_enabled(False)
    send_multi_servo_command({2: 1500, 3: 2021, 4: 2170, 5: 1121, 6: 1500}, lift_ms)
    time.sleep(lift_ms / 1000.0 + 0.1)
    return success


def _init_gripper_switch():
    """Initialise the RPi.GPIO pin for the gripper microswitch."""
    global _gripper_switch_ready, _gpio_mod
    pin = getattr(config, "GRIPPER_SWITCH_PIN_BCM", None)
    if pin is None:
        print("[Brain] Gripper microswitch: disabled (GRIPPER_SWITCH_PIN_BCM=None)")
        return
    try:
        import RPi.GPIO as GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        pud = GPIO.PUD_UP if getattr(config, "GRIPPER_SWITCH_PULL_UP", True) else GPIO.PUD_DOWN
        GPIO.setup(pin, GPIO.IN, pull_up_down=pud)
        _gpio_mod = GPIO
        _gripper_switch_ready = True
        print(f"[Brain] Gripper microswitch: enabled on BCM {pin}")
    except Exception as exc:
        print(f"[Brain] Gripper microswitch disabled (GPIO init failed): {exc}")


def _gripper_switch_pressed() -> bool:
    """Return True if the gripper microswitch is currently triggered."""
    if not _gripper_switch_ready:
        return False
    try:
        pin = getattr(config, "GRIPPER_SWITCH_PIN_BCM", None)
        pressed_state = int(getattr(config, "GRIPPER_SWITCH_PRESSED_STATE", 0))
        return int(_gpio_mod.input(pin)) == pressed_state
    except Exception:
        return False


def _gripper_switch_raw_state() -> int | None:
    """Return raw GPIO state (0/1) for the gripper switch, or None on error."""
    if not _gripper_switch_ready:
        return None
    try:
        pin = getattr(config, "GRIPPER_SWITCH_PIN_BCM", None)
        return int(_gpio_mod.input(pin))
    except Exception:
        return None


def did_last_close_stop_on_switch() -> bool:
    """True if the most recent close_claw() ended because switch trigger was confirmed."""
    return bool(_last_close_stopped_by_switch)


def get_gripper_switch_status() -> dict:
    """Return microswitch status snapshot for GUI diagnostics."""
    raw = _gripper_switch_raw_state()
    return {
        "ready": bool(_gripper_switch_ready),
        "pressed": bool(_gripper_switch_pressed()) if _gripper_switch_ready else False,
        "raw": raw,
        "pressed_state": int(getattr(config, "GRIPPER_SWITCH_PRESSED_STATE", 0)),
        "last_close_switch_stop": bool(_last_close_stopped_by_switch),
    }


def _confirm_switch_trigger(
    reads: int,
    interval_ms: int,
    *,
    baseline_state: int | None = None,
    allow_change_trigger: bool = False,
) -> bool:
    """Debounce helper for claw stop trigger.

    Confirms either:
    - configured pressed-state trigger, and optionally
    - raw-state change from baseline (useful if wiring/logic changed).
    """
    reads = max(1, int(reads))
    interval_ms = max(0, int(interval_ms))

    for i in range(reads):
        pressed = _gripper_switch_pressed()
        changed = False
        if allow_change_trigger and (baseline_state is not None):
            raw = _gripper_switch_raw_state()
            changed = (raw is not None) and (int(raw) != int(baseline_state))
        if not (pressed or changed):
            return False
        if i < (reads - 1) and interval_ms > 0:
            time.sleep(interval_ms / 1000.0)
    return True


def open_claw(time_ms: int = 800) -> bool:
    """Open the gripper (servo 1 to open/home position).
    Aborts any in-progress close first."""
    global _gripper_pos_est
    # Signal any running close loop to stop.
    _claw_abort.set()
    with _gripper_motion_lock:
        _claw_abort.clear()
        ok = send_servo_command(_SERVO_CLAW_ID, _SERVO_CLAW_MIN, max(20, int(time_ms)))
        if ok:
            _gripper_pos_est = _SERVO_CLAW_MIN
        return ok


def close_claw(
    step_us: int | None = None,
    step_time_ms: int | None = None,
    switch_confirm_reads: int | None = None,
    switch_confirm_interval_ms: int | None = None,
    trigger_extra_close_us: int | None = None,
    trigger_extra_close_time_ms: int | None = None,
) -> bool:
    """Close the gripper incrementally, stopping if the microswitch triggers.
    Aborts any other in-progress claw command first."""
    global _gripper_pos_est, _last_close_stopped_by_switch
    _claw_abort.set()
    with _gripper_motion_lock:
        _claw_abort.clear()
        _last_close_stopped_by_switch = False
        if step_us is None:
            step_us = int(getattr(config, "GRIPPER_CLOSE_STEP_US", 35))
        if step_time_ms is None:
            step_time_ms = int(getattr(config, "GRIPPER_CLOSE_STEP_TIME_MS", 70))
        step_us = max(10, int(step_us))
        step_time_ms = max(20, int(step_time_ms))
        current = _gripper_pos_est
        target = _SERVO_CLAW_MAX
        if switch_confirm_reads is None:
            switch_confirm_reads = int(getattr(config, "GRIPPER_SWITCH_CONFIRM_READS", 2))
        switch_confirm_reads = max(1, int(switch_confirm_reads))

        if switch_confirm_interval_ms is None:
            switch_confirm_interval_ms = int(getattr(config, "GRIPPER_SWITCH_CONFIRM_INTERVAL_MS", 8))
        switch_confirm_interval_ms = max(0, int(switch_confirm_interval_ms))

        if trigger_extra_close_us is None:
            trigger_extra_close_us = int(getattr(config, "GRIPPER_TRIGGER_EXTRA_CLOSE_US", 15))
        trigger_extra_close_us = max(0, int(trigger_extra_close_us))

        if trigger_extra_close_time_ms is None:
            trigger_extra_close_time_ms = int(getattr(config, "GRIPPER_TRIGGER_EXTRA_CLOSE_TIME_MS", 120))
        trigger_extra_close_time_ms = max(20, int(trigger_extra_close_time_ms))
        trigger_on_change = bool(getattr(config, "GRIPPER_SWITCH_TRIGGER_ON_CHANGE", True))
        baseline_state = _gripper_switch_raw_state()

        while current < target and not _claw_abort.is_set():
            if _confirm_switch_trigger(
                switch_confirm_reads,
                switch_confirm_interval_ms,
                baseline_state=baseline_state,
                allow_change_trigger=trigger_on_change,
            ):

                # Optional tiny squeeze helps hold objects when closure is very slow.
                if trigger_extra_close_us > 0 and current < target and not _claw_abort.is_set():
                    squeeze_pos = min(target, current + trigger_extra_close_us)
                    ok = send_servo_command(_SERVO_CLAW_ID, squeeze_pos, trigger_extra_close_time_ms)
                    if ok:
                        current = squeeze_pos
                        _gripper_pos_est = current
                    time.sleep(trigger_extra_close_time_ms / 1000.0)

                print(f"[Brain] Gripper microswitch triggered at pos {current}; stopping close")
                # Hold the current position.
                send_servo_command(_SERVO_CLAW_ID, current, 120)
                _gripper_pos_est = current
                _last_close_stopped_by_switch = True
                return True

            current = min(target, current + step_us)
            ok = send_servo_command(_SERVO_CLAW_ID, current, step_time_ms)
            if ok:
                _gripper_pos_est = current
            time.sleep(step_time_ms / 1000.0)

            # Check again immediately after the move because spring-loaded claws
            # can trigger briefly during settling.
            if _confirm_switch_trigger(
                switch_confirm_reads,
                switch_confirm_interval_ms,
                baseline_state=baseline_state,
                allow_change_trigger=trigger_on_change,
            ):
                print(f"[Brain] Gripper switch triggered after move at pos {current}; stopping close")
                send_servo_command(_SERVO_CLAW_ID, current, 120)
                _gripper_pos_est = current
                _last_close_stopped_by_switch = True
                return True

        if not _last_close_stopped_by_switch:
            print(f"[Brain] Gripper reached close limit/abort without confirmed switch (pos={current})")
        return True


def _pick_tts_model_path():
    model_candidates = getattr(config, "TTS_MODEL_CANDIDATES", None)
    if not isinstance(model_candidates, (list, tuple)) or not model_candidates:
        model_candidates = ["/home/arm/piper/en_GB-alan-medium.onnx"]
    for candidate in model_candidates:
        try:
            candidate_path = str(candidate)
        except Exception:
            continue
        if os.path.exists(candidate_path):
            return candidate_path
    return None


def _make_wav(pcm_s16_mono: bytes, sample_rate: int) -> bytes:
    """Wrap raw s16le mono PCM in a WAV container."""
    import struct, wave, io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_s16_mono)
    return buf.getvalue()


def _play_pcm_s16_mono(raw_audio: bytes, sample_rate: int) -> bool:
    def smart_table_take(status_cb=None) -> bool:
        """Autonomously locate an object on the table and pick it up.

        Uses table_detect (OpenCV HSV detection on the table cam preview) for
        X-axis alignment and the HC-SR04 ultrasonic sensor to guide the descent.
        Returns True on successful pickup, False on timeout or failure.
        """
        import table_detect  # local import; no circular dependency

        def _status(msg: str):
            print(f"[Brain] smart_table_take: {msg}")
            if status_cb is not None:
                try:
                    status_cb(msg)
                except Exception:
                    pass

        # --- Config ----------------------------------------------------------
        detect_timeout_s = float(getattr(config, "SMART_TAKE_DETECT_TIMEOUT_S", 5.0))
        base_align_ms    = int(getattr(config,   "SMART_TAKE_BASE_ALIGN_MS",    700))
        base_settle_ms   = int(getattr(config,   "SMART_TAKE_BASE_SETTLE_MS",   300))
        pre_grasp_ms     = int(getattr(config,   "SMART_TAKE_PRE_GRASP_MS",    1200))
        s3_pre           = int(getattr(config,   "SMART_TAKE_PRE_GRASP_S3",    2079))
        s4_pre           = int(getattr(config,   "SMART_TAKE_PRE_GRASP_S4",    2170))
        s5_pre           = int(getattr(config,   "SMART_TAKE_PRE_GRASP_S5",    1840))
        descent_step_us  = int(getattr(config,   "SMART_TAKE_DESCENT_STEP_US",   25))
        descent_step_ms  = int(getattr(config,   "SMART_TAKE_DESCENT_STEP_MS",  280))
        max_steps        = int(getattr(config,   "SMART_TAKE_DESCENT_MAX_STEPS", 20))
        close_dist_cm    = float(getattr(config, "SMART_TAKE_CLOSE_DISTANCE_CM", 10.0))
        held_dist_cm     = float(getattr(config, "SMART_TAKE_HELD_DISTANCE_CM",   8.0))
        lift_ms          = int(getattr(config,   "SMART_TAKE_LIFT_MS",           1700))
        S5_MAX           = int(getattr(config,   "SMART_TAKE_DESCENT_S5_MAX",   2060))

        # Step 1: Power on, open claw, move to home --------------------------------
        _status("Powering up")
        set_servo_power(True)
        time.sleep(0.5)
        open_claw(time_ms=400)
        run_pose("home")
        time.sleep(1.0)

        # Step 2: Enable detection and wait for stable lock -------------------------
        _status("Looking for object")
        table_detect.set_detection_enabled(True)
        deadline = time.time() + detect_timeout_s
        det = None
        while time.time() < deadline:
            d = table_detect.get_detected_object()
            if d is not None and d.get("stable") and (time.time() - d["seen_t"]) < float(
                getattr(config, "TABLE_DETECT_MAX_AGE_S", 0.5)
            ):
                det = d
                break
            time.sleep(0.05)

        if det is None:
            _status("No object detected — aborting")
            table_detect.set_detection_enabled(False)
            run_pose("home")
            return False

        x_norm = det["x"]
        _status(f"Object found at x={x_norm:.2f}")

        # Step 3: Align base servo to detected X -----------------------------------
        servo6_target = _object_x_to_servo6(x_norm)
        _status(f"Aligning base → servo6={servo6_target}")
        send_servo_command(6, servo6_target, base_align_ms)
        time.sleep((base_align_ms + base_settle_ms) / 1000.0)

        # Refresh detection after base move (object may have shifted in frame)
        d2 = table_detect.get_detected_object()
        if d2 is not None and d2.get("stable") and (time.time() - d2["seen_t"]) < 0.5:
            servo6_target = _object_x_to_servo6(d2["x"])
            send_servo_command(6, servo6_target, base_align_ms // 2)
            time.sleep((base_align_ms // 2 + base_settle_ms) / 1000.0)

        # Step 4: Move arm to pre-grasp height -------------------------------------
        _status("Moving to pre-grasp position")
        send_multi_servo_command({3: s3_pre, 4: s4_pre, 5: s5_pre}, pre_grasp_ms)
        time.sleep(pre_grasp_ms / 1000.0 + 0.1)

        # Step 5: Ultrasonic-guided descent ----------------------------------------
        _status("Descending toward object")
        s5_current = s5_pre
        for step in range(max_steps):
            dist = read_ultrasonic_cm()
            dist_str = f"{dist:.1f}" if dist is not None else "None"
            _status(f"Step {step + 1}/{max_steps}: dist={dist_str} cm, s5={s5_current}")
            if dist is not None and dist <= close_dist_cm:
                _status(f"Close distance reached ({dist_str} cm) — gripping")
                break
            if s5_current >= S5_MAX:
                _status(f"S5_MAX={S5_MAX} reached (dist={dist_str} cm) — attempting grip anyway")
                break
            s5_current = min(S5_MAX, s5_current + descent_step_us)
            send_servo_command(5, s5_current, descent_step_ms)
            time.sleep(descent_step_ms / 1000.0)
        else:
            _status(f"Descent loop finished — attempting grip anyway")

        # Step 6: Close claw --------------------------------------------------------
        _status("Closing claw")
        time.sleep(0.2)
        close_claw(
            step_us=20,
            step_time_ms=130,
            switch_confirm_reads=3,
            switch_confirm_interval_ms=10,
            trigger_extra_close_us=8,
            trigger_extra_close_time_ms=100,
        )
        time.sleep(0.15)

        # Step 7: Lift ---------------------------------------------------------------
        _status("Lifting")
        send_multi_servo_command({3: 2021, 4: 2170, 5: 1121}, lift_ms)
        time.sleep(lift_ms / 1000.0 + 0.1)

        # Step 8: Verify grip via ultrasonic ----------------------------------------
        dist_after = read_ultrasonic_cm()
        dist_after_str = f"{dist_after:.1f}" if dist_after is not None else "None"
        if dist_after is not None and dist_after <= held_dist_cm:
            _status(f"Grip verified (dist={dist_after_str} cm)")
            success = True
        else:
            _status(f"No object detected after lift (dist={dist_after_str} cm) — opening claw")
            open_claw(time_ms=400)
            success = False

        # Step 9: Return home -------------------------------------------------------
        table_detect.set_detection_enabled(False)
        send_servo_command(6, 1500, lift_ms)
        run_pose("home")
        return success


# --- Restored top-level compatibility API ---
def _play_pcm_s16_mono(raw_audio: bytes, sample_rate: int) -> bool:
    """Play raw mono s16le PCM via aplay."""
    try:
        cmd = [
            "aplay",
            "-q",
            "-f",
            "S16_LE",
            "-r",
            str(int(sample_rate)),
            "-c",
            "1",
        ]
        alsa_device = str(getattr(config, "TTS_ALSA_DEVICE", "")).strip()
        if alsa_device:
            cmd[1:1] = ["-D", alsa_device]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.communicate(raw_audio)
        return proc.returncode == 0
    except Exception as exc:
        print(f"[Brain] Audio playback failed: {exc}")
        return False


def _pick_rhubarb_bin() -> str | None:
    candidates = getattr(config, "LIPSYNC_RHUBARB_BIN_CANDIDATES", None)
    if not isinstance(candidates, (list, tuple)) or not candidates:
        candidates = ["/usr/bin/rhubarb", "/usr/local/bin/rhubarb", "rhubarb"]
    for c in candidates:
        path = str(c).strip()
        if not path:
            continue
        if os.path.isabs(path):
            if os.path.exists(path):
                return path
        else:
            try:
                p = subprocess.run(["which", path], capture_output=True, text=True, timeout=1.0)
                if p.returncode == 0 and p.stdout.strip():
                    return path
            except Exception:
                continue
    return None


def _build_rhubarb_cues(pcm: bytes, sample_rate: int):
    if not bool(getattr(config, "LIPSYNC_RHUBARB_ENABLED", True)):
        return None
    rhubarb_bin = _pick_rhubarb_bin()
    if not rhubarb_bin:
        return None

    wav_bytes = _make_wav(pcm, sample_rate)
    timeout_s = max(1.0, float(getattr(config, "LIPSYNC_RHUBARB_TIMEOUT_S", 8.0)))
    recognizer = str(getattr(config, "LIPSYNC_RHUBARB_RECOGNIZER", "phonetic")).strip() or "phonetic"

    wav_path = None
    out_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="robotarm_tts_", suffix=".wav", delete=False) as wf:
            wf.write(wav_bytes)
            wav_path = wf.name
        with tempfile.NamedTemporaryFile(prefix="robotarm_rhubarb_", suffix=".json", delete=False) as of:
            out_path = of.name

        cmd = [
            rhubarb_bin,
            "--machineReadable",
            "--recognizer",
            recognizer,
            "--exportFormat",
            "json",
            "-o",
            out_path,
            wav_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        if proc.returncode != 0:
            return None

        with open(out_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cues = data.get("mouthCues") if isinstance(data, dict) else None
        if not isinstance(cues, list):
            return None

        shaped = []
        for c in cues:
            if not isinstance(c, dict):
                continue
            try:
                start = float(c.get("start", 0.0))
                end = float(c.get("end", start))
                value = str(c.get("value", "X")).strip().upper()[:1] or "X"
                if end < start:
                    end = start
                shaped.append({"start": start, "end": end, "value": value})
            except Exception:
                continue
        return shaped if shaped else None
    except Exception:
        return None
    finally:
        for p in (wav_path, out_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def _push_lipsync_cues_to_lcd(cues, lead_in_ms: int):
    if not cues:
        return
    try:
        import lcd
        lcd.set_lipsync_cues(cues, start_delay_s=max(0.0, float(lead_in_ms) / 1000.0))
    except Exception:
        pass


def _tts_worker_loop():
    while True:
        text = _tts_queue.get()
        if text is None:
            _tts_queue.task_done()
            continue
        try:
            if not bool(getattr(config, "TTS_ENABLED", True)):
                continue

            piper_bin = str(getattr(config, "TTS_PIPER_BIN", "")).strip()
            model_path = _pick_tts_model_path()
            sample_rate = int(getattr(config, "TTS_SAMPLE_RATE", 22050))
            lead_in_ms = int(getattr(config, "TTS_BT_LEAD_IN_MS", 0))

            if (not piper_bin) or (not model_path) or (not os.path.exists(piper_bin)):
                print("[Brain] TTS unavailable (missing piper binary or model)")
                continue

            try:
                import lcd
                lcd.set_emotion("focused")
            except Exception:
                pass
            cmd = [piper_bin, "-m", model_path, "--output-raw"]
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            pcm, _ = proc.communicate(input=str(text).encode("utf-8", errors="ignore"))
            if proc.returncode != 0 or not pcm:
                print("[Brain] Piper synthesis failed")
                continue

            # Build Rhubarb viseme cues from the exact audio that will be played.
            cues = _build_rhubarb_cues(pcm, sample_rate)

            if lead_in_ms > 0:
                silence_samples = int(sample_rate * (lead_in_ms / 1000.0))
                pcm = (b"\x00\x00" * silence_samples) + pcm

            _push_lipsync_cues_to_lcd(cues, lead_in_ms)
            _tts_speaking.set()
            _play_pcm_s16_mono(pcm, sample_rate)
        except Exception as exc:
            print(f"[Brain] TTS worker error: {exc}")
        finally:
            _tts_speaking.clear()
            try:
                import lcd
                lcd.set_emotion("neutral")
            except Exception:
                pass
            _tts_queue.task_done()


def _ensure_tts_worker_started():
    global _tts_worker_started
    with _tts_worker_lock:
        if _tts_worker_started:
            return
        th = threading.Thread(target=_tts_worker_loop, daemon=True)
        th.start()
        _tts_worker_started = True


def is_speaking() -> bool:
    return _tts_speaking.is_set()


def say(text):
    if text is None:
        return False
    try:
        _ensure_tts_worker_started()
        _tts_queue.put_nowait(str(text))
        return True
    except queue.Full:
        print("[Brain] TTS queue full; dropping utterance")
        return False
    except Exception as exc:
        print(f"[Brain] say() failed: {exc}")
        return False


class RobotBrain:
    """Compatibility wrapper for older callers expecting an instance API."""

    def send_servo_command(self, servo_num: int, position: int, time_ms: int = 800) -> bool:
        return send_servo_command(servo_num, position, time_ms)

    def send_multi_servo_command(self, positions: dict, time_ms: int = 800) -> bool:
        return send_multi_servo_command(positions, time_ms)

    def is_wrist_tracking_enabled(self) -> bool:
        return is_wrist_tracking_enabled()

    def set_wrist_tracking_enabled(self, enabled: bool):
        set_wrist_tracking_enabled(enabled)

    def run_pose(self, name: str) -> bool:
        return run_pose(name)

    def open_claw(self, time_ms: int = 800) -> bool:
        return open_claw(time_ms=time_ms)

    def close_claw(self, **kwargs) -> bool:
        return close_claw(**kwargs)

    def say(self, text):
        return say(text)

    def is_speaking(self) -> bool:
        return is_speaking()

    def get_gripper_switch_status(self) -> dict:
        return get_gripper_switch_status()

    def send_to_crestron(self, command: str) -> bool:
        return send_to_crestron(command)

    def start_crestron_server(self) -> bool:
        return start_crestron_server()

    def stop_crestron_server(self):
        stop_crestron_server()


# Initialize gripper switch once module is loaded so close_claw can actually stop
# on switch trigger.
try:
    _init_gripper_switch()
except Exception as _exc:
    print(f"[Brain] Gripper switch init warning: {_exc}")
