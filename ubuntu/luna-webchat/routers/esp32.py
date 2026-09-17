import hmac
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import led_controller
from config import ESP32_TOKEN
from online_ai import get_big_brother_mode, set_big_brother_mode
from pi_control import _pi_headless_request
from system_stats import _server_stats

router = APIRouter()

_esp32_gpu_history: deque[float] = deque(maxlen=60)


def _esp32_authorized(request: Request) -> bool:
  supplied = request.headers.get("Authorization", "")
  expected = f"Bearer {ESP32_TOKEN}"
  return bool(ESP32_TOKEN) and hmac.compare_digest(supplied, expected)


@router.get("/api/esp32/status")
async def esp32_status(request: Request) -> dict[str, Any]:
  if not _esp32_authorized(request):
    raise HTTPException(status_code=401, detail="ESP32 authorization required")
  stats = await _server_stats()
  gpu = stats.get("gpu_percent")
  if gpu is not None:
    _esp32_gpu_history.append(float(gpu))
  pi_status = await _pi_headless_request()
  return {
    "ok": True,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "cpu_percent": stats.get("cpu_percent"),
    "gpu_percent": gpu,
    "memory_free_gb": stats.get("memory_free_gb"),
    "gpu_history": list(_esp32_gpu_history),
    "mic_muted": pi_status.get("mic_muted"),
    "speaking": pi_status.get("speaking"),
    "pi_online": bool(pi_status.get("ok")),
    "led_power": led_controller.power_state(),
    "lcd_backlight": pi_status.get("lcd_backlight"),
    "lcd_blanked": pi_status.get("lcd_blanked"),
    "big_brother_mode": get_big_brother_mode(),
  }


@router.post("/api/esp32/action")
async def esp32_action(request: Request) -> dict[str, Any]:
  if not _esp32_authorized(request):
    raise HTTPException(status_code=401, detail="ESP32 authorization required")
  try:
    payload = await request.json()
  except ValueError as exc:
    raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
  action = str(payload.get("action", "")).strip().lower() if isinstance(payload, dict) else ""
  if action in {"big_brother_on", "big_brother_off"}:
    mode = set_big_brother_mode(action == "big_brother_on")
    return {"ok": True, "action": action, "big_brother_mode": mode}
  if action in {"led_on", "led_off"}:
    on = action == "led_on"
    if not await led_controller.set_power(on):
      raise HTTPException(status_code=502, detail="LED sign unavailable")
    return {"ok": True, "action": action, "led_power": led_controller.power_state()}
  if action in {"lcd_on", "lcd_off"}:
    result = await _pi_headless_request("POST", {"action": action})
    if not result.get("ok", False):
      raise HTTPException(status_code=502, detail=result.get("error", "Pi LCD action failed"))
    return {"ok": True, "action": action, "lcd_backlight": result.get("lcd_backlight"), "lcd_blanked": result.get("lcd_blanked")}
  if action not in {"mute", "unmute"}:
    raise HTTPException(status_code=400, detail="Unsupported ESP32 action")
  result = await _pi_headless_request("POST", {"action": action})
  if not result.get("ok", False):
    raise HTTPException(status_code=502, detail=result.get("error", "Pi action failed"))
  return {"ok": True, "action": action, "mic_muted": result.get("mic_muted")}
