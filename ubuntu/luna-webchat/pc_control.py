import re
import threading
from typing import Any

import httpx

from config import WINDOWS_AGENT_TIMEOUT, WINDOWS_AGENT_TOKEN, WINDOWS_AGENT_URL

_pending_pc_actions: dict[str, dict[str, str]] = {}
_pending_pc_actions_lock = threading.Lock()


def _pc_help_text() -> str:
  return (
    "Windows control commands:\n"
    "- /pc run <powershell command>\n"
    "- /pc open <path or url>\n"
    "- /pc read <absolute_path>\n"
    "- /pc write <absolute_path> :: <content>\n"
    "- Robot run <powershell command> on the pc\n"
    "- Robot open <path or url> on the pc\n"
    "- Robot read <absolute_path> on the pc\n"
    "- Robot write <absolute_path> :: <content> on the pc\n"
    "- Robot confirm / Go ahead\n"
    "- Robot cancel / Never mind\n"
    "- /pc cancel\n"
    "Windows actions execute immediately. Confirm/cancel are retained for compatibility with older queued actions."
  )


# Speech-to-text sometimes mis-splits a single program name into two words
# (e.g. "notepad" heard as "note pad"). Only collapse exact, known cases so
# real multi-word paths/URLs are never touched.
_OPEN_TARGET_ALIASES = {
  "note pad": "notepad",
  "note pad.exe": "notepad.exe",
  "power point": "powerpoint",
  "power shell": "powershell",
  "vs code": "code",
  "v s code": "code",
  "task manager": "taskmgr",
  "file explorer": "explorer",
}


def _fix_open_target(target: str) -> str:
  key = target.strip().lower()
  return _OPEN_TARGET_ALIASES.get(key, target)


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
    "go ahead", "robot go ahead", "robot confirm", "confirm",
    "approve", "approved", "robot approve", "yes", "yes please", "do it",
  }:
    return {"verb": "confirm"}
  if normalized_lower in {"never mind", "robot never mind", "robot cancel", "cancel"}:
    return {"verb": "cancel"}

  if lower.startswith("/pc"):
    body = normalized[3:].strip()
    verb, rest = _parse_verb_and_rest(body)
  elif re.match(r"^robot\b", lower):
    body = re.sub(r"^robot\b[\s,.:;!?-]*", "", normalized, flags=re.IGNORECASE).strip()
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
    return {"verb": "open", "target": _fix_open_target(rest.rstrip(" .!?"))}
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
        return "I don't know how to do that."
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
