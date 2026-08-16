import base64
import asyncio
import os
import random
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
CHECKPOINT = os.getenv("COMFY_CHECKPOINT", "sd_turbo.safetensors")
NEGATIVE_PROMPT = os.getenv(
    "COMFY_NEGATIVE_PROMPT",
    "blurry, low quality, distorted, watermark, text artifacts",
)
WIDTH = int(os.getenv("COMFY_WIDTH", "512"))
HEIGHT = int(os.getenv("COMFY_HEIGHT", "512"))
STEPS = int(os.getenv("COMFY_STEPS", "4"))
CFG = float(os.getenv("COMFY_CFG", "1.0"))
SAMPLER = os.getenv("COMFY_SAMPLER", "euler")
SCHEDULER = os.getenv("COMFY_SCHEDULER", "normal")
POLL_SECONDS = float(os.getenv("COMFY_POLL_SECONDS", "0.8"))
TIMEOUT_SECONDS = float(os.getenv("COMFY_TIMEOUT_SECONDS", "120"))

app = FastAPI(title="Luna ComfyUI Image Backend")

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _assert_local_url(name: str, value: str) -> None:
    parsed = urlparse(value)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or host not in _LOCAL_HOSTS:
        raise RuntimeError(f"{name} must be a local URL (localhost/127.0.0.1). Got: {value}")


_assert_local_url("COMFY_URL", COMFY_URL)


class GenRequest(BaseModel):
    prompt: str


def _workflow(prompt: str, seed: int) -> dict[str, Any]:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CHECKPOINT}},
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": NEGATIVE_PROMPT, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": STEPS,
                "cfg": CFG,
                "sampler_name": SAMPLER,
                "scheduler": SCHEDULER,
                "denoise": 1,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "luna", "images": ["6", 0]},
        },
    }


def _extract_image_ref(history_item: dict[str, Any]) -> dict[str, str] | None:
    outputs = history_item.get("outputs", {})
    for node_output in outputs.values():
        images = node_output.get("images")
        if isinstance(images, list) and images:
            image = images[0]
            filename = image.get("filename")
            if filename:
                return {
                    "filename": filename,
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                }
    return None


async def _get_available_checkpoints(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get(f"{COMFY_URL}/object_info/CheckpointLoaderSimple")
    resp.raise_for_status()
    payload = resp.json()
    return payload["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            checkpoints = await _get_available_checkpoints(client)
    except Exception as exc:
        return {
            "ok": False,
            "mode": "comfyui",
            "comfy_url": COMFY_URL,
            "error": str(exc),
        }

    return {
        "ok": CHECKPOINT in checkpoints,
        "mode": "comfyui",
        "comfy_url": COMFY_URL,
        "checkpoint": CHECKPOINT,
        "checkpoints_found": checkpoints,
    }


@app.post("/generate")
async def generate(req: GenRequest) -> dict[str, str]:
    prompt_text = req.prompt.strip()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="Prompt is required.")

    seed = random.randint(1, 2**31 - 1)
    payload = {
        "client_id": str(uuid.uuid4()),
        "prompt": _workflow(prompt_text, seed),
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            checkpoints = await _get_available_checkpoints(client)
            if CHECKPOINT not in checkpoints:
                raise HTTPException(
                    status_code=500,
                    detail=f"Checkpoint '{CHECKPOINT}' not available in ComfyUI. Found: {checkpoints}",
                )

            queued = await client.post(f"{COMFY_URL}/prompt", json=payload)
            queued.raise_for_status()
            prompt_id = queued.json().get("prompt_id")
            if not prompt_id:
                raise HTTPException(status_code=502, detail="ComfyUI did not return prompt_id.")

            deadline = time.time() + TIMEOUT_SECONDS
            image_ref = None
            while time.time() < deadline:
                hist = await client.get(f"{COMFY_URL}/history/{prompt_id}")
                hist.raise_for_status()
                hist_payload = hist.json()
                item = hist_payload.get(prompt_id)
                if item:
                    image_ref = _extract_image_ref(item)
                    if image_ref:
                        break
                await asyncio.sleep(POLL_SECONDS)

            if not image_ref:
                raise HTTPException(status_code=504, detail="Timed out waiting for ComfyUI image output.")

            view = await client.get(
                f"{COMFY_URL}/view",
                params={
                    "filename": image_ref["filename"],
                    "subfolder": image_ref["subfolder"],
                    "type": image_ref["type"],
                },
            )
            view.raise_for_status()
            encoded = base64.b64encode(view.content).decode("ascii")
            return {"image_base64": encoded}

        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"ComfyUI request failed: {exc}") from exc
