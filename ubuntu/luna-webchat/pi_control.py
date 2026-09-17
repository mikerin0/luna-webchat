import asyncio
import json
import re
import shlex
from typing import Any

import httpx

import led_controller
from system_stats import _gpu_usage_percent
from config import (
    LUNA_ARM_GESTURE_EXPLAIN_MIN_CHARS,
    LUNA_ARM_GESTURES_ENABLED,
    LUNA_PI_BEHAVIOR_TIMEOUT,
    LUNA_PI_DETECT_CLOSEUP_TABLE,
    LUNA_PI_DETECT_FRAMES,
    LUNA_PI_DETECT_MIN_CONFIDENCE,
    LUNA_PI_DETECT_MIN_FRAME_HITS,
    LUNA_PI_HEADLESS_CONTROL_TIMEOUT,
    LUNA_PI_HEADLESS_TIMEOUT,
    LUNA_PI_HEADLESS_TOKEN,
    LUNA_PI_HEADLESS_URL,
    LUNA_PI_HOST,
    LUNA_PI_OBJECT_DETECT_SCRIPT,
    LUNA_PI_SSH_KEY,
    LUNA_PI_USER,
    LUNA_PI_VENV,
)

_lcd_gpu_task: asyncio.Task | None = None

_GESTURE_GREETING_RE = re.compile(r"^\s*(hi|hello|hey|yo|good\s+(morning|afternoon|evening))\b", re.IGNORECASE)
_GESTURE_APOLOGY_RE = re.compile(r"\b(sorry|apologi[sz]e|apologies)\b", re.IGNORECASE)
_GESTURE_CONFIRM_RE = re.compile(r"^[\s\"']*(yes|yeah|yep|sure|absolutely|correct|definitely|that.s right)\b", re.IGNORECASE)
_GESTURE_DENY_RE = re.compile(r"^[\s\"']*(no|nope|i can.t|i cannot|i won.t|unfortunately|that.s not)\b", re.IGNORECASE)
_MIC_COMMAND_RE = re.compile(r"\b(?P<action>mute|unmute)\s+(?:the\s+)?(?:mike|mic|microphone)\b", re.IGNORECASE)


def _pi_ssh_prefix() -> str:
  return (
    f"ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i {shlex.quote(LUNA_PI_SSH_KEY)} "
    f"{shlex.quote(LUNA_PI_USER)}@{shlex.quote(LUNA_PI_HOST)}"
  )


def _infer_gesture_intent(user_text: str, reply_text: str) -> str | None:
  """Lightweight, keyword-based mapping from a completed chat turn to a
  conversational arm gesture intent. Deliberately conservative: most short
  factual answers produce no gesture, matching natural non-verbal cadence."""
  reply = (reply_text or "").strip()
  if not reply:
    return None
  if _GESTURE_APOLOGY_RE.search(reply):
    return "apology"
  if _GESTURE_CONFIRM_RE.search(reply):
    return "confirm"
  if _GESTURE_DENY_RE.search(reply):
    return "deny"
  if _GESTURE_GREETING_RE.search(user_text or ""):
    return "greet"
  if len(reply) >= LUNA_ARM_GESTURE_EXPLAIN_MIN_CHARS:
    return "explain"
  return None


async def _pi_headless_request(method: str = "GET", payload: dict[str, Any] | None = None, path: str | None = None, timeout: float | None = None) -> dict[str, Any]:
  if not LUNA_PI_HEADLESS_URL:
    return {"ok": False, "error": "Pi headless URL is not configured."}
  headers = {"Authorization": f"Bearer {LUNA_PI_HEADLESS_TOKEN}"} if LUNA_PI_HEADLESS_TOKEN else {}
  # Actions like arm_power_on/toggle run a multi-step pose on the Pi, so control
  # requests need more headroom than a plain status poll.
  if timeout is None:
    timeout = LUNA_PI_HEADLESS_CONTROL_TIMEOUT if method == "POST" else LUNA_PI_HEADLESS_TIMEOUT
  if path is None:
    path = "/status" if method == "GET" else "/control"
  try:
    async with httpx.AsyncClient(timeout=timeout) as client:
      response = await client.request(method, f"{LUNA_PI_HEADLESS_URL.rstrip('/')}{path}", json=payload, headers=headers)
    if response.status_code not in (200, 409):
      return {"ok": False, "error": f"Pi headless service returned HTTP {response.status_code}."}
    return response.json()
  except (httpx.HTTPError, ValueError) as exc:
    return {"ok": False, "error": f"Pi headless service unreachable: {exc}"}


def _queue_arm_gesture(intent: str, energy: float = 0.4, duration: float = 1.5) -> None:
  if not LUNA_ARM_GESTURES_ENABLED or not LUNA_PI_HEADLESS_URL:
    return
  task = asyncio.create_task(
    _pi_headless_request(
      "POST",
      {"intent": intent, "energy": energy, "duration": duration},
      path="/gesture",
      timeout=LUNA_PI_BEHAVIOR_TIMEOUT,
    )
  )
  task.add_done_callback(lambda completed: completed.exception())


def _lcd_request(prompt: str) -> bool:
  text = (prompt or "").lower()
  return bool(re.search(r"\b(lcd|2[- ]inch|small screen)\b", text))


async def _handle_mic_command(prompt: str) -> str | None:
  match = _MIC_COMMAND_RE.search(prompt or "")
  if match is None:
    return None
  action = match.group("action").lower()
  result = await _pi_headless_request("POST", {"action": action})
  if not result.get("ok", False):
    return f"I couldn't {action} the Pi microphone: {result.get('error', 'Pi control unavailable.')}"
  return f"Pi microphone {action}d."


def _queue_lcd_gpu_monitor() -> None:
  global _lcd_gpu_task
  led_controller.stop_gpu_mode()
  if _lcd_gpu_task is not None and not _lcd_gpu_task.done():
    return

  async def monitor() -> None:
    global _lcd_gpu_task
    last_displayed = None
    try:
      while True:
        usage = _gpu_usage_percent()
        displayed = "GPU N/A" if usage is None else f"GPU {usage:.0f}%"
        if displayed != last_displayed:
          result = await _pi_headless_request(
            "POST",
            {"action": "text", "text": displayed, "duration": -1, "font_size": 72},
          )
          if result.get("ok", True):
            last_displayed = displayed
        await asyncio.sleep(2.0)
    except asyncio.CancelledError:
      pass
    finally:
      _lcd_gpu_task = None

  _lcd_gpu_task = asyncio.create_task(monitor())


def _cancel_lcd_gpu_monitor() -> None:
  global _lcd_gpu_task
  if _lcd_gpu_task is not None and not _lcd_gpu_task.done():
    _lcd_gpu_task.cancel()
  _lcd_gpu_task = None


async def _capture_pi_camera_image(camera_index: int, annotated: bool = False) -> tuple[str | None, str | None]:
    if not LUNA_PI_HOST or not LUNA_PI_USER:
        return None, "Pi camera host is not configured."
    if annotated:
      detector_command = (
        f"source {shlex.quote(LUNA_PI_VENV)} && "
        f"python3 {shlex.quote(LUNA_PI_OBJECT_DETECT_SCRIPT)} "
        f"--camera {int(camera_index)} --frames {LUNA_PI_DETECT_FRAMES} "
        f"--confidence {LUNA_PI_DETECT_MIN_CONFIDENCE} "
        f"--annotated-output /tmp/luna_hailo_annotated_{camera_index}.jpg"
        + (" --closeup" if camera_index == 0 and LUNA_PI_DETECT_CLOSEUP_TABLE else "")
      )
      output_path = f"/tmp/luna_hailo_annotated_{camera_index}.jpg"
      remote_command = (
        f"{detector_command} >/tmp/luna_hailo_detect_{camera_index}.json "
        f"2>/tmp/luna_hailo_detect_{camera_index}.log && "
        f"base64 -w0 {shlex.quote(output_path)}"
      )
    else:
      remote_command = (
        f"source {shlex.quote(LUNA_PI_VENV)} && "
        f"rpicam-jpeg --camera {camera_index} --width 640 --height 360 "
        f"--output /tmp/luna_cam_{camera_index}.jpg --timeout 500 >/tmp/luna_cam_{camera_index}.log 2>&1 && "
        f"base64 -w0 /tmp/luna_cam_{camera_index}.jpg"
      )
    cmd = (
      f"{_pi_ssh_prefix()} {shlex.quote(remote_command)}"
    )
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        return None, "Pi camera capture timed out."
    except Exception as exc:
        return None, f"Failed to contact Pi camera host: {exc}"

    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="ignore").strip() or (stdout or b"").decode("utf-8", errors="ignore").strip()
        return None, f"Pi camera capture failed: {detail or 'unknown error'}"

    data = (stdout or b"").decode("utf-8", errors="ignore").strip()
    if not data:
        return None, "Pi camera returned no image data."
    return data, None


async def _analyze_pi_camera_with_pi_hailo(camera_index: int) -> tuple[str | None, str | None]:
  if not LUNA_PI_HOST or not LUNA_PI_USER:
    return None, "Pi camera host is not configured."

  command = (
    f"source {shlex.quote(LUNA_PI_VENV)} && "
    f"python3 {shlex.quote(LUNA_PI_OBJECT_DETECT_SCRIPT)} "
    f"--camera {int(camera_index)} --frames {LUNA_PI_DETECT_FRAMES} "
    f"--confidence {LUNA_PI_DETECT_MIN_CONFIDENCE}"
    + (" --closeup" if camera_index == 0 and LUNA_PI_DETECT_CLOSEUP_TABLE else "")
  )
  ssh_command = f"{_pi_ssh_prefix()} {shlex.quote(command)}"
  try:
    proc = await asyncio.create_subprocess_shell(
      ssh_command,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
  except asyncio.TimeoutError:
    return None, "Pi Hailo object detection timed out."
  except Exception as exc:
    return None, f"Failed to contact Pi Hailo detector: {exc}"

  raw_output = (stdout or b"").decode("utf-8", errors="ignore").strip()
  if proc.returncode != 0:
    detail = (stderr or b"").decode("utf-8", errors="ignore").strip() or raw_output
    return None, f"Pi Hailo object detection failed: {detail or 'unknown error'}"
  try:
    payload = json.loads(raw_output.splitlines()[-1])
  except (json.JSONDecodeError, IndexError) as exc:
    return None, f"Pi Hailo detector returned invalid data: {exc}"
  if payload.get("error"):
    return None, str(payload["error"])

  detections = [
    item for item in payload.get("detections", [])
    if int(item.get("frames", 0)) >= LUNA_PI_DETECT_MIN_FRAME_HITS
  ]
  if not detections:
    return "The Pi Hailo detector could not reliably identify an object in this frame.", None

  parts = [
    f"{str(item.get('label', 'unknown'))} ({float(item.get('confidence', 0.0)):.0%} confidence)"
    for item in detections
  ]
  response = "The Pi Hailo object detector reports these repeated model detections: " + ", ".join(parts)
  response += ". These are model labels, not a guarantee of the objects' exact identities."
  return response, None


async def _analyze_pi_camera_with_hailo(camera_index: int) -> tuple[str | None, str | None]:
  return await _analyze_pi_camera_with_pi_hailo(camera_index)


async def _legacy_analyze_pi_camera_with_hailo(camera_index: int) -> tuple[str | None, str | None]:
    if not LUNA_PI_HOST or not LUNA_PI_USER:
        return None, "Pi camera host is not configured."

    script_path = "/tmp/luna_camera_analysis.py"
    script_body = (
        "import cv2\n"
        "import numpy as np\n"
        "from pathlib import Path\n"
        f"camera_index = {camera_index}\n"
        "path = Path(\"/tmp/luna_cam_\" + str(camera_index) + \".jpg\")\n"
        "if not path.exists():\n"
        "    print(\"No camera image captured\")\n"
        "    raise SystemExit(1)\n"
        "img = cv2.imread(str(path))\n"
        "if img is None:\n"
        "    print(\"Unable to read captured image\")\n"
        "    raise SystemExit(1)\n"
        "gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)\n"
        "faces = cv2.CascadeClassifier(\"/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml\")\n"
        "profiles = cv2.CascadeClassifier(\"/usr/share/opencv4/haarcascades/haarcascade_profileface.xml\")\n"
        "bodies = cv2.CascadeClassifier(\"/usr/share/opencv4/haarcascades/haarcascade_upperbody.xml\")\n"
        "face_rects = faces.detectMultiScale(gray, 1.1, 4)\n"
        "profile_rects = profiles.detectMultiScale(gray, 1.1, 4)\n"
        "body_rects = bodies.detectMultiScale(gray, 1.1, 4)\n"
        "labels = []\n"
        "if len(face_rects) > 0 or len(profile_rects) > 0 or len(body_rects) > 0:\n"
        "    labels.append(\"a person\")\n"
        "# Use broader image-structure heuristics to suggest a hand or person when the scene has human-like features.\n"
        "hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)\n"
        "hues = hsv[:, :, 0]\n"
        "sats = hsv[:, :, 1]\n"
        "skin_mask = ((hues >= 5) & (hues <= 30)) & (sats > 40)\n"
        "skin_ratio = float(np.mean(skin_mask))\n"
        "edges = cv2.Canny(gray, 50, 150)\n"
        "edge_density = float(np.mean(edges > 0))\n"
        "vertical_edges = np.mean(edges[:, :]) if False else 0.0\n"
        "if skin_ratio > 0.005 and edge_density > 0.03:\n"
        "    labels.append(\"a hand\")\n"
        "if edge_density > 0.08 and skin_ratio > 0.003:\n"
        "    labels.append(\"a person\")\n"
        "# Look for a small bright rectangular object that could be a phone.\n"
        "blur = cv2.GaussianBlur(gray, (5, 5), 0)\n"
        "_, thresh = cv2.threshold(blur, 140, 255, cv2.THRESH_BINARY)\n"
        "contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)\n"
        "phone_like = []\n"
        "for contour in contours:\n"
        "    x, y, w, h = cv2.boundingRect(contour)\n"
        "    if 8 <= w <= 120 and 8 <= h <= 140 and w * h <= 18000:\n"
        "        phone_like.append((w, h))\n"
        "if len(phone_like) > 0:\n"
        "    labels.append(\"a cell phone\")\n"
        "if labels:\n"
        "    description = labels[0]\n"
        "    if len(labels) > 1:\n"
        "        description = labels[0] + \" , \" + labels[1]\n"
        "    if len(labels) > 2:\n"
        "        description = description + \" , \" + labels[2]\n"
        "    label = \"Pi camera \" + str(camera_index + 1) + \" scene: Possible detections only: \" + description + \". I cannot verify these detections reliably from this frame.\"\n"
        "else:\n"
        "    label = \"Pi camera \" + str(camera_index + 1) + \" scene: I cannot reliably identify what is in this frame.\"\n"
        "Path(\"/tmp/luna_cam_\" + str(camera_index) + \".txt\").write_text(label)\n"
        "print(label)\n"
    )
    remote_script = f"cat > {script_path} <<'PY'\n{script_body}PY\npython3 {script_path}\n"

    cmd = (
        f"{_pi_ssh_prefix()} "
        f"'source {shlex.quote(LUNA_PI_VENV)} && rm -f /tmp/luna_cam_{camera_index}.jpg /tmp/luna_cam_{camera_index}.txt {script_path} && "
        f"rpicam-jpeg --camera {camera_index} --width 640 --height 360 --output /tmp/luna_cam_{camera_index}.jpg --timeout 500 >/tmp/luna_cam_{camera_index}.log 2>&1 && "
        f"{remote_script}'"
    )
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as exc:
        return None, f"Failed to contact Pi camera host: {exc}"

    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="ignore").strip() or (stdout or b"").decode("utf-8", errors="ignore").strip()
        return None, f"Pi camera analysis failed: {detail or 'unknown error'}"

    result = (stdout or b"").decode("utf-8", errors="ignore").strip()
    if not result:
        return None, "Pi camera analysis returned no result."
    return result, None


async def _handle_pi_camera_query(last_user: str) -> str | None:
  lower = last_user.strip().lower()
  has_camera_ref = bool(re.search(r"\b(camera|cam|snapshot|photo|picture|image|see|seeing|sees|look|looking|view)\b", lower))
  wants_snapshot = bool(re.search(r"\b(snapshot|photo|picture|image|still|show me|show|view)\b", lower))
  wants_description = bool(re.search(r"\b(describe|what.*see|what.*seeing|what.*sees|look.*at|identify|recognize|analyse|analyze)\b", lower))
  if not has_camera_ref or not (wants_snapshot or wants_description):
    return None

  if "camera 2" in lower or "camera two" in lower or "cam 2" in lower or "cam two" in lower:
    camera_index = 1
  else:
    camera_index = 0

  if wants_snapshot and not wants_description:
    return f"__image__://{camera_index}?annotated=1"

  result, err = await _analyze_pi_camera_with_hailo(camera_index)
  if err:
    if "timed out" in err.lower():
      return f"__image__://{camera_index}?annotated=0"
    return f"I couldn't access the Pi camera feed: {err}"

  if result.startswith("Pi camera") and "scene:" in result:
    return result.split("scene:", 1)[1].strip()
  return result
