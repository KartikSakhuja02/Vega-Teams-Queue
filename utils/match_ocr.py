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
import re
from typing import Optional

from utils.ocr.models import MatchOCRResult, PlayerRowStats, FieldResult  # noqa: F401
from utils.ocr.pipeline import run_pipeline

log = logging.getLogger(__name__)

_ollama_ok:     bool | None = None
_openrouter_ok: bool | None = None


def clean_mvp_tags(name: str) -> tuple[str, bool, Optional[str]]:
    """Detect MVP badge embedded in player name and strip it cleanly."""
    if not name:
        return name, False, None

    is_mvp = False
    mvp_type = None

    if ("敌方" in name and "最佳" in name) or "敌方最佳" in name or "敌方-最佳" in name:
        is_mvp = True
        mvp_type = "Enemy MVP"
    elif ("我方" in name and "最佳" in name) or "我方最佳" in name or "我方-最佳" in name:
        is_mvp = True
        mvp_type = "Team MVP"
    elif re.search(r"\b(enemy\s*mvp)\b", name, re.IGNORECASE):
        is_mvp = True
        mvp_type = "Enemy MVP"
    elif re.search(r"\b(team\s*mvp|match\s*mvp|mvp)\b", name, re.IGNORECASE):
        is_mvp = True
        mvp_type = "Team MVP"

    # Strip badge text from name
    cleaned = re.sub(r"(我方|敌方)\s*[-—·~_]*\s*最佳", "", name)
    cleaned = re.sub(r"最佳\s*[-—·~_]*\s*(我方|敌方)", "", cleaned)
    cleaned = re.sub(r"\b(Enemy\s*MVP|Team\s*MVP|Match\s*MVP|MVP)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" -—·~_:")
    return cleaned or name, is_mvp, mvp_type


def resolve_match_mvps(result: MatchOCRResult) -> MatchOCRResult:
    """
    Resolve and validate MVP designations across both teams:
    - Photo 1 ('敌方-最佳'): Enemy Team MVP (Team 2).
    - Photo 2 ('我方-最佳'): Our Team MVP (Team 1).
    - Between the two team MVPs, the one with the highest ACS
      is designated as the overall Match MVP ('Match MVP').
    - The opposing team's MVP is labeled 'Team MVP' (Team 1) or 'Enemy MVP' (Team 2).
    """
    if not result or not result.success:
        return result

    team1_players = result.team1_players or []
    team2_players = result.team2_players or []
    all_players = team1_players + team2_players
    if not all_players:
        return result

    # 1. Clean embedded tags from names and detect tags
    for p in team1_players:
        cleaned_ign, tag_found, detected_type = clean_mvp_tags(p.ign)
        if tag_found:
            p.ign = cleaned_ign
            p.is_mvp = True
            p.mvp_type = detected_type or "Team MVP"

    for p in team2_players:
        cleaned_ign, tag_found, detected_type = clean_mvp_tags(p.ign)
        if tag_found:
            p.ign = cleaned_ign
            p.is_mvp = True
            p.mvp_type = detected_type or "Enemy MVP"

    # 2. Normalize by team
    # Team 1 is Our Team (我方)
    # Team 2 is Enemy Team (敌方)
    t1_mvps = [p for p in team1_players if p.is_mvp]
    t2_mvps = [p for p in team2_players if p.is_mvp]

    if len(t1_mvps) > 1:
        t1_mvps.sort(key=lambda x: (x.acs, x.kills), reverse=True)
        for p in t1_mvps[1:]:
            p.is_mvp = False
            p.mvp_type = None
        t1_mvps = [t1_mvps[0]]

    if len(t2_mvps) > 1:
        t2_mvps.sort(key=lambda x: (x.acs, x.kills), reverse=True)
        for p in t2_mvps[1:]:
            p.is_mvp = False
            p.mvp_type = None
        t2_mvps = [t2_mvps[0]]

    t1_mvp = t1_mvps[0] if t1_mvps else None
    t2_mvp = t2_mvps[0] if t2_mvps else None

    # 3. Designate Match MVP
    if t1_mvp and t2_mvp:
        if t1_mvp.acs >= t2_mvp.acs:
            t1_mvp.mvp_type = "Match MVP"
            t2_mvp.mvp_type = "Enemy MVP"
        else:
            t2_mvp.mvp_type = "Match MVP"
            t1_mvp.mvp_type = "Team MVP"
    elif t1_mvp:
        t1_mvp.mvp_type = "Match MVP"
    elif t2_mvp:
        t2_mvp.mvp_type = "Match MVP"
    else:
        # If OCR missed both tags completely, assign Match MVP to top ACS player
        top_player = max(all_players, key=lambda x: (x.acs, x.kills))
        top_player.is_mvp = True
        top_player.mvp_type = "Match MVP"

    return result


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
            return resolve_match_mvps(result)
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
            return resolve_match_mvps(result)
        except Exception as exc:
            log.warning("OpenRouter OCR failed (%s) — falling back to Tesseract", exc)

    # ── 3. Local Tesseract (always available) ─────────────────────────────────
    log.info("Running local OpenCV+Tesseract pipeline…")
    tess_res = await loop.run_in_executor(None, run_pipeline, image_bytes)
    return resolve_match_mvps(tess_res)
