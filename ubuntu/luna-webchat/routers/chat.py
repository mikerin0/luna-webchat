import base64
import re
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import led_controller
from config import MEMORY_TOP_K, MODEL, OLLAMA_URL, SYSTEM_PROMPT
from memory import _extract_docx_text, _identity_reply, _memory_context, _normalize_profile, _store_memory
from online_ai import _call_online_ai, _wants_big_brother, get_big_brother_mode
from pc_control import _handle_pc_command
from pi_control import (
    _handle_mic_command,
    _handle_pi_camera_query,
    _infer_gesture_intent,
    _lcd_request,
    _queue_arm_gesture,
    _queue_lcd_gpu_monitor,
)
from led import _queue_led_companion, _queue_led_request
from system_stats import (
    _linux_stats_reply,
    _linux_stats_requested,
    _model_is_too_large_for_host,
    _server_monitoring_requested,
    _server_stats_context,
)
from web_research import _web_research_context

router = APIRouter()


class Message(BaseModel):
    role: str
    content: str


class Attachment(BaseModel):
    kind: Literal["text", "image", "docx"]
    name: str
    content: str | None = None
    image_base64: str | None = None


class ChatRequest(BaseModel):
  messages: list[Message]
  attachments: list[Attachment] = []
  web_research: bool = False
  online_ai: bool = False
  profile: str = "general"
  model: str | None = None


class GenRequest(BaseModel):
    prompt: str


@router.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
  profile = _normalize_profile(req.profile)
  selected_model = (req.model or MODEL).strip() or MODEL
  model_guard = _model_is_too_large_for_host(selected_model)
  if model_guard:
    return {"reply": model_guard}
  last_user = ""
  for m in reversed(req.messages):
    if m.role == "user":
      last_user = m.content
      break

  forced = _identity_reply(last_user)
  if forced is not None:
    if not _queue_led_request(last_user):
      _queue_led_companion(last_user, forced)
    return {"reply": forced}

  if last_user:
    mic_reply = await _handle_mic_command(last_user)
    if mic_reply is not None:
      await _store_memory(profile, "user", last_user)
      await _store_memory(profile, "assistant", mic_reply)
      if not _queue_led_request(last_user):
        _queue_led_companion(last_user, mic_reply)
      return {"reply": mic_reply}

    if _linux_stats_requested(last_user):
      reply = await _linux_stats_reply()
      await _store_memory(profile, "user", last_user)
      await _store_memory(profile, "assistant", reply)
      return {"reply": reply}

    if _lcd_request(last_user) and led_controller.is_gpu_request(last_user):
      lcd_reply = "Showing live GPU usage on the LCD. It will update every few seconds until another display command replaces it."
      await _store_memory(profile, "user", last_user)
      _queue_lcd_gpu_monitor()
      return {"reply": lcd_reply}

    if led_controller.is_gpu_request(last_user):
      led_reply = "Showing live GPU usage on the LED. It will update every few seconds until another LED command replaces it."
      await _store_memory(profile, "user", last_user)
      _queue_led_request(last_user)
      return {"reply": led_reply}

    explicit_led_label = led_controller.requested_label(last_user)
    if explicit_led_label:
      led_reply = f"Displaying {explicit_led_label} on the LED."
      if led_controller.is_nasdaq_request(last_user):
        quote = await led_controller.fetch_nasdaq_quote()
        if quote:
          direction = "up" if float(quote["change"]) >= 0 else "down"
          led_reply = (
            f"Nasdaq is {float(quote['price']):,.2f}, {direction} "
            f"{abs(float(quote['percent'])):.2f}%. Displaying the price on the LED."
          )
      elif led_controller.is_weather_request(last_user):
        weather = await led_controller.fetch_local_weather()
        if weather:
          led_reply = f"Local weather is {weather['temp_f']}F with {weather['description']}. Displaying it on the LED."
      elif led_controller.is_clock_request(last_user):
        led_reply = "Showing a live clock on the LED. It will stay on until another LED command replaces it."
      await _store_memory(profile, "user", last_user)
      _queue_led_request(last_user)
      return {"reply": led_reply}

    pi_camera_reply = await _handle_pi_camera_query(last_user)
    if pi_camera_reply is not None:
      await _store_memory(profile, "user", last_user)
      if not _queue_led_request(last_user):
        _queue_led_companion(last_user, pi_camera_reply)
      return {"reply": pi_camera_reply}

    pc_reply = await _handle_pc_command(profile, last_user)
    if pc_reply is not None:
      await _store_memory(profile, "user", last_user)
      if not _queue_led_request(last_user):
        _queue_led_companion(last_user, pc_reply)
      return {"reply": pc_reply}

  payload_messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {
      "role": "system",
      "content": (
        "Use the earlier turns in this conversation as context when they are relevant to the latest user request. "
        "Treat follow-up questions as continuing the same conversation unless the user clearly asks for a new topic. "
        "Do not ignore prior turns just because they are not the most recent message."
      ),
    },
    {
      "role": "system",
      "content": (
        "You are running inside Mike's local Luna setup, not a generic hosted assistant. "
        "You have a real Pi camera analysis capability for questions about camera 1 or camera 2. "
        "Use only facts explicitly provided by the camera analysis. Treat possible detections as uncertain, never upgrade them into facts, and say plainly when the analysis cannot reliably identify something. "
        "Never guess or invent visual details, names, objects, actions, or other facts. If you do not know, say that you do not know. "
        "You also have a real Windows PC control capability through a companion Windows agent on Mike's network. "
        "When Mike asks about your capabilities, do not deny this integration. "
        "Windows actions execute immediately when Mike requests them; do not claim an action happened unless the Windows agent reports success. "
        "Supported actions include opening apps or URLs, running PowerShell commands, and reading or writing files within allowed Windows folders. "
        "Speech-friendly phrases like 'Robot, open notepad on the PC', 'Robot cancel', and 'Never mind' are valid control phrases. "
        "Windows actions only actually execute when Mike's message starts with 'robot' (e.g. 'robot open notepad') or uses the /pc command form; "
        "this system prompt is present on every turn regardless of phrasing, but that does not mean an action ran. "
        "If Mike asks you to do something on the PC without using the 'robot' keyword or /pc form, do not claim you did it or that anything opened, ran, or changed; "
        "instead say plainly that nothing happened and tell him to start the request with 'robot' to actually run it. "
        "Persistent conversation memory is enabled. Use the supplied memory context and recent memories when answering questions about what you remember, and say that you remember relevant past conversations when memory context supports it."
          " Book excerpts are reference material, not web research. Do not cite or attribute URLs found inside book excerpts as web sources."
      ),
    },
  ]
  text_ctx: list[str] = []
  image_ctx: list[str] = []
  research_ctx = ""

  if last_user:
    memory_ctx = await _memory_context(profile, last_user, MEMORY_TOP_K)
    if memory_ctx:
      payload_messages.append({"role": "system", "content": memory_ctx})

    if last_user and _server_monitoring_requested(last_user):
      stats_ctx = await _server_stats_context()
      if stats_ctx:
        payload_messages.append({"role": "system", "content": stats_ctx})
        payload_messages.append(
          {
            "role": "system",
            "content": (
              "You have current Linux server stats in the system context for this turn. "
              "Use them directly when answering questions about CPU, disk usage, temperature, load, uptime, or host health. "
              "If a value is unavailable, say so plainly instead of guessing."
            ),
          }
        )

  if req.web_research and last_user:
    research_ctx = await _web_research_context(last_user)
    if research_ctx:
      payload_messages.append({"role": "system", "content": research_ctx})
    payload_messages.append(
      {
        "role": "system",
        "content": (
          "You have already been provided current web findings in the system context for this turn. "
          "Do not say you cannot access or browse the internet. "
          "Answer using the provided findings and cite source URLs you used."
          " Only cite URLs from the explicitly labeled Web research findings; never cite URLs found in book excerpts or conversation memory."
        ),
      }
    )

  for a in req.attachments:
    if a.kind == "text" and a.content:
      text_ctx.append(f"FILE: {a.name}\\n{a.content}")
    if a.kind == "docx" and a.content:
      try:
        raw = base64.b64decode(a.content)
      except (ValueError, TypeError):
        raw = b""
      docx_text = _extract_docx_text(raw)
      if docx_text:
        if len(docx_text) > 12000:
          docx_text = docx_text[:12000] + "\n...[truncated]"
        text_ctx.append(f"FILE: {a.name}\n{docx_text}")
    if a.kind == "image" and a.image_base64:
      image_ctx.append(a.image_base64)

  if text_ctx:
    payload_messages.append(
      {
        "role": "system",
        "content": "User supplied file context follows. Use it as reference:\\n\\n" + "\\n\\n".join(text_ctx),
      }
    )

  converted = [m.model_dump() for m in req.messages]
  if image_ctx:
    last_user_idx = -1
    for i in range(len(converted) - 1, -1, -1):
      if converted[i]["role"] == "user":
        last_user_idx = i
        break
    if last_user_idx >= 0:
      converted[last_user_idx]["images"] = image_ctx

  payload_messages.extend(converted)

  want_big_brother = bool(req.online_ai) or get_big_brother_mode() or (bool(last_user) and _wants_big_brother(last_user))
  reply = ""
  if want_big_brother:
    big_brother_messages = payload_messages + [
      {
        "role": "system",
        "content": (
          "\"Big Brother\" is this system's internal nickname for you, a more capable online AI model that "
          "Mike's local assistant Luna escalates hard questions to. Do not comment on the phrase \"big brother\" "
          "or say you have no such person; just answer the underlying question directly as Luna's online backup."
        ),
      }
    ]
    reply = await _call_online_ai(big_brother_messages) or ""
    if reply:
      reply = "This is Big Brother. " + reply
    else:
      want_big_brother = False

  if not want_big_brother:
    payload = {
      "model": selected_model,
      "messages": payload_messages,
      "stream": False,
    }

    try:
      async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
    except httpx.HTTPError as exc:
      raise HTTPException(status_code=502, detail=f"Ollama unreachable: {exc}") from exc

    if r.status_code != 200:
      raise HTTPException(status_code=502, detail=f"Ollama error {r.status_code}: {r.text[:300]}")

    data = r.json()
    reply = data.get("message", {}).get("content", "")

    # One-shot correction path: if research was supplied but the model still claims
    # it cannot browse, force a retry with explicit instruction.
    if research_ctx and re.search(
      r"\b(cannot|can't|do not|don't)\b.{0,40}\b(access|browse)\b.{0,20}\binternet\b",
      reply.lower(),
    ):
      retry_messages = list(payload_messages)
      retry_messages.append(
        {
          "role": "system",
          "content": (
            "Correction: web findings were already retrieved and provided above. "
            "Do not mention inability to access internet. "
            "Provide the best answer from those findings and include source URLs."
          ),
        }
      )
      retry_payload = {
        "model": selected_model,
        "messages": retry_messages,
        "stream": False,
      }
      try:
        async with httpx.AsyncClient(timeout=180) as client:
          rr = await client.post(f"{OLLAMA_URL}/api/chat", json=retry_payload)
        if rr.status_code == 200:
          retry_data = rr.json()
          retry_reply = retry_data.get("message", {}).get("content", "").strip()
          if retry_reply:
            reply = retry_reply
      except httpx.HTTPError:
        pass

  if last_user:
    await _store_memory(profile, "user", last_user)

  if not _queue_led_request(last_user):
    _queue_led_companion(last_user, reply)

  gesture_intent = _infer_gesture_intent(last_user, reply)
  if gesture_intent:
    _queue_arm_gesture(gesture_intent)

  return {"reply": reply}
