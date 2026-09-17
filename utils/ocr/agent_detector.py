"""
utils/ocr/agent_detector.py
----------------------------
Valorant agent recognition & Discord emoji integration.

Combines:
1. Canonical Agent registry & normalization (handles spelling variations like Pheonix/Phoenix, KAYO/KAY/O, Harbour/Harbor).
2. High-precision CV detection combining multi-scale normalized cross correlation with 2D HSV color histogram matching against agents/*.png.
3. Discord custom emoji resolution (guaranteed to render icon without broken :agent: text).
"""
from __future__ import annotations

import glob
import logging
import os
import re
from pathlib import Path
from typing import Optional

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

from utils.ocr.models import MatchOCRResult, PlayerRowStats

log = logging.getLogger(__name__)

# Path to agents directory
AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"

# Canonical list of Valorant / Valorant Mobile agents
CANONICAL_AGENTS = [
    "Astra", "Breach", "Brimstone", "Chamber", "Clove", "Cypher", "Deadlock",
    "Fade", "Gekko", "Harbor", "Iso", "Jett", "KAY/O", "Killjoy", "Neon",
    "Omen", "Phoenix", "Raze", "Reyna", "Sage", "Skye", "Sova", "Tejo",
    "Viper", "Vyse", "Waylay", "Yoru",
]

# Normalization map for aliases, typos, and file naming quirks
_AGENT_ALIASES: dict[str, str] = {
    "pheonix": "Phoenix",
    "phoenix": "Phoenix",
    "harbour": "Harbor",
    "harbor": "Harbor",
    "kayo": "KAY/O",
    "kay/o": "KAY/O",
    "kay-o": "KAY/O",
    "kj": "Killjoy",
    "dead lock": "Deadlock",
    "brim": "Brimstone",
}
for agent in CANONICAL_AGENTS:
    _AGENT_ALIASES[agent.lower()] = agent


def clean_agent_name(name: Optional[str]) -> Optional[str]:
    """Normalize raw agent string to canonical name."""
    if not name:
        return None

    cleaned = str(name).strip().strip("[](){}:'\"_")
    if not cleaned or cleaned.lower() in ("null", "none", "unknown"):
        return None

    key = cleaned.lower()
    if key in _AGENT_ALIASES:
        return _AGENT_ALIASES[key]

    # Substring search (e.g. "Agent: Jett")
    for alias_key, canonical in _AGENT_ALIASES.items():
        if alias_key in key:
            return canonical

    return cleaned.capitalize()


def get_emoji_candidate_names(agent_name: Optional[str]) -> list[str]:
    """
    Return list of valid emoji names to check in Discord.
    Discord emoji names only allow alphanumeric and underscores.
    """
    canon = clean_agent_name(agent_name)
    if not canon:
        return []

    names = []
    # Base alphanumeric name
    safe = re.sub(r"[^a-zA-Z0-9_]", "", canon)
    if safe:
        names.append(safe)

    # Common variations in Discord servers:
    if canon == "Phoenix":
        names.extend(["Phoenix", "Pheonix", "phoenix", "pheonix"])
    elif canon == "Harbor":
        names.extend(["Harbor", "Harbour", "harbor", "harbour"])
    elif canon == "KAY/O":
        names.extend(["KAYO", "kayo", "Kayo", "KayO"])
    else:
        names.extend([canon, canon.lower(), canon.upper()])

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            deduped.append(n)
    return deduped


def get_agent_emoji(bot, agent_name: Optional[str], guild=None) -> str:
    """
    Find Discord custom emoji for an agent.
    Checks guild emojis first (if guild provided), then all bot emojis.
    Returns '<:Name:ID>' string, or '' if not found.
    NEVER returns plain text ':agent:' so Discord won't display raw text.
    """
    if not agent_name or not bot:
        return ""

    candidates = [c.lower() for c in get_emoji_candidate_names(agent_name)]
    if not candidates:
        return ""

    # 1. Search in current guild emojis (highest priority, always renders for server members)
    if guild and hasattr(guild, "emojis"):
        # First pass: exact name match
        for emoji in guild.emojis:
            ename = emoji.name.lower()
            if ename in candidates:
                return str(emoji)
        # Second pass: substring match (e.g. "agent_iso", "val_jett", "v_fade")
        for emoji in guild.emojis:
            ename = emoji.name.lower()
            for cand in candidates:
                if len(cand) >= 3 and cand in ename:
                    return str(emoji)

    # 2. Search in global bot emojis ONLY IF bot has permission to use external emojis
    can_use_external = False
    if guild and hasattr(guild, "me") and guild.me:
        perms = guild.me.guild_permissions
        can_use_external = getattr(perms, "use_external_emojis", False)
    elif not guild:
        can_use_external = True

    if can_use_external and hasattr(bot, "emojis"):
        for emoji in bot.emojis:
            ename = emoji.name.lower()
            if ename in candidates:
                return str(emoji)

    return ""


# ── High-Precision Agent Template & Color Matcher ──────────────────────────────

_TEMPLATE_DATA: dict[str, tuple[np.ndarray, np.ndarray]] = {}  # name -> (bgr, hist_2d)
_TEMPLATES_INITIALIZED = False


def _init_templates():
    """Load and precompute templates and 2D HSV histograms for all agents."""
    global _TEMPLATE_DATA, _TEMPLATES_INITIALIZED
    if _TEMPLATES_INITIALIZED or not _CV2_AVAILABLE:
        return

    if not AGENTS_DIR.exists():
        log.warning("Agents directory not found at %s", AGENTS_DIR)
        _TEMPLATES_INITIALIZED = True
        return

    for path in glob.glob(str(AGENTS_DIR / "*.png")):
        base_name = Path(path).stem
        canonical = clean_agent_name(base_name) or base_name
        im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if im is None or im.shape[2] < 3:
            continue

        bgr = im[:, :, :3]
        if im.shape[2] == 4:
            alpha = im[:, :, 3]
            mask = (alpha > 50).astype(np.uint8)
        else:
            mask = None

        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], mask, [18, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)

        _TEMPLATE_DATA[canonical] = (bgr, hist)

    _TEMPLATES_INITIALIZED = True
    log.info("Loaded %d agent models for high-precision recognition", len(_TEMPLATE_DATA))


def detect_agent_in_row_strip(
    row_strip: np.ndarray,
) -> tuple[Optional[str], float]:
    """
    Detect the agent portrait inside a horizontal row strip.
    Combines multi-scale normalized cross-correlation with 2D HSV color histogram matching.
    Returns (canonical_agent_name, confidence_score).
    """
    if not _CV2_AVAILABLE or row_strip is None or row_strip.size == 0:
        return None, 0.0

    _init_templates()
    if not _TEMPLATE_DATA:
        return None, 0.0

    rh, rw = row_strip.shape[:2]
    if rh < 12 or rw < 12:
        return None, 0.0

    target_size = int(rh * 0.88)
    if target_size < 12:
        target_size = rh

    scores: list[tuple[float, str]] = []

    for name, (bgr, hist_t) in _TEMPLATE_DATA.items():
        try:
            t_res = cv2.resize(bgr, (target_size, target_size), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(row_strip, t_res, cv2.TM_CCOEFF_NORMED)
            _, max_tm, _, max_loc = cv2.minMaxLoc(res)

            # Crop matched box to compute color similarity
            mx, my = max_loc
            matched_box = row_strip[my:my + target_size, mx:mx + target_size]
            if matched_box.shape[0] == target_size and matched_box.shape[1] == target_size:
                box_hsv = cv2.cvtColor(matched_box, cv2.COLOR_BGR2HSV)
                hist_b = cv2.calcHist([box_hsv], [0, 1], None, [18, 16], [0, 180, 0, 256])
                cv2.normalize(hist_b, hist_b, 0, 1, cv2.NORM_MINMAX)
                hist_sim = float(cv2.compareHist(hist_b, hist_t, cv2.HISTCMP_CORREL))
            else:
                hist_sim = 0.0

            # 55% structural template match + 45% color palette match
            combined = 0.55 * max(0.0, float(max_tm)) + 0.45 * max(0.0, hist_sim)
            scores.append((combined, name))
        except Exception:
            continue

    if not scores:
        return None, 0.0

    scores.sort(reverse=True)
    best_score, best_agent = scores[0]
    return best_agent, best_score


def detect_agents_from_image(img_bgr: np.ndarray) -> list[tuple[Optional[str], float]]:
    """
    Detect all 10 players' agents from the full match end-screen screenshot.
    Returns list of 10 (agent_name, score) tuples.
    """
    if not _CV2_AVAILABLE or img_bgr is None or img_bgr.size == 0:
        return [(None, 0.0)] * 10

    h, w = img_bgr.shape[:2]

    # Calibrated Valorant Mobile scoreboard bounds:
    # Table rows span ~ 27% to 91% of image height
    y_start = 0.270 * h
    y_end = 0.915 * h
    step = (y_end - y_start) / 10.0

    # Avatar search window: x from 12% to 22% of image width
    x0, x1 = int(0.120 * w), int(0.225 * w)

    results: list[tuple[Optional[str], float]] = []

    for r in range(10):
        y0 = int(y_start + r * step)
        y1 = int(y_start + (r + 1) * step)
        strip = img_bgr[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]

        agent_name, score = detect_agent_in_row_strip(strip)
        results.append((agent_name, score))

    return results


def resolve_player_agents(
    result: MatchOCRResult,
    image_bytes: Optional[bytes] = None,
) -> MatchOCRResult:
    """
    Resolve agents for all players in MatchOCRResult:
    1. Runs high-precision CV detection if image_bytes is provided.
    2. Falls back to normalized VLM-extracted agent names if CV score is marginal.
    """
    if not result or not result.success:
        return result

    all_players = result.team1_players + result.team2_players
    if not all_players:
        return result

    cv_detections: list[tuple[Optional[str], float]] = []
    if image_bytes and _CV2_AVAILABLE:
        try:
            nparr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                cv_detections = detect_agents_from_image(img)
        except Exception as exc:
            log.warning("Agent CV detection error: %s", exc)

    for idx, p in enumerate(all_players):
        cv_agent, cv_score = cv_detections[idx] if idx < len(cv_detections) else (None, 0.0)

        # Clean any existing VLM detection
        vlm_agent = clean_agent_name(p.agent)

        if cv_agent and cv_score >= 0.35:
            p.agent = cv_agent
            log.debug("Row %d (%s) resolved agent by CV: %s (score=%.3f)", idx, p.ign, cv_agent, cv_score)
        elif vlm_agent:
            p.agent = vlm_agent
        elif cv_agent and cv_score >= 0.28:
            p.agent = cv_agent
        else:
            p.agent = vlm_agent or cv_agent

    return result
