from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import led_controller

router = APIRouter()


class LedTextRequest(BaseModel):
    text: str


class LedProgressRequest(BaseModel):
    percent: float
    label: str = ""


@router.post("/api/led/text")
async def led_text(req: LedTextRequest) -> dict[str, Any]:
  if not led_controller.ENABLED:
    raise HTTPException(status_code=503, detail="LED controller is disabled")
  if not await led_controller.display_text(req.text):
    raise HTTPException(status_code=502, detail="LED sign unavailable")
  return {"ok": True}


@router.post("/api/led/progress")
async def led_progress(req: LedProgressRequest) -> dict[str, Any]:
  if not led_controller.ENABLED:
    raise HTTPException(status_code=503, detail="LED controller is disabled")
  if not await led_controller.display_progress(req.percent, req.label):
    raise HTTPException(status_code=502, detail="LED sign unavailable")
  return {"ok": True}


@router.get("/api/led/status")
async def led_status() -> dict[str, Any]:
  return {"ok": led_controller.ENABLED, "power_state": led_controller.power_state()}


@router.post("/api/led/power")
async def led_power(request: Request) -> dict[str, Any]:
  if not led_controller.ENABLED:
    raise HTTPException(status_code=503, detail="LED controller is disabled")
  try:
    payload = await request.json()
    on = bool(payload.get("on"))
  except (ValueError, AttributeError):
    raise HTTPException(status_code=400, detail="Invalid JSON payload") from None
  if not await led_controller.set_power(on):
    raise HTTPException(status_code=502, detail="LED sign unavailable")
  return {"ok": True, "power_state": led_controller.power_state()}
