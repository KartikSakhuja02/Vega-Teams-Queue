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
    Checks guild emojis first (if guild provided).
    Returns '<:Name:ID>' string, or '' if not found.
    NEVER returns external emojis in a guild context that would degrade to ':agent:' raw text.
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
        # If guild was provided and emoji was not found in guild emojis, return ""
        # DO NOT fall back to external emojis from other servers, because
        # Discord clients suppress external emojis without Nitro/channel perms,
        # causing broken literal text like ':cypher:' or ':jett:'.
        return ""

    # 2. Only if NO guild was provided (e.g. DM), search global bot emojis
    if hasattr(bot, "emojis"):
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
    Detect the agent portrait inside an avatar square strip.
    Combines normalized cross-correlation with 2D HSV color histogram matching.
    Returns (canonical_agent_name, confidence_score).
    """
    if not _CV2_AVAILABLE or row_strip is None or row_strip.size == 0:
        return None, 0.0

    _init_templates()
    if not _TEMPLATE_DATA:
        return None, 0.0

    rh, rw = row_strip.shape[:2]
    if rh < 10 or rw < 10:
        return None, 0.0

    target_size = int(rh * 0.90)

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

            # 60% template correlation + 40% color histogram match
            combined = 0.60 * max(0.0, float(max_tm)) + 0.40 * max(0.0, hist_sim)
            scores.append((combined, name))
        except Exception:
            continue

    if not scores:
        return None, 0.0

    scores.sort(reverse=True)
    best_score, best_agent = scores[0]
    return best_agent, best_score


def detect_agents_from_image(img_bgr: np.ndarray) -> list[dict]:
    """
    Detect all 10 players' agents and row teams from the match end-screen screenshot.
    Returns list of 10 dicts: [{'row': r, 'agent': str, 'score': float, 'team': 1|2}, ...].
    Row team 1 = Green (Team 1), Row team 2 = Red (Team 2).
    """
    if not _CV2_AVAILABLE or img_bgr is None or img_bgr.size == 0:
        return [{"row": r, "agent": None, "score": 0.0, "team": 1 if r < 5 else 2} for r in range(10)]

    h, w = img_bgr.shape[:2]

    # Calibrated Valorant Mobile scoreboard bounds:
    # Table rows span ~ 26.8% to 92.0% of image height
    y_start = 0.268 * h
    y_end = 0.920 * h
    step = (y_end - y_start) / 10.0

    # Avatar icon horizontal search window: 14.0% to 18.8% of image width
    x0, x1 = int(0.140 * w), int(0.188 * w)

    results: list[dict] = []

    for r in range(10):
        y0 = int(y_start + r * step)
        y1 = int(y_start + (r + 1) * step)
        strip = img_bgr[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]

        agent_name, score = detect_agent_in_row_strip(strip)

        # Detect row team color from table background (greenish = Team 1, reddish = Team 2)
        sample = img_bgr[max(0, y0 + 4):min(h, y1 - 4), int(0.45 * w):int(0.55 * w)]
        if sample.size > 0:
            mean_b, mean_g, mean_r = sample.mean(axis=(0, 1))
            row_team = 1 if mean_g > mean_r else 2
        else:
            row_team = 1 if r < 5 else 2

        results.append({
            "row": r,
            "agent": agent_name,
            "score": score,
            "team": row_team,
        })

    return results


def resolve_player_agents(
    result: MatchOCRResult,
    image_bytes: Optional[bytes] = None,
) -> MatchOCRResult:
    """
    Resolve agents for all players in MatchOCRResult:
    1. Runs high-precision CV detection if image_bytes is provided.
    2. Maps visual rows 0..9 to players by matching row team background colors
       and ACS ranks (since scoreboard rows are sorted by ACS descending).
    3. Falls back to VLM-extracted agent names if CV score is marginal.
    """
    if not result or not result.success:
        return result

    all_players = result.team1_players + result.team2_players
    if not all_players:
        return result

    cv_detections: list[dict] = []
    if image_bytes and _CV2_AVAILABLE:
        try:
            nparr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                cv_detections = detect_agents_from_image(img)
        except Exception as exc:
            log.warning("Agent CV detection error: %s", exc)

    def _apply_agent(p: PlayerRowStats, cv_agent: Optional[str], cv_score: float) -> None:
        vlm_agent = clean_agent_name(p.agent)
        if cv_agent and cv_score >= 0.32:
            p.agent = cv_agent
            log.debug("Player %s resolved agent by CV: %s (score=%.3f)", p.ign, cv_agent, cv_score)
        elif vlm_agent:
            p.agent = vlm_agent
        elif cv_agent and cv_score >= 0.25:
            p.agent = cv_agent
        else:
            p.agent = vlm_agent or cv_agent

    if cv_detections:
        t1_rows = [d for d in cv_detections if d.get("team") == 1]
        t2_rows = [d for d in cv_detections if d.get("team") == 2]

        # Method 1: Row background color match + descending ACS rank within team (tie-breaker: damage, kills)
        if len(t1_rows) == len(result.team1_players) and len(t2_rows) == len(result.team2_players):
            sorted_t1 = sorted(result.team1_players, key=lambda p: (-p.acs, -p.damage, -p.kills))
            for p, d in zip(sorted_t1, t1_rows):
                _apply_agent(p, d["agent"], d["score"])

            sorted_t2 = sorted(result.team2_players, key=lambda p: (-p.acs, -p.damage, -p.kills))
            for p, d in zip(sorted_t2, t2_rows):
                _apply_agent(p, d["agent"], d["score"])

        else:
            # Method 2: Global ACS rank across all 10 players (scoreboard is sorted by ACS)
            sorted_all = sorted(all_players, key=lambda p: (-p.acs, -p.damage, -p.kills))
            for idx, p in enumerate(sorted_all):
                if idx < len(cv_detections):
                    d = cv_detections[idx]
                    _apply_agent(p, d["agent"], d["score"])
    else:
        # Fallback if no CV detection: normalize VLM agent names
        for p in all_players:
            p.agent = clean_agent_name(p.agent)

    return result
