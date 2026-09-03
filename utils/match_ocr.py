"""
utils/match_ocr.py
-------------------
Backward-compatible public entry point for the Discord bot.

The actual OCR logic lives in utils/ocr/ (modular package).
This file preserves the existing public API so no cog changes are needed.

Public API:
    await process_match_screenshot(image_bytes: bytes) -> MatchOCRResult

Data structures (re-exported for cog imports):
    PlayerRowStats
    MatchOCRResult

Enable debug output:
    set environment variable  DEBUG_OCR=true
    → saves crops + results to  debug_ocr/<timestamp>/
"""
from __future__ import annotations

import asyncio
import logging

# Re-export the data structures (cogs import from here)
from utils.ocr.models import MatchOCRResult, PlayerRowStats, FieldResult  # noqa: F401
from utils.ocr.pipeline import run_pipeline

log = logging.getLogger(__name__)


async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """
    Process a Valorant match end-screen screenshot.

    Runs the full local OCR pipeline in a thread pool so the Discord
    event loop is never blocked.

    Args:
        image_bytes: Raw bytes of the screenshot (PNG, JPEG, WebP, etc.)

    Returns:
        MatchOCRResult with:
          .success         – False if pipeline failed completely
          .needs_review    – True if confidence is too low to auto-commit to DB
          .confidence      – 0.0–1.0 overall match confidence
          .team1_players   – list[PlayerRowStats]
          .team2_players   – list[PlayerRowStats]
          (and all other fields as before)
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_pipeline, image_bytes)
