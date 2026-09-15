"""
Quick test — run this BEFORE deploying to Railway.
Verifies Bedrock auth + model access + image → JSON pipeline.

Usage:
    python test_bedrock.py path/to/scoreboard_screenshot.png
"""
import asyncio
import os
import sys

# ── Set your credentials here for local testing ───────────────────────────────
# DO NOT commit this file with real values filled in
os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "PASTE_YOUR_KEY_HERE"
os.environ["AWS_REGION"]               = "ap-south-1"
os.environ["BEDROCK_MODEL_ID"]         = "amazon.nova-pro-v1:0"

# ─────────────────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 2:
        print("Usage: python test_bedrock.py path/to/screenshot.png")
        sys.exit(1)

    img_path = sys.argv[1]
    print(f"\nReading image: {img_path}")

    with open(img_path, "rb") as f:
        image_bytes = f.read()
    print(f"Image size: {len(image_bytes):,} bytes")

    print("\nCalling Bedrock… (this may take 5–15 seconds)")

    from utils.bedrock_client import extract_scoreboard
    result = await extract_scoreboard(image_bytes)

    print(f"\n{'─'*50}")
    print(f"Engine:       {result.engine}")
    print(f"Confidence:   {result.confidence:.0%}")
    print(f"Needs review: {result.needs_review}")
    print(f"Speed:        {result.processing_time_ms:.0f} ms")
    print(f"Map:          {result.map_name}")
    print(f"Score:        {result.team1_score} – {result.team2_score} ({result.outcome})")
    print(f"Duration:     {result.duration}")
    print(f"{'─'*50}")

    print(f"\n🟢 Team 1 ({len(result.team1_players)} players)")
    for p in result.team1_players:
        mvp = f" [{p.mvp_type}]" if p.is_mvp else ""
        print(f"  {p.ign:<20} ACS:{p.acs:<5} K/D/A:{p.kills}/{p.deaths}/{p.assists}  DMG:{p.damage}{mvp}")

    print(f"\n🔴 Team 2 ({len(result.team2_players)} players)")
    for p in result.team2_players:
        mvp = f" [{p.mvp_type}]" if p.is_mvp else ""
        print(f"  {p.ign:<20} ACS:{p.acs:<5} K/D/A:{p.kills}/{p.deaths}/{p.assists}  DMG:{p.damage}{mvp}")

    print()
    if result.needs_review:
        print("⚠️  Low confidence — would NOT auto-commit to DB")
    else:
        print("✅  High confidence — would auto-commit to DB")

asyncio.run(main())
