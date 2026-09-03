"""
utils/ocr_client.py
--------------------
Async HTTPS client for the RunPod Serverless OCR endpoint.

The Discord bot calls extract_scoreboard() and gets back a MatchOCRResult.
It knows nothing about RunPod, HTTP, or the VLM internals.

RunPod Serverless async flow:
  POST /run       → {"id": "job_id", "status": "IN_QUEUE"}
  GET  /status/id → {"status": "COMPLETED", "output": {...}}

We poll /status until COMPLETED, FAILED, or timeout.
On timeout or failure: raise RunPodError so the caller can fall back.

Required env vars on Railway:
  RUNPOD_API_KEY       your RunPod API key
  RUNPOD_ENDPOINT_ID   the serverless endpoint ID
  RUNPOD_TIMEOUT_S     (optional, default 120) max seconds to wait
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Optional

import aiohttp

from utils.ocr.models import MatchOCRResult, PlayerRowStats

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_API_KEY     = os.getenv("RUNPOD_API_KEY", "")
_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")
_TIMEOUT_S   = int(os.getenv("RUNPOD_TIMEOUT_S", "120"))

_RUNPOD_BASE = "https://api.runpod.ai/v2"
_POLL_INTERVAL_S = 2.0   # seconds between /status polls

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}


def is_configured() -> bool:
    """Return True if RunPod credentials are set."""
    return bool(_API_KEY and _ENDPOINT_ID)


class RunPodError(RuntimeError):
    """Raised when the RunPod request fails or times out."""


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }


async def _post_job(session: aiohttp.ClientSession, image_b64: str) -> str:
    """Submit the job and return the job ID."""
    url  = f"{_RUNPOD_BASE}/{_ENDPOINT_ID}/run"
    body = {"input": {"image": image_b64}}

    async with session.post(url, json=body, headers=_headers()) as resp:
        if resp.status not in (200, 201):
            text = await resp.text()
            raise RunPodError(f"RunPod /run returned HTTP {resp.status}: {text[:300]}")
        data = await resp.json()
        job_id = data.get("id")
        if not job_id:
            raise RunPodError(f"RunPod /run did not return a job ID: {data}")
        log.info("RunPod job submitted: %s", job_id)
        return job_id


async def _poll_status(session: aiohttp.ClientSession, job_id: str) -> dict:
    """Poll /status until the job reaches a terminal state. Returns output dict."""
    url     = f"{_RUNPOD_BASE}/{_ENDPOINT_ID}/status/{job_id}"
    deadline = time.monotonic() + _TIMEOUT_S

    while True:
        if time.monotonic() > deadline:
            raise RunPodError(f"RunPod job {job_id} timed out after {_TIMEOUT_S}s")

        async with session.get(url, headers=_headers()) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RunPodError(f"RunPod /status returned HTTP {resp.status}: {text[:200]}")
            data = await resp.json()

        status = data.get("status", "UNKNOWN")
        log.debug("Job %s status: %s", job_id, status)

        if status == "COMPLETED":
            output = data.get("output") or {}
            return output
        elif status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            err = data.get("error") or data.get("output", {}).get("error", "unknown")
            raise RunPodError(f"RunPod job {job_id} ended with status {status}: {err}")

        await asyncio.sleep(_POLL_INTERVAL_S)


# ── Response → MatchOCRResult conversion ──────────────────────────────────────

def _player_from_dict(p: dict, team_label: str) -> PlayerRowStats:
    return PlayerRowStats(
        ign=p.get("name") or "Unknown",
        team=team_label,
        is_mvp=bool(p.get("is_mvp")),
        mvp_type=p.get("mvp_type"),
        acs=p.get("acs") or 0,
        kills=p.get("kills") or 0,
        deaths=p.get("deaths") or 0,
        assists=p.get("assists") or 0,
        damage=p.get("damage") or 0,
        first_bloods=p.get("first_bloods") or 0,
        plants=p.get("plants") or 0,
        defuses=p.get("defuses") or 0,
        # Use the model's overall confidence as per-field confidence
        ign_conf=p.get("confidence", 0.7),
        acs_conf=p.get("confidence", 0.7),
        kda_conf=p.get("confidence", 0.7),
        dmg_conf=p.get("confidence", 0.7),
        fb_conf=p.get("confidence", 0.7),
        plants_conf=p.get("confidence", 0.7),
        defuses_conf=p.get("confidence", 0.7),
    )


def _result_from_output(output: dict, elapsed_ms: float) -> MatchOCRResult:
    """Convert RunPod output dict → MatchOCRResult."""
    result_data = output.get("result") or output   # handle both wrapped and direct
    players_raw = result_data.get("players") or []

    t1 = [_player_from_dict(p, "Team 1") for p in players_raw if p.get("team") == 1]
    t2 = [_player_from_dict(p, "Team 2") for p in players_raw if p.get("team") == 2]

    confidence = float(result_data.get("confidence", 0.0))
    needs_review = bool(result_data.get("needs_review", confidence < 0.60))

    return MatchOCRResult(
        success=bool(result_data.get("success", bool(players_raw))),
        error=result_data.get("error"),
        engine="Qwen2.5-VL-7B@RunPod",
        processing_time_ms=elapsed_ms,
        confidence=confidence,
        needs_review=needs_review,
        map_name=result_data.get("map") or "Unknown",
        match_date=result_data.get("match_date") or "Unknown",
        duration=result_data.get("duration") or "Unknown",
        team1_score=result_data.get("team1_score") or 0,
        team2_score=result_data.get("team2_score") or 0,
        outcome=result_data.get("outcome") or "Unknown",
        team1_players=t1,
        team2_players=t2,
    )


# ── Public API ────────────────────────────────────────────────────────────────

async def extract_scoreboard(image_bytes: bytes) -> MatchOCRResult:
    """
    Send a screenshot to RunPod and return a MatchOCRResult.

    Raises RunPodError on failure so the caller can fall back to local OCR.
    Never raises for valid responses — even low-confidence results are returned.
    """
    if not is_configured():
        raise RunPodError("RUNPOD_API_KEY or RUNPOD_ENDPOINT_ID not set")

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    t0 = time.monotonic()

    connector = aiohttp.TCPConnector(limit=4, ssl=True)
    timeout   = aiohttp.ClientTimeout(total=_TIMEOUT_S + 10)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        job_id = await _post_job(session, image_b64)
        output = await _poll_status(session, job_id)

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    log.info("RunPod job %s completed in %.0f ms", job_id, elapsed_ms)

    if not output.get("success", True):
        err = output.get("error", "RunPod returned success=false")
        raise RunPodError(err)

    return _result_from_output(output, elapsed_ms)
