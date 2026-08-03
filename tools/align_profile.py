"""
tools/align_profile.py
-----------------------
Run this script locally to visually align every text field on the Profile.jpg
template.  It renders ALL fields with placeholder values and saves a preview
image so you can see exactly where each label lands.

Usage (run from the project root):
    python tools/align_profile.py

Edit the FIELDS dict below – change x/y until the text sits perfectly on the
template, then copy the final coords into utils/generate_profile.py.

Requirements:
    pip install Pillow
"""

import os
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Paths  (relative to project root — run from there)
# ---------------------------------------------------------------------------
TEMPLATE_PATH = os.path.join("GFX", "Profile.jpg")
FONT_PATH     = os.path.join("Font", "eras-itc-bold", "eras-itc-bold.ttf")
OUTPUT_PATH   = os.path.join("tools", "align_preview.png")

# ---------------------------------------------------------------------------
# Font sizes
# ---------------------------------------------------------------------------
FONT_SIZE_VALUE = 35
FONT_SIZE_SMALL = 22

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
COLOUR_WHITE  = (255, 255, 255, 255)
COLOUR_PURPLE = (180, 100, 255, 255)

# ---------------------------------------------------------------------------
# FIELDS
# Tweak x / y until every value sits perfectly over its placeholder in the
# template, then copy the whole dict to utils/generate_profile.py.
#
# Format:
#   "field_key": {
#       "value":  "<placeholder text shown on preview>",
#       "x":      <int>,   # left edge of text
#       "y":      <int>,   # top  edge of text
#       "size":   <int>,   # font size (optional — defaults to FONT_SIZE_VALUE)
#       "colour": (R,G,B,A), # colour (optional — defaults to COLOUR_WHITE)
#   }
# ---------------------------------------------------------------------------
FIELDS = {
    # ── Player Info card ─────────────────────────────────────────────────
    # Labels: "Discord:", "Created At:", "ID:"
    "discord": {
        "value":  "DarkWiz",
        "x":      950,
        "y":      532,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "created_at": {
        "value":  "Jan 5",
        "x":      1000,
        "y":      576,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "discord_id": {
        "value":  "12345678901234",
        "x":      820,
        "y":      617,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },

    # ── Game Stats card ──────────────────────────────────────────────────
    # Labels: "In Game ID:", "Rank:", "Points:", "Region:"
    "ign": {
        "value":  "DarkWizard",
        "x":      1455,
        "y":      180,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "rank": {
        "value":  "#3",
        "x":      1380,
        "y":      250,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "points": {
        "value":  "1450",
        "x":      1660,
        "y":      250,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "region": {
        "value":  "India",
        "x":      1405,
        "y":      320,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },

    # ── Player Stats card ────────────────────────────────────────────────
    # Labels: "Kills:", "KDR:", "Deaths:", "Winrate:", "Matches:", "MVP:"
    "kills": {
        "value":  "342",
        "x":      1365,
        "y":      505,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "kdr": {
        "value":  "2.41",
        "x":      1640,
        "y":      502,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "deaths": {
        "value":  "142",
        "x":      1405,
        "y":      570,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "winrate": {
        "value":  "63%",
        "x":      1690,
        "y":      570,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "matches": {
        "value":  "88",
        "x":      1430,
        "y":      645,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
    "mvp": {
        "value":  "12",
        "x":      1635,
        "y":      645,
        "size":   FONT_SIZE_VALUE,
        "colour": COLOUR_WHITE,
    },
}

# ---------------------------------------------------------------------------
# Avatar circle  (the purple blob in the Player Info card)
# ---------------------------------------------------------------------------
AVATAR_CENTRE = (940, 360)   # (x, y) pixel coords of circle centre
AVATAR_RADIUS = 140         # radius in pixels

# ---------------------------------------------------------------------------
# Render preview
# ---------------------------------------------------------------------------

def main() -> None:
    img     = Image.open(TEMPLATE_PATH).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    for key, cfg in FIELDS.items():
        size   = cfg.get("size",   FONT_SIZE_VALUE)
        colour = cfg.get("colour", COLOUR_WHITE)
        font   = ImageFont.truetype(FONT_PATH, size)

        draw.text((cfg["x"], cfg["y"]), cfg["value"], font=font, fill=colour)


    # Yellow ring showing the avatar boundary
    ax, ay = AVATAR_CENTRE
    r = AVATAR_RADIUS
    draw.ellipse(
        [ax - r, ay - r, ax + r, ay + r],
        outline=(255, 220, 0, 255),
        width=3,
    )

    out = Image.alpha_composite(img, overlay).convert("RGB")
    out.save(OUTPUT_PATH)
    print(f"\nPreview saved -> {OUTPUT_PATH}")
    print("Open the image, tweak x/y values in FIELDS, re-run, repeat until perfect.")
    print("Then copy the final FIELDS / AVATAR_CENTRE / AVATAR_RADIUS to utils/generate_profile.py.\n")


if __name__ == "__main__":
    main()
