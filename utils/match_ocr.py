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
from utils.ocr.agent_detector import (  # noqa: F401
    resolve_player_agents,
    clean_agent_name,
    get_agent_emoji,
    get_emoji_candidate_names,
)

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
    1. Highest ACS player across the entire match is ALWAYS designated Match MVP.
    2. Team 1 MVP is identified by '我方-最佳' / '我方最佳' (or top ACS on Team 1).
    3. Team 2 MVP is identified by '敌方-最佳' / '敌方最佳' (or top ACS on Team 2).
    4. Both Team MVPs and Match MVP have is_mvp = True so their MVP count increments in stats.
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

    # 2. Identify Team 1 MVP (look for tag first, else top ACS on team 1)
    t1_tagged = [p for p in team1_players if p.is_mvp]
    if t1_tagged:
        t1_mvp = max(t1_tagged, key=lambda x: (x.acs, x.kills))
    elif team1_players:
        t1_mvp = max(team1_players, key=lambda x: (x.acs, x.kills))
    else:
        t1_mvp = None

    for p in team1_players:
        if p != t1_mvp:
            p.is_mvp = False
            p.mvp_type = None

    # 3. Identify Team 2 MVP (look for tag first, else top ACS on team 2)
    t2_tagged = [p for p in team2_players if p.is_mvp]
    if t2_tagged:
        t2_mvp = max(t2_tagged, key=lambda x: (x.acs, x.kills))
    elif team2_players:
        t2_mvp = max(team2_players, key=lambda x: (x.acs, x.kills))
    else:
        t2_mvp = None

    for p in team2_players:
        if p != t2_mvp:
            p.is_mvp = False
            p.mvp_type = None

    # 4. Highest ACS player across all 10 players is ALWAYS Match MVP
    overall_top = max(all_players, key=lambda x: (x.acs, x.kills))

    if t1_mvp:
        t1_mvp.is_mvp = True
        t1_mvp.mvp_type = "Team MVP"
    if t2_mvp:
        t2_mvp.is_mvp = True
        t2_mvp.mvp_type = "Enemy MVP"

    overall_top.is_mvp = True
    overall_top.mvp_type = "Match MVP"

    return result


def _ollama_available() -> bool:
    """Ollama is disabled in favor of OpenRouter (Gemini 2.5 Flash)."""
    return False


def _openrouter_available() -> bool:
    try:
        from utils.openrouter_client import is_configured
        return is_configured()
    except ImportError:
        return False


def recover_and_validate_scores(res: MatchOCRResult, image_bytes: bytes) -> MatchOCRResult:
    """
    Ensure team1_score and team2_score are valid match round counts (0-30).
    If they are missing, 0-0, or invalid (>30, e.g. player ACS hallucinations),
    attempts to extract the true round score from the top center banner of the scoreboard.
    """
    s1 = res.team1_score
    s2 = res.team2_score
    valid = (
        s1 is not None and s2 is not None
        and 0 <= s1 <= 30 and 0 <= s2 <= 30
        and not (s1 == 0 and s2 == 0)
    )
    if valid:
        return res

    log.warning(
        "MatchOCRResult has invalid or missing round scores (s1=%s, s2=%s) — attempting recovery from top banner",
        s1, s2,
    )

    try:
        import cv2
        import numpy as np
        from utils.ocr.pipeline import _parse_score
        from utils.ocr.engines.tesseract_engine import ocr_score_region, is_available as tess_available

        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is not None:
            h, w = img.shape[:2]
            score_crop = img[0:int(h * 0.25), int(w * 0.28):int(w * 0.72)]
            if tess_available():
                score_txt = ocr_score_region(score_crop)
                if score_txt:
                    t1, t2, outc = _parse_score(score_txt)
                    if (t1 > 0 or t2 > 0) and 0 <= t1 <= 30 and 0 <= t2 <= 30:
                        log.info("Recovered round score via Tesseract: %d - %d (%s)", t1, t2, outc)
                        res.team1_score = t1
                        res.team2_score = t2
                        if outc != "Unknown":
                            res.outcome = outc
                        return res
    except Exception as e:
        log.warning("Top banner score recovery failed: %s", e)

    # Sanitize invalid scores so combat scores (e.g. 525) are never preserved
    if res.team1_score is not None and res.team1_score > 30:
        res.team1_score = None
    if res.team2_score is not None and res.team2_score > 30:
        res.team2_score = None

    return res


async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """
    Process a Valorant match scoreboard screenshot.

    Tries engines in order — first available and successful wins.
    Never raises. Always returns a MatchOCRResult.
    """
    loop = asyncio.get_running_loop()

    def _finalize(res: MatchOCRResult) -> MatchOCRResult:
        res = resolve_match_mvps(res)
        res = resolve_player_agents(res, image_bytes)
        res = recover_and_validate_scores(res, image_bytes)
        return res

    # ── 1. OpenRouter (Gemini 2.5 Flash — primary vision engine) ───────────────
    if _openrouter_available():
        try:
            from utils.openrouter_client import extract_scoreboard as openrouter_extract, get_model
            log.info("Running OpenRouter OCR (%s)…", get_model())
            result = await openrouter_extract(image_bytes)
            log.info(
                "OpenRouter: conf=%.2f needs_review=%s engine=%s %.0fms",
                result.confidence, result.needs_review,
                result.engine, result.processing_time_ms,
            )
            return _finalize(result)
        except Exception as exc:
            log.warning("OpenRouter OCR failed (%s) — falling back to Tesseract", exc)

    # ── 2. Local Tesseract (fallback) ─────────────────────────────────────────
    log.info("Running local OpenCV+Tesseract pipeline…")
    tess_res = await loop.run_in_executor(None, run_pipeline, image_bytes)
    return _finalize(tess_res)
