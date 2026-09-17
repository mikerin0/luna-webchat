from typing import Any

import httpx
from fastapi import APIRouter

from config import MODEL, OLLAMA_URL
from system_stats import _server_stats

router = APIRouter()


@router.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@router.get("/api/system/stats")
async def system_stats() -> dict[str, Any]:
  return await _server_stats()


@router.get("/api/models")
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
