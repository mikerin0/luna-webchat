import os
from pathlib import Path
from urllib.parse import urlparse

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
BOOK_MIN_SIMILARITY = float(os.getenv("LUNA_BOOK_MIN_SIMILARITY", "0.45"))
BOOK_CHUNK_SIZE = int(os.getenv("LUNA_BOOK_CHUNK_SIZE", "1800"))
BOOK_CHUNK_OVERLAP = int(os.getenv("LUNA_BOOK_CHUNK_OVERLAP", "220"))
BOOK_SEMANTIC_SCAN_LIMIT = int(os.getenv("LUNA_BOOK_SEMANTIC_SCAN_LIMIT", "1200"))
WINDOWS_AGENT_URL = os.getenv("LUNA_WINDOWS_AGENT_URL", "http://172.31.31.11:8787").strip()
WINDOWS_AGENT_TOKEN = os.getenv("LUNA_WINDOWS_AGENT_TOKEN", "").strip()
WINDOWS_AGENT_TIMEOUT = float(os.getenv("LUNA_WINDOWS_AGENT_TIMEOUT", "20"))
ROBOT_CAMERA_URL = os.getenv("LUNA_ROBOT_CAMERA_URL", "http://172.31.31.103:8003/cam.jpg").strip()
LUNA_PI_HOST = os.getenv("LUNA_PI_HOST", "172.31.31.103").strip()
LUNA_PI_USER = os.getenv("LUNA_PI_USER", "arm").strip()
LUNA_PI_SSH_KEY = os.path.expanduser(os.getenv("LUNA_PI_SSH_KEY", "~/.ssh/id_rsa_pi").strip())
LUNA_PI_VENV = os.getenv("LUNA_PI_VENV", "/home/arm/hailo-rpi5-examples/venv_hailo_rpi_examples/bin/activate").strip()
LUNA_PI_HEADLESS_URL = os.getenv("LUNA_PI_HEADLESS_URL", "http://172.31.31.103:8004").strip()
LUNA_PI_HEADLESS_TOKEN = os.getenv("LUNA_PI_HEADLESS_TOKEN", "").strip()
LUNA_PI_HEADLESS_TIMEOUT = float(os.getenv("LUNA_PI_HEADLESS_TIMEOUT", "8"))
LUNA_PI_HEADLESS_CONTROL_TIMEOUT = float(os.getenv("LUNA_PI_HEADLESS_CONTROL_TIMEOUT", "20"))
LUNA_PI_BEHAVIOR_TIMEOUT = float(os.getenv("LUNA_PI_BEHAVIOR_TIMEOUT", "35"))
LUNA_PI_OBJECT_DETECT_SCRIPT = os.getenv("LUNA_PI_OBJECT_DETECT_SCRIPT", "/home/arm/robotarm/hailo_object_detect.py").strip()
LUNA_PI_DETECT_FRAMES = max(1, int(os.getenv("LUNA_PI_DETECT_FRAMES", "8")))
LUNA_PI_DETECT_MIN_CONFIDENCE = float(os.getenv("LUNA_PI_DETECT_MIN_CONFIDENCE", "0.20"))
LUNA_PI_DETECT_MIN_FRAME_HITS = max(1, int(os.getenv("LUNA_PI_DETECT_MIN_FRAME_HITS", "2")))
LUNA_PI_DETECT_CLOSEUP_TABLE = os.getenv("LUNA_PI_DETECT_CLOSEUP_TABLE", "true").strip().lower() in {"1", "true", "yes", "on"}
LUNA_ARM_GESTURES_ENABLED = os.getenv("LUNA_ARM_GESTURES_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
LUNA_ARM_GESTURE_EXPLAIN_MIN_CHARS = int(os.getenv("LUNA_ARM_GESTURE_EXPLAIN_MIN_CHARS", "220"))
ESP32_TOKEN = os.getenv("LUNA_ESP32_TOKEN", "").strip()
ONLINE_AI_ENABLED = os.getenv("LUNA_ONLINE_AI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
ONLINE_AI_URL = os.getenv("LUNA_ONLINE_AI_URL", "https://api.openai.com/v1/chat/completions").strip()
ONLINE_AI_API_KEY = os.getenv("LUNA_ONLINE_AI_API_KEY", "").strip()
ONLINE_AI_MODEL = os.getenv("LUNA_ONLINE_AI_MODEL", "gpt-4o-mini").strip()
ONLINE_AI_TIMEOUT = float(os.getenv("LUNA_ONLINE_AI_TIMEOUT", "30"))

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _assert_local_url(name: str, value: str, *, allow_empty: bool = False) -> None:
    if allow_empty and not value:
        return
    parsed = urlparse(value)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or host not in _LOCAL_HOSTS:
        raise RuntimeError(f"{name} must be a local URL (localhost/127.0.0.1). Got: {value}")


_assert_local_url("OLLAMA_URL", OLLAMA_URL)
