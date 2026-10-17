import io
import wave
from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro

import audio_limiter

_VOICES_DIR = Path(__file__).resolve().parent / "kokoro-voices"
_VOICE = "af_heart"

# Kokoro's raw output doesn't clip like Piper's (peaks sit well under full
# scale), but it's much quieter overall (crest factor ~7x) -- needs a bigger
# makeup gain to reach a comparable loudness, same limiter shape otherwise.
_MAKEUP_GAIN = 5.5

# Loaded once at import, not per-call -- takes ~1.4s, same model instance is
# reused for every synthesize() for the life of the server process.
_model = Kokoro(str(_VOICES_DIR / "kokoro-v1.0.fp16.onnx"), str(_VOICES_DIR / "voices-v1.0.bin"))


def synthesize(text: str) -> bytes:
  """Render text to speech with Kokoro, returning compressed WAV bytes. Raises on failure."""
  samples, rate = _model.create(text, voice=_VOICE, speed=1.0, lang="en-us")
  pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)

  buf = io.BytesIO()
  with wave.open(buf, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(pcm16.tobytes())

  return audio_limiter.limit(buf.getvalue(), _MAKEUP_GAIN)
