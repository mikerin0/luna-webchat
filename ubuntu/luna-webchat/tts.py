import subprocess
import sys
from pathlib import Path

import audio_limiter

_VOICE_MODEL = Path(__file__).resolve().parent / "piper-voices" / "en_US-libritts_r-medium.onnx"

# Piper's raw output already hits the int16 ceiling on loud peaks while the
# rest of the speech sits far below it (crest factor ~5x) -- no fixed
# attenuation on the playback device can be both loud enough and clean, since
# turning it down preserves the clipped shape, just quieter. A peak limiter
# fixes this without a static waveshaper (e.g. tanh) adding distortion to
# everything that's merely moderate-loud, not just the true peaks.
_MAKEUP_GAIN = 2.0


def synthesize(text: str) -> bytes:
  """Render text to speech with Piper, returning compressed WAV bytes. Raises on failure."""
  result = subprocess.run(
    [sys.executable, "-m", "piper", "-m", str(_VOICE_MODEL), "-f", "-"],
    input=text.encode("utf-8"),
    capture_output=True,
    timeout=30,
    check=True,
  )
  return audio_limiter.limit(result.stdout, _MAKEUP_GAIN)
