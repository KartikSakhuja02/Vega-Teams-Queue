"""
ocr-server/validator.py
------------------------
Validate parsed model output against known Valorant game rules.

Validation levels:
  HARD  — impossible values (e.g. kills=200), mark field as None
  WARN  — unusual but possible values (ACS=750), lower confidence
  SOFT  — structure checks (10 players, 5 per team)

Validation does NOT raise exceptions. It returns:
  - a corrected/cleaned dict (invalid fields set to None)
  - a list of validation warnings
  - an overall validation_score (0.0–1.0)

validation_score feeds into the confidence system.
"""
from __future__ import annotations

import re
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Absolute and normal ranges per field ──────────────────────────────────────
# Format: (abs_min, abs_max, warn_if_above)
_FIELD_RANGES = {
    "acs":          (0, 1000, 700),
    "kills":        (0, 60,   35),
    "deaths":       (0, 60,   35),
    "assists":      (0, 60,   30),
    "damage":       (0, 20000, 8000),
    "first_bloods": (0, 20,   8),
    "plants":       (0, 20,   12),
    "defuses":      (0, 20,   12),
    "team1_score":  (0, 20,   13),
    "team2_score":  (0, 20,   13),
}

VALID_OUTCOMES = {"Victory", "Defeat", "Draw", "Unknown"}
_DATE_RE = re.compile(r"\d{4}[/\-]\d{2}[/\-]\d{2}(\s+\d{2}:\d{2})?")
_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _validate_int_field(value: Optional[int], field: str) -> tuple[Optional[int], list[str]]:
    if value is None:
        return None, []
    if not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None, [f"{field}: cannot convert '{value}' to int"]
    lo, hi, warn_hi = _FIELD_RANGES.get(field, (0, 99999, 99999))
    if not (lo <= value <= hi):
        return None, [f"{field}={value} outside absolute range [{lo},{hi}] — set to null"]
    warnings = []
    if value > warn_hi:
        warnings.append(f"{field}={value} is unusually high (>{warn_hi})")
    return value, warnings


def validate(parsed: dict) -> tuple[dict, list[str], float]:
    """
    Validate a parsed result dict.

    Returns:
        (cleaned_dict, warnings, validation_score)
        validation_score: 0.0 = completely invalid, 1.0 = fully valid
    """
    warnings: list[str] = []
    penalties = 0.0
    result = dict(parsed)

    # ── Score ─────────────────────────────────────────────────────────────────
    for field in ("team1_score", "team2_score"):
        v, w = _validate_int_field(result.get(field), field)
        result[field] = v
        warnings.extend(w)
        if v is None:
            penalties += 0.1

    # ── Match date ────────────────────────────────────────────────────────────
    date = result.get("match_date")
    if date and not _DATE_RE.search(str(date)):
        warnings.append(f"match_date '{date}' does not look like a date — set to null")
        result["match_date"] = None

    # ── Duration ─────────────────────────────────────────────────────────────
    dur = result.get("duration")
    if dur and not _DURATION_RE.match(str(dur)):
        warnings.append(f"duration '{dur}' is not MM:SS format — set to null")
        result["duration"] = None

    # ── Outcome ───────────────────────────────────────────────────────────────
    if result.get("outcome") not in VALID_OUTCOMES:
        result["outcome"] = "Unknown"

    # ── Players ───────────────────────────────────────────────────────────────
    players = result.get("players") or []

    if len(players) != 10:
        warnings.append(f"Expected 10 players, got {len(players)}")
        penalties += 0.4

    team1_count = sum(1 for p in players if p.get("team") == 1)
    team2_count = sum(1 for p in players if p.get("team") == 2)
    if team1_count != 5:
        warnings.append(f"Team 1 has {team1_count} players (expected 5)")
        penalties += 0.15
    if team2_count != 5:
        warnings.append(f"Team 2 has {team2_count} players (expected 5)")
        penalties += 0.15

    # Validate each player's numeric fields
    null_count = 0
    for i, p in enumerate(players):
        player_label = f"Player {i+1} ({p.get('name', '?')})"
        for field in ("acs", "kills", "deaths", "assists", "damage", "first_bloods", "plants", "defuses"):
            v, w = _validate_int_field(p.get(field), field)
            p[field] = v
            for msg in w:
                warnings.append(f"{player_label} {msg}")
            if v is None:
                null_count += 1

        # Name sanity
        name = p.get("name", "")
        if not name or name == "Unknown":
            warnings.append(f"{player_label}: name is empty or 'Unknown'")
            penalties += 0.02

        # K/D/A sanity: at least kills or deaths should be non-None
        if p.get("kills") is None and p.get("deaths") is None:
            warnings.append(f"{player_label}: both kills and deaths are null")
            penalties += 0.03

    # Penalise for a lot of null fields (indicates bad parse)
    total_player_fields = len(players) * 8  # 8 numeric fields per player
    if total_player_fields > 0:
        null_ratio = null_count / total_player_fields
        penalties += null_ratio * 0.5   # up to 0.5 penalty for all nulls

    result["players"] = players

    # ── Check for duplicate names (likely OCR duplication) ───────────────────
    names = [p.get("name", "") for p in players if p.get("name") and p.get("name") != "Unknown"]
    if len(names) != len(set(names)):
        warnings.append("Duplicate player names detected — possible OCR error")
        penalties += 0.1

    validation_score = max(0.0, min(1.0, 1.0 - penalties))
    return result, warnings, round(validation_score, 3)
