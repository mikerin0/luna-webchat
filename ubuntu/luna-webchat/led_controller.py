"""Optional Bluetooth text output for the iPixel LED sign."""

import asyncio
import os
from typing import Any


ENABLED = os.getenv("LUNA_LED_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
DRY_RUN = os.getenv("LUNA_LED_DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "on"}
ADDRESS = os.getenv("LUNA_LED_ADDRESS", "6E:63:BD:E3:20:B5").strip()
COLOR = os.getenv("LUNA_LED_COLOR", "ffffff").strip()
ANIMATION = int(os.getenv("LUNA_LED_ANIMATION", "0"))
SPEED = int(os.getenv("LUNA_LED_SPEED", "80"))
MAX_CHARS = max(1, int(os.getenv("LUNA_LED_MAX_CHARS", "180")))

_lock = asyncio.Lock()


def _display_text_command() -> Any:
    from pypixelcolor.commands.send_text import send_text

    return send_text


async def display_text(text: str) -> bool:
    """Display text on the configured sign, returning False on any device error."""
    message = " ".join(str(text).split())[:MAX_CHARS].strip()
    if not message:
        return False
    if not ENABLED:
        return False
    if DRY_RUN:
        print(f"[LED] dry-run: {message}")
        return True

    async with _lock:
        try:
            from pypixelcolor.lib.device_session import DeviceSession

            async with DeviceSession(ADDRESS) as session:
                await session.execute_command(
                    _display_text_command(),
                    text=message,
                    animation=ANIMATION,
                    speed=SPEED,
                    color=COLOR,
                )
            return True
        except Exception as exc:
            print(f"[LED] display failed: {exc}")
            return False


async def display_reply(reply: str) -> bool:
    return await display_text(reply)