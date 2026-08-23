"""Optional Bluetooth text output for the iPixel LED sign."""

import asyncio
import os
import re
import tempfile
from typing import Any

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
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
NASDAQ_API = "https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC"
LOCAL_WEATHER_API = "https://wttr.in/?format=j1"
GREEN = "00ff00"
RED = "ff0000"
BLUE = "00aaff"
YELLOW = "ffff00"
_LED_REQUEST_RE = re.compile(r"\b(?:show|display|put|send)\b.*\b(?:on|to)\s+(?:the\s+)?(?:led|sign)\b", re.IGNORECASE)

_lock = asyncio.Lock()
_power_state: bool | None = None


def _display_text_command() -> Any:
    from pypixelcolor.commands.send_text import send_text

    return send_text


async def display_text(text: str, color: str | None = None, rainbow_mode: int | None = None) -> bool:
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
                await session.execute_command(
                    _display_text_command(),
                    text=message,
                    animation=ANIMATION,
                    speed=SPEED,
                    color=color or COLOR,
                    rainbow_mode=0 if rainbow_mode is None else int(rainbow_mode),
                )
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
    return await display_text(reply)


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

    words = re.findall(r"[a-z0-9]+", text)
    ignored = {
        "what", "when", "where", "which", "who", "why", "how", "can", "could", "would", "should",
        "is", "are", "was", "were", "do", "does", "did", "about", "for", "from", "with", "without",
        "the", "a", "an", "to", "of", "and", "or", "on", "in", "at", "my", "your", "our", "me",
        "please", "tell", "show", "question", "help", "explain"
    }
    for word in words:
        if word not in ignored:
            return word[:8].upper()
    return DEFAULT_LABEL[:8].upper()


def is_nasdaq_request(prompt: str) -> bool:
    text = str(prompt).lower()
    return bool(requested_label(prompt) == "NASDAQ" and re.search(r"\b(summary|price|quote|market|index|nasdaq)\b", text))


def is_weather_request(prompt: str) -> bool:
    text = str(prompt).lower()
    return bool(requested_label(prompt) == "WEATHER" and re.search(r"\b(weather|local|current|outside|temperature|forecast)\b", text))


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
    """Show a relevant image briefly, then display only a small topic label."""
    subject = image_subject(prompt) if IMAGES_ENABLED else None
    if subject:
        image_url = await _find_image_url(subject)
        if image_url and await display_image_url(image_url):
            await asyncio.sleep(IMAGE_SECONDS)
    label = topic_label(prompt, subject)
    return await display_text(label, rainbow_mode=TOPIC_RAINBOW_MODE)


async def display_requested(prompt: str) -> bool:
    """Handle an explicit LED command without ever displaying the chat reply."""
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
    return await display_text(label)