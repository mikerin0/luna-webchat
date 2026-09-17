import base64
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from config import LUNA_PI_BEHAVIOR_TIMEOUT
from pi_control import _capture_pi_camera_image, _pi_headless_request

router = APIRouter()


@router.get("/api/pi/headless/status")
async def pi_headless_status() -> dict[str, Any]:
  return await _pi_headless_request()


@router.post("/api/pi/headless/control")
async def pi_headless_control(request: Request) -> dict[str, Any]:
  try:
    payload = await request.json()
  except ValueError as exc:
    raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
  return await _pi_headless_request("POST", payload if isinstance(payload, dict) else {})


@router.post("/api/pi/behavior/run")
async def pi_behavior_run(request: Request) -> dict[str, Any]:
  try:
    payload = await request.json()
  except ValueError as exc:
    raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
  return await _pi_headless_request(
    "POST", payload if isinstance(payload, dict) else {}, path="/behavior/run", timeout=LUNA_PI_BEHAVIOR_TIMEOUT
  )


@router.post("/api/pi/gesture")
async def pi_gesture(request: Request) -> dict[str, Any]:
  try:
    payload = await request.json()
  except ValueError as exc:
    raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
  return await _pi_headless_request(
    "POST", payload if isinstance(payload, dict) else {}, path="/gesture", timeout=LUNA_PI_BEHAVIOR_TIMEOUT
  )


@router.post("/api/pi/behavior/receive_object")
async def pi_receive_object(request: Request) -> dict[str, Any]:
  try:
    payload = await request.json()
  except ValueError as exc:
    raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
  return await _pi_headless_request(
    "POST", payload if isinstance(payload, dict) else {}, path="/behavior/receive_object", timeout=LUNA_PI_BEHAVIOR_TIMEOUT
  )


@router.post("/api/pi/behavior/return_object")
async def pi_return_object(request: Request) -> dict[str, Any]:
  try:
    payload = await request.json()
  except ValueError as exc:
    raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
  return await _pi_headless_request(
    "POST", payload if isinstance(payload, dict) else {}, path="/behavior/return_object", timeout=LUNA_PI_BEHAVIOR_TIMEOUT
  )


@router.get("/api/robot/camera")
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
