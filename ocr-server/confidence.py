"""
ocr-server/confidence.py
-------------------------
Compute a final 0.0–1.0 confidence score from multiple signals:

  1. validation_score   — how well the result passes structural/range checks
  2. null_ratio         — fraction of player numeric fields that are null
  3. name_quality       — fraction of players with non-trivial names

Final confidence bands:
  ≥ 0.80  → HIGH   — safe to auto-commit to database
  0.60–0.79 → MEDIUM — commit but log warning
  < 0.60  → LOW    — needs_review=True, do not auto-commit

Weights are calibrated to prioritise:
  - Having all 10 players with non-null KDA (most important)
  - Having non-null ACS and damage
  - Having a parseable score
"""
from __future__ import annotations

from typing import Any, Optional


def _null_ratio(players: list[dict]) -> float:
    """Fraction of player numeric fields that are None."""
    if not players:
        return 1.0
    FIELDS = ("acs", "kills", "deaths", "assists", "damage")
    total = len(players) * len(FIELDS)
    nulls = sum(1 for p in players for f in FIELDS if p.get(f) is None)
    return nulls / total


def _name_quality(players: list[dict]) -> float:
    """Fraction of players with a meaningful (non-'Unknown') name."""
    if not players:
        return 0.0
    good = sum(1 for p in players if p.get("name") and p["name"] not in ("Unknown", ""))
    return good / len(players)


def _score_quality(team1_score: Optional[int], team2_score: Optional[int]) -> float:
    """1.0 if both scores are present and plausible, 0.5 if one is null, 0.0 if both null."""
    if team1_score is not None and team2_score is not None:
        return 1.0
    if team1_score is not None or team2_score is not None:
        return 0.5
    return 0.0


def compute_confidence(
    validation_score: float,
    parsed: dict,
) -> tuple[float, str]:
    """
    Compute final confidence from validation score + heuristics.

    Returns:
        (confidence_float, confidence_label)
    """
    players = parsed.get("players") or []
    n_players = len(players)

    # Component scores
    player_count_score = 1.0 if n_players == 10 else (0.5 if n_players >= 8 else 0.0)
    null_score         = 1.0 - _null_ratio(players)
    name_score         = _name_quality(players)
    score_score        = _score_quality(parsed.get("team1_score"), parsed.get("team2_score"))

    # Weighted average
    confidence = (
        0.35 * validation_score     # structural validity
        + 0.25 * null_score         # no null fields
        + 0.20 * player_count_score # exactly 10 players
        + 0.10 * name_score         # recognisable player names
        + 0.10 * score_score        # parseable team scores
    )
    confidence = round(min(1.0, max(0.0, confidence)), 3)

    if confidence >= 0.80:
        label = "HIGH"
    elif confidence >= 0.60:
        label = "MEDIUM"
    else:
        label = "LOW"

    return confidence, label


def annotate_result(parsed: dict, confidence: float, label: str) -> dict:
    """Add confidence metadata to the result dict returned to Railway."""
    return {
        **parsed,
        "confidence":       confidence,
        "confidence_label": label,
        "needs_review":     confidence < 0.60,
    }
