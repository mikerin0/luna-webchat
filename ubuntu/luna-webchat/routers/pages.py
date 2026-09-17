from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _read_static(name: str) -> str:
    return (_STATIC_DIR / name).read_text()


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@router.get("/arm-demo", response_class=HTMLResponse)
def arm_demo_page() -> HTMLResponse:
  return HTMLResponse(content=_read_static("arm_demo.html"), headers=_NO_CACHE_HEADERS)


@router.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse(content=_read_static("index.html"), headers=_NO_CACHE_HEADERS)
