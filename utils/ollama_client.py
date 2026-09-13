"""
utils/ollama_client.py
-----------------------
Async Ollama client for local vision AI inference.

Uses aiohttp (already in requirements) to call Ollama's REST API directly.
No extra dependencies required.

Environment variables
---------------------
  OLLAMA_BASE_URL      URL of your Ollama server.  Default: http://127.0.0.1:11434
  OLLAMA_MODEL         Model to use for vision.     Default: gemma4:e4b
  OLLAMA_TIMEOUT       Request timeout in seconds.  Default: 120
  OLLAMA_MAX_TOKENS    Max tokens in response.      Default: 1024

Important networking note
--------------------------
If the Discord bot is hosted on Railway, Railway's localhost is NOT your PC.
Set OLLAMA_BASE_URL to a tunnel URL (e.g. Cloudflare Tunnel, ngrok) that
exposes your local Ollama server. Never expose port 11434 directly to the internet.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

# ── Config (read once at import — changes require bot restart) ────────────────
_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
_MODEL      = os.getenv("OLLAMA_MODEL",    "gemma4:e4b")
_TIMEOUT    = int(os.getenv("OLLAMA_TIMEOUT",    "120"))
_MAX_TOKENS = int(os.getenv("OLLAMA_MAX_TOKENS", "1024"))

# GPU concurrency guard: RTX 2050 / 4 GB VRAM → 1 vision inference at a time.
inference_semaphore = asyncio.Semaphore(1)

# Supported MIME types
SUPPORTED_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}

# Default prompt when user sends no text with the image
DEFAULT_PROMPT = (
    "Analyze this image carefully. "
    "Describe what you see and identify important text, errors, warnings, "
    "UI elements, or other relevant information."
)

# Headers sent with every request — required to pass Cloudflare's browser
# integrity check when using a Cloudflare Tunnel URL.
_HEADERS = {
    "User-Agent": "ollama-discord-bot/1.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def is_configured() -> bool:
    """True when OLLAMA_BASE_URL is set (non-default) OR we're running locally."""
    return bool(os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_MODEL"))


async def check_connection() -> tuple[bool, str]:
    """
    Ping Ollama and verify the configured model is present.
    Returns (ok: bool, message: str).
    """
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout, headers=_HEADERS) as session:
            async with session.get(f"{_BASE_URL}/api/tags") as resp:
                if resp.status != 200:
                    return False, f"Ollama returned HTTP {resp.status}"
                data = await resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                if _MODEL not in models:
                    return False, (
                        f"Model '{_MODEL}' not found in Ollama. "
                        f"Available: {', '.join(models) or 'none'}"
                    )
                return True, f"Ollama OK — model '{_MODEL}' ready"
    except aiohttp.ClientConnectorError:
        return False, f"Ollama not reachable at {_BASE_URL}"
    except asyncio.TimeoutError:
        return False, f"Ollama connection timed out at {_BASE_URL}"
    except Exception as exc:
        return False, f"Ollama check failed: {exc}"


async def analyze_image(image_bytes: bytes, prompt: str = DEFAULT_PROMPT) -> str:
    """
    Send an image to Ollama for vision analysis.

    Parameters
    ----------
    image_bytes : Raw image bytes (PNG / JPG / WEBP).
    prompt      : The user's question or DEFAULT_PROMPT.

    Returns
    -------
    Gemma's text response.

    Raises
    ------
    RuntimeError on any connection / model / response error.
    """
    image_b64 = base64.b64encode(image_bytes).decode()

    payload = {
        "model": _MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [image_b64],
            }
        ],
        "stream": False,
        "options": {
            "num_predict": _MAX_TOKENS,
        },
    }

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_HEADERS) as session:
            async with session.post(
                f"{_BASE_URL}/api/chat",
                json=payload,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Ollama returned HTTP {resp.status}: {body[:300]}"
                    )
                data = await resp.json()

    except aiohttp.ClientConnectorError as exc:
        raise RuntimeError(
            f"Cannot connect to Ollama at {_BASE_URL}. "
            "Is Ollama running?"
        ) from exc
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Ollama timed out after {_TIMEOUT}s. "
            "The model may be loading or the image is too complex."
        ) from exc

    # Extract response text
    msg = data.get("message") or {}
    content = msg.get("content", "").strip()
    if not content:
        raise RuntimeError(
            f"Ollama returned an empty response. "
            f"done={data.get('done')}, done_reason={data.get('done_reason')}"
        )

    return content
