import io
import wave

import numpy as np


def limit(wav_bytes: bytes, makeup_gain: float, ceiling: float = 0.95, release_seconds: float = 0.05) -> bytes:
  """Applies makeup gain then a peak limiter (attack-instant, exponential
  release) so quiet voices get louder without pushing true peaks past the
  ceiling. Only engages gain reduction near genuine peaks, unlike a static
  waveshaper (e.g. tanh), which would add distortion to everything merely
  moderate-loud too.
  """
  with wave.open(io.BytesIO(wav_bytes), "rb") as r:
    params = r.getparams()
    rate = r.getframerate()
    samples = np.frombuffer(r.readframes(r.getnframes()), dtype=np.int16)

  x = samples.astype(np.float32) / 32768.0 * makeup_gain
  abs_x = np.abs(x)
  release_coef = float(np.exp(-1.0 / (rate * release_seconds)))
  envelope = np.empty_like(abs_x)
  env = 0.0
  for i in range(len(abs_x)):
    env = max(abs_x[i], env * release_coef)
    envelope[i] = env
  gain = np.minimum(1.0, ceiling / np.maximum(envelope, 1e-6))
  y = np.clip(x * gain, -1.0, 1.0)
  out_samples = (y * 32767.0).astype(np.int16)

  buf = io.BytesIO()
  with wave.open(buf, "wb") as w:
    w.setparams(params)
    w.writeframes(out_samples.tobytes())
  return buf.getvalue()
