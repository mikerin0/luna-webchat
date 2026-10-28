import os
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

APP_HOST = os.getenv("WINDOWS_AGENT_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("WINDOWS_AGENT_PORT", "8787"))
AUTH_TOKEN = os.getenv("WINDOWS_AGENT_TOKEN", "").strip()
COMMAND_TIMEOUT = int(os.getenv("WINDOWS_AGENT_COMMAND_TIMEOUT", "20"))
MAX_OUTPUT_CHARS = int(os.getenv("WINDOWS_AGENT_MAX_OUTPUT", "4000"))

# Comma-separated absolute paths that file read/write actions are allowed to touch.
raw_roots = os.getenv("WINDOWS_AGENT_ALLOWED_ROOTS", "")
ALLOWED_ROOTS = [Path(p.strip()).resolve() for p in raw_roots.split(",") if p.strip()]
if not ALLOWED_ROOTS:
    default_root = Path.home() / "Documents"
    ALLOWED_ROOTS = [default_root.resolve()]


app = FastAPI(title="Luna Windows Agent")


class ActionRequest(BaseModel):
    action: Literal["run", "open", "read", "write", "display"]
    command: str | None = None
    target: str | None = None
    path: str | None = None
    content: str | None = None
    text: str | None = None
    image_base64: str | None = None
    duration: float | None = None


class ActionResponse(BaseModel):
    ok: bool
    summary: str
    output: str = ""


def _check_auth(auth_header: str | None) -> None:
    if not AUTH_TOKEN:
        return
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth_header.removeprefix("Bearer ").strip()
    if token != AUTH_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n...[truncated]"


def _assert_allowed_path(value: str) -> Path:
    if not value:
        raise HTTPException(status_code=400, detail="Path is required")

    p = Path(value).expanduser().resolve()
    for root in ALLOWED_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue

    roots = ", ".join(str(r) for r in ALLOWED_ROOTS)
    raise HTTPException(status_code=403, detail=f"Path is outside allowed roots: {roots}")


def _show_text_popup(text: str, duration_s: float) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("Luna")
    root.attributes("-topmost", True)
    root.configure(bg="black")
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    win_w, win_h = min(900, screen_w - 100), min(400, screen_h - 100)
    root.geometry(f"{win_w}x{win_h}+{(screen_w - win_w) // 2}+{(screen_h - win_h) // 2}")
    tk.Label(
        root,
        text=text,
        font=("Segoe UI", 28, "bold"),
        fg="white",
        bg="black",
        wraplength=win_w - 60,
        justify="center",
    ).pack(expand=True, fill="both", padx=20, pady=20)
    root.after(max(1000, int(duration_s * 1000)), root.destroy)
    root.mainloop()


def _show_image_popup(image_base64: str, caption: str, duration_s: float) -> None:
    import base64
    import io

    import tkinter as tk
    from PIL import Image, ImageTk

    pil_image = Image.open(io.BytesIO(base64.b64decode(image_base64)))

    root = tk.Tk()
    root.title("Luna")
    root.attributes("-topmost", True)
    root.configure(bg="black")

    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    max_w, max_h = min(1000, screen_w - 100), min(700, screen_h - 150)
    pil_image.thumbnail((max_w, max_h), Image.LANCZOS)
    photo = ImageTk.PhotoImage(pil_image)

    win_w = pil_image.width + 40
    win_h = pil_image.height + (70 if caption else 40)
    root.geometry(f"{win_w}x{win_h}+{(screen_w - win_w) // 2}+{(screen_h - win_h) // 2}")

    img_label = tk.Label(root, image=photo, bg="black")
    img_label.image = photo  # keep a reference -- tkinter doesn't hold one itself
    img_label.pack(padx=20, pady=(20, 10 if caption else 20))

    if caption:
        tk.Label(
            root,
            text=caption,
            font=("Segoe UI", 16),
            fg="white",
            bg="black",
            wraplength=win_w - 40,
            justify="center",
        ).pack(pady=(0, 15))

    root.after(max(1000, int(duration_s * 1000)), root.destroy)
    root.mainloop()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "allowed_roots": [str(r) for r in ALLOWED_ROOTS]}


@app.post("/action", response_model=ActionResponse)
def action(req: ActionRequest, authorization: str | None = Header(default=None)) -> ActionResponse:
    _check_auth(authorization)

    if req.action == "run":
        cmd = (req.command or "").strip()
        if not cmd:
            raise HTTPException(status_code=400, detail="command is required for run")

        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        out = _clip(out.strip())
        return ActionResponse(
            ok=proc.returncode == 0,
            summary=f"Ran PowerShell command (exit={proc.returncode}).",
            output=out,
        )

    if req.action == "open":
        target = (req.target or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="target is required for open")
        os.startfile(target)  # type: ignore[attr-defined]
        return ActionResponse(ok=True, summary=f"Opened: {target}")

    if req.action == "read":
        p = _assert_allowed_path((req.path or "").strip())
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Not found: {p}")
        if not p.is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {p}")
        text = p.read_text(encoding="utf-8", errors="ignore")
        return ActionResponse(ok=True, summary=f"Read file: {p}", output=_clip(text))

    if req.action == "write":
        p = _assert_allowed_path((req.path or "").strip())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(req.content or "", encoding="utf-8")
        return ActionResponse(ok=True, summary=f"Wrote file: {p}")

    if req.action == "display":
        text = (req.text or "").strip()
        duration = max(3.0, min(120.0, float(req.duration) if req.duration is not None else 20.0))
        if req.image_base64:
            threading.Thread(
                target=_show_image_popup, args=(req.image_base64, text, duration), daemon=True
            ).start()
            return ActionResponse(ok=True, summary=f"Displaying image on PC screen for {duration:.0f}s.")
        if not text:
            raise HTTPException(status_code=400, detail="text or image_base64 is required for display")
        threading.Thread(target=_show_text_popup, args=(text, duration), daemon=True).start()
        return ActionResponse(ok=True, summary=f"Displaying message on PC screen for {duration:.0f}s.")

    raise HTTPException(status_code=400, detail=f"Unsupported action: {req.action}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("windows_agent:app", host=APP_HOST, port=APP_PORT, reload=False)
