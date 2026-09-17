import html
import io
import json
import math
import re
import sqlite3
import subprocess
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from config import (
    ASSISTANT_NAME,
    BOOK_MIN_SIMILARITY,
    BOOK_SEMANTIC_SCAN_LIMIT,
    MEMORY_DB_PATH,
    MEMORY_EMBED_MODEL,
    MEMORY_MAX_CHARS,
    MEMORY_MIN_SIMILARITY,
    OLLAMA_URL,
    USER_NAME,
)

_db_lock = threading.Lock()
_memory_conn = sqlite3.connect(MEMORY_DB_PATH, check_same_thread=False)
_memory_conn.row_factory = sqlite3.Row


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_profile(profile: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", (profile or "general").strip().lower())
    cleaned = cleaned.strip("-")
    return cleaned or "general"


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
  allow_book_context = bool(
    re.search(r"\b(book|books|ingested|document|source|chapter|excerpt|guitar|guitars|fret|chord|strings?|dummies)\b", q_lower)
  )

  query_emb = await _embed_text(q)

  if query_emb is None:
    chat_rows = _lexical_memory_fallback(profile_key, q, limit) if allow_chat_memory else []
    book_rows = _book_lexical_fallback(profile_key, q, limit) if allow_book_context else []

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

    if allow_book_context:
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
    else:
      book_rows = []

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
            if sim >= BOOK_MIN_SIMILARITY:
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
