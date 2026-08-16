# lcd.py
# Waveshare 2-inch Mini LCD (ST7789) face display
# SPI0, DC=GPIO25, RST=GPIO27, BL=GPIO18, 320x240

import sys
import threading
import time
import math

# luma lives in hailo-venv; add it so it's found regardless of active venv.
_LUMA_SITE = "/home/arm/hailo-venv/lib/python3.11/site-packages"
if _LUMA_SITE not in sys.path:
    sys.path.insert(0, _LUMA_SITE)

_FACES_DIR = "/home/arm/faces"

_lcd_lock = threading.Lock()
_lcd_device = None
_lcd_available = None  # None = not yet tried
_current_face_name = "thinking"
_current_mode_text = ""
_animated_face_enabled = True  # Always use animated face mode
_animated_face_stop = threading.Event()
_animated_face_thread = None
_animated_face_paused_until = 0.0
_lipsync_cues = []
_lipsync_start_t = 0.0
_current_emotion = "neutral"  # neutral, happy, focused, concerned
_emotion_lock = threading.Lock()


def _draw_animated_overlays(draw, width: int, height: int, speaking: bool, phase: float, mouth_shape: str | None = None):
    """Draw animated face with emotions, colored eyes, eyebrows, and expressive lips."""
    
    # Get current emotion
    with _emotion_lock:
        emotion = _current_emotion
    
    # Color palette - warm tones
    skin_tone = (240, 200, 170)  # Warm beige
    eye_white = (250, 248, 245)  # Off-white
    iris_color = (70, 140, 200)  # Warm blue
    pupil_color = (20, 20, 20)  # Dark pupil
    lip_color_normal = (180, 60, 80)  # Warm red
    lip_color_bright = (200, 80, 100)  # Brighter when speaking
    cheek_color = (220, 140, 140)  # Soft pink for blush
    
    # Head shape - rounded rectangle background
    head_margin = int(height * 0.08)
    head_x0 = int(width * 0.1)
    head_y0 = int(height * 0.1)
    head_x1 = int(width * 0.9)
    head_y1 = int(height * 0.85)
    draw.rounded_rectangle(
        [head_x0, head_y0, head_x1, head_y1],
        radius=int((head_x1 - head_x0) * 0.15),
        fill=skin_tone,
        outline=(200, 170, 140),
        width=2
    )
    
    # Eye parameters
    eye_y = int(height * 0.34)
    eye_dx = int(width * 0.13)
    eye_w = max(16, int(width * 0.055))
    eye_h = max(12, int(height * 0.042))
    
    # Blink animation
    blink_period = 4.0
    blink_phase = (phase % blink_period) / blink_period
    blink = 0.0
    if blink_phase < 0.04:
        blink = blink_phase / 0.04
    elif blink_phase < 0.08:
        blink = (0.08 - blink_phase) / 0.04
    
    left_eye_pos = (width // 2 - eye_dx, eye_y)
    right_eye_pos = (width // 2 + eye_dx, eye_y)
    
    # Eyebrow parameters based on emotion
    eyebrow_y = eye_y - int(eye_h * 1.8)
    eyebrow_w = eye_w + 8
    
    if emotion == "happy":
        eyebrow_angle = -8  # Happy: raised, angled down inward
        eye_squint = 0.15
    elif emotion == "focused":
        eyebrow_angle = 12  # Focused: raised, angled up inward
        eye_squint = -0.08
    elif emotion == "concerned":
        eyebrow_angle = 20  # Concerned: angled inward/down
        eye_squint = 0.1
    else:  # neutral
        eyebrow_angle = 0
        eye_squint = 0
    
    # Draw both eyes
    for idx, (cx, cy) in enumerate([left_eye_pos, right_eye_pos]):
        is_left = (idx == 0)
        
        # Draw eye whites
        if blink > 0.65:
            # Blinking - just a line
            lid_h = max(3, int(eye_h * (1.0 - blink)))
            draw.ellipse(
                [cx - eye_w - 2, cy - lid_h // 2, cx + eye_w + 2, cy + lid_h // 2],
                fill=pupil_color
            )
        else:
            # Open eye - full white
            draw.ellipse([cx - eye_w, cy - eye_h, cx + eye_w, cy + eye_h], fill=eye_white)
            
            # Iris with pupil
            iris_w = max(10, int(eye_w * 0.7))
            iris_h = max(8, int(eye_h * 0.65))
            draw.ellipse(
                [cx - iris_w, cy - iris_h, cx + iris_w, cy + iris_h],
                fill=iris_color
            )
            
            # Pupil
            pupil_w = max(4, iris_w // 2)
            pupil_h = max(4, iris_h // 2)
            draw.ellipse(
                [cx - pupil_w, cy - pupil_h, cx + pupil_w, cy + pupil_h],
                fill=pupil_color
            )
            
            # Pupil highlight for life
            hl_w = max(2, pupil_w // 2)
            hl_h = max(2, pupil_h // 2)
            draw.ellipse(
                [cx - hl_w - 2, cy - hl_h - 2, cx, cy],
                fill=(255, 255, 255)
            )
        
        # Draw eyebrows based on emotion
        brow_x0 = cx - eyebrow_w
        brow_x1 = cx + eyebrow_w
        brow_y = eyebrow_y
        
        if is_left:
            # Left eyebrow: angles down-right when happy, up-right when focused
            if emotion == "happy":
                points = [
                    (brow_x0 - 2, brow_y),
                    (cx, brow_y - 4),
                    (brow_x1 + 2, brow_y + 3)
                ]
            elif emotion == "focused":
                points = [
                    (brow_x0 - 2, brow_y + 3),
                    (cx, brow_y - 4),
                    (brow_x1 + 2, brow_y)
                ]
            elif emotion == "concerned":
                points = [
                    (brow_x0 - 2, brow_y),
                    (cx - 8, brow_y - 6),
                    (brow_x1 + 2, brow_y + 4)
                ]
            else:  # neutral
                points = [
                    (brow_x0 - 2, brow_y),
                    (cx, brow_y - 2),
                    (brow_x1 + 2, brow_y)
                ]
        else:
            # Right eyebrow: mirrored
            if emotion == "happy":
                points = [
                    (brow_x0 - 2, brow_y + 3),
                    (cx, brow_y - 4),
                    (brow_x1 + 2, brow_y)
                ]
            elif emotion == "focused":
                points = [
                    (brow_x0 - 2, brow_y),
                    (cx, brow_y - 4),
                    (brow_x1 + 2, brow_y + 3)
                ]
            elif emotion == "concerned":
                points = [
                    (brow_x0 - 2, brow_y + 4),
                    (cx + 8, brow_y - 6),
                    (brow_x1 + 2, brow_y)
                ]
            else:  # neutral
                points = [
                    (brow_x0 - 2, brow_y),
                    (cx, brow_y - 2),
                    (brow_x1 + 2, brow_y)
                ]
        
        draw.polygon(points, fill=(120, 80, 60))
    
    # Add cheek blush based on emotion
    if emotion in ("happy", "focused"):
        cheek_y = int(height * 0.48)
        left_cheek_x = width // 2 - int(width * 0.2)
        right_cheek_x = width // 2 + int(width * 0.2)
        cheek_r = int(width * 0.06)
        
        for cheek_x in (left_cheek_x, right_cheek_x):
            # Draw soft circular blush
            draw.ellipse(
                [cheek_x - cheek_r, cheek_y - cheek_r // 2,
                 cheek_x + cheek_r, cheek_y + cheek_r // 2],
                fill=cheek_color
            )
    
    # Mouth
    mouth_x = width // 2
    mouth_y = int(height * 0.68)
    
    viseme_open_map = {
        "A": 0.40,  # wide open
        "B": 0.12,  # closed lips
        "C": 0.16,  # small rounded
        "D": 0.20,  # mid-open
        "E": 0.10,  # teeth together
        "F": 0.14,  # narrow round
        "G": 0.08,  # very small
        "H": 0.26,  # open
        "X": 0.03,  # closed
    }

    mouth_open = viseme_open_map["X"]
    
    if mouth_shape is not None:
        mouth_open = viseme_open_map.get(str(mouth_shape).upper(), 0.18)
        speaking = str(mouth_shape).upper() != "X"

    # Fallback talk motion if speaking is true but no active viseme cue.
    # If cues exist but haven't started yet, keep the mouth closed.
    if speaking and mouth_shape is None:
        if _lipsync_cues and phase < _lipsync_start_t:
            speaking = False
            mouth_open = viseme_open_map["X"]
        else:
            mouth_open = 0.12 + (0.06 * (0.5 + 0.5 * math.sin(phase * 10.0)))
    
    if speaking:
        lip_color = lip_color_bright if speaking else lip_color_normal
        viseme = str(mouth_shape).upper() if mouth_shape else "X"
        mouth_w = int(width * 0.18)
        mouth_h = max(10, int(height * mouth_open * 0.28))
        
        if viseme in ("A", "H"):
            # Wide open - show inner mouth
            draw.ellipse(
                [mouth_x - mouth_w, mouth_y - mouth_h, mouth_x + mouth_w, mouth_y + mouth_h],
                fill=lip_color
            )
            # Inner mouth - tongue/palate
            inner_h = max(4, mouth_h // 2)
            draw.ellipse(
                [mouth_x - mouth_w + 8, mouth_y - inner_h, mouth_x + mouth_w - 8, mouth_y + inner_h],
                fill=(200, 80, 100)
            )
        elif viseme in ("B", "P", "M"):
            # Closed lips - pressed together
            draw.rounded_rectangle(
                [mouth_x - mouth_w, mouth_y - 5, mouth_x + mouth_w, mouth_y + 5],
                radius=5,
                fill=lip_color
            )
        elif viseme in ("E", "F"):
            # Smile with teeth visible
            draw.rounded_rectangle(
                [mouth_x - mouth_w, mouth_y - mouth_h // 2, mouth_x + mouth_w, mouth_y + mouth_h // 2],
                radius=max(5, mouth_h // 2),
                fill=lip_color
            )
            # Teeth line
            draw.rectangle(
                [mouth_x - mouth_w + 8, mouth_y - 3, mouth_x + mouth_w - 8, mouth_y + 3],
                fill=(230, 230, 220)
            )
        else:
            # Standard open mouth
            draw.rounded_rectangle(
                [mouth_x - mouth_w, mouth_y - mouth_h, mouth_x + mouth_w, mouth_y + mouth_h],
                radius=max(6, mouth_h // 2),
                fill=lip_color
            )
            # Inner mouth highlight
            inner_h = max(4, mouth_h // 2)
            draw.rounded_rectangle(
                [mouth_x - mouth_w + 6, mouth_y - inner_h, mouth_x + mouth_w - 6, mouth_y + inner_h],
                radius=max(2, inner_h // 2),
                fill=(210, 90, 110)
            )
    else:
        # Closed mouth - relaxed
        mouth_w = int(width * 0.18)
        mouth_h = max(3, int(height * 0.014))
        draw.rounded_rectangle(
            [mouth_x - mouth_w, mouth_y - mouth_h, mouth_x + mouth_w, mouth_y + mouth_h],
            radius=max(2, mouth_h),
            fill=lip_color_normal
        )


def _mode_to_text(mode: str) -> str:
    mode_str = str(mode or "").upper()
    if mode_str == "HIGH_CAM":
        return "TOP"
    if mode_str == "TABLE_CAM":
        return "TABLE"
    # Allow custom short overlays (e.g. SMART TAKE).
    if mode_str:
        return mode_str[:16]
    return ""


def _render_animated_face_locked(device, face_name: str, mode_text: str = ""):
    from PIL import Image, ImageDraw, ImageFont

    # Animated mode uses a clean canvas so the new eyes and mouth can stand on their own.
    img = Image.new("RGB", (device.width, device.height), (247, 244, 238))
    draw = ImageDraw.Draw(img)

    head_margin_x = int(device.width * 0.07)
    head_margin_y = int(device.height * 0.08)
    draw.rounded_rectangle(
        [head_margin_x, head_margin_y, device.width - head_margin_x, device.height - head_margin_y],
        radius=28,
        fill=(252, 250, 246),
        outline=(214, 205, 193),
        width=3,
    )

    try:
        speaking = False
        phase = time.time()
        mouth_shape = _current_lipsync_viseme_locked(phase)
        import brain
        speaking = bool(brain.is_speaking())
    except Exception:
        speaking = False
        phase = time.time()
        mouth_shape = _current_lipsync_viseme_locked(phase)

    _draw_animated_overlays(draw, device.width, device.height, speaking, phase, mouth_shape=mouth_shape)

    if mode_text:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
        except Exception:
            font = ImageFont.load_default()
        x = 10
        y = 8
        text = mode_text
        try:
            bbox = draw.textbbox((x, y), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except Exception:
            tw, th = draw.textsize(text, font=font)
        pad_x = 8
        pad_y = 4
        draw.rectangle([x - pad_x, y - pad_y, x + tw + pad_x, y + th + pad_y], fill=(0, 0, 0))
        draw.text((x, y), text, font=font, fill=(255, 255, 255))

    device.display(img)


def _render_current_locked(device):
    """Always render animated face (legacy support for show_face)."""
    _render_animated_face_locked(device, _current_face_name, _current_mode_text)


def _get_device():
    global _lcd_device, _lcd_available
    if _lcd_available is False:
        return None
    if _lcd_device is not None:
        return _lcd_device
    try:
        from luma.core.interface.serial import spi
        from luma.lcd.device import st7789
        serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=27, gpio_LIGHT=18)
        _lcd_device = st7789(serial, width=320, height=240, rotate=0)
        _lcd_available = True
        print("[LCD] ST7789 device initialised (320x240)")
        return _lcd_device
    except Exception as exc:
        _lcd_available = False
        print(f"[LCD] Not available: {exc}")
        return None


def show_face(name: str):
    """Display a named face image on the LCD.
    name: 'thinking' | 'happy' | 'sad' | 'mad' | 'sleeping'
    Safe to call from any thread. No-op if LCD hardware is absent.
    """
    def _show():
        global _current_face_name
        with _lcd_lock:
            device = _get_device()
            if device is None:
                return
            try:
                _current_face_name = str(name)
                face_key = _current_face_name.strip().lower()
                # Legacy face names now map to animated-face emotions.
                face_to_emotion = {
                    "happy": "happy",
                    "thinking": "focused",
                    "sad": "concerned",
                    "mad": "concerned",
                    "sleeping": "neutral",
                }
                mapped = face_to_emotion.get(face_key)
                if mapped:
                    with _emotion_lock:
                        globals()["_current_emotion"] = mapped
                _render_current_locked(device)
                print(f"[LCD] Showing face: {name}")
            except Exception as exc:
                print(f"[LCD] Failed to show '{name}': {exc}")

    threading.Thread(target=_show, daemon=True).start()


def get_current_face_name() -> str:
    """Return the last face name requested for the LCD."""
    with _lcd_lock:
        return str(_current_face_name)


def show_text(text: str, duration_s: float = 0.0, font_size: int = 120):
    """Display large centered text, then optionally restore the prior face."""

    def _show_text():
        from PIL import Image, ImageDraw, ImageFont

        with _lcd_lock:
            device = _get_device()
            if device is None:
                return
            global _animated_face_paused_until
            _animated_face_paused_until = max(_animated_face_paused_until, time.time() + max(0.0, float(duration_s)) + 0.15)
            restore_face = str(_current_face_name)
            restore_mode = str(_current_mode_text)
            try:
                img = Image.new("RGB", (device.width, device.height), (0, 0, 0))
                draw = ImageDraw.Draw(img)
                try:
                    font = ImageFont.truetype(
                        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                        max(24, int(font_size)),
                    )
                except Exception:
                    font = ImageFont.load_default()

                display_text = str(text)
                try:
                    bbox = draw.textbbox((0, 0), display_text, font=font)
                    text_w = bbox[2] - bbox[0]
                    text_h = bbox[3] - bbox[1]
                except Exception:
                    text_w, text_h = draw.textsize(display_text, font=font)

                x = max(0, (device.width - text_w) // 2)
                y = max(0, (device.height - text_h) // 2)
                draw.text((x, y), display_text, font=font, fill=(255, 255, 255))
                device.display(img)
                print(f"[LCD] Showing text: {display_text}")
            except Exception as exc:
                print(f"[LCD] Failed to show text '{text}': {exc}")
                return

        if duration_s > 0:
            time.sleep(max(0.0, float(duration_s)))
            with _lcd_lock:
                device = _get_device()
                if device is None:
                    return
                try:
                    _render_animated_face_locked(device, restore_face, restore_mode)
                    print(f"[LCD] Restored face after text: {restore_face}")
                except Exception as exc:
                    print(f"[LCD] Failed restoring face after text: {exc}")

    threading.Thread(target=_show_text, daemon=True).start()


def set_camera_mode(mode: str):
    """Overlay current camera mode text on top of the active face image.
    mode: 'HIGH_CAM' -> 'TOP', 'TABLE_CAM' -> 'TABLE', anything else clears label.
    """

    def _set_mode():
        global _current_mode_text
        with _lcd_lock:
            device = _get_device()
            if device is None:
                return
            try:
                _current_mode_text = _mode_to_text(mode)
                _render_current_locked(device)
                if _current_mode_text:
                    print(f"[LCD] Camera mode label: {_current_mode_text}")
                else:
                    print("[LCD] Camera mode label cleared")
            except Exception as exc:
                print(f"[LCD] Failed setting camera mode label '{mode}': {exc}")

    threading.Thread(target=_set_mode, daemon=True).start()


def get_emotion() -> str:
    """Get the current emotional state of the face."""
    with _emotion_lock:
        return _current_emotion


def set_emotion(emotion: str) -> bool:
    """Set the emotional state: 'neutral', 'happy', 'focused', 'concerned'.
    Returns True if successful, False if invalid emotion."""
    valid = {"neutral", "happy", "focused", "concerned"}
    if emotion.lower() not in valid:
        print(f"[LCD] Invalid emotion '{emotion}'. Valid: {valid}")
        return False
    with _emotion_lock:
        globals()['_current_emotion'] = emotion.lower()
    print(f"[LCD] Emotion set to: {emotion.lower()}")
    return True


def _animated_face_loop():
    global _animated_face_thread
    frame_delay_s = 0.09
    while not _animated_face_stop.is_set():
        with _lcd_lock:
            device = _get_device()
            if device is None:
                pass
            else:
                try:
                    if _animated_face_enabled and time.time() >= _animated_face_paused_until:
                        _render_animated_face_locked(device, _current_face_name, _current_mode_text)
                except Exception as exc:
                    print(f"[LCD] Animated face render failed: {exc}")
        time.sleep(frame_delay_s)
    _animated_face_thread = None


def _current_lipsync_viseme_locked(now_t: float):
    if not _lipsync_cues:
        return None
    t = now_t - _lipsync_start_t
    if t < 0.0:
        return None
    last_end = 0.0
    for cue in _lipsync_cues:
        try:
            start = float(cue.get("start", 0.0))
            end = float(cue.get("end", start))
            value = str(cue.get("value", "X")).strip().upper()[:1] or "X"
        except Exception:
            continue
        last_end = max(last_end, end)
        if start <= t < end:
            return value
    if t > (last_end + 0.15):
        _lipsync_cues.clear()
    return None


def set_lipsync_cues(cues, start_delay_s: float = 0.0):
    """Set Rhubarb mouth cues for animated mode.
    cues: list of dicts with start/end/value fields.
    """
    if not isinstance(cues, list):
        return False
    with _lcd_lock:
        _lipsync_cues.clear()
        for cue in cues:
            if isinstance(cue, dict):
                _lipsync_cues.append(dict(cue))
        _lipsync_cues.sort(key=lambda c: float(c.get("start", 0.0)))
        global _lipsync_start_t
        _lipsync_start_t = time.time() + max(0.0, float(start_delay_s))
    # Ensure the animation loop is running so visemes update on screen.
    try:
        start_animated_face_mode()
    except Exception:
        pass
    return True


def clear_lipsync_cues():
    with _lcd_lock:
        _lipsync_cues.clear()


def start_animated_face_mode() -> bool:
    """Enable animated face rendering with simple blink and lip-sync motion."""
    global _animated_face_enabled, _animated_face_thread
    with _lcd_lock:
        _animated_face_enabled = True
        _animated_face_stop.clear()
        device = _get_device()
        if device is not None:
            try:
                _render_animated_face_locked(device, _current_face_name, _current_mode_text)
            except Exception as exc:
                print(f"[LCD] Animated face initial render failed: {exc}")
    if _animated_face_thread is None or not _animated_face_thread.is_alive():
        _animated_face_thread = threading.Thread(target=_animated_face_loop, daemon=True)
        _animated_face_thread.start()
    print("[LCD] Animated face mode ON")
    return True


def stop_animated_face_mode() -> bool:
    """Disable animated face rendering and restore the current static face."""
    global _animated_face_enabled
    _animated_face_enabled = False
    _animated_face_stop.set()
    clear_lipsync_cues()
    with _lcd_lock:
        device = _get_device()
        if device is not None:
            try:
                _render_current_locked(device)
            except Exception as exc:
                print(f"[LCD] Failed restoring static face: {exc}")
    print("[LCD] Animated face mode OFF")
    return True


def is_animated_face_mode() -> bool:
    return bool(_animated_face_enabled)
