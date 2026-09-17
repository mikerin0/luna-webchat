"""Optional Bluetooth text output for the iPixel LED sign."""

import asyncio
import time
import os
import random
import re
import subprocess
import tempfile
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx


ENABLED = os.getenv("LUNA_LED_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
DRY_RUN = os.getenv("LUNA_LED_DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "on"}
ADDRESS = os.getenv("LUNA_LED_ADDRESS", "6E:63:BD:E3:20:B5").strip()
COLOR = os.getenv("LUNA_LED_COLOR", "ffffff").strip()
ANIMATION = int(os.getenv("LUNA_LED_ANIMATION", "0"))
SPEED = int(os.getenv("LUNA_LED_SPEED", "80"))
MAX_CHARS = max(1, int(os.getenv("LUNA_LED_MAX_CHARS", "180")))
IMAGES_ENABLED = os.getenv("LUNA_LED_IMAGES_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
IMAGE_SECONDS = max(1.0, float(os.getenv("LUNA_LED_IMAGE_SECONDS", "4")))
DEFAULT_LABEL = os.getenv("LUNA_LED_DEFAULT_LABEL", "Luna").strip() or "Luna"
TOPIC_RAINBOW_MODE = max(0, min(9, int(os.getenv("LUNA_LED_TOPIC_RAINBOW_MODE", "4"))))
TEXT_LINES = max(1, min(2, int(os.getenv("LUNA_LED_TEXT_LINES", "2"))))
TEXT_FONT_HEIGHT_RATIO = max(0.25, min(0.55, float(os.getenv("LUNA_LED_TEXT_FONT_HEIGHT_RATIO", "0.44"))))
PIXEL_ICON_CHANCE = max(0.0, min(1.0, float(os.getenv("LUNA_LED_PIXEL_ICON_CHANCE", "0.30"))))
CLEAR_AFTER_S = max(0.0, float(os.getenv("LUNA_LED_CLEAR_AFTER_S", "60")))
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
NASDAQ_API = "https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC"
LOCAL_WEATHER_API = "https://wttr.in/?format=j1"
CLOCK_TIME_API = "https://worldtimeapi.org/api/timezone/America/New_York"
CLOCK_TIME_API_FALLBACK = "https://timeapi.io/api/Time/current/zone?timeZone=America/New_York"
CLOCK_RESYNC_S = max(30.0, float(os.getenv("LUNA_LED_CLOCK_RESYNC_S", "300")))
NY_TZ = ZoneInfo("America/New_York")
GREEN = "00ff00"
RED = "ff0000"
BLUE = "00aaff"
YELLOW = "ffff00"
_LED_REQUEST_RE = re.compile(r"\b(?:show|display|put|send)\b.*\b(?:on|to)\s+(?:the\s+)?(?:led|sign)\b", re.IGNORECASE)
_DISPLAY_REQUEST_RE = re.compile(r"\b(?:show|display|put|send)\b.*\b(?:on|to)\s+(?:the\s+)?(?:led|sign|lcd|screen)\b", re.IGNORECASE)

_lock = asyncio.Lock()
_power_state: bool | None = None
_clear_task: asyncio.Task | None = None
_clock_task: asyncio.Task | None = None
_gpu_task: asyncio.Task | None = None
_last_text_frame = None
_clock_base_epoch: float | None = None
_clock_base_monotonic: float | None = None
_clock_last_sync_monotonic: float = 0.0


def _display_text_command() -> Any:
    from pypixelcolor.commands.send_text import send_text

    return send_text


def _rgb_from_hex(color: str) -> tuple[int, int, int]:
    hex_color = str(color or COLOR).strip().lstrip("#")
    if len(hex_color) != 6:
        return (255, 255, 255)
    try:
        return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))
    except ValueError:
        return (255, 255, 255)


def _split_two_lines(message: str) -> list[str]:
    words = [w for w in str(message).strip().split() if w]
    if not words:
        return [""]
    if TEXT_LINES <= 1 or len(words) == 1:
        return [" ".join(words)]
    if len(words) == 2:
        return [words[0], words[1]]
    best_idx = 1
    best_score = float("inf")
    for idx in range(1, len(words)):
        left = " ".join(words[:idx])
        right = " ".join(words[idx:])
        score = abs(len(left) - len(right))
        if score < best_score:
            best_idx = idx
            best_score = score
    return [" ".join(words[:best_idx]), " ".join(words[best_idx:])]


def _compact_caption(prompt: str, reply: str, max_chars: int = 52) -> str:
    """Choose one readable thought for the sign instead of mirroring chat prose."""
    clean = re.sub(r"[`*_#]+", "", str(reply or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    sentences = [part.strip(" -:;,.!") for part in re.split(r"(?<=[.!?])\s+", clean) if part.strip()]
    caption = sentences[0] if sentences else ""
    if not caption:
        caption = topic_label(prompt)
    words = caption.split()
    kept: list[str] = []
    length = 0
    for word in words:
        next_length = length + len(word) + (1 if kept else 0)
        if next_length > max_chars:
            break
        kept.append(word)
        length = next_length
    if len(kept) < len(words):
        return " ".join(kept).rstrip(".,;:") + "..."
    return " ".join(kept)


def _pixel_icon_for(text: str) -> str | None:
    """Pick a small, self-drawn icon; emoji fonts are unreliable on the sign host."""
    if random.random() > PIXEL_ICON_CHANCE:
        return None
    content = str(text).lower()
    if re.search(r"\b(sun|clear|weather|warm)\b", content):
        return "sun"
    if re.search(r"\b(rain|storm|snow|cloud)\b", content):
        return "cloud"
    if re.search(r"\b(happy|great|good|love|thanks)\b", content):
        return "heart"
    if re.search(r"\b(robot|luna|arm|ai)\b", content):
        return "robot"
    if re.search(r"\b(yes|done|ready|success|correct)\b", content):
        return "check"
    return "sparkle"


def _rainbow_palette() -> list[tuple[int, int, int]]:
    return [
        (255, 80, 80),
        (255, 170, 70),
        (255, 230, 80),
        (90, 220, 90),
        (80, 200, 255),
        (160, 120, 255),
        (255, 120, 220),
    ]


def _draw_pixel_icon(draw, icon: str, x: int, y: int, scale: int) -> None:
    patterns = {
        "sun": ("..Y..", ".YYY.", "YYYYY", ".YYY.", "..Y.."),
        "cloud": ("..WWW.", ".WWWWW", "WWWWWW", "..BBBB", ".BBBB."),
        "heart": ("R.R", "RRR", ".R."),
        "robot": (".WWW.", "W.B.W", "WWWWW", ".W.W."),
        "check": ("....G", "...G.", "G.G..", ".G...", "....."),
        "sparkle": ("..C..", "C.C.C", ".CCC.", "C.C.C", "..C.."),
    }
    colors = {"W": (255, 255, 255), "Y": (255, 220, 60), "B": (80, 180, 255), "R": (255, 80, 110), "G": (80, 235, 110), "C": (90, 220, 255)}
    for row, pattern_row in enumerate(patterns.get(icon, ())):
        for column, cell in enumerate(pattern_row):
            if cell != ".":
                draw.rectangle((x + column * scale, y + row * scale, x + (column + 1) * scale - 1, y + (row + 1) * scale - 1), fill=colors[cell])


def _render_text_image(message: str, width: int, height: int, color: str | None, rainbow_mode: int | None, pixel_icon: str | None = None):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    lines = _split_two_lines(message)
    font = ImageFont.load_default()
    line_heights: list[int] = []
    line_widths: list[int] = []
    line_offsets: list[int] = []
    for font_size in range(max(10, int(height * TEXT_FONT_HEIGHT_RATIO)), 7, -1):
        try:
            test_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except Exception:
            test_font = ImageFont.load_default()
        widths = []
        heights = []
        offsets = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=test_font)
            widths.append(bbox[2] - bbox[0])
            heights.append(bbox[3] - bbox[1])
            offsets.append(bbox[1])
        max_width = max(widths) if widths else 0
        total_height = sum(heights) + (max(0, len(lines) - 1) * max(2, font_size // 8))
        if max_width <= width - 4 and total_height <= height - 4:
            font = test_font
            line_widths = widths
            line_heights = heights
            line_offsets = offsets
            break
    if not line_widths:
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_widths.append(bbox[2] - bbox[0])
            line_heights.append(bbox[3] - bbox[1])
            line_offsets.append(bbox[1])

    spacing = max(2, (line_heights[0] // 8) if line_heights else 2)
    total_height = sum(line_heights) + (max(0, len(lines) - 1) * spacing)
    y = (height - total_height) // 2
    use_rainbow = rainbow_mode is not None and int(rainbow_mode) > 0
    base_color = _rgb_from_hex(color or COLOR)
    palette = _rainbow_palette()
    for line_idx, line in enumerate(lines):
        x = (width - line_widths[line_idx]) // 2
        draw_y = y - line_offsets[line_idx]
        if use_rainbow and line:
            cx = x
            for char_idx, ch in enumerate(line):
                cb = draw.textbbox((0, 0), ch, font=font)
                cw = cb[2] - cb[0]
                draw.text((cx, draw_y), ch, font=font, fill=palette[(line_idx + char_idx) % len(palette)])
                cx += cw
        else:
            draw.text((x, draw_y), line, font=font, fill=base_color)
        y += line_heights[line_idx] + spacing
    if pixel_icon:
        icon_scale = max(2, min(6, height // 12))
        _draw_pixel_icon(draw, pixel_icon, width - (6 * icon_scale) - 3, 3, icon_scale)
    return image


def _render_progress_image(percent: float, label: str, width: int, height: int, color: str | None):
    from PIL import Image, ImageDraw, ImageFont

    pct = max(0, min(100, round(percent)))
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    base_color = _rgb_from_hex(color or COLOR)

    caption = f"{label} {pct}%".strip()
    font = ImageFont.load_default()
    for font_size in range(max(10, int(height * 0.4)), 7, -1):
        try:
            test_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except Exception:
            test_font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), caption, font=test_font)
        if (bbox[2] - bbox[0]) <= width - 4:
            font = test_font
            break
    bbox = draw.textbbox((0, 0), caption, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((max(0, (width - text_w) // 2), max(0, height // 4 - text_h // 2 - bbox[1])), caption, font=font, fill=base_color)

    # Bar occupies the lower portion of the panel, label/percent sits above it.
    bar_x0, bar_x1 = 3, width - 4
    bar_y0, bar_y1 = int(height * 0.55), height - 4
    draw.rectangle((bar_x0, bar_y0, bar_x1, bar_y1), outline=base_color, width=1)
    fill_x1 = bar_x0 + int((bar_x1 - bar_x0 - 2) * (pct / 100.0))
    if fill_x1 > bar_x0 + 1:
        draw.rectangle((bar_x0 + 1, bar_y0 + 1, fill_x1, bar_y1 - 1), fill=base_color)
    return image


async def _send_pil_image(session: Any, image) -> None:
    from pypixelcolor.commands.send_image import send_image

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
            image.save(image_file.name, format="PNG")
            temp_path = image_file.name
        await session.execute_command(send_image, temp_path, resize_method="fit")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _cancel_clear_task() -> None:
    global _clear_task
    if _clear_task is not None and not _clear_task.done():
        _clear_task.cancel()
    _clear_task = None


def _clear_effect_frames(effect: str, frame, width: int, height: int):
    from PIL import Image

    steps = 8
    if effect in {"left", "right", "up", "down"}:
        for step in range(1, steps + 1):
            canvas = Image.new("RGB", (width, height), (0, 0, 0))
            if effect == "left":
                dx, dy = -int((width * step) / steps), 0
            elif effect == "right":
                dx, dy = int((width * step) / steps), 0
            elif effect == "up":
                dx, dy = 0, -int((height * step) / steps)
            else:
                dx, dy = 0, int((height * step) / steps)
            canvas.paste(frame, (dx, dy))
            yield canvas
        return

    tile = max(4, min(width, height) // 8)
    center_x = width // 2
    center_y = height // 2
    for step in range(1, steps + 1):
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        for x in range(0, width, tile):
            for y in range(0, height, tile):
                piece = frame.crop((x, y, min(x + tile, width), min(y + tile, height)))
                piece_cx = x + (piece.width // 2)
                piece_cy = y + (piece.height // 2)
                vx = piece_cx - center_x
                vy = piece_cy - center_y
                dest_x = x + int(vx * 0.16 * step) + random.randint(-1, 1)
                dest_y = y + int(vy * 0.16 * step) + random.randint(-1, 1)
                canvas.paste(piece, (dest_x, dest_y))
        yield canvas


async def _clear_after_delay() -> None:
    global _clear_task
    try:
        await asyncio.sleep(CLEAR_AFTER_S)
        async with _lock:
            if not ENABLED:
                return
            frame = _last_text_frame
            if frame is None:
                return
            from PIL import Image
            from pypixelcolor.lib.device_session import DeviceSession

            effect = random.choice(["left", "right", "up", "down", "explode"])
            async with DeviceSession(ADDRESS) as session:
                for out in _clear_effect_frames(effect, frame, frame.width, frame.height):
                    await _send_pil_image(session, out)
                    await asyncio.sleep(0.08)
                await _send_pil_image(session, Image.new("RGB", (frame.width, frame.height), (0, 0, 0)))
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[LED] clear effect failed: {exc}")
    finally:
        _clear_task = None


def _schedule_clear_task() -> None:
    global _clear_task
    _cancel_clear_task()
    if CLEAR_AFTER_S <= 0:
        return
    _clear_task = asyncio.create_task(_clear_after_delay())


def _cancel_clock_task() -> None:
    global _clock_task
    if _clock_task is not None and not _clock_task.done():
        _clock_task.cancel()
    _clock_task = None


def _cancel_gpu_task() -> None:
    global _gpu_task
    if _gpu_task is not None and not _gpu_task.done():
        _gpu_task.cancel()
    _gpu_task = None


def _clock_display_value(now: datetime) -> str:
    return now.strftime("%I:%M%p").lstrip("0")


async def _sync_clock_from_internet() -> bool:
    global _clock_base_epoch, _clock_base_monotonic, _clock_last_sync_monotonic
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "LunaLocal/1.0"}) as client:
            try:
                response = await client.get(CLOCK_TIME_API)
                response.raise_for_status()
                payload = response.json()
                dt = datetime.fromisoformat(str(payload["datetime"]).replace("Z", "+00:00")).astimezone(NY_TZ)
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                response = await client.get(CLOCK_TIME_API_FALLBACK)
                response.raise_for_status()
                payload = response.json()
                dt = datetime.fromisoformat(str(payload["dateTime"]))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=NY_TZ)
                else:
                    dt = dt.astimezone(NY_TZ)
        now_monotonic = time.monotonic()
        _clock_base_epoch = dt.timestamp()
        _clock_base_monotonic = now_monotonic
        _clock_last_sync_monotonic = now_monotonic
        return True
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return False


def _clock_now() -> datetime:
    if _clock_base_epoch is not None and _clock_base_monotonic is not None:
        ts = _clock_base_epoch + (time.monotonic() - _clock_base_monotonic)
        return datetime.fromtimestamp(ts, tz=NY_TZ)
    return datetime.now(tz=NY_TZ)


def _render_clock_image(width: int, height: int):
    now = _clock_now()
    return _render_text_image(_clock_display_value(now), width, height, COLOR, TOPIC_RAINBOW_MODE)


async def _clock_loop() -> None:
    global _clock_task
    last_displayed_value = None
    try:
        await _sync_clock_from_internet()
        while True:
            if time.monotonic() - _clock_last_sync_monotonic >= CLOCK_RESYNC_S:
                await _sync_clock_from_internet()
            displayed_value = _clock_display_value(_clock_now())
            if displayed_value != last_displayed_value:
                async with _lock:
                    if not ENABLED:
                        return
                    from pypixelcolor.lib.device_session import DeviceSession

                    async with DeviceSession(ADDRESS) as session:
                        info = session.get_device_info()
                        frame = _render_clock_image(int(info.width), int(info.height))
                        await _send_pil_image(session, frame)
                        globals()["_last_text_frame"] = frame.copy()
                last_displayed_value = displayed_value
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[LED] clock mode failed: {exc}")
    finally:
        _clock_task = None


def start_clock_mode() -> bool:
    global _clock_task
    if not ENABLED:
        return False
    _cancel_gpu_task()
    _cancel_clear_task()
    if _clock_task is None or _clock_task.done():
        _clock_task = asyncio.create_task(_clock_loop())
    return True


def _gpu_usage_percent() -> float | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        values = [float(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        if not values:
            return None
        return max(0.0, min(100.0, sum(values) / len(values)))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


async def _gpu_loop() -> None:
    global _gpu_task
    last_displayed_value = None
    try:
        while True:
            usage = _gpu_usage_percent()
            displayed_value = "G:N/A" if usage is None else f"G:{usage:.0f}%"
            if displayed_value != last_displayed_value:
                await display_text(displayed_value, color=GREEN if usage is not None else RED, schedule_clear=False)
                last_displayed_value = displayed_value
            await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[LED] GPU monitor failed: {exc}")
    finally:
        _gpu_task = None


def start_gpu_mode() -> bool:
    global _gpu_task
    if not ENABLED:
        return False
    _cancel_clock_task()
    _cancel_clear_task()
    if _gpu_task is None or _gpu_task.done():
        _gpu_task = asyncio.create_task(_gpu_loop())
    return True


def stop_gpu_mode() -> None:
    _cancel_gpu_task()


async def display_text(text: str, color: str | None = None, rainbow_mode: int | None = None, schedule_clear: bool = True, pixel_icon: str | None = None) -> bool:
    """Display text on the configured sign, returning False on any device error."""
    message = " ".join(str(text).split())[:MAX_CHARS].strip()
    if not message:
        return False
    if not ENABLED:
        return False
    if DRY_RUN:
        print(f"[LED] dry-run: {message} color={color or COLOR} rainbow={rainbow_mode if rainbow_mode is not None else 0}")
        return True

    async with _lock:
        try:
            from pypixelcolor.lib.device_session import DeviceSession

            async with DeviceSession(ADDRESS) as session:
                info = session.get_device_info()
                frame = _render_text_image(message, int(info.width), int(info.height), color, rainbow_mode, pixel_icon)
                await _send_pil_image(session, frame)
                globals()["_last_text_frame"] = frame.copy()
            if schedule_clear:
                _cancel_clock_task()
                _cancel_gpu_task()
                _schedule_clear_task()
            else:
                _cancel_clear_task()
            return True
        except Exception as exc:
            print(f"[LED] display failed: {exc}")
            return False


def power_state() -> bool | None:
    return _power_state


async def set_power(on: bool) -> bool:
    """Set sign power and remember the last successful command."""
    global _power_state
    if not ENABLED:
        return False
    if DRY_RUN:
        _power_state = bool(on)
        print(f"[LED] dry-run power: {'on' if on else 'off'}")
        return True

    async with _lock:
        try:
            from pypixelcolor.commands.set_power import set_power as set_power_command
            from pypixelcolor.lib.device_session import DeviceSession

            async with DeviceSession(ADDRESS) as session:
                await session.execute_command(set_power_command, on=bool(on))
            _power_state = bool(on)
            return True
        except Exception as exc:
            print(f"[LED] power command failed: {exc}")
            return False


async def display_reply(reply: str) -> bool:
    return await display_text(_compact_caption("", reply), pixel_icon=_pixel_icon_for(reply))


async def display_progress(percent: float, label: str = "", color: str | None = None) -> bool:
    """Show a bar-graph progress meter for a long-running background job."""
    if not ENABLED:
        return False
    if DRY_RUN:
        print(f"[LED] dry-run progress: {label} {round(max(0, min(100, percent)))}%")
        return True

    async with _lock:
        try:
            from pypixelcolor.lib.device_session import DeviceSession

            async with DeviceSession(ADDRESS) as session:
                info = session.get_device_info()
                frame = _render_progress_image(percent, label, int(info.width), int(info.height), color)
                await _send_pil_image(session, frame)
                globals()["_last_text_frame"] = frame.copy()
            _cancel_clock_task()
            _cancel_gpu_task()
            _cancel_clear_task()
            return True
        except Exception as exc:
            print(f"[LED] progress display failed: {exc}")
            return False


def image_subject(prompt: str) -> str | None:
    """Return a concrete subject only when the prompt merits a visual."""
    text = str(prompt).lower()
    explicit = re.search(r"(?:show|display|find|give me|see)\s+(?:a\s+)?(?:picture|photo|image)\s+(?:of|about)\s+([^?.!,;]+)", text)
    if explicit:
        return " ".join(explicit.group(1).split())[:80]

    subjects = {
        "tomato": "tomato plant",
        "tomatoes": "tomato plant",
        "cat": "cat",
        "cats": "cat",
        "dog": "dog",
        "dogs": "dog",
        "flower": "flower",
        "flowers": "flower",
        "tree": "tree",
        "trees": "tree",
        "mountain": "mountain",
        "mountains": "mountain",
        "ocean": "ocean",
        "robot": "robot",
        "robots": "robot",
        "car": "car",
        "cars": "car",
    }
    for word, subject in subjects.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            return subject
    return None


def requested_label(prompt: str) -> str | None:
    """Extract an explicit LED request as one short, human-readable label."""
    if not _LED_REQUEST_RE.search(str(prompt)):
        return None
    text = str(prompt).lower()
    aliases = (
        (r"\bnasdaq\b", "NASDAQ"),
        (r"\bbitcoin\b|\bbtc\b", "BITCOIN"),
        (r"\bclock\b|\btime\b", "CLOCK"),
        (r"\bweather\b", "WEATHER"),
        (r"\btraffic\b", "TRAFFIC"),
        (r"\bnews\b", "NEWS"),
        (r"\bcat(?:s)?\b", "CAT"),
        (r"\bdog(?:s)?\b", "DOG"),
        (r"\btomato(?:es)?\b", "TOMATO"),
    )
    for pattern, label in aliases:
        if re.search(pattern, text):
            return label
    before_target = re.split(r"\b(?:on|to)\s+(?:the\s+)?(?:led|sign)\b", text, maxsplit=1)[0]
    words = re.findall(r"[a-z0-9]+", before_target)
    ignored = {"show", "display", "put", "send", "me", "the", "a", "an", "on", "to", "please", "summary", "information", "info"}
    for word in reversed(words):
        if word not in ignored:
            return word[:8].upper()
    return DEFAULT_LABEL[:8].upper()


def topic_label(prompt: str, subject: str | None = None) -> str:
    """Pick one concise label token from a free-form user question."""
    if subject:
        base = re.sub(r"[^a-zA-Z0-9]", "", subject.split()[0])
        if base:
            return base[:8].upper()

    text = str(prompt).lower()
    priority_terms = (
        "denon", "yamaha", "marantz", "onkyo", "pioneer", "sony", "avr", "receiver",
        "nasdaq", "bitcoin", "weather", "traffic", "news", "cat", "dog", "tomato"
    )
    for term in priority_terms:
        if re.search(rf"\b{re.escape(term)}\b", text):
            return term[:8].upper()

    phrase_aliases = (
        (r"\bzone\s+controls?\b", "ZONECTRL"),
        (r"\bsurround\s+sound\b", "SURROUND"),
        (r"\bhdmi\s+arc\b", "HDMIARC"),
        (r"\bzone\s+2\b", "ZONE2"),
    )
    for pattern, label in phrase_aliases:
        if re.search(pattern, text):
            return label

    words = re.findall(r"[a-z0-9]+", text)
    ignored = {
        "what", "when", "where", "which", "who", "why", "how", "can", "could", "would", "should",
        "is", "are", "was", "were", "be", "been", "do", "does", "did", "about", "for", "from", "with", "without",
        "the", "a", "an", "to", "of", "and", "or", "on", "in", "at", "my", "your", "our", "me",
        "please", "tell", "show", "question", "help", "explain", "so", "there", "that", "this", "it", "if", "then",
        "tould", "wouldnt", "cant", "dont", "didnt"
    }
    significant = [word for word in words if word not in ignored]
    if len(significant) >= 2:
        first = significant[-2]
        second = significant[-1]
        if first == "zone" and second.startswith("control"):
            return "ZONECTRL"
        joined = f"{first}{second}"
        if len(joined) <= 8:
            return joined.upper()
    if significant:
        return significant[-1][:8].upper()
    return DEFAULT_LABEL[:8].upper()


def is_nasdaq_request(prompt: str) -> bool:
    text = str(prompt).lower()
    return bool(requested_label(prompt) == "NASDAQ" and re.search(r"\b(summary|price|quote|market|index|nasdaq)\b", text))


def is_weather_request(prompt: str) -> bool:
    text = str(prompt).lower()
    return bool(requested_label(prompt) == "WEATHER" and re.search(r"\b(weather|local|current|outside|temperature|forecast)\b", text))


def is_clock_request(prompt: str) -> bool:
    text = str(prompt).lower()
    if re.search(r"\b(gpu|graphics processor|graphics card)\b", text):
        return False
    return bool(_LED_REQUEST_RE.search(text) and re.search(r"\b(clock|time)\b", text))


def is_gpu_request(prompt: str) -> bool:
    text = str(prompt).lower()
    return bool(_DISPLAY_REQUEST_RE.search(text) and re.search(r"\b(gpu|graphics processor|graphics card)\b", text))


async def fetch_local_weather() -> dict[str, str] | None:
    """Fetch current weather using the service's automatic local lookup."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "LunaLocal/1.0"}) as client:
            response = await client.get(LOCAL_WEATHER_API)
            response.raise_for_status()
            condition = response.json()["current_condition"][0]
        temp_f = round(float(condition["temp_F"]))
        description = str(condition["weatherDesc"][0]["value"]).strip().lower()
        if any(word in description for word in ("rain", "drizzle", "shower", "thunder")):
            kind, color = "RAIN", BLUE
        elif any(word in description for word in ("snow", "sleet", "ice")):
            kind, color = "SNOW", BLUE
        elif any(word in description for word in ("sun", "clear")):
            kind, color = "SUN", YELLOW
        else:
            kind, color = "CLD", COLOR
        return {"temp_f": str(temp_f), "description": description, "kind": kind, "color": color}
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None


def _weather_display_value(weather: dict[str, str]) -> str:
    return f"{weather['temp_f']}F {weather['kind']}"[:8]


async def fetch_nasdaq_quote() -> dict[str, float | str] | None:
    """Fetch the Nasdaq Composite quote without requiring an API key."""
    params = {"range": "1d", "interval": "1d", "includePrePost": "false"}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "LunaLocal/1.0"}) as client:
            response = await client.get(NASDAQ_API, params=params)
            response.raise_for_status()
            meta = response.json()["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
        previous = float(meta.get("chartPreviousClose") or meta.get("previousClose"))
        change = price - previous
        return {"price": price, "change": change, "percent": change / previous * 100, "color": GREEN if change >= 0 else RED}
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None


def _quote_display_value(quote: dict[str, float | str]) -> str:
    price = float(quote["price"])
    if price >= 100000:
        return f"{price / 1000:.1f}K"[:8]
    return f"${price:,.0f}"[:8]


async def _find_image_url(subject: str) -> str | None:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": subject,
        "gsrnamespace": 6,
        "gsrlimit": 1,
        "prop": "imageinfo",
        "iiprop": "url",
    }
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "LunaLocal/1.0"}) as client:
            response = await client.get(WIKIMEDIA_API, params=params)
            response.raise_for_status()
            pages = response.json().get("query", {}).get("pages", {})
            return next(iter(pages.values())).get("imageinfo", [{}])[0].get("url")
    except (httpx.HTTPError, ValueError, IndexError, AttributeError, StopIteration):
        return None


async def display_image_url(url: str) -> bool:
    if not ENABLED or not IMAGES_ENABLED:
        return False
    temp_path = None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "LunaLocal/1.0"}) as client:
            response = await client.get(url)
            response.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as image_file:
            image_file.write(response.content)
            temp_path = image_file.name
        from pypixelcolor.commands.send_image import send_image
        from pypixelcolor.lib.device_session import DeviceSession

        async with _lock:
            async with DeviceSession(ADDRESS) as session:
                await session.execute_command(send_image, temp_path, resize_method="crop")
        return True
    except Exception as exc:
        print(f"[LED] image display failed: {exc}")
        return False
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


async def display_companion(prompt: str, reply: str) -> bool:
    """Show a relevant image briefly, then a concise companion caption."""
    subject = image_subject(prompt) if IMAGES_ENABLED else None
    if subject:
        image_url = await _find_image_url(subject)
        if image_url and await display_image_url(image_url):
            await asyncio.sleep(IMAGE_SECONDS)
    caption = _compact_caption(prompt, reply)
    return await display_text(caption, rainbow_mode=TOPIC_RAINBOW_MODE, pixel_icon=_pixel_icon_for(f"{prompt} {reply}"))


async def display_requested(prompt: str) -> bool:
    """Handle an explicit LED command without ever displaying the chat reply."""
    if is_clock_request(prompt):
        return start_clock_mode()
    if is_gpu_request(prompt):
        return start_gpu_mode()
    label = requested_label(prompt)
    if not label:
        return False
    if is_nasdaq_request(prompt):
        quote = await fetch_nasdaq_quote()
        if quote:
            await display_text(_quote_display_value(quote), color=str(quote["color"]))
            await asyncio.sleep(IMAGE_SECONDS)
        return await display_text(label)
    if is_weather_request(prompt):
        weather = await fetch_local_weather()
        if weather:
            await display_text(_weather_display_value(weather), color=weather["color"])
            await asyncio.sleep(IMAGE_SECONDS)
        return await display_text(label)
    subject = image_subject(prompt) if IMAGES_ENABLED else None
    if subject:
        image_url = await _find_image_url(subject)
        if image_url and await display_image_url(image_url):
            await asyncio.sleep(IMAGE_SECONDS)
    return await display_text(label, pixel_icon=_pixel_icon_for(prompt))