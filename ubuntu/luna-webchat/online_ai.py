import re
from typing import Any

import httpx

from config import ONLINE_AI_API_KEY, ONLINE_AI_ENABLED, ONLINE_AI_MODEL, ONLINE_AI_TIMEOUT, ONLINE_AI_URL

_BIG_BROTHER_PHRASE_RE = re.compile(r"\bask\s+(?:your\s+|the\s+)?big\s+brother\b", re.IGNORECASE)

_big_brother_mode = False


def _wants_big_brother(text: str) -> bool:
  return bool(_BIG_BROTHER_PHRASE_RE.search(text or ""))


def get_big_brother_mode() -> bool:
  return _big_brother_mode


def set_big_brother_mode(enabled: bool) -> bool:
  global _big_brother_mode
  _big_brother_mode = bool(enabled)
  return _big_brother_mode


async def _call_online_ai(messages: list[dict[str, Any]]) -> str | None:
  if not ONLINE_AI_ENABLED or not ONLINE_AI_API_KEY:
    return None
  payload = {"model": ONLINE_AI_MODEL, "messages": messages, "stream": False}
  headers = {"Authorization": f"Bearer {ONLINE_AI_API_KEY}"}
  try:
    async with httpx.AsyncClient(timeout=ONLINE_AI_TIMEOUT) as client:
      r = await client.post(ONLINE_AI_URL, json=payload, headers=headers)
    if r.status_code != 200:
      print(f"[BigBrother] online AI error {r.status_code}: {r.text[:300]}")
      return None
    data = r.json()
    return str(data["choices"][0]["message"]["content"]).strip() or None
  except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
    print(f"[BigBrother] online AI request failed: {exc}")
    return None
