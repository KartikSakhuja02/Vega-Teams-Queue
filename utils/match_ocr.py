"""
utils/match_ocr.py
-------------------
Public entry point for the Discord bot.

Priority chain (first available engine wins):
  1. Local Ollama (qwen2.5vl:3b) — if OLLAMA_BASE_URL is set and reachable
  2. OpenRouter VLM             — if OPENROUTER_API_KEY_2 is set
  3. Local Tesseract            — always available as last resort

The cogs only call process_match_screenshot() and get back a MatchOCRResult.

Re-exported for cog imports (unchanged API):
    PlayerRowStats, MatchOCRResult, FieldResult
"""
from __future__ import annotations

import asyncio
import logging

from utils.ocr.models import MatchOCRResult, PlayerRowStats, FieldResult  # noqa: F401
from utils.ocr.pipeline import run_pipeline

log = logging.getLogger(__name__)

_ollama_ok:     bool | None = None
_openrouter_ok: bool | None = None


def _ollama_available() -> bool:
    global _ollama_ok
    if _ollama_ok is None:
        try:
            from utils.ollama_client import is_configured
            _ollama_ok = is_configured()
            log.info(
                "Ollama OCR: %s",
                "ACTIVE" if _ollama_ok else "inactive (OLLAMA_BASE_URL / OLLAMA_MODEL not set)",
            )
        except ImportError:
            _ollama_ok = False
    return _ollama_ok


def _openrouter_available() -> bool:
    global _openrouter_ok
    if _openrouter_ok is None:
        try:
            from utils.openrouter_client import is_configured
            _openrouter_ok = is_configured()
            log.info(
                "OpenRouter OCR: %s",
                "ACTIVE (OPENROUTER_API_KEY_2 found)" if _openrouter_ok
                else "inactive (OPENROUTER_API_KEY_2 not set)",
            )
        except ImportError:
            _openrouter_ok = False
    return _openrouter_ok


async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """
    Process a Valorant match scoreboard screenshot.

    Tries engines in order — first available and successful wins.
    Never raises. Always returns a MatchOCRResult.
    """
    loop = asyncio.get_running_loop()

    # ── 1. Local Ollama (qwen2.5vl:3b — free, no rate limits) ─────────────────
    if _ollama_available():
        try:
            from utils.ollama_client import extract_scoreboard as ollama_extract
            log.info("Running Ollama OCR…")
            result = await ollama_extract(image_bytes)
            log.info(
                "Ollama: conf=%.2f needs_review=%s engine=%s %.0fms",
                result.confidence, result.needs_review,
                result.engine, result.processing_time_ms,
            )
            return result
        except Exception as exc:
            log.warning("Ollama OCR failed (%s) — trying next engine", exc)

    # ── 2. OpenRouter (cloud fallback — works when local Ollama is off) ──────
    if _openrouter_available():
        try:
            from utils.openrouter_client import extract_scoreboard as openrouter_extract
            log.info("Running OpenRouter OCR…")
            result = await openrouter_extract(image_bytes)
            log.info(
                "OpenRouter: conf=%.2f needs_review=%s engine=%s %.0fms",
                result.confidence, result.needs_review,
                result.engine, result.processing_time_ms,
            )
            return result
        except Exception as exc:
            log.warning("OpenRouter OCR failed (%s) — falling back to Tesseract", exc)

    # ── 3. Local Tesseract (always available) ─────────────────────────────────
    log.info("Running local OpenCV+Tesseract pipeline…")
    return await loop.run_in_executor(None, run_pipeline, image_bytes)
