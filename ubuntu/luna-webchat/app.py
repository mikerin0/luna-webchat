import os
import re
import io
import json
import math
import base64
import html
import asyncio
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import zipfile
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("LUNA_MODEL", "llama-me:latest")
ASSISTANT_NAME = os.getenv("LUNA_ASSISTANT_NAME", "Luna")
USER_NAME = os.getenv("LUNA_USER_NAME", "Mike")
SYSTEM_PROMPT = os.getenv(
  "LUNA_SYSTEM_PROMPT",
  f"You are {ASSISTANT_NAME}. The user is {USER_NAME}. Always remember these identities. "
  f"If asked your name, answer {ASSISTANT_NAME}. If asked the user's name, answer {USER_NAME}. "
  "Keep replies concise and friendly.",
)
WEB_RESEARCH_ENABLED = os.getenv("WEB_RESEARCH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
WEB_RESEARCH_MAX_RESULTS = int(os.getenv("WEB_RESEARCH_MAX_RESULTS", "3"))
WEB_RESEARCH_TIMEOUT = float(os.getenv("WEB_RESEARCH_TIMEOUT", "20"))
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MEMORY_DB_PATH = os.getenv("LUNA_MEMORY_DB", str(REPOSITORY_ROOT / "luna_memory.db"))
MEMORY_EMBED_MODEL = os.getenv("LUNA_EMBED_MODEL", "nomic-embed-text:latest")
MEMORY_TOP_K = int(os.getenv("LUNA_MEMORY_TOP_K", "6"))
MEMORY_MAX_CHARS = int(os.getenv("LUNA_MEMORY_MAX_CHARS", "1800"))
MEMORY_RECENT_K = int(os.getenv("LUNA_MEMORY_RECENT_K", "6"))
MEMORY_MIN_SIMILARITY = float(os.getenv("LUNA_MEMORY_MIN_SIMILARITY", "0.22"))
BOOK_CHUNK_SIZE = int(os.getenv("LUNA_BOOK_CHUNK_SIZE", "1800"))
BOOK_CHUNK_OVERLAP = int(os.getenv("LUNA_BOOK_CHUNK_OVERLAP", "220"))
BOOK_SEMANTIC_SCAN_LIMIT = int(os.getenv("LUNA_BOOK_SEMANTIC_SCAN_LIMIT", "1200"))
WINDOWS_AGENT_URL = os.getenv("LUNA_WINDOWS_AGENT_URL", "http://172.31.31.11:8787").strip()
WINDOWS_AGENT_TOKEN = os.getenv("LUNA_WINDOWS_AGENT_TOKEN", "").strip()
WINDOWS_AGENT_TIMEOUT = float(os.getenv("LUNA_WINDOWS_AGENT_TIMEOUT", "20"))
ROBOT_CAMERA_URL = os.getenv("LUNA_ROBOT_CAMERA_URL", "http://172.31.31.103:8003/cam.jpg").strip()
LUNA_PI_HOST = os.getenv("LUNA_PI_HOST", "172.31.31.103").strip()
LUNA_PI_USER = os.getenv("LUNA_PI_USER", "arm").strip()
LUNA_PI_PASSWORD = os.getenv("LUNA_PI_PASSWORD", "i82much").strip()
LUNA_PI_VENV = os.getenv("LUNA_PI_VENV", "/home/arm/hailo-rpi5-examples/venv_hailo_rpi_examples/bin/activate").strip()
LUNA_PI_HEADLESS_URL = os.getenv("LUNA_PI_HEADLESS_URL", "http://172.31.31.103:8004").strip()
LUNA_PI_HEADLESS_TOKEN = os.getenv("LUNA_PI_HEADLESS_TOKEN", "").strip()
LUNA_PI_HEADLESS_TIMEOUT = float(os.getenv("LUNA_PI_HEADLESS_TIMEOUT", "8"))
LUNA_PI_HEADLESS_CONTROL_TIMEOUT = float(os.getenv("LUNA_PI_HEADLESS_CONTROL_TIMEOUT", "20"))
LUNA_PI_OBJECT_DETECT_SCRIPT = os.getenv("LUNA_PI_OBJECT_DETECT_SCRIPT", "/home/arm/robotarm/hailo_object_detect.py").strip()
LUNA_PI_DETECT_FRAMES = max(1, int(os.getenv("LUNA_PI_DETECT_FRAMES", "8")))
LUNA_PI_DETECT_MIN_CONFIDENCE = float(os.getenv("LUNA_PI_DETECT_MIN_CONFIDENCE", "0.20"))
LUNA_PI_DETECT_MIN_FRAME_HITS = max(1, int(os.getenv("LUNA_PI_DETECT_MIN_FRAME_HITS", "2")))
LUNA_PI_DETECT_CLOSEUP_TABLE = os.getenv("LUNA_PI_DETECT_CLOSEUP_TABLE", "true").strip().lower() in {"1", "true", "yes", "on"}

app = FastAPI(title="Luna Local Chat")

_db_lock = threading.Lock()
_memory_conn = sqlite3.connect(MEMORY_DB_PATH, check_same_thread=False)
_memory_conn.row_factory = sqlite3.Row
_pending_pc_actions: dict[str, dict[str, str]] = {}
_pending_pc_actions_lock = threading.Lock()

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _assert_local_url(name: str, value: str, *, allow_empty: bool = False) -> None:
    if allow_empty and not value:
        return
    parsed = urlparse(value)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or host not in _LOCAL_HOSTS:
        raise RuntimeError(f"{name} must be a local URL (localhost/127.0.0.1). Got: {value}")


_assert_local_url("OLLAMA_URL", OLLAMA_URL)


class Message(BaseModel):
    role: str
    content: str


class Attachment(BaseModel):
    kind: Literal["text", "image", "docx"]
    name: str
    content: str | None = None
    image_base64: str | None = None


class ChatRequest(BaseModel):
  messages: list[Message]
  attachments: list[Attachment] = []
  web_research: bool = False
  profile: str = "general"
  model: str | None = None


class GenRequest(BaseModel):
    prompt: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_profile(profile: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", (profile or "general").strip().lower())
    cleaned = cleaned.strip("-")
    return cleaned or "general"


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
      f"sshpass -p {shlex.quote(LUNA_PI_PASSWORD)} ssh -o StrictHostKeyChecking=no "
      f"{shlex.quote(LUNA_PI_USER)}@{shlex.quote(LUNA_PI_HOST)} {shlex.quote(remote_command)}"
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
  ssh_command = (
    f"sshpass -p {shlex.quote(LUNA_PI_PASSWORD)} ssh -o StrictHostKeyChecking=no "
    f"{shlex.quote(LUNA_PI_USER)}@{shlex.quote(LUNA_PI_HOST)} {shlex.quote(command)}"
  )
  try:
    proc = await asyncio.create_subprocess_shell(
      ssh_command,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
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
        f"sshpass -p {shlex.quote(LUNA_PI_PASSWORD)} ssh -o StrictHostKeyChecking=no {shlex.quote(LUNA_PI_USER)}@{shlex.quote(LUNA_PI_HOST)} "
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
    if "camera 1" not in lower and "camera one" not in lower and "camera 2" not in lower and "camera two" not in lower:
        return None

    if "snapshot" in lower:
        if "camera 2" in lower or "camera two" in lower:
            camera_index = 1
        else:
            camera_index = 0
        return f"__image__://{camera_index}?annotated=1"

    if "camera 2" in lower or "camera two" in lower:
        camera_index = 1
    else:
        camera_index = 0

    result, err = await _analyze_pi_camera_with_hailo(camera_index)
    if err:
        return f"I couldn't access the Pi camera feed: {err}"

    if result.startswith("Pi camera") and "scene:" in result:
        return result.split("scene:", 1)[1].strip()
    return result


def _init_memory_db() -> None:
    with _db_lock:
        _memory_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _memory_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_profile_created ON memories(profile, created_at DESC)"
        )
        _memory_conn.execute(
          """
          CREATE TABLE IF NOT EXISTS book_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile TEXT NOT NULL,
            source_name TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding TEXT,
            created_at TEXT NOT NULL
          )
          """
        )
        _memory_conn.execute(
          "CREATE INDEX IF NOT EXISTS idx_book_chunks_profile_created ON book_chunks(profile, created_at DESC)"
        )
        _memory_conn.execute(
          "CREATE INDEX IF NOT EXISTS idx_book_chunks_profile_source ON book_chunks(profile, source_name, chunk_index)"
        )
        _memory_conn.commit()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return -1.0
    return dot / (norm_a * norm_b)


async def _embed_text(text: str) -> list[float] | None:
    t = text.strip()
    if not t:
        return None

    payloads = [
        {"model": MEMORY_EMBED_MODEL, "input": t},
        {"model": MEMORY_EMBED_MODEL, "prompt": t},
    ]

    for payload in payloads:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{OLLAMA_URL}/api/embed", json=payload)
            if r.status_code == 404:
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.post(f"{OLLAMA_URL}/api/embeddings", json=payload)
            if r.status_code != 200:
                continue
            data = r.json()
            emb = data.get("embedding")
            if emb is None and isinstance(data.get("embeddings"), list) and data["embeddings"]:
                emb = data["embeddings"][0]
            if isinstance(emb, list) and emb:
                return [float(x) for x in emb]
        except (httpx.HTTPError, ValueError, TypeError):
            continue

    return None


async def _store_memory(profile: str, role: str, content: str) -> None:
    text = (content or "").strip()
    if not text:
        return

    clipped = text[:MEMORY_MAX_CHARS]
    emb = await _embed_text(clipped)
    emb_json = json.dumps(emb) if emb else None

    with _db_lock:
        _memory_conn.execute(
            "INSERT INTO memories(profile, role, content, embedding, created_at) VALUES (?, ?, ?, ?, ?)",
            (profile, role, clipped, emb_json, _now_iso()),
        )
        _memory_conn.commit()


def _lexical_memory_fallback(profile: str, query: str, limit: int) -> list[sqlite3.Row]:
  terms = _query_terms(query, max_terms=8)
  if not terms:
    return []

  like_sql = " OR ".join(["LOWER(content) LIKE ?" for _ in terms])
  params: list[Any] = [profile] + [f"%{w}%" for w in terms] + [limit]
  with _db_lock:
    cur = _memory_conn.execute(
      f"SELECT role, content, created_at FROM memories WHERE profile = ? AND role = 'user' AND ({like_sql}) ORDER BY id DESC LIMIT ?",
      params,
    )
    return list(cur.fetchall())


def _recent_memories(profile: str, limit: int) -> list[sqlite3.Row]:
    with _db_lock:
        cur = _memory_conn.execute(
            "SELECT role, content, created_at FROM memories WHERE profile = ? ORDER BY id DESC LIMIT ?",
            (profile, limit),
        )
        return list(cur.fetchall())


def _merge_memory_rows(primary: list[sqlite3.Row], secondary: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
    merged: list[sqlite3.Row] = []
    seen: set[tuple[str, str, str]] = set()

    for row in primary + secondary:
        content = str(row["content"] or "")
        if not content:
            continue
        key = (str(row["role"]), content, str(row["created_at"]))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
        if len(merged) >= limit:
            break

    return merged


def _recent_chat_memories(profile: str, limit: int) -> list[sqlite3.Row]:
    with _db_lock:
        cur = _memory_conn.execute(
            """
            SELECT role, content, created_at
            FROM memories
            WHERE profile = ? AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT ?
            """,
            (profile, limit),
        )
        return list(cur.fetchall())


def _book_lexical_fallback(profile: str, query: str, limit: int) -> list[sqlite3.Row]:
  terms = _query_terms(query, max_terms=8)
  if not terms:
    return []

  like_sql = " OR ".join(["LOWER(content) LIKE ?" for _ in terms])
  params: list[Any] = [profile] + [f"%{w}%" for w in terms] + [limit]
  with _db_lock:
    cur = _memory_conn.execute(
      f"""
      SELECT source_name, chunk_index, content, created_at
      FROM book_chunks
      WHERE profile = ? AND ({like_sql})
      ORDER BY id DESC
      LIMIT ?
      """,
      params,
    )
    return list(cur.fetchall())


def _html_to_text(raw: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_docx_text(raw: bytes) -> str:
  pieces: list[str] = []
  try:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
      for name in sorted(zf.namelist()):
        lower = name.lower()
        if not lower.startswith("word/") or not lower.endswith(".xml"):
          continue
        if lower.endswith(("styles.xml", "fonttable.xml", "settings.xml", "websettings.xml", "theme/theme1.xml")):
          continue
        xml = zf.read(name).decode("utf-8", errors="ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"</w:tr>", "\n", xml)
        xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
        xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"(?:\s*\n\s*)+", "\n", text)
        text = text.strip()
        if text:
          pieces.append(text)
  except zipfile.BadZipFile:
    return ""

  return "\n\n".join(pieces).strip()


def _query_terms(query: str, max_terms: int = 8) -> list[str]:
    stop = {
        "the", "and", "for", "with", "that", "this", "what", "from", "your", "my", "you", "are",
        "was", "were", "have", "has", "had", "into", "about", "book", "context", "please", "tell",
        "me", "our", "his", "her", "its", "their", "who", "when", "where", "why", "how", "can",
    }
    words = [w for w in re.findall(r"[a-zA-Z0-9]{3,}", query.lower()) if w not in stop]
    if not words:
        words = re.findall(r"[a-zA-Z0-9]{3,}", query.lower())

    ranked = sorted(dict.fromkeys(words), key=lambda w: (-len(w), w))
    return ranked[:max_terms]


def _chunk_text(text: str, size: int, overlap: int) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []

    chunk_size = max(600, size)
    step = max(300, chunk_size - max(0, overlap))
    out: list[str] = []
    i = 0
    while i < len(cleaned):
        chunk = cleaned[i : i + chunk_size].strip()
        if chunk:
            out.append(chunk)
        i += step
    return out


def _extract_epub_text(file_path: Path) -> str:
    pieces: list[str] = []
    with zipfile.ZipFile(file_path) as zf:
        for name in zf.namelist():
            n = name.lower()
            if n.endswith((".xhtml", ".html", ".htm")):
                try:
                    raw = zf.read(name).decode("utf-8", errors="ignore")
                except Exception:
                    continue
                txt = _html_to_text(raw)
                if txt:
                    pieces.append(txt)
    return "\n\n".join(pieces)


def _extract_pdf_text(file_path: Path) -> str:
    # Prefer poppler if available because it is reliable for many PDFs.
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as out:
        out_path = Path(out.name)
    try:
        proc = subprocess.run(
            ["pdftotext", str(file_path), str(out_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and out_path.exists():
            return out_path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        pass
    finally:
        if out_path.exists():
            out_path.unlink(missing_ok=True)

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(file_path))
        parts: list[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n\n".join(parts)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not extract PDF text. Install poppler (pdftotext) or pypdf. "
                f"Details: {exc}"
            ),
        ) from exc


def _extract_via_calibre(file_path: Path) -> str:
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as out:
        out_path = Path(out.name)
    try:
        proc = subprocess.run(
            ["ebook-convert", str(file_path), str(out_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise HTTPException(
                status_code=400,
                detail=f"ebook-convert failed for {file_path.name}: {stderr[:260]}",
            )
        return out_path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail="ebook-convert not found. Install Calibre CLI on this machine.",
        ) from exc
    finally:
        if out_path.exists():
            out_path.unlink(missing_ok=True)


def _extract_book_text(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in {".txt", ".md", ".markdown"}:
        return file_path.read_text(encoding="utf-8", errors="ignore")
    if ext in {".epub"}:
        return _extract_epub_text(file_path)
    if ext in {".pdf"}:
        return _extract_pdf_text(file_path)
    if ext in {".mobi", ".azw", ".azw3", ".fb2", ".rtf"}:
        return _extract_via_calibre(file_path)

    raise HTTPException(
        status_code=400,
        detail="Unsupported format. Use txt, md, epub, pdf, mobi, azw, or azw3.",
    )


async def _replace_book_chunks(profile: str, source_name: str, chunks: list[str]) -> int:
    profile_key = _normalize_profile(profile)
    with _db_lock:
        _memory_conn.execute(
            "DELETE FROM book_chunks WHERE profile = ? AND source_name = ?",
            (profile_key, source_name),
        )
        _memory_conn.commit()

    inserted = 0
    for idx, chunk in enumerate(chunks):
        emb = await _embed_text(chunk)
        emb_json = json.dumps(emb) if emb else None
        with _db_lock:
            _memory_conn.execute(
                """
                INSERT INTO book_chunks(profile, source_name, chunk_index, content, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (profile_key, source_name, idx, chunk, emb_json, _now_iso()),
            )
            _memory_conn.commit()
        inserted += 1

    return inserted


async def _memory_context(profile: str, query: str, limit: int) -> str:
  profile_key = _normalize_profile(profile)
  q = query.strip()
  if not q:
    return ""

  q_lower = q.lower()
  allow_chat_memory = any(
    token in q_lower
    for token in ("remember", "earlier", "before", "previous", "last time", "you said", "i said")
  )
  asks_personal_fact = bool(
    re.search(r"\b(what|which|who|where|when|how)\b.*\bmy\b", q_lower)
    or re.search(r"\bmy\b.*\b(name|favorite|favourite|color|colour|age|birthday|goal|preference)\b", q_lower)
    or re.search(r"\b(i|me)\b.*\b(name|favorite|favourite|color|colour|age|birthday|goal|preference)\b", q_lower)
  )
  allow_chat_memory = allow_chat_memory or asks_personal_fact

  query_emb = await _embed_text(q)

  if query_emb is None:
    chat_rows = _lexical_memory_fallback(profile_key, q, limit) if allow_chat_memory else []
    book_rows = _book_lexical_fallback(profile_key, q, limit)

    snippets: list[str] = []
    chat_cap = max(2, limit // 2)
    q_norm = q_lower
    for r in chat_rows[:chat_cap]:
      if r["content"] and str(r["content"]).strip().lower() != q_norm:
        snippets.append(f"- ({r['created_at']}) {r['role']}: {r['content']}")
    for r in book_rows:
      if r["content"] and len(snippets) < limit:
        snippets.append(
          f"- ({r['created_at']}) book:{r['source_name']}#{r['chunk_index']}: {r['content']}"
        )
    if len(snippets) < limit:
      for r in chat_rows[chat_cap:]:
        if r["content"] and str(r["content"]).strip().lower() != q_norm:
          snippets.append(f"- ({r['created_at']}) {r['role']}: {r['content']}")
        if len(snippets) >= limit:
          break

    if not snippets:
      return ""
    return (
      f"Long-term memory snippets for profile '{profile_key}'. "
      "Use only if relevant and do not invent details:\n" + "\n".join(snippets[:limit])
    )

  with _db_lock:
    if allow_chat_memory:
      mem_cur = _memory_conn.execute(
        "SELECT role, content, embedding, created_at FROM memories WHERE profile = ? AND role = 'user' ORDER BY id DESC LIMIT 700",
        (profile_key,),
      )
      mem_rows = list(mem_cur.fetchall())
    else:
      mem_rows = []

    book_cur = _memory_conn.execute(
      """
      SELECT source_name, chunk_index, content, embedding, created_at
      FROM book_chunks
      WHERE profile = ?
      ORDER BY id DESC
      LIMIT ?
      """,
      (profile_key, BOOK_SEMANTIC_SCAN_LIMIT),
    )
    book_rows = list(book_cur.fetchall())

    scored: list[tuple[float, str]] = []
    for row in mem_rows:
        emb_raw = row["embedding"]
        if not emb_raw:
            continue
        try:
            emb_vec = json.loads(emb_raw)
            if not isinstance(emb_vec, list):
                continue
            sim = _cosine_similarity(query_emb, [float(x) for x in emb_vec])
            if sim >= MEMORY_MIN_SIMILARITY and str(row["content"] or "").strip().lower() != q.lower():
                scored.append((sim, f"- ({row['created_at']}) {row['role']}: {row['content']}"))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue

    for row in book_rows:
        emb_raw = row["embedding"]
        if not emb_raw:
            continue
        try:
            emb_vec = json.loads(emb_raw)
            if not isinstance(emb_vec, list):
                continue
            sim = _cosine_similarity(query_emb, [float(x) for x in emb_vec])
            if sim >= MEMORY_MIN_SIMILARITY:
                scored.append(
                    (
                        sim,
                        f"- ({row['created_at']}) book:{row['source_name']}#{row['chunk_index']}: {row['content']}",
                    )
                )
        except (ValueError, TypeError, json.JSONDecodeError):
            continue

    scored.sort(key=lambda item: item[0], reverse=True)
    snippets: list[str] = []
    seen: set[str] = set()
    for _, line in scored:
        if line in seen:
            continue
        seen.add(line)
        snippets.append(line)
        if len(snippets) >= limit:
            break

    if not snippets:
      if allow_chat_memory:
        chat_rows = _lexical_memory_fallback(profile_key, q, limit)
        q_norm = q_lower
        for r in chat_rows:
          if r["content"] and str(r["content"]).strip().lower() != q_norm:
            snippets.append(f"- ({r['created_at']}) {r['role']}: {r['content']}")
          if len(snippets) >= limit:
            break
      if not snippets:
        return ""

    return (
        f"Long-term memory snippets for profile '{profile_key}'. "
        "Use only if relevant and do not invent details:\n" + "\n".join(snippets)
    )


_init_memory_db()


def _identity_reply(text: str) -> str | None:
    t = text.lower().strip()
    asks_assistant = bool(re.search(r"\b(your name|who are you|what are you called)\b", t))
    asks_user = bool(re.search(r"\b(my name|who am i|do you know my name)\b", t))

    if asks_assistant and asks_user:
        return f"My name is {ASSISTANT_NAME}. Your name is {USER_NAME}."
    if asks_assistant:
        return f"My name is {ASSISTANT_NAME}."
    if asks_user:
        return f"Your name is {USER_NAME}."
    return None


def _clean_html_text(raw: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _decode_duckduckgo_href(href: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/l/?"):
        qs = parse_qs(urlparse(href).query)
        uddg = qs.get("uddg", [""])[0]
        if uddg:
            return unquote(uddg)
    return href


async def _web_research_context(query: str) -> str:
    if not WEB_RESEARCH_ENABLED:
        return ""

    q = query.strip()
    if not q:
        return ""

    search_url = "https://html.duckduckgo.com/html/?" + urlencode({"q": q})
    headers = {"User-Agent": "Mozilla/5.0 (compatible; LunaLocal/1.0)"}

    try:
        async with httpx.AsyncClient(timeout=WEB_RESEARCH_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(search_url, headers=headers)
            r.raise_for_status()
            html = r.text
    except httpx.HTTPError:
        return ""

    results: list[tuple[str, str]] = []
    for match in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.IGNORECASE):
        href, title_html = match.groups()
        url = _decode_duckduckgo_href(href)
        if not url.startswith("http"):
            continue
        title = _clean_html_text(title_html)
        if title:
            results.append((title, url))
        if len(results) >= WEB_RESEARCH_MAX_RESULTS:
            break

    if not results:
        return ""

    findings: list[str] = []
    async with httpx.AsyncClient(timeout=WEB_RESEARCH_TIMEOUT, follow_redirects=True) as client:
        for idx, (title, url) in enumerate(results, start=1):
            snippet = ""
            try:
                page = await client.get(url, headers=headers)
                if page.status_code == 200 and page.text:
                    snippet = _clean_html_text(page.text)[:900]
            except httpx.HTTPError:
                snippet = ""

            if snippet:
                findings.append(f"[{idx}] {title} - {url}\\nSnippet: {snippet}")
            else:
                findings.append(f"[{idx}] {title} - {url}")

    if not findings:
        return ""

    return (
        "Web research findings (from public pages). "
        "Use these as references and cite source URLs in your answer:\n\n"
        + "\n\n".join(findings)
    )


def _read_cpu_times() -> tuple[int, int]:
  with open("/proc/stat", "r", encoding="utf-8") as handle:
    first_line = handle.readline().strip()

  parts = first_line.split()
  if len(parts) < 5 or parts[0] != "cpu":
    raise OSError("/proc/stat did not contain a cpu line")

  values = [int(value) for value in parts[1:]]
  idle = values[3] + (values[4] if len(values) > 4 else 0)
  total = sum(values)
  return total, idle


async def _cpu_usage_percent(sample_delay: float = 0.12) -> float | None:
  try:
    total_1, idle_1 = _read_cpu_times()
    await asyncio.sleep(sample_delay)
    total_2, idle_2 = _read_cpu_times()
  except (OSError, ValueError):
    return None

  total_delta = total_2 - total_1
  idle_delta = idle_2 - idle_1
  if total_delta <= 0:
    return None

  usage = (1.0 - (idle_delta / total_delta)) * 100.0
  return max(0.0, min(100.0, usage))


def _temperature_readings() -> list[dict[str, Any]]:
  readings: list[dict[str, Any]] = []

  def _add_reading(label: str, raw_value: str) -> None:
    try:
      value = float(raw_value.strip())
    except ValueError:
      return
    temp_c = value / 1000.0 if value > 1000 else value
    if temp_c < -30 or temp_c > 150:
      return
    readings.append({"label": label, "celsius": round(temp_c, 1)})

  thermal_root = Path("/sys/class/thermal")
  if thermal_root.exists():
    for zone in sorted(thermal_root.glob("thermal_zone*")):
      temp_file = zone / "temp"
      if not temp_file.exists():
        continue
      label = zone.name
      type_file = zone / "type"
      if type_file.exists():
        label_text = type_file.read_text(encoding="utf-8", errors="ignore").strip()
        if label_text:
          label = label_text
      _add_reading(label, temp_file.read_text(encoding="utf-8", errors="ignore"))

  hwmon_root = Path("/sys/class/hwmon")
  if hwmon_root.exists():
    for hwmon in sorted(hwmon_root.glob("hwmon*")):
      name_file = hwmon / "name"
      chip_name = hwmon.name
      if name_file.exists():
        chip_label = name_file.read_text(encoding="utf-8", errors="ignore").strip()
        if chip_label:
          chip_name = chip_label
      for temp_file in sorted(hwmon.glob("temp*_input")):
        prefix = temp_file.name[:-6]
        label_file = hwmon / f"{prefix}_label"
        label = chip_name
        if label_file.exists():
          label_text = label_file.read_text(encoding="utf-8", errors="ignore").strip()
          if label_text:
            label = f"{chip_name} {label_text}"
        _add_reading(label, temp_file.read_text(encoding="utf-8", errors="ignore"))

  unique: list[dict[str, Any]] = []
  seen: set[tuple[str, float]] = set()
  for row in readings:
    key = (str(row["label"]), float(row["celsius"]))
    if key in seen:
      continue
    seen.add(key)
    unique.append(row)

  unique.sort(key=lambda item: float(item["celsius"]), reverse=True)
  return unique[:5]


async def _server_stats() -> dict[str, Any]:
  cpu = await _cpu_usage_percent()
  disk = shutil.disk_usage("/")
  disk_used_pct = round((disk.used / (disk.total or 1)) * 100.0, 1)
  temps = _temperature_readings()

  load_avg = None
  try:
    load_avg = os.getloadavg()
  except OSError:
    load_avg = None

  uptime_seconds = None
  try:
    uptime_text = Path("/proc/uptime").read_text(encoding="utf-8", errors="ignore").split()[0]
    uptime_seconds = int(float(uptime_text))
  except (OSError, ValueError, IndexError):
    uptime_seconds = None

  return {
    "cpu_percent": None if cpu is None else round(cpu, 1),
    "disk_total_gb": round(disk.total / (1024 ** 3), 1),
    "disk_used_gb": round(disk.used / (1024 ** 3), 1),
    "disk_free_gb": round(disk.free / (1024 ** 3), 1),
    "disk_used_percent": disk_used_pct,
    "temps": temps,
    "load_average": None if load_avg is None else [round(value, 2) for value in load_avg],
    "uptime_seconds": uptime_seconds,
  }


def _server_monitoring_requested(text: str) -> bool:
  q = (text or "").lower().strip()
  if not q:
    return False

  has_stats_words = bool(re.search(r"\b(cpu|disk|storage|temp|temperature|load|uptime|usage|utilization|health|stats?)\b", q))
  has_monitoring_intent = bool(re.search(r"\b(what|how|show|check|watch|monitor|current|right now|status)\b", q))
  has_server_context = bool(re.search(r"\b(server|linux|machine|host|system|this box|this server)\b", q))
  return has_stats_words and (has_monitoring_intent or has_server_context)


def _mem_info_gb() -> tuple[float | None, float | None]:
  total_kb = None
  available_kb = None
  try:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
      if line.startswith("MemTotal:"):
        total_kb = float(line.split()[1])
      elif line.startswith("MemAvailable:"):
        available_kb = float(line.split()[1])
  except OSError:
    return None, None

  total_gb = None if total_kb is None else round(total_kb / (1024 ** 2), 1)
  available_gb = None if available_kb is None else round(available_kb / (1024 ** 2), 1)
  return total_gb, available_gb


def _model_is_too_large_for_host(model_name: str) -> str | None:
  model = (model_name or "").lower()
  if "70b" not in model:
    return None

  total_gb, available_gb = _mem_info_gb()
  if total_gb is None:
    return "This machine does not report enough memory information to safely run a 70B model. Use 8B instead."

  if total_gb < 48 or (available_gb is not None and available_gb < 20):
    avail_text = f"{available_gb} GB available" if available_gb is not None else "unknown available memory"
    return (
      f"{model_name} is too large for this host ({total_gb} GB RAM, {avail_text}). "
      "It will stall or take an impractically long time to respond. Use 8B instead."
    )

  return None


async def _server_stats_context() -> str:
  stats = await _server_stats()
  temps = stats.get("temps") or []
  temp_lines = ", ".join(f"{item['label']}: {item['celsius']} C" for item in temps) if temps else "unavailable"
  load_avg = stats.get("load_average")
  load_text = ", ".join(str(value) for value in load_avg) if load_avg else "unavailable"
  uptime_seconds = stats.get("uptime_seconds")
  uptime_text = f"{uptime_seconds} seconds" if uptime_seconds is not None else "unavailable"

  return (
    "Current Linux server stats for this host. Use these facts directly when answering the user:\n"
    f"- CPU usage: {stats.get('cpu_percent', 'unavailable')}%\n"
    f"- Disk usage on /: {stats.get('disk_used_percent', 'unavailable')}% used "
    f"({stats.get('disk_used_gb', 'unavailable')} GB used / {stats.get('disk_total_gb', 'unavailable')} GB total)\n"
    f"- Temperatures: {temp_lines}\n"
    f"- Load average: {load_text}\n"
    f"- Uptime: {uptime_text}\n"
    "If a value is unavailable, say so plainly instead of guessing."
  )


def _pc_help_text() -> str:
  return (
    "Windows control commands:\n"
    "- /pc run <powershell command>\n"
    "- /pc open <path or url>\n"
    "- /pc read <absolute_path>\n"
    "- /pc write <absolute_path> :: <content>\n"
    "- Luna run <powershell command> on the pc\n"
    "- Luna open <path or url> on the pc\n"
    "- Luna read <absolute_path> on the pc\n"
    "- Luna write <absolute_path> :: <content> on the pc\n"
    "- Luna confirm / Go ahead\n"
    "- Luna cancel / Never mind\n"
    "- /pc cancel\n"
    "Windows actions execute immediately. Confirm/cancel are retained for compatibility with older queued actions."
  )


def _parse_pc_message(message: str) -> dict[str, str] | None:
  text = (message or "").strip()
  lower = text.lower()
  if "camera 1" in lower or "camera one" in lower or "camera 2" in lower or "camera two" in lower:
    return None

  def _parse_verb_and_rest(core: str) -> tuple[str, str]:
    parts = core.split(None, 1)
    if not parts:
      return "help", ""
    verb = parts[0].strip().lower().strip(".,!?;:")
    rest = parts[1].strip() if len(parts) > 1 else ""
    return verb, rest

  normalized = text.strip()
  lower = normalized.lower()

  normalized_lower = re.sub(r"[\s,.:;!?-]+", " ", lower).strip()
  if normalized_lower in {
    "go ahead", "luna go ahead", "luna confirm", "confirm",
    "approve", "approved", "luna approve", "yes", "yes please", "do it",
  }:
    return {"verb": "confirm"}
  if normalized_lower in {"never mind", "luna never mind", "luna cancel", "cancel"}:
    return {"verb": "cancel"}

  if lower.startswith("/pc"):
    body = normalized[3:].strip()
    verb, rest = _parse_verb_and_rest(body)
  elif re.match(r"^luna\b", lower):
    body = re.sub(r"^luna\b[\s,.:;!?-]*", "", normalized, flags=re.IGNORECASE).strip()
    body = re.sub(r"\b(on|in|at)\s+the\s+pc\b", "", body, flags=re.IGNORECASE).strip()
    body = re.sub(r"\b(on|in|at)\s+my\s+pc\b", "", body, flags=re.IGNORECASE).strip()
    body = re.sub(r"\bplease\b", "", body, flags=re.IGNORECASE).strip()
    verb, rest = _parse_verb_and_rest(body)
  else:
    return None

  if verb in {"help", "confirm", "cancel"}:
    return {"verb": verb}
  if verb == "run":
    if not rest:
      return {"verb": "error", "message": "Missing command. Example: /pc run Get-Process | Select-Object -First 5"}
    return {"verb": "run", "command": rest.rstrip(" .!?")}
  if verb == "open":
    if not rest:
      return {"verb": "error", "message": "Missing target. Example: /pc open notepad.exe or /pc open https://example.com"}
    return {"verb": "open", "target": rest.rstrip(" .!?")}
  if verb == "read":
    if not rest:
      return {"verb": "error", "message": "Missing path. Example: /pc read C:\\Users\\Mike\\Documents\\notes.txt"}
    return {"verb": "read", "path": rest.rstrip(" .!?")}
  if verb == "write":
        if "::" not in rest:
            return {"verb": "error", "message": "Use format: /pc write <absolute_path> :: <content>"}
        path, content = rest.split("::", 1)
        path = path.strip()
        content = content.strip()
        if not path:
            return {"verb": "error", "message": "Missing path before ::"}
        return {"verb": "write", "path": path, "content": content}

  return {"verb": "error", "message": f"Unknown /pc action: {verb}"}


def _pc_payload_from_command(command: dict[str, str]) -> dict[str, Any] | None:
    verb = command.get("verb", "")
    if verb == "run":
        return {"action": "run", "command": command["command"]}
    if verb == "open":
        return {"action": "open", "target": command["target"]}
    if verb == "read":
        return {"action": "read", "path": command["path"]}
    if verb == "write":
        return {"action": "write", "path": command["path"], "content": command.get("content", "")}
    return None


async def _execute_pc_payload(payload: dict[str, Any]) -> str:
    if not WINDOWS_AGENT_URL:
        return "Windows agent URL is not configured. Set LUNA_WINDOWS_AGENT_URL."

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if WINDOWS_AGENT_TOKEN:
        headers["Authorization"] = f"Bearer {WINDOWS_AGENT_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=WINDOWS_AGENT_TIMEOUT) as client:
            r = await client.post(f"{WINDOWS_AGENT_URL.rstrip('/')}/action", json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return f"Windows agent unreachable at {WINDOWS_AGENT_URL}: {exc}"

    if r.status_code != 200:
        detail = r.text.strip()[:400]
        return f"Windows agent error {r.status_code}: {detail}"

    data = r.json()
    ok = bool(data.get("ok", False))
    summary = str(data.get("summary", "")).strip()
    output = str(data.get("output", "")).strip()
    if not ok:
        return f"Windows action failed: {summary or output or 'unknown error'}"
    if output:
        if len(output) > 1600:
            output = output[:1600] + "\n...[truncated]"
        return f"Windows action succeeded. {summary}\n\n{output}".strip()
    return f"Windows action succeeded. {summary}".strip()


async def _handle_pc_command(profile: str, last_user: str) -> str | None:
    parsed = _parse_pc_message(last_user)
    if parsed is None:
        return None

    verb = parsed.get("verb", "")
    if verb == "help":
        return _pc_help_text()
    if verb == "error":
        return parsed.get("message", "Invalid /pc command.") + "\n\n" + _pc_help_text()
    if verb == "cancel":
        with _pending_pc_actions_lock:
            _pending_pc_actions.pop(profile, None)
        return "Cancelled pending Windows action."
    if verb == "confirm":
        with _pending_pc_actions_lock:
            pending = _pending_pc_actions.pop(profile, None)
        if not pending:
            return "No pending Windows action to confirm."
        payload = _pc_payload_from_command(pending)
        if payload is None:
            return "Pending Windows action was invalid."
        return await _execute_pc_payload(payload)

    payload = _pc_payload_from_command(parsed)
    if payload is None:
      return "Windows action was invalid."
    return await _execute_pc_payload(payload)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}

@app.get("/api/system/stats")
async def system_stats() -> dict[str, Any]:
  return await _server_stats()


async def _pi_headless_request(method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
  if not LUNA_PI_HEADLESS_URL:
    return {"ok": False, "error": "Pi headless URL is not configured."}
  headers = {"Authorization": f"Bearer {LUNA_PI_HEADLESS_TOKEN}"} if LUNA_PI_HEADLESS_TOKEN else {}
  # Actions like arm_power_on/toggle run a multi-step pose on the Pi, so control
  # requests need more headroom than a plain status poll.
  timeout = LUNA_PI_HEADLESS_CONTROL_TIMEOUT if method == "POST" else LUNA_PI_HEADLESS_TIMEOUT
  try:
    async with httpx.AsyncClient(timeout=timeout) as client:
      response = await client.request(method, f"{LUNA_PI_HEADLESS_URL.rstrip('/')}/status" if method == "GET" else f"{LUNA_PI_HEADLESS_URL.rstrip('/')}/control", json=payload, headers=headers)
    if response.status_code != 200:
      return {"ok": False, "error": f"Pi headless service returned HTTP {response.status_code}."}
    return response.json()
  except (httpx.HTTPError, ValueError) as exc:
    return {"ok": False, "error": f"Pi headless service unreachable: {exc}"}


@app.get("/api/pi/headless/status")
async def pi_headless_status() -> dict[str, Any]:
  return await _pi_headless_request()


@app.post("/api/pi/headless/control")
async def pi_headless_control(request: Request) -> dict[str, Any]:
  try:
    payload = await request.json()
  except ValueError as exc:
    raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
  return await _pi_headless_request("POST", payload if isinstance(payload, dict) else {})


@app.get("/api/robot/camera")
async def robot_camera(request: Request) -> Any:
    cam_param = (request.query_params.get("cam") or "0").strip()
    camera_index = 0
    if cam_param.isdigit():
        camera_index = int(cam_param)

    annotated = (request.query_params.get("annotated") or "").strip().lower() in {"1", "true", "yes", "on"}
    image_b64, err = await _capture_pi_camera_image(camera_index, annotated=annotated)
    if err or not image_b64:
        raise HTTPException(status_code=502, detail=f"Robot camera unreachable: {err or 'no image returned'}")

    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Robot camera payload invalid: {exc}") from exc

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return Response(content=image_bytes, media_type="image/jpeg", headers=headers)


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse(
        content="""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Luna Local Chat+</title>
  <style>
    :root { --bg:#f7f4ef; --card:#fffdf9; --ink:#1f1d1a; --accent:#0f766e; --muted:#6b645c; --warn:#8a4b08; }
    body { margin:0; font-family: ui-serif, Georgia, Cambria, \"Times New Roman\", serif; background: radial-gradient(circle at 10% 10%, #efe7da 0, var(--bg) 45%); color:var(--ink); }
    .wrap { max-width:900px; margin:24px auto; padding:16px; }
    .card { background:var(--card); border:1px solid #e7dece; border-radius:16px; box-shadow:0 12px 30px rgba(0,0,0,.06); overflow:hidden; }
    .head { padding:16px 18px; border-bottom:1px solid #eee2d0; display:flex; justify-content:space-between; align-items:center; }
    .head h1 { font-size:20px; margin:0; letter-spacing:.2px; }
    .badge { font-size:12px; color:#fff; background:var(--accent); padding:4px 8px; border-radius:999px; }
    #log { height:58vh; overflow:auto; padding:16px; display:flex; flex-direction:column; gap:10px; }
    .msg { max-width:82%; padding:10px 12px; border-radius:12px; line-height:1.35; white-space:pre-wrap; }
    .u { align-self:flex-end; background:#e8f8f6; border:1px solid #bce9e3; }
    .a { align-self:flex-start; background:#f3eee5; border:1px solid #e4dacb; }
    .foot { display:grid; gap:10px; padding:14px; border-top:1px solid #eee2d0; }
    .row { display:flex; gap:10px; }
    input, select { flex:1; font-size:16px; padding:11px 12px; border-radius:10px; border:1px solid #d8cdbd; }
    .small { font-size:14px; }
    button { border:0; background:var(--accent); color:#fff; padding:11px 14px; border-radius:10px; font-weight:600; cursor:pointer; }
    button:disabled { opacity:.6; cursor:not-allowed; }
    .ghost { background:#f1ece1; color:#3e362d; border:1px solid #d8cdbd; }
    .pill { display:inline-block; padding:4px 8px; background:#f4efe7; border:1px solid #e4dacb; border-radius:999px; font-size:12px; margin:2px 6px 0 0; }
    .warn { color:var(--warn); }
    .note { color:var(--muted); font-size:13px; margin-top:8px; }
    @media (max-width: 640px) { .row { flex-direction:column; } .msg { max-width:92%; } }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"card\">
      <div class=\"head\"><h1>Luna Chat+</h1><span class=\"badge\">local ollama</span></div>
      <div id=\"log\"></div>
      <form class=\"foot\" id=\"f\">
        <div class=\"row\">
          <input id=\"q\" autocomplete=\"off\" placeholder=\"Ask Luna anything...\" />
          <button class=\"ghost\" id=\"mic\" type=\"button\">Start Mic</button>
          <button id=\"send\" type=\"submit\">Send</button>
        </div>
        <div class=\"row\">
          <label class=\"small\" for=\"profileSelect\" style=\"display:flex;align-items:center;white-space:nowrap;\">Memory profile</label>
          <select class=\"small\" id=\"profileSelect\"></select>
          <button class=\"ghost\" id=\"newProfile\" type=\"button\">New Profile</button>
        </div>
        <div class=\"row\">
          <label class=\"small\" for=\"modelSelect\" style=\"display:flex;align-items:center;white-space:nowrap;\">Model</label>
          <select class=\"small\" id=\"modelSelect\"></select>
        </div>
        <div class=\"row\">
          <span class=\"small\" id=\"piStatus\">Pi headless: checking...</span>
          <button class=\"ghost small\" id=\"piMute\" type=\"button\">Mute Pi Mic</button>
          <button class=\"ghost small\" id=\"piPower\" type=\"button\">Arm Power</button>
        </div>
        <div class=\"row\">
          <input class=\"small\" id=\"files\" type=\"file\" multiple />
          <label class=\"small\" style=\"display:flex;align-items:center;gap:6px;border:1px solid #d8cdbd;border-radius:10px;padding:8px 10px;background:#fff8ec;white-space:nowrap;\">
            <input id=\"webResearch\" type=\"checkbox\" />
            Web research
          </label>
        </div>
        <div class=\"row\">
          <input class=\"small\" id=\"bookFile\" type=\"file\" accept=\".txt,.md,.markdown,.epub,.pdf,.mobi,.azw,.azw3\" />
          <button class=\"ghost\" id=\"ingestBook\" type=\"button\">Ingest Book</button>
        </div>
        <div id=\"attachments\"></div>
      </form>
    </div>
    <div class=\"note\">Pick a memory profile to compartmentalize context and choose a model per chat session. You can use Start Mic for voice input on browsers that support speech recognition (Edge/Chrome). Identity is enforced server-side: assistant=Luna, user=Mike. Web research is opt-in and uses public web pages without paid API keys. Windows control is available via /pc run, /pc open, /pc read, and /pc write. Ask about CPU, disk, load, uptime, or temperature to get Linux server stats.</div>
  </div>

<script>
const log = document.getElementById('log');
const form = document.getElementById('f');
const q = document.getElementById('q');
const micBtn = document.getElementById('mic');
const send = document.getElementById('send');
const fileInput = document.getElementById('files');
const bookFileInput = document.getElementById('bookFile');
const ingestBookBtn = document.getElementById('ingestBook');
const profileSelect = document.getElementById('profileSelect');
const modelSelect = document.getElementById('modelSelect');
const piStatus = document.getElementById('piStatus');
const piMuteBtn = document.getElementById('piMute');
const piPowerBtn = document.getElementById('piPower');
const newProfileBtn = document.getElementById('newProfile');
const webResearch = document.getElementById('webResearch');
const attachmentsBox = document.getElementById('attachments');
let recognition = null;
let micListening = false;
let micFinalTranscript = '';
let chatHistory = [];
let inputHistory = [];
let inputHistoryIndex = -1;
let inputHistoryDraft = '';

async function refreshPiHeadlessStatus() {
  try {
    const r = await fetch('/api/pi/headless/status');
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'offline');
    piStatus.textContent = `Pi headless: ${data.mic_muted ? 'muted' : 'listening'}${data.speaking ? ' / speaking' : ''}`;
    piMuteBtn.textContent = data.mic_muted ? 'Unmute Pi Mic' : 'Mute Pi Mic';
    piPowerBtn.textContent = data.arm_power === true ? 'Arm Power: ON' : data.arm_power === false ? 'Arm Power: OFF' : 'Arm Power: unknown';
  } catch (_) {
    piStatus.textContent = 'Pi headless: offline';
  }
}

async function controlPi(payload) {
  const r = await fetch('/api/pi/headless/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(data.error || 'Pi control failed');
  return data;
}

piMuteBtn.addEventListener('click', async () => {
  try {
    const data = await fetch('/api/pi/headless/status').then(r => r.json());
    await controlPi({ action: data.mic_muted ? 'unmute' : 'mute' });
    await refreshPiHeadlessStatus();
  } catch (err) {
    addSystem('Pi control error: ' + err.message, true);
  }
});

piPowerBtn.addEventListener('click', async () => {
  try {
    await controlPi({ action: 'arm_power_toggle' });
    await refreshPiHeadlessStatus();
  } catch (err) {
    addSystem('Arm power control error: ' + err.message, true);
  }
});

q.addEventListener('keydown', (e) => {
  if ((e.key !== 'ArrowUp' && e.key !== 'ArrowDown') || inputHistory.length === 0) return;

  e.preventDefault();
  if (e.key === 'ArrowUp') {
    if (inputHistoryIndex === -1) inputHistoryDraft = q.value;
    inputHistoryIndex = Math.min(inputHistoryIndex + 1, inputHistory.length - 1);
    q.value = inputHistory[inputHistory.length - 1 - inputHistoryIndex];
  } else {
    inputHistoryIndex -= 1;
    if (inputHistoryIndex < 0) {
      inputHistoryIndex = -1;
      q.value = inputHistoryDraft;
    } else {
      q.value = inputHistory[inputHistory.length - 1 - inputHistoryIndex];
    }
  }
  q.setSelectionRange(q.value.length, q.value.length);
});

q.addEventListener('input', () => {
  inputHistoryIndex = -1;
});

function speechApiCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function setMicUi(listening) {
  micListening = listening;
  micBtn.textContent = listening ? 'Stop Mic' : 'Start Mic';
}

function setupSpeechRecognition() {
  const Ctor = speechApiCtor();
  if (!Ctor) {
    micBtn.disabled = true;
    micBtn.title = 'Speech recognition is not supported in this browser.';
    return;
  }

  if (!window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
    addSystem('Mic note: this page is not in a secure context. Many browsers block speech recognition on non-HTTPS IP URLs.', true);
  }

  if (navigator.permissions && navigator.permissions.query) {
    navigator.permissions.query({ name: 'microphone' }).then((status) => {
      if (status.state === 'denied') {
        addSystem('Microphone permission is denied in browser settings for this site. Allow mic access and reload.', true);
      }
    }).catch(() => {});
  }

  recognition = new Ctor();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = 'en-US';

  recognition.onresult = (event) => {
    if (!micListening) return;
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const text = event.results[i][0].transcript;
      if (event.results[i].isFinal) micFinalTranscript += text + ' ';
      else interim += text;
    }
    const merged = (micFinalTranscript + interim).trim();
    if (merged) q.value = merged;
  };

  recognition.onerror = (event) => {
    const code = String(event.error || 'unknown');
    if (code === 'not-allowed' || code === 'service-not-allowed') {
      addSystem(
        'Mic error: not allowed. In Edge/Chrome, allow microphone permission for this site. If using an IP URL over HTTP, browser security may block speech recognition; use localhost or HTTPS.',
        true
      );
    } else {
      addSystem('Mic error: ' + code, true);
    }
    setMicUi(false);
  };

  recognition.onend = () => {
    setMicUi(false);
  };

  micBtn.addEventListener('click', async () => {
    if (!recognition) return;
    if (micListening) {
      recognition.stop();
      setMicUi(false);
      return;
    }

    try {
      micFinalTranscript = '';
      recognition.start();
      setMicUi(true);
      q.focus();
    } catch (err) {
      addSystem('Could not start mic: ' + err.message, true);
      setMicUi(false);
    }
  });
}

function ensureOption(selectEl, value) {
  if (!value) return;
  const exists = Array.from(selectEl.options).some(opt => opt.value === value);
  if (!exists) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = value;
    selectEl.appendChild(opt);
  }
}

async function loadProfiles() {
  let profiles = ['general'];
  try {
    const r = await fetch('/api/memory/profiles');
    const data = await r.json();
    if (r.ok && Array.isArray(data.profiles) && data.profiles.length) {
      profiles = data.profiles;
    }
  } catch (err) {
    addSystem('Could not load profile list. Using local defaults.', true);
  }

  profileSelect.innerHTML = '';
  for (const p of profiles) {
    const opt = document.createElement('option');
    opt.value = p;
    opt.textContent = p;
    profileSelect.appendChild(opt);
  }

  const savedProfile = (localStorage.getItem('luna_profile') || 'general').trim() || 'general';
  ensureOption(profileSelect, savedProfile);
  profileSelect.value = savedProfile;
}

async function loadModels() {
  let models = [];
  let defaultModel = '';
  const curatedModels = [
    { value: 'llama3.1:70b', label: 'llama3.1:70b (70B preset)' },
  ];
  try {
    const r = await fetch('/api/models');
    const data = await r.json();
    if (r.ok && Array.isArray(data.models)) models = data.models;
    defaultModel = String(data.default_model || '');
  } catch (err) {
    addSystem('Could not load model list from Ollama.', true);
  }

  modelSelect.innerHTML = '';
  for (const m of models) {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    modelSelect.appendChild(opt);
  }

  for (const preset of curatedModels) {
    ensureOption(modelSelect, preset.value);
    const opt = Array.from(modelSelect.options).find(option => option.value === preset.value);
    if (opt) opt.textContent = preset.label;
  }

  const savedModel = (localStorage.getItem('luna_model') || defaultModel || '').trim();
  if (savedModel) ensureOption(modelSelect, savedModel);
  if (modelSelect.options.length === 0) {
    const fallback = defaultModel || 'llama-me:latest';
    ensureOption(modelSelect, fallback);
  }
  modelSelect.value = savedModel || modelSelect.options[0].value;
}

newProfileBtn.addEventListener('click', () => {
  const raw = prompt('New profile name (example: stock-trading, electronics):', 'general');
  if (raw === null) return;
  const value = raw.trim();
  if (!value) return;
  ensureOption(profileSelect, value);
  profileSelect.value = value;
  localStorage.setItem('luna_profile', value);
});

profileSelect.addEventListener('change', () => {
  localStorage.setItem('luna_profile', profileSelect.value || 'general');
});

modelSelect.addEventListener('change', () => {
  localStorage.setItem('luna_model', modelSelect.value || '');
});

ingestBookBtn.addEventListener('click', async () => {
  const file = (bookFileInput.files || [])[0];
  if (!file) {
    addSystem('Pick a book file first (mobi/epub/pdf/txt).', true);
    return;
  }

  ingestBookBtn.disabled = true;
  try {
    const fd = new FormData();
    fd.append('file', file, file.name);
    fd.append('profile', (profileSelect.value || 'general').trim() || 'general');

    const r = await fetch('/api/ingest-book', {
      method: 'POST',
      body: fd
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'book ingestion failed');
    addSystem(`Ingested ${data.source_name} into profile ${data.profile}: ${data.chunks} chunks.`);
    bookFileInput.value = '';
    await loadProfiles();
  } catch (err) {
    addSystem('Ingest error: ' + err.message, true);
  } finally {
    ingestBookBtn.disabled = false;
  }
});

function add(role, content) {
  const d = document.createElement('div');
  d.className = 'msg ' + (role === 'user' ? 'u' : 'a');
  if (typeof content === 'string' && content.startsWith('__image__://')) {
    const snapshot = content.replace('__image__://', '');
    const [cam, query] = snapshot.split('?', 2);
    const img = document.createElement('img');
    img.src = `/api/robot/camera?cam=${encodeURIComponent(cam)}${query ? '&' + query : ''}&t=${Date.now()}`;
    img.alt = 'Camera snapshot';
    img.style.maxWidth = '100%';
    img.style.borderRadius = '10px';
    d.appendChild(img);
  } else {
    d.textContent = content;
  }
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

function addSystem(content, isWarn=false) {
  const d = document.createElement('div');
  d.className = 'msg a';
  if (isWarn) d.classList.add('warn');
  d.textContent = content;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

function ext(name) {
  const parts = name.toLowerCase().split('.');
  return parts.length > 1 ? parts.pop() : '';
}

function renderAttachments() {
  const files = Array.from(fileInput.files || []);
  attachmentsBox.innerHTML = files.length
    ? files.map(f => `<span class=\"pill\">${f.name}</span>`).join('')
    : '';
}

fileInput.addEventListener('change', renderAttachments);

function readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ''));
    r.onerror = () => reject(new Error('text read failed'));
    r.readAsText(file);
  });
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ''));
    r.onerror = () => reject(new Error('image read failed'));
    r.readAsDataURL(file);
  });
}

async function buildAttachments(files) {
  const out = [];
  for (const f of files) {
    const mime = (f.type || '').toLowerCase();
    const extension = ext(f.name);
    if (mime.startsWith('image/')) {
      const dataUrl = await readFileAsDataURL(f);
      const base64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
      out.push({ kind: 'image', name: f.name, image_base64: base64 });
      continue;
    }

    const textLike = mime.startsWith('text/') || ['md','txt','csv','json','yaml','yml','log','py','js','ts','html','css'].includes(extension);
    if (textLike) {
      let text = await readFileAsText(f);
      if (text.length > 12000) text = text.slice(0, 12000) + '\\n...[truncated]';
      out.push({ kind: 'text', name: f.name, content: text });
      continue;
    }

    if (extension === 'docx') {
      const dataUrl = await readFileAsDataURL(f);
      const base64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
      out.push({ kind: 'docx', name: f.name, content: base64 });
      continue;
    }

    addSystem(`Skipped unsupported file: ${f.name}`, true);
  }
  return out;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (micListening && recognition) {
    try { recognition.stop(); } catch (_) {}
    setMicUi(false);
  }
  const text = q.value.trim();
  const files = Array.from(fileInput.files || []);
  if (!text && files.length === 0) return;

  micFinalTranscript = '';
  q.value = '';
  const userText = text || 'Please analyze the attached files.';
  if (text) {
    if (inputHistory[inputHistory.length - 1] !== text) inputHistory.push(text);
    if (inputHistory.length > 50) inputHistory.shift();
    inputHistoryIndex = -1;
    add('user', text);
  } else {
    addSystem('Sent attachments with no text prompt.');
  }
  chatHistory.push({ role: 'user', content: userText });

  send.disabled = true;
  try {
    const attachments = await buildAttachments(files);
    const turnMessages = chatHistory.slice();

    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: turnMessages,
        attachments,
        web_research: !!webResearch.checked,
        profile: (profileSelect.value || 'general').trim() || 'general',
        model: (modelSelect.value || '').trim() || null
      })
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'request failed');
    const reply = data.reply || '';
    add('assistant', reply);
    chatHistory.push({ role: 'assistant', content: reply });
    fileInput.value = '';
    renderAttachments();
  } catch (err) {
    add('assistant', 'Error: ' + err.message);
  } finally {
    send.disabled = false;
    q.focus();
  }
});

loadProfiles();
loadModels();
setupSpeechRecognition();
refreshPiHeadlessStatus();
setInterval(refreshPiHeadlessStatus, 5000);

</script>
</body>
</html>""",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
  profile = _normalize_profile(req.profile)
  selected_model = (req.model or MODEL).strip() or MODEL
  model_guard = _model_is_too_large_for_host(selected_model)
  if model_guard:
    return {"reply": model_guard}
  last_user = ""
  for m in reversed(req.messages):
    if m.role == "user":
      last_user = m.content
      break

  forced = _identity_reply(last_user)
  if forced is not None:
    return {"reply": forced}

  if last_user:
    pi_camera_reply = await _handle_pi_camera_query(last_user)
    if pi_camera_reply is not None:
      await _store_memory(profile, "user", last_user)
      return {"reply": pi_camera_reply}

    pc_reply = await _handle_pc_command(profile, last_user)
    if pc_reply is not None:
      await _store_memory(profile, "user", last_user)
      return {"reply": pc_reply}

  payload_messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {
      "role": "system",
      "content": (
        "Use the earlier turns in this conversation as context when they are relevant to the latest user request. "
        "Treat follow-up questions as continuing the same conversation unless the user clearly asks for a new topic. "
        "Do not ignore prior turns just because they are not the most recent message."
      ),
    },
    {
      "role": "system",
      "content": (
        "You are running inside Mike's local Luna setup, not a generic hosted assistant. "
        "You have a real Pi camera analysis capability for questions about camera 1 or camera 2. "
        "Use only facts explicitly provided by the camera analysis. Treat possible detections as uncertain, never upgrade them into facts, and say plainly when the analysis cannot reliably identify something. "
        "Never guess or invent visual details, names, objects, actions, or other facts. If you do not know, say that you do not know. "
        "You also have a real Windows PC control capability through a companion Windows agent on Mike's network. "
        "When Mike asks about your capabilities, do not deny this integration. "
        "Windows actions execute immediately when Mike requests them; do not claim an action happened unless the Windows agent reports success. "
        "Supported actions include opening apps or URLs, running PowerShell commands, and reading or writing files within allowed Windows folders. "
        "Speech-friendly phrases like 'Luna, open notepad on the PC', 'Luna cancel', and 'Never mind' are valid control phrases. "
        "Persistent conversation memory is enabled. Use the supplied memory context and recent memories when answering questions about what you remember, and say that you remember relevant past conversations when memory context supports it."
      ),
    },
  ]
  text_ctx: list[str] = []
  image_ctx: list[str] = []
  research_ctx = ""

  if last_user:
    memory_ctx = await _memory_context(profile, last_user, MEMORY_TOP_K)
    if memory_ctx:
      payload_messages.append({"role": "system", "content": memory_ctx})

    if last_user and _server_monitoring_requested(last_user):
      stats_ctx = await _server_stats_context()
      if stats_ctx:
        payload_messages.append({"role": "system", "content": stats_ctx})
        payload_messages.append(
          {
            "role": "system",
            "content": (
              "You have current Linux server stats in the system context for this turn. "
              "Use them directly when answering questions about CPU, disk usage, temperature, load, uptime, or host health. "
              "If a value is unavailable, say so plainly instead of guessing."
            ),
          }
        )

  if req.web_research and last_user:
    research_ctx = await _web_research_context(last_user)
    if research_ctx:
      payload_messages.append({"role": "system", "content": research_ctx})
    payload_messages.append(
      {
        "role": "system",
        "content": (
          "You have already been provided current web findings in the system context for this turn. "
          "Do not say you cannot access or browse the internet. "
          "Answer using the provided findings and cite source URLs you used."
        ),
      }
    )

  for a in req.attachments:
    if a.kind == "text" and a.content:
      text_ctx.append(f"FILE: {a.name}\\n{a.content}")
    if a.kind == "docx" and a.content:
      try:
        raw = base64.b64decode(a.content)
      except (ValueError, TypeError):
        raw = b""
      docx_text = _extract_docx_text(raw)
      if docx_text:
        if len(docx_text) > 12000:
          docx_text = docx_text[:12000] + "\n...[truncated]"
        text_ctx.append(f"FILE: {a.name}\n{docx_text}")
    if a.kind == "image" and a.image_base64:
      image_ctx.append(a.image_base64)

  if text_ctx:
    payload_messages.append(
      {
        "role": "system",
        "content": "User supplied file context follows. Use it as reference:\\n\\n" + "\\n\\n".join(text_ctx),
      }
    )

  converted = [m.model_dump() for m in req.messages]
  if image_ctx:
    last_user_idx = -1
    for i in range(len(converted) - 1, -1, -1):
      if converted[i]["role"] == "user":
        last_user_idx = i
        break
    if last_user_idx >= 0:
      converted[last_user_idx]["images"] = image_ctx

  payload_messages.extend(converted)

  payload = {
    "model": selected_model,
    "messages": payload_messages,
    "stream": False,
  }

  try:
    async with httpx.AsyncClient(timeout=180) as client:
      r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
  except httpx.HTTPError as exc:
    raise HTTPException(status_code=502, detail=f"Ollama unreachable: {exc}") from exc

  if r.status_code != 200:
    raise HTTPException(status_code=502, detail=f"Ollama error {r.status_code}: {r.text[:300]}")

  data = r.json()
  reply = data.get("message", {}).get("content", "")

  # One-shot correction path: if research was supplied but the model still claims
  # it cannot browse, force a retry with explicit instruction.
  if research_ctx and re.search(
    r"\b(cannot|can't|do not|don't)\b.{0,40}\b(access|browse)\b.{0,20}\binternet\b",
    reply.lower(),
  ):
    retry_messages = list(payload_messages)
    retry_messages.append(
      {
        "role": "system",
        "content": (
          "Correction: web findings were already retrieved and provided above. "
          "Do not mention inability to access internet. "
          "Provide the best answer from those findings and include source URLs."
        ),
      }
    )
    retry_payload = {
      "model": selected_model,
      "messages": retry_messages,
      "stream": False,
    }
    try:
      async with httpx.AsyncClient(timeout=180) as client:
        rr = await client.post(f"{OLLAMA_URL}/api/chat", json=retry_payload)
      if rr.status_code == 200:
        retry_data = rr.json()
        retry_reply = retry_data.get("message", {}).get("content", "").strip()
        if retry_reply:
          reply = retry_reply
    except httpx.HTTPError:
      pass

  if last_user:
    await _store_memory(profile, "user", last_user)

  return {"reply": reply}


@app.get("/api/memory/profiles")
def memory_profiles() -> dict[str, list[str]]:
  with _db_lock:
    cur = _memory_conn.execute("SELECT DISTINCT profile FROM memories ORDER BY profile ASC")
    profiles = [row[0] for row in cur.fetchall()]
  if "general" not in profiles:
    profiles.insert(0, "general")
  return {"profiles": profiles}


@app.get("/api/models")
async def models() -> dict[str, Any]:
  model_names: list[str] = []

  try:
    async with httpx.AsyncClient(timeout=20) as client:
      r = await client.get(f"{OLLAMA_URL}/api/tags")
    if r.status_code == 200:
      payload = r.json()
      for item in payload.get("models", []):
        name = str(item.get("name", "")).strip()
        if name:
          model_names.append(name)
  except httpx.HTTPError:
    pass

  if MODEL not in model_names:
    model_names.insert(0, MODEL)

  seen: set[str] = set()
  deduped: list[str] = []
  for name in model_names:
    if name in seen:
      continue
    seen.add(name)
    deduped.append(name)

  return {"models": deduped, "default_model": MODEL}


@app.post("/api/ingest-book")
async def ingest_book(
    file: UploadFile = File(...),
    profile: str = Form("general"),
) -> dict[str, Any]:
  profile_key = _normalize_profile(profile)
  source_name = Path(file.filename or "book").name
  if not source_name:
    raise HTTPException(status_code=400, detail="Missing file name.")

  ext = Path(source_name).suffix.lower()
  allowed = {".txt", ".md", ".markdown", ".epub", ".pdf", ".mobi", ".azw", ".azw3"}
  if ext not in allowed:
    raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

  raw = await file.read()
  if not raw:
    raise HTTPException(status_code=400, detail="Uploaded file is empty.")
  if len(raw) > 40 * 1024 * 1024:
    raise HTTPException(status_code=400, detail="File too large. Keep uploads under 40MB.")

  with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
    temp_path = Path(tmp.name)
    tmp.write(raw)

  try:
    text = _extract_book_text(temp_path)
  finally:
    temp_path.unlink(missing_ok=True)

  chunks = _chunk_text(text, BOOK_CHUNK_SIZE, BOOK_CHUNK_OVERLAP)
  if not chunks:
    raise HTTPException(status_code=400, detail="No readable text found in uploaded file.")

  inserted = await _replace_book_chunks(profile_key, source_name, chunks)
  return {
    "ok": True,
    "profile": profile_key,
    "source_name": source_name,
    "chunks": inserted,
    "characters": len(text),
  }
