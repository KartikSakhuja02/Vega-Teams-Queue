"""
utils/generate_leaderboard.py
-----------------------------
Renders high-definition NeatQueue-style competitive leaderboard cards
using Pillow. Composites Discord member avatars, rank badges, custom
medals (gold, silver, bronze), and rank movement indicators.
"""

from __future__ import annotations

import io
import os
import logging
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# In-memory avatar cache (URL -> Image) to keep interactions blazing fast
_AVATAR_CACHE: dict[str, Image.Image] = {}


def get_font(bold: bool = False, size: int = 15) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Segoe UI font or fallback to system fonts."""
    font_file = "segoeuib.ttf" if bold else "segoeui.ttf"
    local_path = os.path.join(_ROOT, "Font", "segoe-ui", font_file)
    if os.path.exists(local_path):
        try:
            return ImageFont.truetype(local_path, size)
        except Exception:
            pass

    win_path = os.path.join("C:/Windows/Fonts", font_file)
    if os.path.exists(win_path):
        try:
            return ImageFont.truetype(win_path, size)
        except Exception:
            pass

    # Arial fallback
    arial_file = "arialbd.ttf" if bold else "arial.ttf"
    win_arial = os.path.join("C:/Windows/Fonts", arial_file)
    if os.path.exists(win_arial):
        try:
            return ImageFont.truetype(win_arial, size)
        except Exception:
            pass

    return ImageFont.load_default()


def draw_discord_default_avatar(size: int = 24) -> Image.Image:
    """Draws a crisp Discord blurple default avatar."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=max(3, size // 5), fill=(88, 101, 242, 255))

    scale = size / 24.0
    cx, cy = size / 2.0, size / 2.0
    eye_r = 1.6 * scale
    draw.ellipse([cx - 4.5 * scale - eye_r, cy - 0.5 * scale - eye_r, cx - 4.5 * scale + eye_r, cy - 0.5 * scale + eye_r], fill=(255, 255, 255, 255))
    draw.ellipse([cx + 4.5 * scale - eye_r, cy - 0.5 * scale - eye_r, cx + 4.5 * scale + eye_r, cy - 0.5 * scale + eye_r], fill=(255, 255, 255, 255))
    draw.arc([cx - 3.5 * scale, cy - scale, cx + 3.5 * scale, cy + 3.2 * scale], start=25, end=155, fill=(255, 255, 255, 255), width=max(1, int(1.4 * scale)))
    return img


def draw_medal(rank: int, size: int = 22) -> Image.Image:
    """Draws medal with blue ribbons and numbered gold/silver/bronze coin."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Blue ribbons
    draw.polygon([(size * 0.18, 0), (size * 0.38, 0), (size * 0.5, size * 0.44), (size * 0.32, size * 0.44)], fill=(74, 144, 226, 255))
    draw.polygon([(size * 0.82, 0), (size * 0.62, 0), (size * 0.5, size * 0.44), (size * 0.68, size * 0.44)], fill=(53, 115, 196, 255))

    if rank == 1:
        outer = (220, 165, 30, 255)
        fill_col = (255, 204, 0, 255)
        text_col = (110, 75, 0, 255)
    elif rank == 2:
        outer = (150, 155, 165, 255)
        fill_col = (210, 215, 220, 255)
        text_col = (70, 75, 80, 255)
    else:
        outer = (165, 85, 35, 255)
        fill_col = (210, 125, 45, 255)
        text_col = (85, 35, 10, 255)

    cx, cy = size * 0.5, size * 0.58
    r = size * 0.34
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=outer)
    draw.ellipse([cx - r + 1, cy - r + 1, cx + r - 1, cy + r - 1], fill=fill_col)

    f = get_font(bold=True, size=max(8, int(size * 0.42)))
    num_str = str(rank)
    bbox = f.getbbox(num_str)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - tw / 2.0 - bbox[0], cy - th / 2.0 - bbox[1]), num_str, font=f, fill=text_col)
    return img


async def fetch_avatar_image(avatar_url: Optional[str]) -> Image.Image:
    """Download avatar image asynchronously with fast timeout & in-memory caching."""
    if not avatar_url:
        return draw_discord_default_avatar(24)

    if avatar_url in _AVATAR_CACHE:
        return _AVATAR_CACHE[avatar_url]

    try:
        timeout = aiohttp.ClientTimeout(total=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(avatar_url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    img = Image.open(io.BytesIO(data)).convert("RGBA")
                    if len(_AVATAR_CACHE) > 200:
                        _AVATAR_CACHE.clear()
                    _AVATAR_CACHE[avatar_url] = img
                    return img
    except Exception:
        pass

    return draw_discord_default_avatar(24)


def render_leaderboard_image(
    players: list[dict],
    avatars: Optional[dict[int, Image.Image]] = None,
    metric: str = "elo",
) -> io.BytesIO:
    """
    Renders the exact NeatQueue-style leaderboard image card.
    Returns a BytesIO PNG ready for Discord embed image attachment.
    """
    if avatars is None:
        avatars = {}

    card_width = 540
    row_height = 37
    row_gap = 2
    pad_x = 10
    pad_y = 10

    num_rows = min(10, len(players)) if players else 1
    total_height = pad_y * 2 + num_rows * (row_height + row_gap) - row_gap

    card = Image.new("RGBA", (card_width, total_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    # Outer rounded card background
    draw.rounded_rectangle(
        [0, 0, card_width - 1, total_height - 1],
        radius=8,
        fill=(24, 25, 28, 255),
        outline=(46, 48, 53, 255),
        width=1,
    )

    f_bold = get_font(bold=True, size=15)
    f_reg = get_font(bold=False, size=15)

    if not players:
        # Empty placeholder
        msg = "No players ranked yet."
        bbox = f_reg.getbbox(msg)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            ((card_width - tw) / 2.0, (total_height - th) / 2.0),
            msg,
            font=f_reg,
            fill=(140, 145, 155, 255),
        )
        buf = io.BytesIO()
        card.save(buf, format="PNG")
        buf.seek(0)
        return buf

    cur_y = pad_y
    for i, p in enumerate(players[:10]):
        rank = p.get("rank_num", i + 1)
        ign = p.get("ign") or p.get("discord_username") or "Player"
        pid = p.get("discord_id")
        elo = p.get("elo", 1000)
        wins = p.get("wins", 0)
        matches = p.get("matches_played", 0)
        losses = max(0, matches - wins)
        kills = p.get("kills", 0)
        deaths = p.get("deaths", 0)
        kd = round(kills / max(1, deaths), 2)
        mvps = p.get("mvp_count", 0)
        win_pct = round((wins / max(1, matches)) * 100, 1)
        delta = p.get("rank_delta", 0)

        row_x0 = pad_x
        row_x1 = card_width - pad_x - 1
        row_y0 = cur_y
        row_y1 = cur_y + row_height - 1

        # Background & border styling per rank
        if rank == 1:
            bg_col = (37, 34, 22, 255)
            border_col = (212, 160, 23, 255)
            accent_col = (254, 231, 92, 255)
            has_border = True
        elif rank == 2:
            bg_col = (34, 36, 39, 255)
            border_col = (142, 146, 151, 255)
            accent_col = (192, 192, 192, 255)
            has_border = True
        elif rank == 3:
            bg_col = (39, 32, 27, 255)
            border_col = (154, 83, 40, 255)
            accent_col = (205, 127, 50, 255)
            has_border = True
        else:
            bg_col = (30, 31, 35, 255) if (i % 2 == 0) else (27, 28, 32, 255)
            border_col = None
            accent_col = (88, 101, 242, 255)
            has_border = False

        if has_border:
            draw.rounded_rectangle([row_x0, row_y0, row_x1, row_y1], radius=4, fill=bg_col, outline=border_col, width=1)
        else:
            draw.rounded_rectangle([row_x0, row_y0, row_x1, row_y1], radius=3, fill=bg_col)

        # Left accent vertical bar
        bar_w = 5
        bar_r = 3
        draw.rounded_rectangle([row_x0, row_y0, row_x0 + bar_w + 1, row_y1], radius=bar_r, fill=accent_col)
        draw.rectangle([row_x0 + bar_r, row_y0, row_x0 + bar_w, row_y1], fill=accent_col)

        draw_x = row_x0 + bar_w + 6
        mid_y = (row_y0 + row_y1) / 2.0

        # Movement indicator
        if rank >= 4:
            if delta < 0:
                tri_y = mid_y - 1
                draw.polygon([(draw_x, tri_y - 4), (draw_x + 8, tri_y - 4), (draw_x + 4, tri_y + 4)], fill=(237, 66, 69, 255))
                draw_x += 10
            elif delta > 0:
                tri_y = mid_y - 1
                draw.polygon([(draw_x, tri_y + 4), (draw_x + 8, tri_y + 4), (draw_x + 4, tri_y - 4)], fill=(87, 242, 135, 255))
                draw_x += 10
            else:
                draw_x += 2

        # Rank text: "1." or "4."
        rank_str = f"{rank}."
        r_bbox = f_bold.getbbox(rank_str)
        r_h = r_bbox[3] - r_bbox[1]
        draw.text((draw_x, mid_y - r_h / 2.0 - r_bbox[1]), rank_str, font=f_bold, fill=(255, 255, 255, 255))
        draw_x += (r_bbox[2] - r_bbox[0]) + 8

        # Avatar
        av_size = 24
        av_img = avatars.get(pid)
        if av_img is None:
            av_img = draw_discord_default_avatar(av_size)
        else:
            av_img = av_img.resize((av_size, av_size), Image.Resampling.LANCZOS).convert("RGBA")
            mask = Image.new("L", (av_size, av_size), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, av_size - 1, av_size - 1], radius=4, fill=255)
            av_rounded = Image.new("RGBA", (av_size, av_size), (0, 0, 0, 0))
            av_rounded.paste(av_img, (0, 0), mask)
            av_img = av_rounded

        av_y = int(mid_y - av_size / 2.0)
        card.paste(av_img, (int(draw_x), av_y), av_img)
        draw_x += av_size + 8

        # Player Name (truncate to avoid overlapping stats)
        name_str = ign[:17]
        n_bbox = f_bold.getbbox(name_str)
        n_h = n_bbox[3] - n_bbox[1]
        draw.text((draw_x, mid_y - n_h / 2.0 - n_bbox[1]), name_str, font=f_bold, fill=(255, 255, 255, 255))
        draw_x += (n_bbox[2] - n_bbox[0]) + 6

        # Medal (if top 3)
        if rank in (1, 2, 3):
            medal_img = draw_medal(rank, size=20)
            med_y = int(mid_y - 10)
            card.paste(medal_img, (int(draw_x), med_y), medal_img)

        # Right-aligned stats
        metric_l = (metric or "elo").lower()
        if metric_l == "wins":
            stats_str = f"({wins}W) ({wins}-{losses})"
        elif metric_l in ("winrate", "win_rate"):
            stats_str = f"({win_pct}%) ({wins}-{losses})"
        elif metric_l in ("kda", "kd"):
            stats_str = f"({kd} KD) ({wins}-{losses})"
        elif metric_l in ("mvp", "mvps"):
            stats_str = f"({mvps} MVP) ({wins}-{losses})"
        else:
            stats_str = f"({elo}) ({wins}-{losses})"

        s_bbox = f_reg.getbbox(stats_str)
        s_w = s_bbox[2] - s_bbox[0]
        s_h = s_bbox[3] - s_bbox[1]
        right_x = row_x1 - 10 - s_w
        draw.text((right_x, mid_y - s_h / 2.0 - s_bbox[1]), stats_str, font=f_reg, fill=(185, 187, 190, 255))

        cur_y += row_height + row_gap

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    buf.seek(0)
    return buf
