"""
utils/match_ocr.py
-------------------
Public entry point for the Discord bot.

Strategy (priority order):
  1. RunPod VLM (Qwen2.5-VL-7B) — if RUNPOD_API_KEY + RUNPOD_ENDPOINT_ID set
  2. Local OpenCV + Tesseract — always available as fallback

The cogs only call process_match_screenshot() and receive a MatchOCRResult.
They never know which engine was used.

Public API (unchanged from before):
    await process_match_screenshot(image_bytes: bytes) -> MatchOCRResult

Re-exported for cog imports:
    PlayerRowStats
    MatchOCRResult
    FieldResult
"""
from __future__ import annotations

import asyncio
import logging

from utils.ocr.models import MatchOCRResult, PlayerRowStats, FieldResult  # noqa: F401
from utils.ocr.pipeline import run_pipeline

log = logging.getLogger(__name__)

# Lazy-import the RunPod client so the bot starts even without aiohttp installed
_runpod_available: bool | None = None


def _check_runpod() -> bool:
    global _runpod_available
    if _runpod_available is None:
        try:
            from utils.ocr_client import is_configured
            _runpod_available = is_configured()
            if _runpod_available:
                log.info("RunPod OCR client configured — VLM pipeline active")
            else:
                log.info("RunPod credentials not set — using local Tesseract only")
        except ImportError:
            log.warning("utils/ocr_client.py not found — using local Tesseract only")
            _runpod_available = False
    return _runpod_available


async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """
    Process a Valorant match end-screen screenshot.

    Tries RunPod VLM first (if configured), then falls back to local Tesseract.
    Runs CPU-bound work in a thread pool to avoid blocking the Discord event loop.

    Args:
        image_bytes: Raw bytes of the screenshot (PNG, JPEG, WebP, etc.)

    Returns:
        MatchOCRResult with:
          .success         – False only if the pipeline completely failed
          .needs_review    – True if confidence is too low to auto-commit to DB
          .confidence      – 0.0–1.0 overall confidence
          .engine          – identifies which engine produced the result
          .team1_players   – list[PlayerRowStats]
          .team2_players   – list[PlayerRowStats]
    """
    loop = asyncio.get_running_loop()

    # ── 1. Try RunPod VLM ─────────────────────────────────────────────────────
    if _check_runpod():
        try:
            from utils.ocr_client import extract_scoreboard, RunPodError
            log.info("Sending screenshot to RunPod VLM…")
            result = await extract_scoreboard(image_bytes)
            log.info(
                "RunPod result: conf=%.2f needs_review=%s engine=%s",
                result.confidence, result.needs_review, result.engine,
            )
            return result
        except Exception as e:
            log.warning("RunPod OCR failed (%s) — falling back to local Tesseract", e)

    # ── 2. Local Tesseract fallback ───────────────────────────────────────────
    log.info("Running local OpenCV+Tesseract pipeline…")
    return await loop.run_in_executor(None, run_pipeline, image_bytes)
