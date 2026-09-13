"""
utils/match_ocr.py
-------------------
Public entry point for the Discord bot.

Priority chain (first available engine wins):
  1. OpenRouter VLM  — if OPENROUTER_API_KEY is set
  2. Amazon Bedrock  — if AWS_ACCESS_KEY_ID or AWS_BEARER_TOKEN_BEDROCK is set
  3. Local Tesseract — always available as last resort

The cogs only call process_match_screenshot() and get back a MatchOCRResult.
They never need to know which engine ran.

Re-exported for cog imports (unchanged API):
    PlayerRowStats, MatchOCRResult, FieldResult
"""
from __future__ import annotations

import asyncio
import logging

from utils.ocr.models import MatchOCRResult, PlayerRowStats, FieldResult  # noqa: F401
from utils.ocr.pipeline import run_pipeline

log = logging.getLogger(__name__)

_openrouter_ok: bool | None = None
_bedrock_ok:    bool | None = None


def _openrouter_available() -> bool:
    global _openrouter_ok
    if _openrouter_ok is None:
        try:
            from utils.openrouter_client import is_configured
            _openrouter_ok = is_configured()
            log.info(
                "OpenRouter OCR: %s",
                "ACTIVE (OPENROUTER_API_KEY found)" if _openrouter_ok
                else "inactive (OPENROUTER_API_KEY not set)",
            )
        except ImportError:
            _openrouter_ok = False
    return _openrouter_ok


def _bedrock_available() -> bool:
    global _bedrock_ok
    if _bedrock_ok is None:
        try:
            from utils.bedrock_client import is_configured
            _bedrock_ok = is_configured()
            log.info(
                "Bedrock OCR: %s",
                "ACTIVE" if _bedrock_ok else "inactive (no AWS credentials)",
            )
        except ImportError:
            _bedrock_ok = False
    return _bedrock_ok


async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """
    Process a Valorant match scoreboard screenshot.

    Tries engines in order — first available and successful wins.
    Never raises. Always returns a MatchOCRResult.
    """
    loop = asyncio.get_running_loop()

    # ── 1. OpenRouter (free VLM, fully async) ─────────────────────────────────
    if _openrouter_available():
        try:
            from utils.openrouter_client import extract_scoreboard
            log.info("Running OpenRouter OCR…")
            result = await extract_scoreboard(image_bytes)
            log.info(
                "OpenRouter: conf=%.2f needs_review=%s engine=%s %.0fms",
                result.confidence, result.needs_review,
                result.engine, result.processing_time_ms,
            )
            return result
        except Exception as exc:
            log.warning("OpenRouter OCR failed (%s) — trying next engine", exc)

    # ── 2. Amazon Bedrock ─────────────────────────────────────────────────────
    if _bedrock_available():
        try:
            from utils.bedrock_client import extract_scoreboard as bedrock_extract
            log.info("Running Bedrock OCR…")
            result = await bedrock_extract(image_bytes)
            log.info(
                "Bedrock: conf=%.2f needs_review=%s engine=%s %.0fms",
                result.confidence, result.needs_review,
                result.engine, result.processing_time_ms,
            )
            return result
        except Exception as exc:
            log.warning("Bedrock OCR failed (%s) — falling back to local Tesseract", exc)

    # ── 3. Local Tesseract (always available) ─────────────────────────────────
    log.info("Running local OpenCV+Tesseract pipeline…")
    return await loop.run_in_executor(None, run_pipeline, image_bytes)
