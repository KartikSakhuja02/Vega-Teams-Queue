"""
utils/ocr/models.py
-------------------
Data structures for the Valorant scoreboard OCR pipeline.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FieldResult:
    """A single extracted field with confidence metadata."""
    value: Any
    confidence: float = 0.0   # 0.0–1.0
    raw_variants: list[str] = field(default_factory=list)  # raw OCR strings per variant
    source: str = "tesseract"  # 'tesseract', 'fallback', 'validated'

    def is_reliable(self, threshold: float = 0.6) -> bool:
        return self.confidence >= threshold


@dataclass
class PlayerRowStats:
    """All statistics for one player row."""
    ign: str = "Unknown"
    team: str = "Team 1"
    is_mvp: bool = False
    mvp_type: Optional[str] = None  # "Team MVP" | "Match MVP"

    acs: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    damage: int = 0
    first_bloods: int = 0
    plants: int = 0
    defuses: int = 0

    # Per-field confidence (0.0–1.0)
    ign_conf: float = 0.0
    acs_conf: float = 0.0
    kda_conf: float = 0.0
    dmg_conf: float = 0.0
    fb_conf: float = 0.0
    plants_conf: float = 0.0
    defuses_conf: float = 0.0

    @property
    def kda_str(self) -> str:
        return f"{self.kills}/{self.deaths}/{self.assists}"

    @property
    def kd_ratio(self) -> float:
        return round(self.kills / max(1, self.deaths), 2)

    @property
    def overall_confidence(self) -> float:
        scores = [self.acs_conf, self.kda_conf, self.dmg_conf]
        return round(sum(scores) / len(scores), 3)


@dataclass
class MatchOCRResult:
    """Complete OCR result for one match scoreboard."""
    success: bool = False
    error: Optional[str] = None
    engine: str = "OpenCV+Tesseract"
    processing_time_ms: float = 0.0
    confidence: float = 0.0   # overall match confidence (0.0–1.0)
    needs_review: bool = False  # True → don't auto-commit to DB

    map_name: str = "Unknown"
    match_date: str = "Unknown"
    duration: str = "Unknown"
    team1_score: int = 0
    team2_score: int = 0
    outcome: str = "Unknown"

    team1_players: list[PlayerRowStats] = field(default_factory=list)
    team2_players: list[PlayerRowStats] = field(default_factory=list)

    @property
    def all_players(self) -> list[PlayerRowStats]:
        return self.team1_players + self.team2_players
