import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response

import kokoro_tts
import tts
from config import OPENWEBUI_API_KEY, OPENWEBUI_URL, TRANSCRIBE_LANGUAGE, TTS_ENGINE, WAKE_WORD_VARIANTS
from routers.chat import ChatRequest, Message, chat
from routers.esp32 import _esp32_authorized

router = APIRouter()

_ENGINES = {"piper": tts, "kokoro": kokoro_tts}


def _synthesize(text: str, engine: str | None = None) -> bytes:
  module = _ENGINES.get((engine or TTS_ENGINE).strip().lower())
  if module is None:
    raise HTTPException(status_code=400, detail=f"Unknown TTS engine '{engine}'. Use 'piper' or 'kokoro'.")
  return module.synthesize(text)


@router.get("/api/voicebox/sample")
async def voicebox_sample(
  text: str = "Hi, I'm Luna. This is a quick sample so you can check how I sound.",
  engine: str | None = None,
) -> Response:
  try:
    wav_bytes = _synthesize(text, engine)
  except HTTPException:
    raise
  except Exception as exc:
    raise HTTPException(status_code=502, detail=f"Speech synthesis failed: {exc}") from exc
  return Response(content=wav_bytes, media_type="audio/wav")


async def _transcribe(wav_bytes: bytes) -> str:
  if not OPENWEBUI_API_KEY:
    raise HTTPException(status_code=503, detail="Open WebUI API key is not configured.")
  try:
    async with httpx.AsyncClient(timeout=60) as client:
      r = await client.post(
        f"{OPENWEBUI_URL.rstrip('/')}/api/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {OPENWEBUI_API_KEY}"},
        files={"file": ("talk.wav", wav_bytes, "audio/wav")},
        data={"language": TRANSCRIBE_LANGUAGE},
      )
  except httpx.HTTPError as exc:
    raise HTTPException(status_code=502, detail=f"Transcription service unreachable: {exc}") from exc
  if r.status_code != 200:
    raise HTTPException(status_code=502, detail=f"Transcription failed ({r.status_code}): {r.text[:300]}")
  text = str(r.json().get("text", "")).strip()
  if not text:
    raise HTTPException(status_code=400, detail="No speech detected in the recording.")
  return text


@router.post("/api/esp32/voicebox/wake_check")
async def voicebox_wake_check(request: Request, file: UploadFile) -> dict[str, Any]:
  if not _esp32_authorized(request):
    raise HTTPException(status_code=401, detail="ESP32 authorization required")

  wav_bytes = await file.read()
  if not wav_bytes:
    raise HTTPException(status_code=400, detail="Empty upload.")

  try:
    transcript = await _transcribe(wav_bytes)
  except HTTPException as exc:
    if exc.status_code == 400:
      return {"detected": False, "transcript": ""}
    raise

  lowered = transcript.lower()
  detected = any(re.search(rf"\b{re.escape(v)}\b", lowered) for v in WAKE_WORD_VARIANTS)
  return {"detected": detected, "transcript": transcript}


@router.post("/api/esp32/voicebox/talk")
async def voicebox_talk(request: Request, file: UploadFile) -> Response:
  if not _esp32_authorized(request):
    raise HTTPException(status_code=401, detail="ESP32 authorization required")

  wav_bytes = await file.read()
  if not wav_bytes:
    raise HTTPException(status_code=400, detail="Empty upload.")

  transcript = await _transcribe(wav_bytes)

  chat_req = ChatRequest(messages=[Message(role="user", content=transcript)], profile="voicebox")
  chat_result: dict[str, Any] = await chat(chat_req)
  reply = str(chat_result.get("reply", "")).strip()
  if not reply:
    reply = "I don't have a reply for that."

  try:
    reply_wav = _synthesize(reply)
  except HTTPException:
    raise
  except Exception as exc:
    raise HTTPException(status_code=502, detail=f"Speech synthesis failed: {exc}") from exc

  return Response(content=reply_wav, media_type="audio/wav")
