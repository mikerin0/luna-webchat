import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from config import BOOK_CHUNK_OVERLAP, BOOK_CHUNK_SIZE
from memory import _chunk_text, _db_lock, _extract_book_text, _memory_conn, _normalize_profile, _replace_book_chunks

router = APIRouter()


@router.get("/api/memory/profiles")
def memory_profiles() -> dict[str, list[str]]:
  with _db_lock:
    cur = _memory_conn.execute("SELECT DISTINCT profile FROM memories ORDER BY profile ASC")
    profiles = [row[0] for row in cur.fetchall()]
  if "general" not in profiles:
    profiles.insert(0, "general")
  return {"profiles": profiles}


@router.post("/api/ingest-book")
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
