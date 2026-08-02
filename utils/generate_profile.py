"""
utils/generate_profile.py
--------------------------
Generates a profile card image by compositing player stats on top of
GFX/Profile.jpg using Pillow.

Called by cogs/profile.py.  Returns a BytesIO PNG ready to send as a
Discord file attachment.

HOW TO CALIBRATE TEXT POSITIONS
    1. Run:  python tools/align_profile.py
    2. Open tools/align_preview.png
    3. Tweak the x/y values inside FIELDS (and AVATAR_CENTRE / AVATAR_RADIUS)
    4. Re-run until every value sits perfectly
    5. Copy the final values here

Requirements (add to requirements.txt):
    Pillow>=10.0.0
    aiohttp>=3.9.0       (for downloading the Discord avatar)
"""

from __future__ import annotations

import io
import os
import asyncio
import logging
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE          = os.path.dirname(os.path.abspath(__file__))
_ROOT          = os.path.dirname(_HERE)
TEMPLATE_PATH  = os.path.join(_ROOT, "GFX", "Profile.jpg")
FONT_PATH      = os.path.join(_ROOT, "Font", "eras-itc-bold", "eras-itc-bold.ttf")

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
COLOUR_WHITE  = (255, 255, 255, 255)
COLOUR_PURPLE = (180, 100, 255, 255)

# ---------------------------------------------------------------------------
# FIELDS  —  copy the tuned values from tools/align_profile.py here
#
# Each entry maps a data key → rendering config.
# The "fmt" callable (optional) transforms the raw value before rendering.
# ---------------------------------------------------------------------------
FIELDS: dict[str, dict[str, Any]] = {
    # ── Player Info card ─────────────────────────────────────────────────
    "discord": {
        "x": 588, "y": 417,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "created_at": {
        "x": 588, "y": 451,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "discord_id": {
        "x": 588, "y": 485,
        "size": 28, "colour": COLOUR_WHITE,
    },

    # ── Game Stats card ──────────────────────────────────────────────────
    "ign": {
        "x": 1050, "y": 155,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "rank": {
        "x": 1050, "y": 207,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "points": {
        "x": 1230, "y": 207,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "region": {
        "x": 1050, "y": 261,
        "size": 28, "colour": COLOUR_WHITE,
    },

    # ── Player Stats card ────────────────────────────────────────────────
    "kills": {
        "x": 1050, "y": 399,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "kdr": {
        "x": 1230, "y": 399,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "deaths": {
        "x": 1050, "y": 452,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "winrate": {
        "x": 1230, "y": 452,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "matches": {
        "x": 1050, "y": 506,
        "size": 28, "colour": COLOUR_WHITE,
    },
    "mvp": {
        "x": 1230, "y": 506,
        "size": 28, "colour": COLOUR_WHITE,
    },
}

# ---------------------------------------------------------------------------
# Avatar compositing
# ---------------------------------------------------------------------------
AVATAR_CENTRE = (714, 270)   # (x, y) pixel coords of circle centre
AVATAR_RADIUS = 120          # radius in pixels


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


def _circle_crop(img: Image.Image, diameter: int) -> Image.Image:
    """Crop *img* into a circle of the given diameter."""
    img  = img.resize((diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diameter, diameter], fill=255)
    result = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    result.paste(img.convert("RGBA"), (0, 0), mask)
    return result


async def _fetch_avatar(avatar_url: str | None, diameter: int) -> Image.Image | None:
    """Download the Discord avatar and return it as a circle-cropped RGBA image."""
    if not avatar_url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(avatar_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        avatar_img = Image.open(io.BytesIO(data)).convert("RGBA")
        return _circle_crop(avatar_img, diameter)
    except Exception as exc:
        log.warning("Failed to download avatar: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_profile_card(
    profile: dict,
    avatar_url: str | None = None,
) -> io.BytesIO:
    """
    Compose a profile card image and return it as a PNG BytesIO.

    `profile` must contain at minimum the keys returned by
    ``db.get_player_profile()``, plus ``discord_username`` and
    ``registered_at`` from the same row.

    Extra computed values added here:
      - kdr        = kills / deaths  (0 deaths → kills as float)
      - winrate    = wins / matches  (0 matches → 0 %)
      - rank       = regional_rank as "#N"
      - points     = elo
    """
    # ── derive computed fields ───────────────────────────────────────────
    kills   = profile.get("kills",   0)
    deaths  = profile.get("deaths",  0)
    wins    = profile.get("wins",    0)
    matches = profile.get("matches_played", 0)
    mvp_count = profile.get("mvp_count", 0)

    kdr      = f"{kills / deaths:.2f}" if deaths > 0 else f"{kills:.2f}"
    winrate  = f"{round(wins / matches * 100)}%" if matches > 0 else "0%"

    registered_at = profile.get("registered_at")
    created_at_str = (
        registered_at.strftime("%b %d, %Y") if registered_at else "—"
    )

    data: dict[str, str] = {
        "discord":    profile.get("discord_username", "—"),
        "created_at": created_at_str,
        "discord_id": str(profile.get("discord_id", "—")),
        "ign":        profile.get("ign", "—"),
        "rank":       f"#{profile.get('regional_rank', '—')}",
        "points":     str(profile.get("elo", 0)),
        "region":     profile.get("region", "—"),
        "kills":      str(kills),
        "kdr":        kdr,
        "deaths":     str(deaths),
        "winrate":    winrate,
        "matches":    str(matches),
        "mvp":        str(mvp_count),
    }

    # ── fetch avatar in the background while we open the template ────────
    diameter   = AVATAR_RADIUS * 2
    avatar_coro = _fetch_avatar(avatar_url, diameter)

    # Open template synchronously (fast local read)
    base    = Image.open(TEMPLATE_PATH).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    # ── paste avatar ─────────────────────────────────────────────────────
    avatar_img = await avatar_coro
    if avatar_img:
        ax, ay = AVATAR_CENTRE
        paste_x = ax - AVATAR_RADIUS
        paste_y = ay - AVATAR_RADIUS
        overlay.paste(avatar_img, (paste_x, paste_y), avatar_img)

    # ── draw text fields ─────────────────────────────────────────────────
    for key, cfg in FIELDS.items():
        text   = data.get(key, "—")
        size   = cfg.get("size",   28)
        colour = cfg.get("colour", COLOUR_WHITE)
        font   = _load_font(size)
        draw.text((cfg["x"], cfg["y"]), text, font=font, fill=colour)

    # ── composite & return ───────────────────────────────────────────────
    out    = Image.alpha_composite(base, overlay).convert("RGB")
    buf    = io.BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf
