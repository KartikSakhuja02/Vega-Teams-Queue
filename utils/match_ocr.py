"""
utils/match_ocr.py
-------------------
Public entry point for the Discord bot.

Priority chain (first available wins):
  1. Amazon Bedrock  — if AWS_BEARER_TOKEN_BEDROCK is set
                       No GPU, no Docker, just an HTTPS API call.
  2. RunPod VLM      — if RUNPOD_API_KEY + RUNPOD_ENDPOINT_ID are set
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

# Cache checked-once flags so we don't re-import on every request
_bedrock_ok:  bool | None = None
_runpod_ok:   bool | None = None


def _bedrock_available() -> bool:
    global _bedrock_ok
    if _bedrock_ok is None:
        try:
            from utils.bedrock_client import is_configured
            _bedrock_ok = is_configured()
            log.info(
                "Bedrock OCR: %s",
                "ACTIVE (AWS_BEARER_TOKEN_BEDROCK found)" if _bedrock_ok
                else "inactive (AWS_BEARER_TOKEN_BEDROCK not set)"
            )
        except ImportError:
            _bedrock_ok = False
    return _bedrock_ok


def _runpod_available() -> bool:
    global _runpod_ok
    if _runpod_ok is None:
        try:
            from utils.ocr_client import is_configured
            _runpod_ok = is_configured()
            log.info(
                "RunPod OCR: %s",
                "ACTIVE" if _runpod_ok else "inactive (credentials not set)"
            )
        except ImportError:
            _runpod_ok = False
    return _runpod_ok


async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """
    Process a Valorant match scoreboard screenshot.

    Tries engines in order:
      1. Amazon Bedrock (fast, accurate, no GPU needed)
      2. RunPod VLM (GPU-based, self-hosted)
      3. Local Tesseract (always available fallback)

    Returns MatchOCRResult — never raises.
    """
    loop = asyncio.get_running_loop()

    # ── 1. Amazon Bedrock ─────────────────────────────────────────────────────
    if _bedrock_available():
        try:
            from utils.bedrock_client import extract_scoreboard
            log.info("Running Bedrock OCR…")
            result = await extract_scoreboard(image_bytes)
            log.info(
                "Bedrock: conf=%.2f needs_review=%s engine=%s",
                result.confidence, result.needs_review, result.engine,
            )
            return result
        except Exception as exc:
            log.warning("Bedrock OCR failed (%s) — trying next engine", exc)

    # ── 2. RunPod VLM ─────────────────────────────────────────────────────────
    if _runpod_available():
        try:
            from utils.ocr_client import extract_scoreboard as rp_extract
            log.info("Running RunPod VLM OCR…")
            result = await rp_extract(image_bytes)
            log.info(
                "RunPod: conf=%.2f needs_review=%s engine=%s",
                result.confidence, result.needs_review, result.engine,
            )
            return result
        except Exception as exc:
            log.warning("RunPod OCR failed (%s) — falling back to local Tesseract", exc)

    # ── 3. Local Tesseract ────────────────────────────────────────────────────
    log.info("Running local OpenCV+Tesseract pipeline…")
    return await loop.run_in_executor(None, run_pipeline, image_bytes)
