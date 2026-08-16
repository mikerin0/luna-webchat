# voice_assistant.py
# Continuous voice loop: USB mic -> OpenAI Whisper -> GPT-4 Turbo -> Piper TTS
#
# Flow per cycle:
#   1. Record a fixed-length chunk from the USB mic via arecord.
#   2. Transcribe with Whisper.  Empty result = silence.
#   3. Accumulate transcript chunks until a silence chunk ends an utterance.
#   4. Send the full utterance to GPT-4 Turbo with rolling conversation context.
#   5. Speak the reply via brain.say() (queued Piper TTS with BT anti-clipping).

import io
import os
import re
import struct
import subprocess
import threading
import time
import wave

import config

try:
    import webrtcvad
except Exception:
    webrtcvad = None

# ---------------------------------------------------------------------------
# .env loader (keeps API key out of source code)
# ---------------------------------------------------------------------------
_DOTENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_dotenv():
    try:
        with open(_DOTENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and val:
                    os.environ.setdefault(key, val)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[Voice] .env load warning: {exc}")


_load_dotenv()

# ---------------------------------------------------------------------------
# Configuration (all overridable in config.py)
# ---------------------------------------------------------------------------
_ENABLED = bool(getattr(config, "VOICE_ASSISTANT_ENABLED", True))
_MIC_DEVICE = str(getattr(config, "VOICE_MIC_ALSA_DEVICE", "plughw:2,0"))
_MIC_RATE = int(getattr(config, "VOICE_MIC_SAMPLE_RATE", 16000))
_CHUNK_S = float(getattr(config, "VOICE_RECORD_CHUNK_S", 4.0))
_VAD_MODE = int(getattr(config, "VOICE_VAD_MODE", 2))
_VAD_FRAME_MS = int(getattr(config, "VOICE_VAD_FRAME_MS", 30))
_VAD_START_FRAMES = int(getattr(config, "VOICE_VAD_START_FRAMES", 4))
_VAD_END_SILENCE_MS = int(getattr(config, "VOICE_VAD_END_SILENCE_MS", 900))
_MAX_UTTERANCE_S = float(getattr(config, "VOICE_MAX_UTTERANCE_S", 12.0))
_MIN_UTTERANCE_MS = int(getattr(config, "VOICE_MIN_UTTERANCE_MS", 450))
_WHISPER_MODEL = str(getattr(config, "VOICE_WHISPER_MODEL", "whisper-1"))
_GPT_MODEL = str(getattr(config, "VOICE_GPT_MODEL", "gpt-4-turbo"))
_GPT_TEMPERATURE = float(getattr(config, "VOICE_GPT_TEMPERATURE", 0.7))
_MAX_CONTEXT_TURNS = int(getattr(config, "VOICE_MAX_CONTEXT_TURNS", 10))
_LUNA_CHAT_URL = str(getattr(config, "VOICE_LUNA_CHAT_URL", "")).strip()
_LUNA_CHAT_PROFILE = str(getattr(config, "VOICE_LUNA_CHAT_PROFILE", "general"))
_LUNA_CHAT_VERIFY_SSL = bool(getattr(config, "VOICE_LUNA_CHAT_VERIFY_SSL", True))
_LUNA_CHAT_TIMEOUT_S = float(getattr(config, "VOICE_LUNA_CHAT_TIMEOUT_S", 25.0))
_SYSTEM_PROMPT = str(getattr(
    config,
    "VOICE_SYSTEM_PROMPT",
    "You are a helpful voice assistant built into a robot arm. "
    "Keep all responses brief: 1 to 3 sentences maximum.",
))
# RMS threshold below which a chunk is considered silence (0-32767 scale).
# Raise VOICE_ENERGY_THRESHOLD in config.py if background noise triggers false positives.
_ENERGY_THRESHOLD = float(getattr(config, "VOICE_ENERGY_THRESHOLD", 400.0))
# AI tools (poses and faces) that GPT can call via function calling.
_AI_TOOLS_ENABLED = bool(getattr(config, "VOICE_AI_TOOLS_ENABLED", False))
_AI_TOOLS_ENABLED_LOCK = threading.Lock()

# Whisper hallucinates stock phrases on silence/noise.  Any transcript that
# matches one of these (case-insensitive, stripped) is discarded entirely.
_WHISPER_HALLUCINATIONS = frozenset([
    "thank you for watching",
    "thanks for watching",
    "thank you for watching!",
    "thanks for watching!",
    "thank you.",
    "thanks.",
    "goodbye",
    "goodbye.",
    "bye",
    "bye.",
    "you're welcome",
    "you're welcome.",
    "you're welcome! have a great day.",
    "you're welcome! have a great day!",
    "have a great day",
    "have a great day!",
    "have a great day.",
    "see you next time",
    "see you next time!",
    "see you later",
    "please subscribe",
    "like and subscribe",
    "subscribe",
    "...",
    ".",
    "",
])

# URL-shaped transcript detector for Whisper noise hallucinations, e.g.
# "www.microsoft.com www.microsoft.com".
_URL_TOKEN_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?:/\S*)?$",
    re.IGNORECASE,
)
# Extra seconds to stay suppressed after TTS finishes (BT reverb tail).
_POST_SPEECH_MUTE_S = float(getattr(config, "VOICE_POST_SPEECH_MUTE_S", 1.5))

# ---------------------------------------------------------------------------
# OpenAI client (lazy-initialised, one instance)
# ---------------------------------------------------------------------------
_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        try:
            from openai import OpenAI
        except ImportError:
            print("[Voice] openai package not installed – run: pip install openai")
            return None
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        base_url = (
            os.environ.get("OPENAI_BASE_URL", "").strip()
            or str(getattr(config, "VOICE_OPENAI_BASE_URL", "")).strip()
        )
        if not api_key:
            print("[Voice] OPENAI_API_KEY not set; check .env file")
            return None
        try:
            if base_url:
                _client = OpenAI(api_key=api_key, base_url=base_url)
                print(f"[Voice] OpenAI-compatible client ready via {base_url}")
            else:
                _client = OpenAI(api_key=api_key)
                print("[Voice] OpenAI client ready")
        except Exception as exc:
            print(f"[Voice] OpenAI client init failed: {exc}")
            return None
        return _client


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------
_history: list[dict] = []
_history_lock = threading.Lock()


def _build_messages(user_text: str) -> list[dict]:
    with _history_lock:
        # Keep up to _MAX_CONTEXT_TURNS full turns (each turn = 2 messages).
        trimmed = _history[-(  _MAX_CONTEXT_TURNS * 2):]
        msgs = [{"role": "system", "content": _SYSTEM_PROMPT}]
        msgs.extend(trimmed)
        msgs.append({"role": "user", "content": user_text})
        return msgs


def _add_to_history(user_text: str, assistant_text: str):
    with _history_lock:
        _history.append({"role": "user", "content": user_text})
        _history.append({"role": "assistant", "content": assistant_text})
        # Hard cap to avoid unbounded growth.
        while len(_history) > _MAX_CONTEXT_TURNS * 2:
            _history.pop(0)


def clear_history():
    """Clear conversation context (called externally to reset the chat)."""
    with _history_lock:
        _history.clear()
    print("[Voice] Conversation history cleared")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rms(wav_bytes: bytes) -> float:
    """Return RMS amplitude of s16le PCM samples found after a 44-byte WAV header."""
    pcm = wav_bytes[44:]  # skip WAV header
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack_from(f"<{n}h", pcm)
    mean_sq = sum(s * s for s in samples) / n
    return mean_sq ** 0.5


_post_speech_until: float = 0.0  # suppress listening until this timestamp
_mic_muted = threading.Event()


def _robot_is_speaking() -> bool:
    """True if brain is actively rendering/playing TTS, or in the mute tail."""
    try:
        import brain
        if brain.is_speaking():
            global _post_speech_until
            _post_speech_until = time.time() + _POST_SPEECH_MUTE_S
            return True
    except Exception:
        pass
    return time.time() < _post_speech_until


def is_muted() -> bool:
    return _mic_muted.is_set()


def set_muted(muted: bool):
    if muted:
        _mic_muted.set()
    else:
        _mic_muted.clear()


def toggle_muted() -> bool:
    if is_muted():
        _mic_muted.clear()
        return False
    _mic_muted.set()
    return True


# ---------------------------------------------------------------------------
# Audio recording
# ---------------------------------------------------------------------------
def _record_chunk() -> bytes | None:
    """Record one fixed-length chunk from the USB mic.
    Returns raw WAV bytes (with header), or None on error."""
    try:
        result = subprocess.run(
            [
                "arecord",
                "-D", _MIC_DEVICE,
                "-f", "S16_LE",
                "-r", str(_MIC_RATE),
                "-c", "1",
                "-d", str(int(_CHUNK_S)),
                "-q",
                "-t", "wav",
                "-",
            ],
            capture_output=True,
            timeout=_CHUNK_S + 6.0,
        )
        if result.returncode == 0 and len(result.stdout) > 44:
            return result.stdout
        if result.returncode != 0:
            print(f"[Voice] arecord error (rc={result.returncode}): {result.stderr[:120]}")
        return None
    except subprocess.TimeoutExpired:
        print("[Voice] arecord timed out")
        return None
    except FileNotFoundError:
        print("[Voice] arecord not found; install alsa-utils")
        return None
    except Exception as exc:
        print(f"[Voice] Record error: {exc}")
        return None


def _pcm_to_wav(pcm: bytes) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_MIC_RATE)
        wf.writeframes(pcm)
    return bio.getvalue()


def _record_utterance_vad() -> bytes | None:
    """Capture one speech utterance and stop after silence endpointing."""
    if webrtcvad is None:
        return _record_chunk()

    frame_ms = _VAD_FRAME_MS if _VAD_FRAME_MS in (10, 20, 30) else 30
    frame_bytes = int(_MIC_RATE * (frame_ms / 1000.0) * 2)
    end_silence_frames = max(1, int(_VAD_END_SILENCE_MS / frame_ms))
    min_frames = max(1, int(_MIN_UTTERANCE_MS / frame_ms))
    max_frames = max(min_frames + 2, int((_MAX_UTTERANCE_S * 1000) / frame_ms))

    vad = webrtcvad.Vad(max(0, min(3, _VAD_MODE)))

    cmd = [
        "arecord",
        "-D", _MIC_DEVICE,
        "-f", "S16_LE",
        "-r", str(_MIC_RATE),
        "-c", "1",
        "-q",
        "-t", "raw",
        "-",
    ]

    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if proc.stdout is None:
            return None

        preroll = []
        speech_run = 0
        silence_run = 0
        started = False
        collected = bytearray()

        while True:
            if is_muted() or _robot_is_speaking():
                return None

            frame = proc.stdout.read(frame_bytes)
            if not frame or len(frame) < frame_bytes:
                return None

            try:
                is_speech = vad.is_speech(frame, _MIC_RATE)
            except Exception:
                is_speech = False

            if not started:
                preroll.append(frame)
                if len(preroll) > (_VAD_START_FRAMES * 2):
                    preroll.pop(0)
                if is_speech:
                    speech_run += 1
                else:
                    speech_run = 0
                if speech_run >= _VAD_START_FRAMES:
                    started = True
                    for f in preroll:
                        collected.extend(f)
                    preroll.clear()
                continue

            collected.extend(frame)
            total_frames = len(collected) // frame_bytes
            if is_speech:
                silence_run = 0
            else:
                silence_run += 1

            # Stop after sustained silence once we have enough voiced audio.
            if total_frames >= min_frames and silence_run >= end_silence_frames:
                break
            if total_frames >= max_frames:
                break

        if len(collected) < frame_bytes:
            return None
        return _pcm_to_wav(bytes(collected))
    except FileNotFoundError:
        print("[Voice] arecord not found; install alsa-utils")
        return None
    except Exception as exc:
        print(f"[Voice] VAD record error: {exc}")
        return None
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=0.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Transcription (Whisper)
# ---------------------------------------------------------------------------
def _is_hallucination(text: str) -> bool:
    """Return True if text is a known Whisper hallucination on silence."""
    normalised = text.strip().rstrip(".!").lower().strip()
    if normalised in _WHISPER_HALLUCINATIONS:
        return True
    # Also catch variants wrapped in quotes or brackets.
    stripped = normalised.strip('"\' []()') 
    return stripped in _WHISPER_HALLUCINATIONS


def _is_url_like_hallucination(text: str) -> bool:
    """Return True for transcripts that are only URL-like tokens.

    This blocks common Whisper noise artifacts such as repeated
    "www.something.com" strings.
    """
    tokens = [t for t in text.strip().split() if t]
    if not tokens:
        return False

    canonical_urls = []
    for token in tokens:
        cleaned = token.strip().strip(".,!?;:'\"()[]{}")
        if not cleaned:
            continue
        if not _URL_TOKEN_RE.match(cleaned):
            return False
        canonical = cleaned.lower()
        if canonical.startswith("http://"):
            canonical = canonical[7:]
        elif canonical.startswith("https://"):
            canonical = canonical[8:]
        if canonical.startswith("www."):
            canonical = canonical[4:]
        canonical = canonical.rstrip("/")
        canonical_urls.append(canonical)

    if not canonical_urls:
        return False

    # All-url utterances are usually noise for this assistant; repeated same
    # URL is especially likely to be a hallucination.
    if len(canonical_urls) >= 2 and len(set(canonical_urls)) == 1:
        return True
    return len(canonical_urls) == len(tokens)


def _transcribe(wav_bytes: bytes) -> str:
    """Send WAV bytes to Whisper. Returns stripped transcript, '' if silent/hallucination."""
    client = _get_client()
    if client is None:
        return ""
    try:
        audio_file = io.BytesIO(wav_bytes)
        audio_file.name = "audio.wav"
        response = client.audio.transcriptions.create(
            model=_WHISPER_MODEL,
            file=audio_file,
            language="en",
            # A minimal prompt biases Whisper toward conversational speech and
            # away from the stock silence-hallucinations it produces.
            prompt="Conversation with a robot arm assistant.",
        )
        text = (response.text or "").strip()
        if _is_hallucination(text):
            print(f"[Voice] Whisper hallucination suppressed: {text!r}")
            return ""
        if _is_url_like_hallucination(text):
            print(f"[Voice] Whisper URL-like hallucination suppressed: {text!r}")
            return ""
        return text
    except Exception as exc:
        print(f"[Voice] Whisper error: {exc}")
        return ""


# ---------------------------------------------------------------------------
# AI Tools (poses, gripper, and face emotion via function calling)
# ---------------------------------------------------------------------------
def _get_available_poses() -> list[str]:
    """Get list of available pose names."""
    try:
        import poses
        return list(poses.POSES.keys())
    except Exception as exc:
        print(f"[Voice] Error getting poses: {exc}")
        return []


def _execute_pose(pose_name: str) -> str:
    """Execute a named pose. Returns status message."""
    try:
        import brain
        import poses
        pose_name = str(pose_name).lower()
        if pose_name not in poses.POSES:
            return f"Pose '{pose_name}' not found. Available: {', '.join(poses.POSES.keys())}"
        brain.run_pose(pose_name)
        return f"Executing pose: {pose_name}"
    except Exception as exc:
        print(f"[Voice] Error executing pose: {exc}")
        return f"Failed to execute pose: {exc}"


def _open_claw(time_ms: int = 800) -> str:
    """Open the gripper/claw. Returns status message."""
    try:
        import brain
        brain.open_claw(time_ms)
        return f"Opening claw (duration: {time_ms}ms)"
    except Exception as exc:
        print(f"[Voice] Error opening claw: {exc}")
        return f"Failed to open claw: {exc}"


def _close_claw() -> str:
    """Close the gripper/claw. Returns status message."""
    try:
        import brain
        brain.close_claw()
        return "Closing claw"
    except Exception as exc:
        print(f"[Voice] Error closing claw: {exc}")
        return f"Failed to close claw: {exc}"


def _set_face_emotion(emotion: str) -> str:
    """Set animated face emotion. Returns status message."""
    try:
        import lcd
        value = str(emotion or "").strip().lower()
        if not value:
            return "Missing emotion. Try: neutral, happy, focused, concerned."
        ok = bool(lcd.set_emotion(value))
        if not ok:
            return "Invalid emotion. Try: neutral, happy, focused, concerned."
        return f"Emotion set to: {value}"
    except Exception as exc:
        print(f"[Voice] Error setting emotion: {exc}")
        return f"Failed to set emotion: {exc}"


def _get_function_tools() -> list[dict]:
    """Return OpenAI function calling tools for poses, gripper, and face emotion."""
    return [
        {
            "type": "function",
            "function": {
                "name": "execute_pose",
                "description": "Execute a robot arm pose. The pose runs a predefined sequence of movements.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pose_name": {
                            "type": "string",
                            "description": f"Name of the pose to execute. Available poses: {', '.join(_get_available_poses())}",
                        }
                    },
                    "required": ["pose_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "open_claw",
                "description": "Open the robot's gripper/claw.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "time_ms": {
                            "type": "integer",
                            "description": "Time in milliseconds for the open motion (default 800).",
                        }
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "close_claw",
                "description": "Close the robot's gripper/claw. The claw will stop automatically when it hits the microswitch.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_face_emotion",
                "description": "Set animated face emotion.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "emotion": {
                            "type": "string",
                            "description": "Emotion name: neutral, happy, focused, or concerned.",
                        }
                    },
                    "required": ["emotion"],
                },
            },
        },
    ]


def _process_tool_call(tool_name: str, tool_input: dict) -> str:
    """Process a tool call from GPT and return the result."""
    if tool_name == "execute_pose":
        pose_name = tool_input.get("pose_name", "")
        return _execute_pose(pose_name)
    elif tool_name == "open_claw":
        time_ms = int(tool_input.get("time_ms", 800))
        return _open_claw(time_ms)
    elif tool_name == "close_claw":
        return _close_claw()
    elif tool_name == "set_face_emotion":
        return _set_face_emotion(tool_input.get("emotion", ""))
    else:
        return f"Unknown tool: {tool_name}"


def set_ai_tools_enabled(enabled: bool):
    """Toggle AI tools (poses/faces) on or off."""
    global _AI_TOOLS_ENABLED
    with _AI_TOOLS_ENABLED_LOCK:
        _AI_TOOLS_ENABLED = bool(enabled)
        status = "enabled" if _AI_TOOLS_ENABLED else "disabled"
        print(f"[Voice] AI tools {status}")


def get_ai_tools_enabled() -> bool:
    """Check if AI tools are currently enabled."""
    with _AI_TOOLS_ENABLED_LOCK:
        return _AI_TOOLS_ENABLED


# ---------------------------------------------------------------------------
# Luna webchat routing (shares Windows PC control, camera, and memory with
# typed chat). Falls back to the direct model below if unreachable.
# ---------------------------------------------------------------------------
def _call_luna_chat(user_text: str) -> str | None:
    if not _LUNA_CHAT_URL:
        return None
    try:
        import requests
        with _history_lock:
            trimmed = list(_history[-(_MAX_CONTEXT_TURNS * 2):])
        messages = [{"role": m["role"], "content": m["content"]} for m in trimmed]
        messages.append({"role": "user", "content": user_text})
        response = requests.post(
            _LUNA_CHAT_URL,
            json={"messages": messages, "profile": _LUNA_CHAT_PROFILE},
            timeout=_LUNA_CHAT_TIMEOUT_S,
            verify=_LUNA_CHAT_VERIFY_SSL,
        )
        if response.status_code != 200:
            print(f"[Voice] Luna chat error {response.status_code}: {response.text[:200]}")
            return None
        reply = str(response.json().get("reply", "")).strip()
        if not reply:
            return None
        _add_to_history(user_text, reply)
        return reply
    except Exception as exc:
        print(f"[Voice] Luna chat unreachable, falling back to direct model: {exc}")
        return None


# ---------------------------------------------------------------------------
# GPT-4 Turbo chat
# ---------------------------------------------------------------------------
def _chat(user_text: str) -> str:
    """Send utterance to GPT-4 Turbo with rolling history. Returns reply."""
    client = _get_client()
    if client is None:
        return ""
    try:
        messages = _build_messages(user_text)
        
        # Add tools if AI tools are enabled
        kwargs = {
            "model": _GPT_MODEL,
            "messages": messages,
            "max_tokens": 200,
            "temperature": max(0.0, min(1.5, _GPT_TEMPERATURE)),
        }
        
        if get_ai_tools_enabled():
            kwargs["tools"] = _get_function_tools()
        
        response = client.chat.completions.create(**kwargs)
        
        # Check if response contains tool calls (function calling)
        if response.choices[0].message.tool_calls:
            # Handle tool calls from GPT
            tool_summaries = []
            for tool_call in response.choices[0].message.tool_calls:
                tool_name = tool_call.function.name
                tool_input = {}
                try:
                    import json
                    tool_input = json.loads(tool_call.function.arguments)
                except Exception:
                    pass
                result = _process_tool_call(tool_name, tool_input)
                print(f"[Voice] Tool call: {tool_name} -> {result}")
                tool_summaries.append(f"{tool_name}: {result}")

            # Ask the model for a natural spoken confirmation instead of
            # the previous fixed phrase.
            reply = ""
            try:
                summary = " | ".join(tool_summaries)[:500]
                followup_messages = list(messages)
                followup_messages.append({
                    "role": "user",
                    "content": (
                        "You just executed these robot tool actions: "
                        + summary
                        + ". Reply briefly in one friendly sentence."
                    ),
                })
                followup = client.chat.completions.create(
                    model=_GPT_MODEL,
                    messages=followup_messages,
                    max_tokens=120,
                    temperature=max(0.0, min(1.5, _GPT_TEMPERATURE)),
                )
                reply = (followup.choices[0].message.content or "").strip()
            except Exception as exc:
                print(f"[Voice] Tool follow-up error: {exc}")

            if not reply:
                reply = f"Okay. {tool_summaries[0]}" if tool_summaries else "Okay."
        else:
            # Normal text response
            reply = (response.choices[0].message.content or "").strip()
        
        _add_to_history(user_text, reply)
        return reply
    except Exception as exc:
        print(f"[Voice] GPT error: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Main voice loop
# ---------------------------------------------------------------------------
def _voice_loop():
    if webrtcvad is None:
        print("[Voice] webrtcvad not available, using fixed chunks")
    else:
        print(f"[Voice] Listening on {_MIC_DEVICE} with VAD endpointing")

    while True:
        # --- Speaker/mute suppression: skip recording while muted or talking ---
        if is_muted() or _robot_is_speaking():
            time.sleep(0.2)
            continue

        wav = _record_utterance_vad()
        if wav is None:
            time.sleep(0.15)
            continue

        # --- Energy gate: don't send silent audio to Whisper ---
        rms = _rms(wav)
        if rms < _ENERGY_THRESHOLD:
            continue

        # --- Utterance complete: transcribe and respond ---
        transcript = _transcribe(wav)
        if transcript:
            print(f"[Voice] Heard (rms={rms:.0f}): {transcript}")
            print(f"[Voice] → GPT: {transcript}")
            reply = _call_luna_chat(transcript)
            if reply is None:
                reply = _chat(transcript)
            if reply:
                print(f"[Voice] ← GPT: {reply}")
                try:
                    import brain
                    brain.say(reply)
                except Exception as exc:
                    print(f"[Voice] say() error: {exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_started = False
_start_lock = threading.Lock()


def start():
    """Start the background voice assistant thread. Safe to call multiple times."""
    global _started
    if not _ENABLED:
        print("[Voice] Voice assistant disabled (VOICE_ASSISTANT_ENABLED=False)")
        return
    with _start_lock:
        if _started:
            return
        _started = True
    # Eagerly validate the OpenAI client at startup so errors surface immediately.
    if _get_client() is None:
        print("[Voice] Voice assistant will not start: OpenAI client unavailable")
        _started = False  # allow retry
        return
    set_muted(bool(getattr(config, "VOICE_MIC_START_MUTED", False)))
    threading.Thread(target=_voice_loop, daemon=True, name="VoiceAssistant").start()
    print("[Voice] Voice assistant started")
