"""
cogs/solo_queue.py
------------------
10-Man Solo Player Matchmaking Cog (Server B).
Features:
- Persistent embed sent to the designated Discord channel in Server B.
- Live view of individual players waiting in the 10-man queue (0/10).
- Automatic match formation upon 10th player joining:
  - Dequeues all 10 players atomically.
  - Generates private match lobby text channel (#match-{id}).
  - Configurable Captain Selection templates (Highest ELO, Random, First Joined, Highest Winrate).
  - Configurable Player Draft templates (Snake Draft 1-2-2-2-1, Alternating Draft, Auto ELO Balance).
  - Map Veto phase (Captains alternate banning maps until 1 map remains).
  - Optional automated voice channel generation for Team 1 and Team 2.
- Strictly NO emojis in titles, descriptions, buttons, or footers.
- Button labels strictly: "Join Queue" and "Leave Queue".
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import logging
import os
import random
import re
import time
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from cogs.bot_logger import send_log, COL_WARNING

log = logging.getLogger(__name__)

# ── Environment Configuration ────────────────────────────────────────────────
SOLO_QUEUE_CHANNEL_ID: int = int(os.environ.get("SOLO_QUEUE_CHANNEL_ID", "0"))
SOLO_MATCH_CATEGORY_ID: int = int(os.environ.get("SOLO_MATCH_CATEGORY_ID", "0"))
SOLO_VOICE_CATEGORY_ID: int = int(os.environ.get("SOLO_VOICE_CATEGORY_ID", "0"))
SOLO_QUEUE_MESSAGE_CONFIG_KEY: str = "solo_queue_message_id"

TEAM_MOD_ROLE_IDS_RAW: str = (
    os.environ.get("TEAM_MOD_ROLE_IDS", "").strip()
    or os.environ.get("HELP_ADMIN_ROLE_IDS", "")
)


def _parse_role_ids(raw_value: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw_value.split(","):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        try:
            ids.append(int(cleaned))
        except ValueError:
            pass
    return ids


STAFF_ROLE_IDS: list[int] = _parse_role_ids(TEAM_MOD_ROLE_IDS_RAW)


def _is_admin(member: discord.Member) -> bool:
    """Check if member has administrator or staff moderation privileges."""
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    return any(r.id in STAFF_ROLE_IDS for r in member.roles)


MAP_POOL_RAW = os.environ.get(
    "MAP_POOL",
    "Ascent, Bind, Haven, Split, Sunset, Lotus, Abyss",
)
MAP_POOL: list[str] = [m.strip() for m in MAP_POOL_RAW.split(",") if m.strip()]

MAPS_DIR: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps")


def get_solo_map_file(selected_map: Optional[str]) -> Optional[discord.File]:
    """Return a discord.File attachment for the selected map if an image exists in maps/."""
    if not selected_map:
        return None
    clean_name = selected_map.strip().lower()
    path = os.path.join(MAPS_DIR, f"{clean_name}.png")
    if os.path.exists(path):
        return discord.File(path, filename=f"{clean_name}.png")
    return None

CAPTAIN_SELECTION_MODE = os.environ.get("CAPTAIN_SELECTION_MODE", "HIGHEST_ELO").upper()
DRAFT_MODE = os.environ.get("DRAFT_MODE", "SNAKE").upper()
VETO_MODE = os.environ.get("VETO_MODE", "ALTERNATING_BAN").upper()

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")

# ── Dynamic Config Keys & Presets ─────────────────────────────────────────────
CONFIG_KEY_CAPTAIN_MODE = "solo_captain_mode"
CONFIG_KEY_DRAFT_MODE = "solo_draft_mode"
CONFIG_KEY_VETO_MODE = "solo_veto_mode"
CONFIG_KEY_SCORING_MODE = "solo_scoring_mode"
CONFIG_KEY_RESULTS_CHANNEL_ID = "solo_results_channel_id"
CONFIG_KEY_MAP_POOL = "solo_map_pool"
CONFIG_KEY_THEME = "solo_embed_colour"
CONFIG_KEY_QUEUE_PAUSED = "solo_queue_paused"
CONFIG_KEY_QUEUE_PAUSE_UNTIL = "solo_queue_pause_until"


def parse_duration_string(s: str) -> Optional[int]:
    """Parse duration string like '30m', '1h', '2h30m', '45s', '1d', or raw minutes into seconds."""
    s = s.strip().lower()
    if not s:
        return None
    if s.isdigit():
        return int(s) * 60

    total_seconds = 0
    pattern = re.compile(r"(\d+)\s*([dhms])")
    matches = pattern.findall(s)
    if not matches:
        m = re.match(r"^(\d+)\s*(mins?|minutes?|hours?|hrs?|sec|seconds?|days?)?$", s)
        if m:
            num = int(m.group(1))
            unit = (m.group(2) or "m").lower()
            if "d" in unit:
                return num * 86400
            elif "h" in unit:
                return num * 3600
            elif "s" in unit:
                return num
            else:
                return num * 60
        return None

    unit_multipliers = {
        "d": 86400,
        "h": 3600,
        "m": 60,
        "s": 1,
    }
    for val, unit in matches:
        total_seconds += int(val) * unit_multipliers.get(unit, 60)

    return total_seconds if total_seconds > 0 else None

RESULTS_CHANNEL_ID: int = int(os.environ.get("RESULTS_CHANNEL_ID", os.environ.get("SOLO_RESULTS_CHANNEL_ID", "0")))

THEME_PRESETS: dict[str, str] = {
    "PURPLE": "#5B4FCF",
    "VALORANT_RED": "#FF4655",
    "CYBER_CYAN": "#00F5FF",
    "GOLD": "#FFD700",
    "EMERALD": "#00E676",
    "DEFAULT": "#5B4FCF",
}


async def get_solo_captain_mode() -> str:
    val = await db.get_config(CONFIG_KEY_CAPTAIN_MODE)
    if val and val.upper() in CAPTAIN_SELECTION_TEMPLATES:
        return val.upper()
    return CAPTAIN_SELECTION_MODE


async def get_solo_draft_mode() -> str:
    val = await db.get_config(CONFIG_KEY_DRAFT_MODE)
    if val and val.upper() in ("SNAKE", "ALTERNATING", "AUTO_BALANCE"):
        return val.upper()
    return DRAFT_MODE


async def get_solo_veto_mode() -> str:
    val = await db.get_config(CONFIG_KEY_VETO_MODE)
    if val and val.upper() in ("ALTERNATING_BAN", "BAN_BAN_PICK", "RANDOM_MAP", "CAPTAIN_PICK", "MAP_VOTE", "VOTE"):
        return val.upper()
    return VETO_MODE


async def get_solo_scoring_mode() -> str:
    val = await db.get_config(CONFIG_KEY_SCORING_MODE)
    if val and val.upper() in ("DEFAULT", "PERFORMANCE"):
        return val.upper()
    return "DEFAULT"


async def get_solo_results_channel_id() -> int:
    val = await db.get_config(CONFIG_KEY_RESULTS_CHANNEL_ID)
    if val and str(val).isdigit():
        return int(val)
    return RESULTS_CHANNEL_ID


def calculate_player_elo(
    scoring_mode: str,
    is_winner: bool,
    is_draw: bool,
    is_mvp: bool,
    mvp_type: Optional[str],
    kills: int,
    deaths: int,
    assists: int,
    acs: int,
    damage: int,
    first_bloods: int,
) -> int:
    """
    Calculate the ELO adjustment (+/-) for a player based on match outcome and template.
    Templates:
      - 'DEFAULT': Flat ELO (+25 win, -20 loss, 0 draw) + 5 Match MVP bonus.
      - 'PERFORMANCE': Combat performance-scaled ELO with carry protection.
    """
    kd_ratio = kills / max(1, deaths)
    is_match_mvp = is_mvp and (mvp_type == "Match MVP" or "match" in str(mvp_type).lower())
    is_team_mvp = (not is_match_mvp) and (
        is_mvp
        or mvp_type in ("Team MVP", "Enemy MVP")
        or "team" in str(mvp_type).lower()
        or "enemy" in str(mvp_type).lower()
    )

    mvp_bonus = 5 if is_match_mvp else (2 if is_team_mvp else 0)

    if scoring_mode != "PERFORMANCE":
        if is_draw:
            return mvp_bonus
        base = 25 if is_winner else -20
        return base + mvp_bonus

    # PERFORMANCE MODE:
    perf_mod = 0

    # 1. K/D Ratio impact
    if kd_ratio >= 2.0:
        perf_mod += 6
    elif kd_ratio >= 1.5:
        perf_mod += 4
    elif kd_ratio >= 1.2:
        perf_mod += 2
    elif kd_ratio >= 0.9:
        perf_mod += 0
    elif kd_ratio >= 0.6:
        perf_mod -= 3
    else:
        perf_mod -= 5

    # 2. ACS impact
    if acs >= 300:
        perf_mod += 4
    elif acs >= 240:
        perf_mod += 2
    elif acs >= 180:
        perf_mod += 0
    elif acs > 0 and acs < 120:
        perf_mod -= 3

    # 3. First Bloods impact
    if first_bloods >= 4:
        perf_mod += 3
    elif first_bloods >= 2:
        perf_mod += 1

    if is_draw:
        delta = perf_mod + mvp_bonus
        return max(-5, min(10, delta))

    if is_winner:
        total = 20 + perf_mod + mvp_bonus
        return max(12, min(38, total))
    else:
        # Carry protection on loss:
        total = -20 + perf_mod + mvp_bonus
        return max(-28, min(-8, total))


async def get_solo_map_pool() -> list[str]:
    val = await db.get_config(CONFIG_KEY_MAP_POOL)
    if val:
        maps = [m.strip() for m in val.split(",") if m.strip()]
        if len(maps) >= 1:
            return maps
    return list(MAP_POOL)


async def get_solo_embed_colour() -> discord.Colour:
    val = await db.get_config(CONFIG_KEY_THEME)
    if val:
        hex_code = THEME_PRESETS.get(val.upper(), val)
        try:
            return discord.Colour.from_str(hex_code)
        except Exception:
            pass
    return EMBED_COLOUR


async def _get_or_fetch_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except Exception:
        return None


# =============================================================================
# Captain Selection Templates (Modular & Customizable)
# =============================================================================

def select_captains_highest_elo(players: list[dict]) -> tuple[dict, dict]:
    """Top 2 highest ELO players become captains."""
    sorted_p = sorted(players, key=lambda p: p.get("elo", 1000), reverse=True)
    return sorted_p[0], sorted_p[1]


def select_captains_random(players: list[dict]) -> tuple[dict, dict]:
    """2 random players from the 10-man lobby become captains."""
    shuffled = list(players)
    random.shuffle(shuffled)
    return shuffled[0], shuffled[1]


def select_captains_first_joined(players: list[dict]) -> tuple[dict, dict]:
    """The first 2 players who joined the queue become captains."""
    sorted_p = sorted(players, key=lambda p: p.get("joined_at") or 0)
    return sorted_p[0], sorted_p[1]


def select_captains_highest_winrate(players: list[dict]) -> tuple[dict, dict]:
    """Top 2 players with the highest win percentage become captains."""
    def winrate(p: dict) -> float:
        played = p.get("matches_played", 0)
        return (p.get("wins", 0) / played) if played > 0 else 0.0

    sorted_p = sorted(players, key=winrate, reverse=True)
    return sorted_p[0], sorted_p[1]


CAPTAIN_SELECTION_TEMPLATES: dict[str, Callable[[list[dict]], tuple[dict, dict]]] = {
    "HIGHEST_ELO": select_captains_highest_elo,
    "RANDOM": select_captains_random,
    "FIRST_JOINED": select_captains_first_joined,
    "HIGHEST_WINRATE": select_captains_highest_winrate,
}


def select_captains(players: list[dict], mode: Optional[str] = None) -> tuple[dict, dict]:
    """Select 2 captains based on the configured template."""
    chosen_mode = (mode or CAPTAIN_SELECTION_MODE).upper()
    handler = CAPTAIN_SELECTION_TEMPLATES.get(chosen_mode, select_captains_highest_elo)
    return handler(players)


def auto_balance_teams(players: list[dict]) -> tuple[list[int], list[int]]:
    """Divide 10 players into two 5-player teams to minimize total ELO difference."""
    sorted_players = sorted(players, key=lambda p: p.get("elo", 1000), reverse=True)
    t1: list[dict] = []
    t2: list[dict] = []
    t1_sum = 0
    t2_sum = 0

    for p in sorted_players:
        elo = p.get("elo", 1000)
        if len(t1) == 5:
            t2.append(p)
            t2_sum += elo
        elif len(t2) == 5:
            t1.append(p)
            t1_sum += elo
        elif t1_sum <= t2_sum:
            t1.append(p)
            t1_sum += elo
        else:
            t2.append(p)
            t2_sum += elo

    return [p["discord_id"] for p in t1], [p["discord_id"] for p in t2]


# =============================================================================
# Player Draft Templates (Modular & Customizable)
# =============================================================================

# Standard Snake Draft Sequence (1-2-2-2-1):
# Step 1: C1 picks 1
# Step 2: C2 picks 1
# Step 3: C2 picks 1
# Step 4: C1 picks 1
# Step 5: C1 picks 1
# Step 6: C2 picks 1
# Step 7: C2 picks 1
# Step 8 (automatic): Last remaining player goes to C1.
DRAFT_SNAKE_SEQUENCE: list[tuple[int, int]] = [
    (1, 1),
    (2, 2),
    (3, 2),
    (4, 1),
    (5, 1),
    (6, 2),
    (7, 2),
]

# Alternating Draft Sequence (1-1-1-1-1-1-1-1):
DRAFT_ALTERNATING_SEQUENCE: list[tuple[int, int]] = [
    (1, 1),
    (2, 2),
    (3, 1),
    (4, 2),
    (5, 1),
    (6, 2),
    (7, 1),
]


def get_draft_active_captain_id(
    draft_step: int,
    c1_id: int,
    c2_id: int,
    mode: Optional[str] = None,
) -> int:
    """Return which captain picks on the given draft step."""
    chosen_mode = (mode or DRAFT_MODE).upper()
    seq = (
        DRAFT_ALTERNATING_SEQUENCE
        if chosen_mode == "ALTERNATING"
        else DRAFT_SNAKE_SEQUENCE
    )
    for step_num, cap_num in seq:
        if step_num == draft_step:
            return c1_id if cap_num == 1 else c2_id
    return c1_id


# =============================================================================
# Embed Builders (Strictly ZERO Emojis)
# =============================================================================

def build_solo_queue_embed(
    queued_players: list[dict],
    colour: Optional[discord.Colour] = None,
    is_paused: bool = False,
    pause_until: Optional[float] = None,
) -> discord.Embed:
    """Queue panel embed with numbered player list, ELO, region, and join time."""
    count = len(queued_players)

    if is_paused:
        if pause_until:
            desc = f"`[ {count} / 10 ]`\n\n**Queue Paused** — Reopens <t:{int(pause_until)}:R>"
        else:
            desc = f"`[ {count} / 10 ]`\n\n**Queue Paused** — Closed indefinitely by staff"
        embed_col = discord.Colour(0xE74C3C)
        title_text = "VEGA QUEUE [PAUSED]"
    else:
        desc = f"`[ {count} / 10 ]`"
        embed_col = colour or EMBED_COLOUR
        title_text = "VEGA QUEUE"

    embed = discord.Embed(
        title=title_text,
        description=desc,
        colour=embed_col,
    )

    if queued_players:
        lines: list[str] = []
        for idx, p in enumerate(queued_players, 1):
            ign = p.get("ign") or p.get("discord_username") or "Player"
            elo = p.get("elo", 1000)
            region = p.get("region") or "Global"
            ts = int(p["joined_at"].timestamp()) if p.get("joined_at") else 0
            time_str = f" • <t:{ts}:R>" if ts else ""
            lines.append(f"`{idx}.` **{ign}** — `{elo} ELO` `[{region}]`{time_str}")
        embed.add_field(name="Players", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Players", value="*No players in queue yet*", inline=False)

    embed.set_footer(text="Vega Esports • 10-Man Queue")
    return embed


def build_solo_checkin_embed(
    match: dict,
    players_by_id: dict[int, dict],
    connected_pids: set[int],
    lobby_vc_id: int,
    colour: Optional[discord.Colour] = None,
) -> discord.Embed:
    """Minimalist voice check-in embed."""
    all_pids = list(dict.fromkeys(
        match.get("team1_player_ids", [])
        + match.get("team2_player_ids", [])
        + match.get("available_player_ids", [])
    ))
    checked_count = sum(1 for pid in all_pids if pid in connected_pids)

    lines = []
    for pid in all_pids:
        ign = players_by_id.get(pid, {}).get("ign") or f"Player"
        icon = "🟢" if pid in connected_pids else "🔴"
        lines.append(f"{icon} {ign}")

    embed = discord.Embed(
        title=f"QUEUE #{match['id']} — VOICE CHECK-IN",
        description=(
            f"`[ {checked_count} / {len(all_pids)} in Voice ]` — <#{lobby_vc_id}>\n\n"
            + "  ".join(lines)
        ),
        colour=colour or EMBED_COLOUR,
    )
    embed.set_footer(text="Draft starts when all 10 are in voice.")
    return embed


def build_solo_draft_embed(
    match: dict,
    players_by_id: dict[int, dict],
    colour: Optional[discord.Colour] = None,
) -> discord.Embed:
    """Ultra-minimalist draft embed — no emojis, plain text only."""
    c1_id = match["captain1_id"]
    c2_id = match["captain2_id"]
    turn_id = match["current_turn_captain_id"]
    step = match["draft_step"]

    t1_ids = match.get("team1_player_ids", [])
    t2_ids = match.get("team2_player_ids", [])
    avail_ids = match.get("available_player_ids", [])

    def _name(pid: int) -> str:
        return players_by_id.get(pid, {}).get("ign") or str(pid)

    # Build team columns: captain first, then picks, then empty slots
    def _team_col(cap_id: int, ids: list[int]) -> str:
        rows = [f"{_name(cap_id)} (cap)"]
        rows += [_name(p) for p in ids if p != cap_id]
        rows += ["-" for _ in range(5 - len(ids))]
        return "\n".join(rows)

    picker = _name(turn_id)
    embed = discord.Embed(
        title=f"Queue {match['id']}  —  Draft  [{step}/7]",
        description=f"{picker}'s pick",
        colour=colour or EMBED_COLOUR,
    )
    embed.add_field(name=f"Team A  {len(t1_ids)}/5", value=_team_col(c1_id, t1_ids), inline=True)
    embed.add_field(name=f"Team B  {len(t2_ids)}/5", value=_team_col(c2_id, t2_ids), inline=True)
    if avail_ids:
        embed.add_field(
            name="Available",
            value=",  ".join(_name(p) for p in avail_ids),
            inline=False,
        )
    return embed


def build_solo_map_veto_embed(
    match: dict,
    players_by_id: dict[int, dict],
    colour: Optional[discord.Colour] = None,
) -> discord.Embed:
    """Map veto and final Match Ready embed matching the vertical reference UI."""
    status = match.get("status", "MAP_VETO")
    turn_id = match.get("current_turn_captain_id")
    selected_map = match.get("selected_map")
    avail_maps = match.get("available_maps", [])
    banned_maps = match.get("banned_maps", [])
    c1_id = match.get("captain1_id")
    c2_id = match.get("captain2_id")

    t1_ids = match.get("team1_player_ids") or ([c1_id] if c1_id else [])
    t2_ids = match.get("team2_player_ids") or ([c2_id] if c2_id else [])

    if status == "IN_PROGRESS":
        # Calculate Team 1 and Team 2 average ELO
        t1_elos = [players_by_id.get(pid, {}).get("elo", 1000) for pid in t1_ids]
        t2_elos = [players_by_id.get(pid, {}).get("elo", 1000) for pid in t2_ids]
        t1_avg = round(sum(t1_elos) / len(t1_elos)) if t1_elos else 1000
        t2_avg = round(sum(t2_elos) / len(t2_elos)) if t2_elos else 1000

        # Ensure captain is listed first in mentions
        ordered_t1 = ([c1_id] if c1_id in t1_ids else []) + [pid for pid in t1_ids if pid != c1_id]
        ordered_t2 = ([c2_id] if c2_id in t2_ids else []) + [pid for pid in t2_ids if pid != c2_id]

        t1_mentions = " , ".join(f"<@{pid}>" for pid in ordered_t1) if ordered_t1 else "-"
        t2_mentions = " , ".join(f"<@{pid}>" for pid in ordered_t2) if ordered_t2 else "-"

        v1_id = match.get("voice_team1_id")
        v2_id = match.get("voice_team2_id")

        details_lines = []
        if v1_id:
            details_lines.append(f"<#{v1_id}>")
        if v2_id:
            details_lines.append(f"<#{v2_id}>")
        if selected_map:
            details_lines.append(f"Map: **{selected_map}**")
        details_lines.append("\n> Use `/submit-result` when the match concludes.")

        desc = (
            f"**__Team 1 - {t1_avg}__**\n"
            f"{t1_mentions}\n\n"
            f"**__Team 2 - {t2_avg}__**\n"
            f"{t2_mentions}\n\n"
            f"**Match Details**\n"
            + "\n".join(details_lines)
        )

        embed = discord.Embed(
            title=f"⚔️ Queue#{match['id']}",
            description=desc,
            colour=discord.Colour(0xE74C3C),
            timestamp=datetime.now(timezone.utc),
        )

        clean_map = selected_map.strip().lower() if selected_map else ""
        if clean_map:
            map_path = os.path.join(MAPS_DIR, f"{clean_map}.png")
            if os.path.exists(map_path):
                embed.set_image(url=f"attachment://{clean_map}.png")

        return embed
    else:
        def _names(ids: list) -> str:
            parts = []
            for pid in ids:
                p = players_by_id.get(pid, {})
                name = p.get("ign") or p.get("username") or f"<@{pid}>"
                if pid in (c1_id, c2_id):
                    parts.append(f"{name} (cap)")
                else:
                    parts.append(name)
            return ",  ".join(parts) or "-"

        t1 = _names(t1_ids)
        t2 = _names(t2_ids)
        action = "Pick" if len(avail_maps) == 2 else "Ban"
        picker = players_by_id.get(turn_id, {}).get("ign") if turn_id else None
        if not picker and turn_id:
            picker = f"<@{turn_id}>"
        elif not picker:
            picker = "Captain"
        avail_str = ",  ".join(avail_maps) if avail_maps else "-"
        banned_str = ",  ".join(f"~~{m}~~" for m in banned_maps) if banned_maps else "-"
        desc = (
            f"{picker} — {action}\n\n"
            f"Maps: {avail_str}\n"
            f"Banned: {banned_str}\n\n"
            f"Team A — {t1}\n"
            f"Team B — {t2}"
        )
        embed = discord.Embed(
            title=f"Queue {match['id']}  —  Map Veto",
            description=desc,
            colour=colour or EMBED_COLOUR,
        )
        return embed


def build_solo_map_vote_embed(
    match: dict,
    players_by_id: dict[int, dict],
    map_options: list[str],
    votes_by_user: dict[int, str],
    end_time: float,
    colour: Optional[discord.Colour] = None,
) -> discord.Embed:
    """Minimalist map voting embed — 4 random maps, 1-minute countdown, live vote counts."""
    t1_ids = match.get("team1_player_ids", [])
    t2_ids = match.get("team2_player_ids", [])

    def _names(ids: list) -> str:
        parts = [players_by_id.get(pid, {}).get("ign") or str(pid) for pid in ids]
        return ",  ".join(parts) or "-"

    t1 = _names(t1_ids)
    t2 = _names(t2_ids)

    counts = {m: 0 for m in map_options}
    for m in votes_by_user.values():
        if m in counts:
            counts[m] += 1

    total_voted = len(votes_by_user)
    lines = [
        f"Voting ends <t:{int(end_time)}:R>  ({total_voted}/10 voted)\n",
    ]
    for m in map_options:
        lines.append(f"**{m}** — {counts[m]} votes")

    lines.append(f"\nTeam A — {t1}")
    lines.append(f"Team B — {t2}")

    return discord.Embed(
        title=f"Queue {match['id']}  —  Map Vote",
        description="\n".join(lines),
        colour=colour or EMBED_COLOUR,
    )


def build_solo_config_embed(
    captain_mode: str,
    draft_mode: str,
    veto_mode: str,
    scoring_mode: str,
    results_ch_id: int,
    theme: str,
    map_pool: list[str],
    colour: discord.Colour,
) -> discord.Embed:
    """Construct the rich admin configuration overview embed."""
    embed = discord.Embed(
        title="VEGA 10-MAN SOLO QUEUE — CONFIGURATION CENTER",
        description=(
            "> **Matchmaking Templates & Style Settings**\n"
            "> Use `/solo_config <option>` or the interactive dropdowns below to update match rules in real time."
        ),
        colour=colour,
    )
    embed.add_field(
        name="👑 Captain Selection Template",
        value=f"`{captain_mode}`\n*(Highest ELO, Random, First Joined, or Highest Winrate)*",
        inline=True,
    )
    embed.add_field(
        name="👥 Player Draft Template",
        value=f"`{draft_mode}`\n*(Snake 1-2-2-2-1, Alternating, or Auto ELO Balance)*",
        inline=True,
    )
    embed.add_field(
        name="🗺️ Map Veto Template",
        value=f"`{veto_mode}`\n*(Alternating Bans, Ban-Ban-Pick, Random Map, Captain Pick, or 4-Map Vote)*",
        inline=True,
    )
    scoring_desc = (
        "Performance Combat-Based (Stats Scaling)"
        if scoring_mode == "PERFORMANCE"
        else "Default Flat ELO (+25/-20)"
    )
    embed.add_field(
        name="📊 ELO Scoring Template",
        value=f"`{scoring_mode}`\n*({scoring_desc})*",
        inline=True,
    )
    results_ch_val = f"<#{results_ch_id}>" if results_ch_id else "*Not configured*"
    embed.add_field(
        name="📢 Results Channel",
        value=results_ch_val,
        inline=True,
    )
    embed.add_field(
        name="🎨 Active Accent Theme",
        value=f"`{theme}`\n*(Embed colour scheme)*",
        inline=True,
    )
    embed.add_field(
        name="🎯 Active Map Pool",
        value=" • ".join(f"`{m}`" for m in map_pool),
        inline=False,
    )
    embed.set_footer(text="Settings are stored in the database and apply to all future matches.")
    return embed


async def finalize_teams_and_move(
    bot: discord.Client,
    match: dict,
    guild: discord.Guild,
    t1_ids: list[int],
    t2_ids: list[int],
) -> None:
    """
    Unlock Team A & Team B voice channels for their respective players,
    and automatically move any players currently connected to voice in parallel.
    """
    v1_id = match.get("voice_team1_id")
    v2_id = match.get("voice_team2_id")

    t1_vc: Optional[discord.VoiceChannel] = None
    t2_vc: Optional[discord.VoiceChannel] = None

    if v1_id:
        ch1 = guild.get_channel(v1_id)
        if isinstance(ch1, discord.VoiceChannel):
            t1_vc = ch1
    if v2_id:
        ch2 = guild.get_channel(v2_id)
        if isinstance(ch2, discord.VoiceChannel):
            t2_vc = ch2

    async def _handle_player(vc: discord.VoiceChannel, pid: int, team_name: str) -> None:
        mem = await _get_or_fetch_member(guild, pid)
        if not mem:
            return
        try:
            await vc.set_permissions(mem, view_channel=True, connect=True, speak=True)
        except Exception as e:
            log.debug("Could not set %s voice permissions for %s: %s", team_name, mem.name, e)
        if mem.voice and mem.voice.channel:
            try:
                await mem.move_to(vc, reason=f"Queue #{match['id']} {team_name} VC")
            except Exception as e:
                log.debug("Could not move %s to %s VC: %s", mem.name, team_name, e)

    tasks = []
    if t1_vc:
        for pid in t1_ids:
            tasks.append(_handle_player(t1_vc, pid, "Team 1"))
    if t2_vc:
        for pid in t2_ids:
            tasks.append(_handle_player(t2_vc, pid, "Team 2"))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# =============================================================================
# Interactive Views & Dropdowns
# =============================================================================

class SoloQueueView(discord.ui.View):
    """
    Persistent view for joining/leaving the 10-man solo queue.
    Button labels strictly: 'Join Queue' and 'Leave Queue'.
    """

    def __init__(self, cog: Optional[SoloQueueCog] = None, is_paused: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if is_paused:
            self.join_queue_button.disabled = True

    def _resolve_cog(self, interaction: discord.Interaction) -> Optional[SoloQueueCog]:
        if self.cog is not None:
            return self.cog
        return interaction.client.get_cog("SoloQueue")

    @discord.ui.button(
        label="Join Queue",
        style=discord.ButtonStyle.primary,
        custom_id="solo_queue:join",
    )
    async def join_queue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        cog = self._resolve_cog(interaction)
        if cog:
            await cog.handle_join_queue(interaction)
        else:
            await interaction.response.send_message(
                "Queue system is currently initializing. Please try again shortly.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Leave Queue",
        style=discord.ButtonStyle.danger,
        custom_id="solo_queue:leave",
    )
    async def leave_queue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        cog = self._resolve_cog(interaction)
        if cog:
            await cog.handle_leave_queue(interaction)
        else:
            await interaction.response.send_message(
                "Queue system is currently initializing. Please try again shortly.",
                ephemeral=True,
            )


class PlayerDraftSelect(discord.ui.Select):
    """Dropdown for the active captain to select a player."""

    def __init__(
        self,
        match_id: int,
        available_players: list[dict],
        players_by_id: Optional[dict[int, dict]] = None,
        colour: Optional[discord.Colour] = None,
        draft_mode: Optional[str] = None,
    ) -> None:
        options = [
            discord.SelectOption(
                label=f"{p.get('ign') or p.get('discord_username') or 'Player'} (ELO: {p.get('elo', 1000)})",
                value=str(p["discord_id"]),
                description=f"Region: {p.get('region', 'Global')} | Wins: {p.get('wins', 0)}",
            )
            for p in available_players[:25]
        ]
        super().__init__(
            placeholder="Select a player for your team...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="solo:draft_select",
        )
        self.match_id = match_id
        self.players_by_id = players_by_id or {}
        self.colour = colour
        self.draft_mode = draft_mode

    async def callback(self, interaction: discord.Interaction) -> None:
        match = await db.get_solo_match_by_id(self.match_id)
        if not match or match["status"] != "DRAFTING":
            await interaction.response.send_message("Drafting is no longer active for this match.", ephemeral=True)
            return

        if interaction.user.id != match["current_turn_captain_id"]:
            await interaction.response.send_message("It is not your turn to pick.", ephemeral=True)
            return

        picked_id = int(self.values[0])
        avail_ids = list(match["available_player_ids"])
        if picked_id not in avail_ids:
            await interaction.response.send_message("That player is no longer available in the pool.", ephemeral=True)
            return

        # Acknowledge the interaction immediately to prevent Discord's 3-second 10062 Unknown Interaction timeout
        if not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except Exception as e:
                log.debug("Could not defer interaction in PlayerDraftSelect: %s", e)

        async def _safe_edit_draft_message(*, content=None, embed=None, view=None, map_name: Optional[str] = None):
            # 1. Edit via interaction.message directly using bot token (immune to 10062 expiration)
            if interaction.message:
                try:
                    f = get_solo_map_file(map_name)
                    kwargs = {"content": content, "embed": embed, "view": view}
                    if f:
                        kwargs["attachments"] = [f]
                    return await interaction.message.edit(**kwargs)
                except Exception as e:
                    log.debug("interaction.message.edit failed in PlayerDraftSelect: %s", e)
            # 2. Try editing via interaction response/webhook
            try:
                f = get_solo_map_file(map_name)
                kwargs = {"content": content, "embed": embed, "view": view}
                if f:
                    kwargs["attachments"] = [f]
                if not interaction.response.is_done():
                    return await interaction.response.edit_message(**kwargs)
                else:
                    return await interaction.edit_original_response(**kwargs)
            except Exception as e:
                log.debug("interaction edit response failed in PlayerDraftSelect: %s", e)
            # 3. Fallback: fetch from channel using match panel_message_id
            panel_id = match.get("panel_message_id")
            if interaction.channel and panel_id:
                try:
                    msg = await interaction.channel.fetch_message(panel_id)
                    f = get_solo_map_file(map_name)
                    kwargs = {"content": content, "embed": embed, "view": view}
                    if f:
                        kwargs["attachments"] = [f]
                    return await msg.edit(**kwargs)
                except Exception as e:
                    log.error("Fallback msg.edit failed in PlayerDraftSelect: %s", e)

        avail_ids.remove(picked_id)
        c1_id = match["captain1_id"]
        c2_id = match["captain2_id"]
        t1_ids = list(match["team1_player_ids"])
        t2_ids = list(match["team2_player_ids"])

        # Assign to active captain
        if interaction.user.id == c1_id:
            t1_ids.append(picked_id)
        else:
            t2_ids.append(picked_id)

        next_step = match["draft_step"] + 1

        # Check if draft complete (after 7 picks, 1 player remains and automatically joins the other team)
        if next_step > 7 or len(avail_ids) <= 1:
            if avail_ids:
                last_player_id = avail_ids.pop(0)
                if len(t1_ids) < 5:
                    t1_ids.append(last_player_id)
                else:
                    t2_ids.append(last_player_id)

            if interaction.guild:
                asyncio.create_task(finalize_teams_and_move(interaction.client, match, interaction.guild, t1_ids, t2_ids))

            veto_mode = await get_solo_veto_mode()
            colour = self.colour or await get_solo_embed_colour()
            map_pool = await get_solo_map_pool()

            all_match_pids = t1_ids + t2_ids
            missing_pids = [pid for pid in all_match_pids if pid not in self.players_by_id]
            if missing_pids:
                fetched = await db.get_players_bulk(missing_pids)
                for p in fetched:
                    self.players_by_id[p["discord_id"]] = p

            existing_map = match.get("selected_map")
            if existing_map:
                updated_match = await db.update_solo_match_draft(
                    match_id=self.match_id,
                    team1_player_ids=t1_ids,
                    team2_player_ids=t2_ids,
                    available_player_ids=[],
                    current_turn_captain_id=None,
                    draft_step=next_step,
                    status="IN_PROGRESS",
                )
                if not updated_match:
                    updated_match = dict(match)
                    updated_match["team1_player_ids"] = t1_ids
                    updated_match["team2_player_ids"] = t2_ids
                    updated_match["status"] = "IN_PROGRESS"
                if not updated_match.get("team1_player_ids"):
                    updated_match["team1_player_ids"] = t1_ids
                if not updated_match.get("team2_player_ids"):
                    updated_match["team2_player_ids"] = t2_ids
                updated_match["selected_map"] = existing_map
                embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)
                await _safe_edit_draft_message(content=None, embed=embed, view=None, map_name=existing_map)
                if interaction.channel:
                    await interaction.channel.send(
                        f"**MATCH READY • MAP: {existing_map.upper()}**\n"
                        f"Captains: <@{c1_id}> and <@{c2_id}>\n"
                        f"Queue ready on **{existing_map}**. Use `/submit-result` when done."
                    )
                return

            if veto_mode == "RANDOM_MAP":
                final_map = random.choice(map_pool)
                updated_match = await db.update_solo_match_map_veto(
                    match_id=self.match_id,
                    available_maps=[],
                    banned_maps=[],
                    selected_map=final_map,
                    current_turn_captain_id=None,
                    status="IN_PROGRESS",
                )
                if not updated_match:
                    updated_match = dict(match)
                    updated_match["selected_map"] = final_map
                    updated_match["status"] = "IN_PROGRESS"
                if not updated_match.get("team1_player_ids"):
                    updated_match["team1_player_ids"] = t1_ids
                if not updated_match.get("team2_player_ids"):
                    updated_match["team2_player_ids"] = t2_ids
                embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)
                await _safe_edit_draft_message(content=None, embed=embed, view=None, map_name=final_map)
                if interaction.channel:
                    await interaction.channel.send(
                        f"**MATCH READY • MAP: {final_map.upper()}**\n"
                        f"Captains: <@{c1_id}> and <@{c2_id}>\n"
                        f"Queue ready on **{final_map}**. Use `/submit-result` when done."
                    )
                return
            elif veto_mode in ("MAP_VOTE", "VOTE"):
                pool_copy = list(map_pool) if map_pool else ["Bind", "Haven", "Split", "Ascent"]
                selected_4 = random.sample(pool_copy, min(4, len(pool_copy)))
                await db.update_solo_match_draft(
                    match_id=self.match_id,
                    team1_player_ids=t1_ids,
                    team2_player_ids=t2_ids,
                    available_player_ids=[],
                    current_turn_captain_id=c1_id,
                    draft_step=next_step,
                    status="MAP_VETO",
                )
                updated_match = await db.get_solo_match_by_id(self.match_id)
                vote_view = SoloMapVoteView(
                    interaction.client,
                    updated_match,
                    self.players_by_id,
                    selected_4,
                    colour=colour,
                    timeout=60.0,
                    channel=interaction.channel,
                    panel_message=interaction.message,
                )
                embed = build_solo_map_vote_embed(
                    updated_match,
                    self.players_by_id,
                    selected_4,
                    {},
                    vote_view.end_time,
                    colour=colour,
                )
                vote_content = "**TEAMS DRAFTED • MAP VOTING ACTIVE**\nVote for the map below (1 min). Map with the highest votes will be played!"
                await _safe_edit_draft_message(content=vote_content, embed=embed, view=vote_view)
                if interaction.message:
                    vote_view.message = interaction.message
                    await db.update_solo_match_panel(self.match_id, interaction.message.id)
                return

            # Move to MAP_VETO
            updated_match = await db.update_solo_match_draft(
                match_id=self.match_id,
                team1_player_ids=t1_ids,
                team2_player_ids=t2_ids,
                available_player_ids=[],
                current_turn_captain_id=c1_id,
                draft_step=next_step,
                status="MAP_VETO",
            )
            if not updated_match or not updated_match.get("available_maps"):
                updated_match = await db.update_solo_match_map_veto(
                    match_id=self.match_id,
                    available_maps=list(map_pool) if map_pool else ["Bind", "Haven", "Split", "Ascent"],
                    banned_maps=[],
                    selected_map=None,
                    current_turn_captain_id=c1_id,
                    status="MAP_VETO",
                )

            embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)
            veto_view = SoloMapVetoView(updated_match, self.players_by_id, veto_mode=veto_mode, colour=colour)
            await _safe_edit_draft_message(content=None, embed=embed, view=veto_view)
            return

        # Advance draft step
        draft_mode = self.draft_mode or await get_solo_draft_mode()
        colour = self.colour or await get_solo_embed_colour()
        next_turn_id = get_draft_active_captain_id(next_step, c1_id, c2_id, mode=draft_mode)
        updated_match = await db.update_solo_match_draft(
            match_id=self.match_id,
            team1_player_ids=t1_ids,
            team2_player_ids=t2_ids,
            available_player_ids=avail_ids,
            current_turn_captain_id=next_turn_id,
            draft_step=next_step,
            status="DRAFTING",
        )

        all_match_pids = t1_ids + t2_ids + avail_ids
        missing_pids = [pid for pid in all_match_pids if pid not in self.players_by_id]
        if missing_pids:
            fetched = await db.get_players_bulk(missing_pids)
            for p in fetched:
                self.players_by_id[p["discord_id"]] = p

        avail_player_dicts = [self.players_by_id[pid] for pid in avail_ids if pid in self.players_by_id]
        embed = build_solo_draft_embed(updated_match, self.players_by_id, colour=colour)
        view = SoloDraftView(
            updated_match,
            avail_player_dicts,
            players_by_id=self.players_by_id,
            colour=colour,
            draft_mode=draft_mode,
        )

        await _safe_edit_draft_message(content=None, embed=embed, view=view)


class SoloDraftView(discord.ui.View):
    """View holding the player selection dropdown during drafting."""

    def __init__(
        self,
        match: dict,
        available_players: list[dict],
        players_by_id: Optional[dict[int, dict]] = None,
        colour: Optional[discord.Colour] = None,
        draft_mode: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=None)
        if available_players:
            self.add_item(
                PlayerDraftSelect(
                    match["id"],
                    available_players,
                    players_by_id=players_by_id,
                    colour=colour,
                    draft_mode=draft_mode,
                )
            )


class SoloMapVetoView(discord.ui.View):
    """View holding dynamic map ban/pick buttons during map veto."""

    def __init__(
        self,
        match: dict,
        players_by_id: dict[int, dict],
        veto_mode: str = "ALTERNATING_BAN",
        colour: Optional[discord.Colour] = None,
    ) -> None:
        super().__init__(timeout=None)
        self.match = match
        self.players_by_id = players_by_id
        self.veto_mode = veto_mode
        self.colour = colour

        avail_maps = match.get("available_maps", [])
        if match.get("status") == "IN_PROGRESS" or len(avail_maps) <= 1:
            return

        is_pick_phase = (self.veto_mode in ("BAN_BAN_PICK", "CAPTAIN_PICK")) and len(avail_maps) == 2

        for map_name in list(dict.fromkeys(avail_maps)):
            if is_pick_phase:
                btn = discord.ui.Button(
                    label=f"Pick {map_name}",
                    style=discord.ButtonStyle.success,
                    custom_id=f"solo_map_pick:{map_name}",
                )
                btn.callback = self._create_map_pick_callback(map_name)
            else:
                btn = discord.ui.Button(
                    label=f"Ban {map_name}",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"solo_map_ban:{map_name}",
                )
                btn.callback = self._create_map_ban_callback(map_name)
            self.add_item(btn)

    def _create_map_pick_callback(self, chosen_map: str):
        async def callback(interaction: discord.Interaction) -> None:
            match = await db.get_solo_match_by_id(self.match["id"])
            if not match or match["status"] != "MAP_VETO":
                await interaction.response.send_message("Map veto is not currently active.", ephemeral=True)
                return

            if interaction.user.id != match["current_turn_captain_id"]:
                await interaction.response.send_message("It is not your turn to pick a map.", ephemeral=True)
                return

            avail_maps = list(match.get("available_maps", []))
            banned_maps = list(match.get("banned_maps", []))

            if chosen_map not in avail_maps:
                await interaction.response.send_message("That map is not available.", ephemeral=True)
                return

            avail_maps.remove(chosen_map)
            banned_maps.extend(avail_maps)

            c1_id = match["captain1_id"]
            c2_id = match["captain2_id"]

            updated_match = await db.update_solo_match_map_veto(
                match_id=match["id"],
                available_maps=[],
                banned_maps=banned_maps,
                selected_map=chosen_map,
                current_turn_captain_id=None,
                status="IN_PROGRESS",
            )
            if not updated_match:
                updated_match = dict(match)
                updated_match["selected_map"] = chosen_map
                updated_match["status"] = "IN_PROGRESS"

            colour = self.colour or await get_solo_embed_colour()
            embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)

            map_file = get_solo_map_file(chosen_map)
            files_arg = [map_file] if map_file else []
            if not interaction.response.is_done():
                await interaction.response.edit_message(content=None, embed=embed, view=None, attachments=files_arg)
            else:
                await interaction.edit_original_response(content=None, embed=embed, view=None, attachments=files_arg)

            if interaction.message:
                await db.update_solo_match_panel(match["id"], interaction.message.id)

            if interaction.channel:
                await interaction.channel.send(
                    f"**MATCH READY • MAP: {chosen_map.upper()}**\n"
                    f"Captains: <@{c1_id}> and <@{c2_id}>\n"
                    f"Queue ready on **{chosen_map}**. Use `/submit-result` when done."
                )
        return callback

    def _create_map_ban_callback(self, map_to_ban: str):
        async def callback(interaction: discord.Interaction) -> None:
            match = await db.get_solo_match_by_id(self.match["id"])
            if not match or match["status"] != "MAP_VETO":
                await interaction.response.send_message("Map veto is not currently active.", ephemeral=True)
                return

            if interaction.user.id != match["current_turn_captain_id"]:
                await interaction.response.send_message("It is not your turn to ban a map.", ephemeral=True)
                return

            avail_maps = list(match.get("available_maps", []))
            banned_maps = list(match.get("banned_maps", []))

            if map_to_ban not in avail_maps:
                await interaction.response.send_message("That map is already banned.", ephemeral=True)
                return

            avail_maps.remove(map_to_ban)
            banned_maps.append(map_to_ban)

            c1_id = match["captain1_id"]
            c2_id = match["captain2_id"]
            next_turn_id = c2_id if match["current_turn_captain_id"] == c1_id else c1_id
            colour = self.colour or await get_solo_embed_colour()

            # If 2 maps remain and mode is BAN_BAN_PICK or CAPTAIN_PICK, enter pick phase!
            if len(avail_maps) == 2 and self.veto_mode in ("BAN_BAN_PICK", "CAPTAIN_PICK"):
                picker_id = c1_id if self.veto_mode == "CAPTAIN_PICK" else next_turn_id
                updated_match = await db.update_solo_match_map_veto(
                    match_id=match["id"],
                    available_maps=avail_maps,
                    banned_maps=banned_maps,
                    selected_map=None,
                    current_turn_captain_id=picker_id,
                    status="MAP_VETO",
                )
                embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)
                next_view = SoloMapVetoView(updated_match, self.players_by_id, veto_mode=self.veto_mode, colour=colour)

                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=embed, view=next_view)
                else:
                    await interaction.edit_original_response(embed=embed, view=next_view)

                if interaction.channel:
                    await interaction.channel.send(f"<@{picker_id}> Please pick the final map from the remaining 2 above!")
                return

            # If only 1 map remains, it is the selected map!
            if len(avail_maps) <= 1:
                final_map = avail_maps[0] if avail_maps else map_to_ban
                updated_match = await db.update_solo_match_map_veto(
                    match_id=match["id"],
                    available_maps=[],
                    banned_maps=banned_maps,
                    selected_map=final_map,
                    current_turn_captain_id=None,
                    status="IN_PROGRESS",
                )
                if not updated_match:
                    updated_match = dict(match)
                    updated_match["selected_map"] = final_map
                    updated_match["status"] = "IN_PROGRESS"
                if not updated_match.get("team1_player_ids"):
                    updated_match["team1_player_ids"] = match.get("team1_player_ids", [])
                if not updated_match.get("team2_player_ids"):
                    updated_match["team2_player_ids"] = match.get("team2_player_ids", [])

                embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)

                map_file = get_solo_map_file(final_map)
                files_arg = [map_file] if map_file else []
                if not interaction.response.is_done():
                    await interaction.response.edit_message(content=None, embed=embed, view=None, attachments=files_arg)
                else:
                    await interaction.edit_original_response(content=None, embed=embed, view=None, attachments=files_arg)

                if interaction.message:
                    await db.update_solo_match_panel(match["id"], interaction.message.id)

                if interaction.channel:
                    await interaction.channel.send(
                        f"**MATCH READY • MAP: {final_map.upper()}**\n"
                        f"Captains: <@{c1_id}> and <@{c2_id}>\n"
                        f"Queue ready on **{final_map}**. Use `/submit-result` when done."
                    )
                return

            # Continue veto
            updated_match = await db.update_solo_match_map_veto(
                match_id=match["id"],
                available_maps=avail_maps,
                banned_maps=banned_maps,
                selected_map=None,
                current_turn_captain_id=next_turn_id,
                status="MAP_VETO",
            )
            if not updated_match:
                updated_match = dict(match)
                updated_match["available_maps"] = avail_maps
                updated_match["banned_maps"] = banned_maps
                updated_match["current_turn_captain_id"] = next_turn_id
                updated_match["status"] = "MAP_VETO"

            embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)
            next_view = SoloMapVetoView(updated_match, self.players_by_id, veto_mode=self.veto_mode, colour=colour)

            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=next_view)
            else:
                await interaction.edit_original_response(embed=embed, view=next_view)

            if interaction.channel:
                await interaction.channel.send(f"<@{next_turn_id}> Please ban a map below.")
        return callback


_MAP_VOTE_TASKS: set[asyncio.Task] = set()


class SoloMapVoteView(discord.ui.View):
    """
    1-minute voting view for 4 randomly selected maps.
    All 10 players vote; the map with the highest votes is selected.
    """

    def __init__(
        self,
        bot: commands.Bot,
        match: dict,
        players_by_id: dict[int, dict],
        map_options: list[str],
        colour: Optional[discord.Colour] = None,
        timeout: float = 60.0,
        channel: Optional[discord.abc.Messageable] = None,
        panel_message: Optional[discord.Message] = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.match = match
        self.players_by_id = players_by_id
        self.map_options = map_options
        self.colour = colour
        self.timeout_duration = timeout
        self.end_time = time.time() + timeout
        self.votes: dict[int, str] = {}
        all_pids = (
            match.get("team1_player_ids", [])
            + match.get("team2_player_ids", [])
            + match.get("available_player_ids", [])
            + [match.get("captain1_id"), match.get("captain2_id")]
        )
        self.all_player_ids = {pid for pid in all_pids if pid}
        self.is_finalized: bool = False
        self.channel: Optional[discord.abc.Messageable] = channel
        self.message: Optional[discord.Message] = panel_message

        self._build_buttons()
        # Explicit timer task tracked in global set so Python GC never cancels it
        task = asyncio.create_task(self._run_timer())
        self._timer_task: Optional[asyncio.Task] = task
        _MAP_VOTE_TASKS.add(task)
        task.add_done_callback(_MAP_VOTE_TASKS.discard)

    async def _run_timer(self) -> None:
        try:
            remaining = self.end_time - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            if not self.is_finalized:
                log.info("SoloMapVoteView timer completed for match #%s, finalizing now...", self.match.get("id"))
                await self._finalize(self.channel, None)
            else:
                log.info("SoloMapVoteView timer expired for match #%s but already finalized.", self.match.get("id"))
        except asyncio.CancelledError:
            log.info("SoloMapVoteView timer task cancelled for match #%s.", self.match.get("id"))
        except Exception as e:
            log.exception("Error in SoloMapVoteView timer task for match #%s: %s", self.match.get("id"), e)

    def _build_buttons(self) -> None:
        self.clear_items()
        counts = {m: 0 for m in self.map_options}
        for m in self.votes.values():
            if m in counts:
                counts[m] += 1

        for m in self.map_options:
            cnt = counts[m]
            label = f"{m} ({cnt})" if cnt > 0 else m
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if cnt > 0 else discord.ButtonStyle.secondary,
                custom_id=f"solo_map_vote:{m}",
            )
            btn.callback = self._make_vote_callback(m)
            self.add_item(btn)

    def _make_vote_callback(self, map_name: str):
        async def callback(interaction: discord.Interaction) -> None:
            if self.is_finalized or time.time() >= self.end_time:
                if not self.is_finalized:
                    await self._finalize(interaction.channel, interaction=interaction)
                else:
                    await interaction.response.send_message("Voting has already concluded.", ephemeral=True)
                return

            if interaction.user.id not in self.all_player_ids:
                await interaction.response.send_message("Only players in this queue can vote.", ephemeral=True)
                return

            if self.message is None and interaction.message:
                self.message = interaction.message
            if self.channel is None and interaction.channel:
                self.channel = interaction.channel

            prev = self.votes.get(interaction.user.id)
            if prev == map_name:
                await interaction.response.send_message(f"You already voted for **{map_name}**.", ephemeral=True)
                return

            self.votes[interaction.user.id] = map_name
            self._build_buttons()
            embed = build_solo_map_vote_embed(
                self.match,
                self.players_by_id,
                self.map_options,
                self.votes,
                self.end_time,
                colour=self.colour,
            )

            # If all players have voted, conclude immediately
            if len(self.votes) >= len(self.all_player_ids) and len(self.all_player_ids) > 0:
                self.message = interaction.message or self.message
                if not interaction.response.is_done():
                    await interaction.response.defer()
                await self._finalize(interaction.channel, interaction=interaction)
                return

            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        return callback

    async def on_timeout(self) -> None:
        if not self.is_finalized:
            log.info("SoloMapVoteView on_timeout for match #%s, finalizing now...", self.match.get("id"))
            await self._finalize(self.channel, None)

    async def _finalize(
        self,
        channel: Optional[discord.abc.Messageable],
        interaction: Optional[discord.Interaction] = None,
    ) -> None:
        if self.is_finalized:
            return
        self.is_finalized = True
        self.stop()

        # Only cancel timer task if called from OUTSIDE the timer task itself (e.g. from an interaction)
        curr_task = asyncio.current_task()
        if hasattr(self, "_timer_task") and self._timer_task and not self._timer_task.done():
            if self._timer_task != curr_task:
                self._timer_task.cancel()

        try:
            log.info("SoloMapVoteView._finalize: starting finalization for match #%s", self.match.get("id"))
            counts = {m: 0 for m in self.map_options}
            for m in self.votes.values():
                if m in counts:
                    counts[m] += 1

            max_votes = max(counts.values()) if counts else 0
            if max_votes > 0:
                top_maps = [m for m, c in counts.items() if c == max_votes]
                final_map = random.choice(top_maps)
            else:
                final_map = random.choice(self.map_options) if self.map_options else "Bind"

            banned = [m for m in self.map_options if m != final_map]
            log.info(
                "SoloMapVoteView._finalize match #%s: vote counts=%s selected final_map=%s",
                self.match.get("id"), counts, final_map,
            )

            current_match = await db.get_solo_match_by_id(self.match["id"])
            if not current_match:
                current_match = dict(self.match)

            draft_mode = await get_solo_draft_mode()
            colour = self.colour or await get_solo_embed_colour()

            # Locate channel & panel message reliably
            ch_id = current_match.get("channel_id")
            target_ch = None
            if ch_id and getattr(self, "bot", None):
                target_ch = self.bot.get_channel(ch_id)
                if not target_ch:
                    try:
                        target_ch = await self.bot.fetch_channel(ch_id)
                        log.info("SoloMapVoteView._finalize: fetched channel %s via API", ch_id)
                    except Exception as e:
                        log.error("SoloMapVoteView._finalize: could not fetch channel %s: %s", ch_id, e)
            if not target_ch:
                target_ch = (
                    channel
                    or getattr(self, "channel", None)
                    or (self.message.channel if self.message else None)
                )

            panel_msg = self.message
            panel_msg_id = current_match.get("panel_message_id")
            if target_ch and panel_msg_id and hasattr(target_ch, "fetch_message"):
                try:
                    panel_msg = await target_ch.fetch_message(panel_msg_id)
                except Exception as e:
                    log.debug("Could not fetch panel_msg by ID %s: %s", panel_msg_id, e)

            log.info(
                "SoloMapVoteView._finalize for match #%s: target_ch=%s panel_msg_id=%s panel_msg=%s final_map=%s",
                current_match.get("id"), target_ch, panel_msg_id, panel_msg, final_map,
            )

            c1_id = current_match["captain1_id"]
            c2_id = current_match["captain2_id"]
            avail_ids = list(current_match.get("available_player_ids", []))

            # Ensure all player data is loaded for display
            all_match_pids = list(dict.fromkeys(
                current_match.get("team1_player_ids", [])
                + current_match.get("team2_player_ids", [])
                + avail_ids
                + [c1_id, c2_id]
            ))
            try:
                fetched = await db.get_players_bulk([pid for pid in all_match_pids if pid])
                for p in fetched:
                    self.players_by_id[p["discord_id"]] = p
            except Exception as e:
                log.debug("SoloMapVoteView._finalize: could not fetch players bulk: %s", e)

            # If drafting is still needed (SNAKE or ALTERNATING with available players)
            if draft_mode in ("SNAKE", "ALTERNATING") and len(avail_ids) > 0:
                await db.update_solo_match_map_veto(
                    match_id=current_match["id"],
                    available_maps=[],
                    banned_maps=banned,
                    selected_map=final_map,
                    current_turn_captain_id=c1_id,
                    status="DRAFTING",
                )
                updated_match = await db.update_solo_match_draft(
                    match_id=current_match["id"],
                    team1_player_ids=current_match.get("team1_player_ids", [c1_id]),
                    team2_player_ids=current_match.get("team2_player_ids", [c2_id]),
                    available_player_ids=avail_ids,
                    current_turn_captain_id=c1_id,
                    draft_step=1,
                    status="DRAFTING",
                )
                if not updated_match:
                    updated_match = dict(current_match)
                    updated_match["status"] = "DRAFTING"
                    updated_match["draft_step"] = 1
                    updated_match["current_turn_captain_id"] = c1_id
                updated_match["selected_map"] = final_map

                avail_player_dicts = [self.players_by_id[pid] for pid in avail_ids if pid in self.players_by_id]
                draft_embed = build_solo_draft_embed(updated_match, self.players_by_id, colour=colour)
                draft_view = SoloDraftView(
                    updated_match,
                    avail_player_dicts,
                    players_by_id=self.players_by_id,
                    colour=colour,
                    draft_mode=draft_mode,
                )

                if panel_msg:
                    try:
                        await panel_msg.edit(content=None, embed=draft_embed, view=draft_view)
                        log.info("SoloMapVoteView._finalize: edited panel to draft embed")
                    except Exception as e:
                        log.debug("Failed to edit panel to draft: %s", e)
                elif target_ch and hasattr(target_ch, "send"):
                    new_msg = await target_ch.send(embed=draft_embed, view=draft_view)
                    await db.update_solo_match_panel(current_match["id"], new_msg.id)
                    log.info("SoloMapVoteView._finalize: sent new draft panel message (ID: %s)", new_msg.id)

                if target_ch and hasattr(target_ch, "send"):
                    await target_ch.send(
                        f"**MAP SELECTED: {final_map.upper()} • DRAFT COMMENCING**\n"
                        f"Captains: <@{c1_id}> and <@{c2_id}>.\n"
                        f"<@{c1_id}> Please make the first draft pick below."
                    )
            else:
                updated_match = await db.update_solo_match_map_veto(
                    match_id=current_match["id"],
                    available_maps=[],
                    banned_maps=banned,
                    selected_map=final_map,
                    current_turn_captain_id=None,
                    status="IN_PROGRESS",
                )
                if not updated_match:
                    updated_match = dict(current_match)
                    updated_match["selected_map"] = final_map
                    updated_match["status"] = "IN_PROGRESS"
                if not updated_match.get("team1_player_ids"):
                    updated_match["team1_player_ids"] = current_match.get("team1_player_ids", [])
                if not updated_match.get("team2_player_ids"):
                    updated_match["team2_player_ids"] = current_match.get("team2_player_ids", [])

                log.info(
                    "SoloMapVoteView._finalize match #%s: updated to IN_PROGRESS. T1=%s, T2=%s",
                    current_match["id"], updated_match.get("team1_player_ids"), updated_match.get("team2_player_ids"),
                )

                # Build the final Match Ready embed with teams and map name
                embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)

                # Transition panel message in-place to the Match Ready embed
                edited_panel = False
                if interaction and not interaction.is_expired():
                    try:
                        map_file = get_solo_map_file(final_map)
                        edit_kwargs = {"content": None, "embed": embed, "view": None}
                        if map_file:
                            edit_kwargs["attachments"] = [map_file]
                        if not interaction.response.is_done():
                            await interaction.response.edit_message(**edit_kwargs)
                            edited_panel = True
                            log.info("SoloMapVoteView._finalize: transitioned message via interaction response")
                        else:
                            await interaction.edit_original_response(**edit_kwargs)
                            edited_panel = True
                            log.info("SoloMapVoteView._finalize: transitioned message via interaction original_response")
                    except Exception as e:
                        log.debug("Could not edit via interaction in SoloMapVoteView: %s", e)

                if not edited_panel and panel_msg:
                    try:
                        map_file = get_solo_map_file(final_map)
                        edit_kwargs = {"content": None, "embed": embed, "view": None}
                        if map_file:
                            edit_kwargs["attachments"] = [map_file]
                        await panel_msg.edit(**edit_kwargs)
                        edited_panel = True
                        log.info("SoloMapVoteView._finalize: transitioned panel_msg (ID: %s) in-place to Match Ready", panel_msg.id)
                    except Exception as e:
                        log.warning("Could not edit panel_msg (ID: %s) in SoloMapVoteView: %s", getattr(panel_msg, "id", None), e)

                if not edited_panel and target_ch and hasattr(target_ch, "send"):
                    try:
                        map_file = get_solo_map_file(final_map)
                        send_kwargs = {"embed": embed}
                        if map_file:
                            send_kwargs["file"] = map_file
                        ready_msg = await target_ch.send(**send_kwargs)
                        await db.update_solo_match_panel(current_match["id"], ready_msg.id)
                        edited_panel = True
                        log.info("SoloMapVoteView._finalize: sent new Match Ready embed to target_ch (ID: %s)", ready_msg.id)
                    except Exception as e:
                        log.error("Failed to send Match Ready embed to channel: %s", e)

                log.info("SoloMapVoteView._finalize: panel transition done, edited_panel=%s", edited_panel)

                # Announce match ready in channel
                if target_ch and hasattr(target_ch, "send"):
                    try:
                        await target_ch.send(
                            f"**MATCH READY • MAP: {final_map.upper()}**\n"
                            f"Captains: <@{c1_id}> and <@{c2_id}>\n"
                            f"Queue ready on **{final_map}**. Use `/submit-result` when done."
                        )
                        log.info("SoloMapVoteView._finalize: sent ready announcement text to channel")
                    except Exception as e:
                        log.error("Failed to send ready announcement: %s", e)

            log.info("SoloMapVoteView._finalize: finished successfully for match #%s", self.match.get("id"))
        except Exception as e:
            log.exception("CRITICAL ERROR in SoloMapVoteView._finalize for match #%s: %s", self.match.get("id"), e)


class MatchResultVoteView(discord.ui.View):
    """
    Voting view to confirm match results. Requires 6 confirm votes from match participants.
    """

    def __init__(
        self,
        match: dict,
        all_player_ids: list[int],
        result_embed: discord.Embed,
        on_confirmed_callback,
        on_declined_callback,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.match = match
        self.all_player_ids = set(all_player_ids)
        self.result_embed = result_embed
        self.on_confirmed_callback = on_confirmed_callback
        self.on_declined_callback = on_declined_callback

        self.confirms: set[int] = set()
        self.declines: set[int] = set()
        self.is_resolved: bool = False

        self._update_labels()

    def _update_labels(self) -> None:
        self.confirm_btn.label = f"Confirm ({len(self.confirms)}/6)"
        self.decline_btn.label = f"Decline ({len(self.declines)}/5)" if self.declines else "Decline"

    @discord.ui.button(
        label="Confirm (0/6)",
        style=discord.ButtonStyle.success,
        custom_id="solo_result_vote:confirm",
    )
    async def confirm_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.is_resolved:
            await interaction.response.send_message("Voting has already concluded.", ephemeral=True)
            return

        if interaction.user.id not in self.all_player_ids and not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.response.send_message(
                "Only players who participated in this match can vote.",
                ephemeral=True,
            )
            return

        uid = interaction.user.id
        self.declines.discard(uid)
        self.confirms.add(uid)
        self._update_labels()

        if len(self.confirms) >= 6:
            self.is_resolved = True
            for child in self.children:
                child.disabled = True  # type: ignore[attr-defined]
            await interaction.response.edit_message(
                content="✅ **Result Confirmed by Match Players!** Updating ELO and posting to results channel...",
                embed=self.result_embed,
                view=self,
            )
            await self.on_confirmed_callback(interaction)
        else:
            await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="Decline",
        style=discord.ButtonStyle.danger,
        custom_id="solo_result_vote:decline",
    )
    async def decline_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.is_resolved:
            await interaction.response.send_message("Voting has already concluded.", ephemeral=True)
            return

        if interaction.user.id not in self.all_player_ids and not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.response.send_message(
                "Only players who participated in this match can vote.",
                ephemeral=True,
            )
            return

        uid = interaction.user.id
        self.confirms.discard(uid)
        self.declines.add(uid)
        self._update_labels()

        if len(self.declines) >= 5:
            self.is_resolved = True
            for child in self.children:
                child.disabled = True  # type: ignore[attr-defined]
            await interaction.response.edit_message(
                content="❌ **Result Declined by Match Players.** Submission has been cancelled. Please take a clear screenshot and try again.",
                embed=self.result_embed,
                view=self,
            )
            await self.on_declined_callback(interaction)
        else:
            await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        if not self.is_resolved:
            self.is_resolved = True
            for child in self.children:
                child.disabled = True  # type: ignore[attr-defined]
            if self.on_declined_callback:
                await self.on_declined_callback(None)


# =============================================================================
# Interactive Admin Configuration Panel Components
# =============================================================================

class SoloConfigCaptainSelect(discord.ui.Select):
    def __init__(self, current_mode: str) -> None:
        options = [
            discord.SelectOption(
                label="Highest ELO (Default)",
                value="HIGHEST_ELO",
                description="Top 2 highest ELO players become captains",
                default=(current_mode == "HIGHEST_ELO"),
            ),
            discord.SelectOption(
                label="Random",
                value="RANDOM",
                description="2 random players from lobby become captains",
                default=(current_mode == "RANDOM"),
            ),
            discord.SelectOption(
                label="First Joined Queue",
                value="FIRST_JOINED",
                description="The first 2 players who joined the queue",
                default=(current_mode == "FIRST_JOINED"),
            ),
            discord.SelectOption(
                label="Highest Winrate",
                value="HIGHEST_WINRATE",
                description="Top 2 players with the highest win rate",
                default=(current_mode == "HIGHEST_WINRATE"),
            ),
        ]
        super().__init__(
            placeholder="Select Captain Selection Template...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view: SoloConfigPanelView = self.view  # type: ignore[assignment]
        new_mode = self.values[0]
        await db.set_config(CONFIG_KEY_CAPTAIN_MODE, new_mode)
        await view.refresh(interaction)


class SoloConfigDraftSelect(discord.ui.Select):
    def __init__(self, current_mode: str) -> None:
        options = [
            discord.SelectOption(
                label="Snake Draft (Default)",
                value="SNAKE",
                description="Snake Draft sequence (1-2-2-2-1)",
                default=(current_mode == "SNAKE"),
            ),
            discord.SelectOption(
                label="Alternating Draft",
                value="ALTERNATING",
                description="Alternating single picks (1-1-1-1-1-1-1-1)",
                default=(current_mode == "ALTERNATING"),
            ),
            discord.SelectOption(
                label="Auto ELO Balance",
                value="AUTO_BALANCE",
                description="Automatically splits teams by ELO (skips drafting)",
                default=(current_mode == "AUTO_BALANCE"),
            ),
        ]
        super().__init__(
            placeholder="Select Player Draft Template...",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view: SoloConfigPanelView = self.view  # type: ignore[assignment]
        new_mode = self.values[0]
        await db.set_config(CONFIG_KEY_DRAFT_MODE, new_mode)
        await view.refresh(interaction)


class SoloConfigVetoSelect(discord.ui.Select):
    def __init__(self, current_mode: str) -> None:
        options = [
            discord.SelectOption(
                label="Alternating Bans (Default)",
                value="ALTERNATING_BAN",
                description="Captains alternate banning maps until 1 remains",
                default=(current_mode == "ALTERNATING_BAN"),
            ),
            discord.SelectOption(
                label="Ban-Ban-Pick (Decider Pick)",
                value="BAN_BAN_PICK",
                description="Captains ban until 2 remain, then captain picks",
                default=(current_mode == "BAN_BAN_PICK"),
            ),
            discord.SelectOption(
                label="Random Map (Skip Veto)",
                value="RANDOM_MAP",
                description="Instantly picks a random map from the map pool",
                default=(current_mode == "RANDOM_MAP"),
            ),
            discord.SelectOption(
                label="Captain Pick",
                value="CAPTAIN_PICK",
                description="Captains ban, then Captain 1 picks from remaining 2",
                default=(current_mode == "CAPTAIN_PICK"),
            ),
            discord.SelectOption(
                label="4-Map Player Vote (1 Min)",
                value="MAP_VOTE",
                description="4 random maps voted on by all players for 1 min",
                default=(current_mode in ("MAP_VOTE", "VOTE")),
            ),
        ]
        super().__init__(
            placeholder="Select Map Veto Template...",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view: SoloConfigPanelView = self.view  # type: ignore[assignment]
        new_mode = self.values[0]
        await db.set_config(CONFIG_KEY_VETO_MODE, new_mode)
        await view.refresh(interaction)


class SoloConfigScoringSelect(discord.ui.Select):
    def __init__(self, current_mode: str) -> None:
        options = [
            discord.SelectOption(
                label="Default Flat ELO (Default)",
                value="DEFAULT",
                description="+25 Win / -20 Loss / +5 Match MVP",
                default=(current_mode == "DEFAULT"),
            ),
            discord.SelectOption(
                label="Performance Combat-Based ELO",
                value="PERFORMANCE",
                description="Scaled by K/D, ACS, First Bloods & Carry Protection",
                default=(current_mode == "PERFORMANCE"),
            ),
        ]
        super().__init__(
            placeholder="Select ELO Scoring Template...",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view: SoloConfigPanelView = self.view  # type: ignore[assignment]
        new_mode = self.values[0]
        await db.set_config(CONFIG_KEY_SCORING_MODE, new_mode)
        await view.refresh(interaction)


class SoloConfigThemeSelect(discord.ui.Select):
    def __init__(self, current_theme: str) -> None:
        options = [
            discord.SelectOption(
                label="Vega Purple (#5B4FCF)",
                value="PURPLE",
                description="Signature Vega Esports Purple",
                default=(current_theme in ("PURPLE", "DEFAULT", "#5B4FCF")),
            ),
            discord.SelectOption(
                label="Valorant Red (#FF4655)",
                value="VALORANT_RED",
                description="Official Valorant Crimson Red",
                default=(current_theme in ("VALORANT_RED", "#FF4655")),
            ),
            discord.SelectOption(
                label="Cyber Cyan (#00F5FF)",
                value="CYBER_CYAN",
                description="Neon Esports Electric Cyan",
                default=(current_theme in ("CYBER_CYAN", "#00F5FF")),
            ),
            discord.SelectOption(
                label="Champion Gold (#FFD700)",
                value="GOLD",
                description="Tournament Champion Gold",
                default=(current_theme in ("GOLD", "#FFD700")),
            ),
            discord.SelectOption(
                label="Emerald Green (#00E676)",
                value="EMERALD",
                description="Vibrant Competitive Emerald",
                default=(current_theme in ("EMERALD", "#00E676")),
            ),
        ]
        super().__init__(
            placeholder="Select Visual Theme / Colour...",
            min_values=1,
            max_values=1,
            options=options,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view: SoloConfigPanelView = self.view  # type: ignore[assignment]
        new_theme = self.values[0]
        await db.set_config(CONFIG_KEY_THEME, new_theme)
        asyncio.create_task(view.cog.refresh_queue_message())
        await view.refresh(interaction)


class SoloConfigPanelView(discord.ui.View):
    """Interactive control panel for admins to configure 10-man solo queue."""

    def __init__(
        self,
        cog: "SoloQueueCog",
        captain_mode: str,
        draft_mode: str,
        veto_mode: str,
        scoring_mode: str,
        theme: str,
    ) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.add_item(SoloConfigCaptainSelect(captain_mode))
        self.add_item(SoloConfigDraftSelect(draft_mode))
        self.add_item(SoloConfigVetoSelect(veto_mode))
        self.add_item(SoloConfigScoringSelect(scoring_mode))
        self.add_item(SoloConfigThemeSelect(theme))

    async def refresh(self, interaction: discord.Interaction) -> None:
        (
            captain_mode,
            draft_mode,
            veto_mode,
            scoring_mode,
            results_ch_id,
            theme_val,
            map_pool,
            colour,
        ) = await asyncio.gather(
            get_solo_captain_mode(),
            get_solo_draft_mode(),
            get_solo_veto_mode(),
            get_solo_scoring_mode(),
            get_solo_results_channel_id(),
            db.get_config(CONFIG_KEY_THEME),
            get_solo_map_pool(),
            get_solo_embed_colour(),
        )
        theme_val = theme_val or "PURPLE"

        new_view = SoloConfigPanelView(self.cog, captain_mode, draft_mode, veto_mode, scoring_mode, theme_val)
        embed = build_solo_config_embed(captain_mode, draft_mode, veto_mode, scoring_mode, results_ch_id, theme_val, map_pool, colour)
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=new_view)
        else:
            await interaction.edit_original_response(embed=embed, view=new_view)


# =============================================================================
# SoloQueueCog
# =============================================================================

class SoloQueueCog(commands.Cog, name="SoloQueue"):
    """Manages the 10-man solo player queue, drafting, and map veto on Server B."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._queue_message_posted: bool = False
        self._refresh_lock: asyncio.Lock = asyncio.Lock()
        self._match_lock: asyncio.Lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None
        self._auto_resume_task: Optional[asyncio.Task] = None
        # Cache panel message ID in memory to avoid a DB round-trip on every refresh
        self._panel_message_id: Optional[int] = None

    async def is_queue_paused(self) -> tuple[bool, Optional[float]]:
        """Check if the queue is paused. Returns (is_paused, pause_until_timestamp)."""
        paused_val = await db.get_config(CONFIG_KEY_QUEUE_PAUSED)
        if paused_val != "1":
            return False, None

        until_val = await db.get_config(CONFIG_KEY_QUEUE_PAUSE_UNTIL)
        if until_val and until_val != "0":
            try:
                until_ts = float(until_val)
                if time.time() >= until_ts:
                    # Timer expired! Automatically unpause
                    await db.set_config(CONFIG_KEY_QUEUE_PAUSED, "0")
                    await db.set_config(CONFIG_KEY_QUEUE_PAUSE_UNTIL, "0")
                    self._schedule_queue_panel_refresh()
                    return False, None
                return True, until_ts
            except ValueError:
                pass

        return True, None

    def _schedule_auto_resume(self, seconds: float) -> None:
        """Schedule an automatic queue resume after `seconds`."""
        if self._auto_resume_task and not self._auto_resume_task.done():
            self._auto_resume_task.cancel()

        async def _sleeper():
            try:
                await asyncio.sleep(seconds)
                await db.set_config(CONFIG_KEY_QUEUE_PAUSED, "0")
                await db.set_config(CONFIG_KEY_QUEUE_PAUSE_UNTIL, "0")
                await self.refresh_queue_message()
                log.info("Solo queue automatically resumed after timer expired.")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error("Error in auto-resume task: %s", e)

        self._auto_resume_task = asyncio.create_task(_sleeper())

    def _schedule_queue_panel_refresh(self, delay: float = 0.5) -> None:
        """Schedule a debounced refresh of the queue panel to minimize Discord API latency."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._debounced_refresh(delay))

    async def _debounced_refresh(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.refresh_queue_message()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Error in debounced solo queue refresh: %s", e)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._queue_message_posted:
            return
        self._queue_message_posted = True
        is_paused, pause_until = await self.is_queue_paused()
        if is_paused and pause_until:
            rem = pause_until - time.time()
            if rem > 0:
                self._schedule_auto_resume(rem)
        await self.refresh_queue_message()

    async def _get_channel(self) -> Optional[discord.TextChannel]:
        """Fetch the configured solo queue text channel."""
        if not SOLO_QUEUE_CHANNEL_ID:
            log.warning("SOLO_QUEUE_CHANNEL_ID is not configured in environment.")
            return None

        channel = self.bot.get_channel(SOLO_QUEUE_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            return channel

        try:
            fetched = await self.bot.fetch_channel(SOLO_QUEUE_CHANNEL_ID)
            if isinstance(fetched, discord.TextChannel):
                return fetched
        except Exception as e:
            log.error("Could not fetch solo queue channel %d: %s", SOLO_QUEUE_CHANNEL_ID, e)

        return None

    async def refresh_queue_message(self) -> None:
        """Update or post the persistent queue panel, using an in-memory message ID cache."""
        async with self._refresh_lock:
            channel = await self._get_channel()
            if not channel:
                return

            try:
                queued_players = await db.get_solo_queue()
            except Exception as e:
                log.error("Failed to query solo queue from database: %s", e)
                return

            is_paused, pause_until = await self.is_queue_paused()
            embed = build_solo_queue_embed(queued_players, is_paused=is_paused, pause_until=pause_until)
            view = SoloQueueView(self, is_paused=is_paused)

            # Fast-path: use in-memory cached ID (avoids DB round-trip)
            panel_id = self._panel_message_id
            if panel_id is None:
                stored = await db.get_config(SOLO_QUEUE_MESSAGE_CONFIG_KEY)
                if stored:
                    panel_id = int(stored)
                    self._panel_message_id = panel_id

            if panel_id:
                try:
                    existing_msg = await channel.fetch_message(panel_id)
                    await existing_msg.edit(content=None, embed=embed, view=view, attachments=[])
                    log.info("Refreshed solo queue panel message (ID: %d).", panel_id)
                    return
                except discord.NotFound:
                    log.warning("Panel message %d was deleted. Posting new one.", panel_id)
                    self._panel_message_id = None
                except Exception as e:
                    log.error("Error editing solo queue message %d: %s", panel_id, e)

            try:
                msg = await channel.send(embed=embed, view=view)
                try:
                    await msg.pin()
                except discord.Forbidden:
                    pass
                self._panel_message_id = msg.id
                await db.set_config(SOLO_QUEUE_MESSAGE_CONFIG_KEY, str(msg.id))
                log.info("Sent new solo queue panel message (ID: %d).", msg.id)
            except Exception as e:
                log.error("Failed to post solo queue panel message: %s", e)

    # =========================================================================
    # Queue Actions
    # =========================================================================

    async def handle_join_queue(self, interaction: discord.Interaction) -> None:
        """Handle player joining 10-man solo queue — fully optimised for instant response."""
        await interaction.response.defer(ephemeral=True)

        is_paused, pause_until = await self.is_queue_paused()
        if is_paused:
            if pause_until:
                await interaction.followup.send(
                    f"The queue is currently paused by staff. It will reopen <t:{int(pause_until)}:R>.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "The queue is currently stopped by staff.",
                    ephemeral=True,
                )
            return

        user_id = interaction.user.id

        # Parallel: fetch player record and current queue in one round-trip
        player, queued_players = await asyncio.gather(
            db.get_player(user_id),
            db.get_solo_queue(),
        )

        if not player:
            b_reg_id = int(os.environ.get("SERVER_B_REGISTRATION_CHANNEL_ID", "0") or "0")
            ch_hint = f" in <#{b_reg_id}>" if b_reg_id else ""
            await interaction.followup.send(
                f"You must register first using `/register`{ch_hint}.",
                ephemeral=True,
            )
            return

        if player.get("is_banned"):
            await interaction.followup.send("You are banned from queues.", ephemeral=True)
            return

        if player.get("status") == "IN_MATCH":
            await interaction.followup.send("You are currently in an active match.", ephemeral=True)
            return

        if any(p["discord_id"] == user_id for p in queued_players):
            await interaction.followup.send("You are already in queue.", ephemeral=True)
            return

        # Write to DB, then immediately reply — refresh runs in background
        await asyncio.gather(
            db.add_player_to_solo_queue(user_id),
            db.set_player_status(user_id, "IN_QUEUE"),
        )
        self._schedule_queue_panel_refresh()
        await interaction.followup.send("Joined queue.", ephemeral=True)
        log.info("Player %s (%d) joined 10-man solo queue.", interaction.user.name, user_id)

        if interaction.guild:
            asyncio.create_task(self._check_and_create_solo_match(interaction.guild))

    async def handle_leave_queue(self, interaction: discord.Interaction) -> None:
        """Handle player leaving 10-man solo queue — optimised for instant response."""
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        removed = await db.remove_player_from_solo_queue(user_id)
        if removed:
            await asyncio.gather(
                db.set_player_status(user_id, "IDLE"),
                interaction.followup.send("Left queue.", ephemeral=True),
            )
            self._schedule_queue_panel_refresh()
            log.info("Player %s (%d) left 10-man solo queue.", interaction.user.name, user_id)
        else:
            await interaction.followup.send("You are not in the queue.", ephemeral=True)

    # =========================================================================
    # Match Creation & Channel Automation (10 Players)
    # =========================================================================

    async def _check_and_create_solo_match(self, guild: discord.Guild) -> None:
        """Check if 10 players are queued, and form a match with dedicated channels."""
        async with self._match_lock:
            queued = await db.get_solo_queue()
            if len(queued) < 10:
                return

            match_players = queued[:10]
            player_ids = [p["discord_id"] for p in match_players]

            # Parallel atomic dequeue and player status update
            await asyncio.gather(
                db.clear_solo_queue(player_ids),
                db.set_players_status_bulk(player_ids, "IN_MATCH"),
            )

            self._schedule_queue_panel_refresh(delay=0.1)

            try:
                # Parallel fetch of configurations
                captain_mode, map_pool, colour = await asyncio.gather(
                    get_solo_captain_mode(),
                    get_solo_map_pool(),
                    get_solo_embed_colour(),
                )

                # Captain selection via configured template
                cap1, cap2 = select_captains(match_players, mode=captain_mode)
                c1_id = cap1["discord_id"]
                c2_id = cap2["discord_id"]

                avail_ids = [pid for pid in player_ids if pid not in (c1_id, c2_id)]

                # Permission overwrites for text channel
                text_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        embed_links=True,
                        read_message_history=True,
                        manage_channels=True,
                        manage_messages=True,
                        attach_files=True,
                    ),
                }

                # Permission overwrites for voice lobby (Open to all 10 players)
                voice_lobby_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        speak=True,
                        move_members=True,
                        manage_channels=True,
                    ),
                }

                # Permission overwrites for team voice channels (LOCKED initially)
                team_locked_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        speak=True,
                        move_members=True,
                        manage_channels=True,
                    ),
                }

                # Fetch all 10 members in parallel
                members = await asyncio.gather(*[_get_or_fetch_member(guild, pid) for pid in player_ids])

                for mem in members:
                    if mem:
                        text_overwrites[mem] = discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                            attach_files=True,
                        )
                        voice_lobby_overwrites[mem] = discord.PermissionOverwrite(
                            view_channel=True,
                            connect=True,
                            speak=True,
                        )
                        team_locked_overwrites[mem] = discord.PermissionOverwrite(
                            view_channel=True,
                            connect=False,  # Locked until teams are picked!
                        )

                for role_id in STAFF_ROLE_IDS:
                    role = guild.get_role(role_id)
                    if role:
                        text_overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                        )
                        voice_lobby_overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True,
                            connect=True,
                            speak=True,
                            move_members=True,
                        )
                        team_locked_overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True,
                            connect=True,
                            speak=True,
                            move_members=True,
                        )

                # Categories
                # Reference parent matchmaking category (e.g. » MATCHMAKING «)
                parent_category: Optional[discord.CategoryChannel] = None
                if SOLO_MATCH_CATEGORY_ID:
                    cat = guild.get_channel(SOLO_MATCH_CATEGORY_ID)
                    if isinstance(cat, discord.CategoryChannel):
                        parent_category = cat

                # Get sequential queue number starting from 1
                queue_num = await db.get_next_solo_match_id()

                # Create dedicated category for this queue match (placed right below parent Matchmaking category)
                pos = (parent_category.position + 1) if parent_category else None
                cat_kwargs = {
                    "name": f"Queue #{queue_num}",
                    "overwrites": text_overwrites,
                }
                if pos is not None:
                    cat_kwargs["position"] = pos

                try:
                    match_category = await guild.create_category(**cat_kwargs)
                except Exception as e:
                    log.warning("Could not create match category Queue #%s with position: %s, retrying without position", queue_num, e)
                    cat_kwargs.pop("position", None)
                    match_category = await guild.create_category(**cat_kwargs)

                # Create 1 text channel and 3 voice channels inside match_category
                text_channel, lobby_vc, team_a_vc, team_b_vc = await asyncio.gather(
                    guild.create_text_channel(
                        name=f"queue-{queue_num}",
                        overwrites=text_overwrites,
                        category=match_category,
                        topic=f"10-Man Solo Ranked Queue #{queue_num}",
                    ),
                    guild.create_voice_channel(
                        name=f"🔊 Queue {queue_num} Lobby",
                        overwrites=voice_lobby_overwrites,
                        category=match_category,
                    ),
                    guild.create_voice_channel(
                        name=f"Team 1 - #{queue_num}",
                        overwrites=team_locked_overwrites,
                        category=match_category,
                    ),
                    guild.create_voice_channel(
                        name=f"Team 2 - #{queue_num}",
                        overwrites=team_locked_overwrites,
                        category=match_category,
                    ),
                )

                players_by_id = {p["discord_id"]: p for p in match_players}

                # Create match in DB with VOICE_CHECKIN status and sequential ID
                match = await db.create_solo_match(
                    channel_id=text_channel.id,
                    captain1_id=c1_id,
                    captain2_id=c2_id,
                    available_player_ids=avail_ids,
                    available_maps=list(map_pool),
                    status="VOICE_CHECKIN",
                    voice_lobby_id=lobby_vc.id,
                    voice_team1_id=team_a_vc.id,
                    voice_team2_id=team_b_vc.id,
                    match_id=queue_num,
                )

                if not match:
                    log.error("Failed to insert solo match record.")
                    return

                # Send DMs in non-blocking background task so match lobby posts immediately
                dm_content = f"Queue {match['id']} is ready! {text_channel.mention}"

                async def _send_dms_background():
                    async def _send_one(m):
                        if m:
                            try:
                                await m.send(dm_content)
                            except Exception:
                                pass
                    await asyncio.gather(*[_send_one(m) for m in members], return_exceptions=True)

                asyncio.create_task(_send_dms_background())

                # Check initial voice connections
                connected_pids = {m.id for m in lobby_vc.members if m.id in player_ids}
                checkin_embed = build_solo_checkin_embed(
                    match, players_by_id, connected_pids, lobby_vc.id, colour=colour
                )

                pings = " ".join(f"<@{pid}>" for pid in player_ids)
                panel_msg = await text_channel.send(
                    content=(
                        f"{pings}\n"
                        f"**10-MAN QUEUE FOUND — VOICE CHECK-IN**\n"
                        f"All 10 players please connect to {lobby_vc.mention} to begin!"
                    ),
                    embed=checkin_embed,
                )
                await db.update_solo_match_panel(match["id"], panel_msg.id)
                log.info("Created 10-man solo queue #%d in channel #%s.", match["id"], text_channel.name)

                # If all 10 players are somehow already in voice, start immediately
                if len(connected_pids) >= 10:
                    await self._start_match_after_checkin(match, guild, text_channel, players_by_id=players_by_id)

            except Exception as e:
                log.error("Error creating 10-man solo match: %s", e, exc_info=True)

    # =========================================================================
    # Voice Check-in & Flow Handlers
    # =========================================================================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Track player voice connections to the match voice lobby."""
        if member.bot:
            return
        if before.channel == after.channel:
            return

        target_channel_ids = []
        if after.channel:
            target_channel_ids.append(after.channel.id)
        if before.channel:
            target_channel_ids.append(before.channel.id)

        for vc_id in target_channel_ids:
            match = await db.get_solo_match_by_voice_channel(vc_id)
            if match and match.get("status") == "VOICE_CHECKIN" and match.get("voice_lobby_id") == vc_id:
                await self._handle_voice_checkin_update(match, member.guild)

    async def _handle_voice_checkin_update(self, match: dict, guild: discord.Guild) -> None:
        """Update the voice check-in embed and trigger drafting when 10/10 are connected."""
        lobby_vc_id = match.get("voice_lobby_id")
        if not lobby_vc_id:
            return
        lobby_vc = guild.get_channel(lobby_vc_id)
        if not isinstance(lobby_vc, discord.VoiceChannel):
            return

        all_pids = (
            match.get("team1_player_ids", [])
            + match.get("team2_player_ids", [])
            + match.get("available_player_ids", [])
        )
        all_pids = list(dict.fromkeys(all_pids))
        connected_pids = {m.id for m in lobby_vc.members if m.id in all_pids}

        channel_id = match.get("channel_id")
        panel_msg_id = match.get("panel_message_id")
        if not channel_id:
            return
        ch = guild.get_channel(channel_id)
        if not isinstance(ch, discord.TextChannel):
            return

        players, colour = await asyncio.gather(
            db.get_players_bulk(all_pids),
            get_solo_embed_colour(),
        )
        players_by_id = {p["discord_id"]: p for p in players}

        embed = build_solo_checkin_embed(match, players_by_id, connected_pids, lobby_vc.id, colour=colour)

        if panel_msg_id:
            try:
                msg = await ch.fetch_message(panel_msg_id)
                await msg.edit(embed=embed)
            except Exception as e:
                log.debug("Could not edit checkin message: %s", e)

        if len(connected_pids) >= 10 and match.get("status") == "VOICE_CHECKIN":
            await self._start_match_after_checkin(match, guild, ch, players_by_id=players_by_id)

    async def _start_match_after_checkin(
        self,
        match: dict,
        guild: discord.Guild,
        channel: discord.TextChannel,
        players_by_id: Optional[dict[int, dict]] = None,
    ) -> None:
        """Advance match from VOICE_CHECKIN to DRAFTING or AUTO_BALANCE."""
        current_match = await db.get_solo_match_by_id(match["id"])
        if not current_match or current_match.get("status") != "VOICE_CHECKIN":
            return

        draft_mode, veto_mode, map_pool, colour = await asyncio.gather(
            get_solo_draft_mode(),
            get_solo_veto_mode(),
            get_solo_map_pool(),
            get_solo_embed_colour(),
        )

        all_pids = (
            current_match.get("team1_player_ids", [])
            + current_match.get("team2_player_ids", [])
            + current_match.get("available_player_ids", [])
        )
        all_pids = list(dict.fromkeys(all_pids))

        if not players_by_id:
            fetched_players = await db.get_players_bulk(all_pids)
            players_by_id = {p["discord_id"]: p for p in fetched_players}

        match_players = [players_by_id[pid] for pid in all_pids if pid in players_by_id]

        c1_id = current_match["captain1_id"]
        c2_id = current_match["captain2_id"]

        if draft_mode == "AUTO_BALANCE":
            t1_ids, t2_ids = auto_balance_teams(match_players)
            c1_id = t1_ids[0]
            c2_id = t2_ids[0]

            await finalize_teams_and_move(self.bot, current_match, guild, t1_ids, t2_ids)

            if veto_mode == "RANDOM_MAP":
                final_map = random.choice(map_pool)
                await db.update_solo_match_draft(
                    match_id=current_match["id"],
                    team1_player_ids=t1_ids,
                    team2_player_ids=t2_ids,
                    available_player_ids=[],
                    current_turn_captain_id=None,
                    draft_step=8,
                    status="IN_PROGRESS",
                )
                await db.update_solo_match_map_veto(
                    match_id=current_match["id"],
                    available_maps=[],
                    banned_maps=[],
                    selected_map=final_map,
                    current_turn_captain_id=None,
                    status="IN_PROGRESS",
                )
                updated = await db.get_solo_match_by_id(current_match["id"])
                map_file = get_solo_map_file(final_map)
                send_kwargs = {
                    "content": (
                        f"**ALL PLAYERS CHECKED IN • TEAMS AUTO-BALANCED**\n"
                        f"Map randomly selected: **{final_map.upper()}**!\n"
                        f"Teams have been moved to their respective voice channels.\n\n"
                        f"Captains <@{c1_id}> and <@{c2_id}>: Please set up the custom lobby and invite all players."
                    ),
                    "embed": embed,
                }
                if map_file:
                    send_kwargs["file"] = map_file
                panel_msg = await channel.send(**send_kwargs)
                await db.update_solo_match_panel(current_match["id"], panel_msg.id)
            elif veto_mode in ("MAP_VOTE", "VOTE"):
                pool_copy = list(map_pool) if map_pool else ["Bind", "Haven", "Split", "Ascent"]
                selected_4 = random.sample(pool_copy, min(4, len(pool_copy)))
                await db.update_solo_match_draft(
                    match_id=current_match["id"],
                    team1_player_ids=t1_ids,
                    team2_player_ids=t2_ids,
                    available_player_ids=[],
                    current_turn_captain_id=c1_id,
                    draft_step=8,
                    status="MAP_VETO",
                )
                updated = await db.get_solo_match_by_id(current_match["id"])
                vote_view = SoloMapVoteView(
                    self.bot,
                    updated,
                    players_by_id,
                    selected_4,
                    colour=colour,
                    timeout=60.0,
                    channel=channel,
                )
                embed = build_solo_map_vote_embed(
                    updated,
                    players_by_id,
                    selected_4,
                    {},
                    vote_view.end_time,
                    colour=colour,
                )
                panel_msg = await channel.send(
                    content=(
                        f"**ALL PLAYERS CHECKED IN • TEAMS AUTO-BALANCED**\n"
                        f"Captains: <@{c1_id}> and <@{c2_id}>.\n"
                        f"Teams have been moved to their respective voice channels.\n\n"
                        f"Vote for the map below (1 min). The map with the most votes will be played!"
                    ),
                    embed=embed,
                    view=vote_view,
                )
                vote_view.message = panel_msg
                vote_view.channel = channel
                await db.update_solo_match_panel(current_match["id"], panel_msg.id)
            else:
                await db.update_solo_match_draft(
                    match_id=current_match["id"],
                    team1_player_ids=t1_ids,
                    team2_player_ids=t2_ids,
                    available_player_ids=[],
                    current_turn_captain_id=c1_id,
                    draft_step=8,
                    status="MAP_VETO",
                )
                updated = await db.get_solo_match_by_id(current_match["id"])
                embed = build_solo_map_veto_embed(updated, players_by_id, colour=colour)
                veto_view = SoloMapVetoView(updated, players_by_id, veto_mode=veto_mode, colour=colour)
                panel_msg = await channel.send(
                    content=(
                        f"**ALL PLAYERS CHECKED IN • TEAMS AUTO-BALANCED**\n"
                        f"Captains: <@{c1_id}> and <@{c2_id}>.\n"
                        f"Teams have been moved to their respective voice channels.\n\n"
                        f"<@{c1_id}> Please ban the first map below."
                    ),
                    embed=embed,
                    view=veto_view,
                )
                await db.update_solo_match_panel(current_match["id"], panel_msg.id)
            log.info("Match #%d started after checkin with AUTO_BALANCE.", current_match["id"])
            return

        # SNAKE or ALTERNATING drafting
        await db.set_solo_match_status(current_match["id"], "DRAFTING")
        current_match["status"] = "DRAFTING"
        avail_ids = current_match.get("available_player_ids", [])
        avail_dicts = [players_by_id[pid] for pid in avail_ids if pid in players_by_id]
        embed = build_solo_draft_embed(current_match, players_by_id, colour=colour)
        view = SoloDraftView(
            current_match,
            avail_dicts,
            players_by_id=players_by_id,
            colour=colour,
            draft_mode=draft_mode,
        )

        panel_msg = await channel.send(
            content=(
                f"**ALL PLAYERS CHECKED IN • DRAFT COMMENCING**\n"
                f"Captains selected: <@{c1_id}> and <@{c2_id}>.\n"
                f"<@{c1_id}> Please make the first draft pick below."
            ),
            embed=embed,
            view=view,
        )
        await db.update_solo_match_panel(current_match["id"], panel_msg.id)
        log.info("Match #%d started after checkin with drafting.", current_match["id"])

    # =========================================================================
    # Admin Commands & Solo Config Suite
    # =========================================================================

    solo_config = app_commands.Group(
        name="solo_config",
        description="Configure 10-man solo queue templates, draft, veto, and styling.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @solo_config.command(name="panel", description="Open the interactive 10-man solo queue configuration panel.")
    async def solo_config_panel_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions to configure solo queue.", ephemeral=True)
            return

        captain_mode = await get_solo_captain_mode()
        draft_mode = await get_solo_draft_mode()
        veto_mode = await get_solo_veto_mode()
        scoring_mode = await get_solo_scoring_mode()
        results_ch_id = await get_solo_results_channel_id()
        theme_val = (await db.get_config(CONFIG_KEY_THEME)) or "PURPLE"
        map_pool = await get_solo_map_pool()
        colour = await get_solo_embed_colour()

        embed = build_solo_config_embed(captain_mode, draft_mode, veto_mode, scoring_mode, results_ch_id, theme_val, map_pool, colour)
        view = SoloConfigPanelView(self, captain_mode, draft_mode, veto_mode, scoring_mode, theme_val)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @solo_config.command(name="view", description="View all active 10-man solo queue configurations.")
    async def solo_config_view_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        captain_mode = await get_solo_captain_mode()
        draft_mode = await get_solo_draft_mode()
        veto_mode = await get_solo_veto_mode()
        scoring_mode = await get_solo_scoring_mode()
        results_ch_id = await get_solo_results_channel_id()
        theme_val = (await db.get_config(CONFIG_KEY_THEME)) or "PURPLE"
        map_pool = await get_solo_map_pool()
        colour = await get_solo_embed_colour()

        embed = build_solo_config_embed(captain_mode, draft_mode, veto_mode, scoring_mode, results_ch_id, theme_val, map_pool, colour)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @solo_config.command(name="scoring", description="Set the ELO scoring template.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Default Flat ELO (+25/-20)", value="DEFAULT"),
        app_commands.Choice(name="Performance Combat-Based ELO (Stats Scaling)", value="PERFORMANCE"),
    ])
    async def solo_config_scoring_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions.", ephemeral=True)
            return
        await db.set_config(CONFIG_KEY_SCORING_MODE, mode.value)
        await interaction.followup.send(f"Scoring template updated to **`{mode.value}`** ({mode.name}).", ephemeral=True)

    @solo_config.command(name="results_channel", description="Set the dedicated match results channel.")
    async def solo_config_results_channel_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions.", ephemeral=True)
            return
        await db.set_config(CONFIG_KEY_RESULTS_CHANNEL_ID, str(channel.id))
        await interaction.followup.send(f"Dedicated match results channel set to {channel.mention} (`{channel.id}`).", ephemeral=True)

    @solo_config.command(name="captain_mode", description="Set the captain selection template.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Highest ELO (Default)", value="HIGHEST_ELO"),
        app_commands.Choice(name="Random", value="RANDOM"),
        app_commands.Choice(name="First Joined Queue", value="FIRST_JOINED"),
        app_commands.Choice(name="Highest Winrate", value="HIGHEST_WINRATE"),
    ])
    async def solo_config_captain_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions.", ephemeral=True)
            return
        await db.set_config(CONFIG_KEY_CAPTAIN_MODE, mode.value)
        await interaction.followup.send(f"Captain selection template updated to **`{mode.value}`** ({mode.name}).", ephemeral=True)

    @solo_config.command(name="draft_mode", description="Set the player draft template.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Snake Draft 1-2-2-2-1 (Default)", value="SNAKE"),
        app_commands.Choice(name="Alternating Draft 1-1-1-1", value="ALTERNATING"),
        app_commands.Choice(name="Auto ELO Balance (Skip Draft)", value="AUTO_BALANCE"),
    ])
    async def solo_config_draft_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions.", ephemeral=True)
            return
        await db.set_config(CONFIG_KEY_DRAFT_MODE, mode.value)
        await interaction.followup.send(f"Player draft template updated to **`{mode.value}`** ({mode.name}).", ephemeral=True)

    @solo_config.command(name="veto_mode", description="Set the map veto format.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Alternating Bans (Default)", value="ALTERNATING_BAN"),
        app_commands.Choice(name="Ban-Ban-Pick (Decider Pick)", value="BAN_BAN_PICK"),
        app_commands.Choice(name="Random Map (Skip Veto)", value="RANDOM_MAP"),
        app_commands.Choice(name="Captain Pick", value="CAPTAIN_PICK"),
        app_commands.Choice(name="4-Map Player Vote (1 Min)", value="MAP_VOTE"),
    ])
    async def solo_config_veto_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions.", ephemeral=True)
            return
        await db.set_config(CONFIG_KEY_VETO_MODE, mode.value)
        await interaction.followup.send(f"Map veto template updated to **`{mode.value}`** ({mode.name}).", ephemeral=True)

    @solo_config.command(name="theme", description="Set the visual accent theme.")
    @app_commands.choices(preset=[
        app_commands.Choice(name="Vega Purple (#5B4FCF)", value="PURPLE"),
        app_commands.Choice(name="Valorant Red (#FF4655)", value="VALORANT_RED"),
        app_commands.Choice(name="Cyber Cyan (#00F5FF)", value="CYBER_CYAN"),
        app_commands.Choice(name="Champion Gold (#FFD700)", value="GOLD"),
        app_commands.Choice(name="Emerald Green (#00E676)", value="EMERALD"),
    ])
    async def solo_config_theme_cmd(
        self,
        interaction: discord.Interaction,
        preset: Optional[app_commands.Choice[str]] = None,
        custom_hex: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions.", ephemeral=True)
            return
        choice = preset.value if preset else (custom_hex or "PURPLE")
        if choice.startswith("#"):
            try:
                discord.Colour.from_str(choice)
            except Exception:
                await interaction.followup.send(f"Invalid hex code: `{choice}`. Use format like `#FF4655`.", ephemeral=True)
                return
        await db.set_config(CONFIG_KEY_THEME, choice)
        await self.refresh_queue_message()
        await interaction.followup.send(f"Visual theme updated to **`{choice}`**.", ephemeral=True)

    @solo_config.command(name="map_pool", description="Set the comma-separated active map pool.")
    async def solo_config_map_pool_cmd(self, interaction: discord.Interaction, maps: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions.", ephemeral=True)
            return
        parsed_maps = [m.strip() for m in maps.split(",") if m.strip()]
        if len(parsed_maps) < 1:
            await interaction.followup.send("Please provide at least 1 map.", ephemeral=True)
            return
        clean_str = ", ".join(parsed_maps)
        await db.set_config(CONFIG_KEY_MAP_POOL, clean_str)
        await interaction.followup.send(f"Active map pool updated to: {clean_str}", ephemeral=True)

    @solo_config.command(name="clear_queue", description="Clear all players from the 10-man solo queue.")
    async def solo_config_clear_cmd(self, interaction: discord.Interaction) -> None:
        await self._handle_clear_solo_queue(interaction)

    @solo_config.command(name="stop", description="Stop/pause the queue (optional duration like 30m, 1h).")
    @app_commands.describe(timing="Optional duration (e.g., 30m, 1h, 2h, 45m). Leave blank to stop indefinitely.")
    async def solo_config_stop_cmd(self, interaction: discord.Interaction, timing: Optional[str] = None) -> None:
        await self._handle_stop_queue(interaction, timing)

    @solo_config.command(name="start", description="Resume/start the queue so players can join again.")
    async def solo_config_start_cmd(self, interaction: discord.Interaction) -> None:
        await self._handle_start_queue(interaction)

    async def _handle_clear_solo_queue(self, interaction: discord.Interaction) -> None:
        """Internal helper to clear the 10-man solo queue and reset player statuses."""
        await interaction.response.defer(ephemeral=True)

        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions to clear the solo queue.", ephemeral=True)
            return

        queued = await db.get_solo_queue()
        count = len(queued)

        if count == 0:
            await interaction.followup.send("The 10-man solo queue is already empty.", ephemeral=True)
            return

        # Reset all queued players' status back to IDLE
        for player in queued:
            try:
                await db.set_player_status(player["discord_id"], "IDLE")
            except Exception as exc:
                log.warning("Failed to reset status for player %d: %s", player["discord_id"], exc)

        # Clear the queue table
        await db.clear_solo_queue()

        # Refresh the persistent queue panel
        await self.refresh_queue_message()

        log.info(
            "Staff %s (%d) cleared the 10-man solo queue (%d players removed).",
            interaction.user.name,
            interaction.user.id,
            count,
        )

        try:
            await send_log(
                self.bot,
                title="10-Man Solo Queue Cleared",
                description=f"{interaction.user.mention} cleared all players from the 10-man solo queue.",
                colour=COL_WARNING,
                fields=[
                    ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                    ("Players Evicted", str(count), True),
                ],
                guild_id=interaction.guild_id,
            )
        except Exception as e:
            log.debug("Failed to send clear queue log: %s", e)

        await interaction.followup.send(
            f"Successfully cleared **{count}** player(s) from the 10-man solo queue and reset their status to IDLE.",
            ephemeral=True,
        )

    @app_commands.command(
        name="clear_solo_queue",
        description="Clear all players from the 10-man solo queue and reset status to IDLE (Staff only).",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def clear_solo_queue_cmd(self, interaction: discord.Interaction) -> None:
        """Staff command to clear all waiting players from the 10-man solo queue."""
        await self._handle_clear_solo_queue(interaction)

    @app_commands.command(
        name="clear-solo-queue",
        description="Clear all players from the 10-man solo queue and reset status to IDLE (Staff only).",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def clear_solo_queue_hyphen_cmd(self, interaction: discord.Interaction) -> None:
        """Staff command to clear all waiting players from the 10-man solo queue."""
        await self._handle_clear_solo_queue(interaction)

    @app_commands.command(
        name="post_solo_queue",
        description="Post or refresh the 10-man solo queue panel in the configured channel.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def post_solo_queue_command(self, interaction: discord.Interaction) -> None:
        """Admin command to manually post or refresh the 10-man queue embed."""
        await interaction.response.defer(ephemeral=True)

        if not SOLO_QUEUE_CHANNEL_ID:
            await interaction.followup.send(
                "SOLO_QUEUE_CHANNEL_ID is not configured in environment variables.",
                ephemeral=True,
            )
            return

        await self.refresh_queue_message()
        await interaction.followup.send("10-Man solo queue panel refreshed.", ephemeral=True)

    # ── /stop-queue and /start-queue ──────────────────────────────────────────

    @app_commands.command(
        name="stop-queue",
        description="Stop/pause the queue. Provide optional timing (e.g. 30m, 1h) or leave blank for indefinite.",
    )
    @app_commands.describe(
        timing="Optional pause duration (e.g., 30m, 1h, 2h, 45m). Leave blank to stop indefinitely."
    )
    @app_commands.default_permissions(manage_guild=True)
    async def stop_queue_hyphen_cmd(
        self,
        interaction: discord.Interaction,
        timing: Optional[str] = None,
    ) -> None:
        """Stop or pause the queue (hyphen version)."""
        await self._handle_stop_queue(interaction, timing)

    @app_commands.command(
        name="stop_queue",
        description="Stop/pause the queue. Provide optional timing (e.g. 30m, 1h) or leave blank for indefinite.",
    )
    @app_commands.describe(
        timing="Optional pause duration (e.g., 30m, 1h, 2h, 45m). Leave blank to stop indefinitely."
    )
    @app_commands.default_permissions(manage_guild=True)
    async def stop_queue_underscore_cmd(
        self,
        interaction: discord.Interaction,
        timing: Optional[str] = None,
    ) -> None:
        """Stop or pause the queue (underscore version)."""
        await self._handle_stop_queue(interaction, timing)

    @app_commands.command(
        name="start-queue",
        description="Resume/start the queue so players can join again.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def start_queue_hyphen_cmd(self, interaction: discord.Interaction) -> None:
        """Resume the queue (hyphen version)."""
        await self._handle_start_queue(interaction)

    @app_commands.command(
        name="start_queue",
        description="Resume/start the queue so players can join again.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def start_queue_underscore_cmd(self, interaction: discord.Interaction) -> None:
        """Resume the queue (underscore version)."""
        await self._handle_start_queue(interaction)

    async def _handle_stop_queue(
        self,
        interaction: discord.Interaction,
        timing: Optional[str] = None,
    ) -> None:
        """Handle stopping or pausing the queue."""
        await interaction.response.defer(ephemeral=True)

        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send(
                "You do not have staff/admin permissions to stop the queue.",
                ephemeral=True,
            )
            return

        if timing:
            seconds = parse_duration_string(timing)
            if not seconds or seconds <= 0:
                await interaction.followup.send(
                    f"Invalid duration format: `{timing}`. Examples: `30m`, `1h`, `2h30m`, `45s`, `1d`, or raw minutes like `15`.",
                    ephemeral=True,
                )
                return

            pause_until = time.time() + seconds
            await db.set_config(CONFIG_KEY_QUEUE_PAUSED, "1")
            await db.set_config(CONFIG_KEY_QUEUE_PAUSE_UNTIL, str(pause_until))
            self._schedule_auto_resume(seconds)
            await self.refresh_queue_message()

            until_int = int(pause_until)
            log.info(
                "Staff %s (%d) stopped the queue for %s (resumes at %d).",
                interaction.user.name,
                interaction.user.id,
                timing,
                until_int,
            )

            try:
                await send_log(
                    self.bot,
                    title="Queue Stopped (Timed)",
                    description=f"{interaction.user.mention} stopped the queue for **{timing}**.\nReopens automatically <t:{until_int}:R> (<t:{until_int}:f>).",
                    colour=COL_WARNING,
                    fields=[
                        ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                        ("Duration", timing, True),
                        ("Reopens", f"<t:{until_int}:R>", True),
                    ],
                    guild_id=interaction.guild_id,
                )
            except Exception as e:
                log.debug("Failed to send stop queue log: %s", e)

            await interaction.followup.send(
                f"Queue stopped for **{timing}**. It will automatically reopen <t:{until_int}:R>.",
                ephemeral=True,
            )
        else:
            # Indefinite stop
            if self._auto_resume_task and not self._auto_resume_task.done():
                self._auto_resume_task.cancel()

            await db.set_config(CONFIG_KEY_QUEUE_PAUSED, "1")
            await db.set_config(CONFIG_KEY_QUEUE_PAUSE_UNTIL, "0")
            await self.refresh_queue_message()

            log.info(
                "Staff %s (%d) stopped the queue indefinitely.",
                interaction.user.name,
                interaction.user.id,
            )

            try:
                await send_log(
                    self.bot,
                    title="Queue Stopped (Indefinite)",
                    description=f"{interaction.user.mention} stopped the queue indefinitely.\nUse `/start-queue` to reopen.",
                    colour=COL_WARNING,
                    fields=[
                        ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                        ("Duration", "Indefinite", True),
                    ],
                    guild_id=interaction.guild_id,
                )
            except Exception as e:
                log.debug("Failed to send stop queue log: %s", e)

            await interaction.followup.send(
                "Queue has been stopped indefinitely. Nobody can join until staff uses `/start-queue`.",
                ephemeral=True,
            )

    async def _handle_start_queue(self, interaction: discord.Interaction) -> None:
        """Handle resuming/starting the queue."""
        await interaction.response.defer(ephemeral=True)

        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send(
                "You do not have staff/admin permissions to start the queue.",
                ephemeral=True,
            )
            return

        is_paused, _ = await self.is_queue_paused()
        if not is_paused:
            await interaction.followup.send("The queue is already running and open!", ephemeral=True)
            return

        if self._auto_resume_task and not self._auto_resume_task.done():
            self._auto_resume_task.cancel()

        await db.set_config(CONFIG_KEY_QUEUE_PAUSED, "0")
        await db.set_config(CONFIG_KEY_QUEUE_PAUSE_UNTIL, "0")
        await self.refresh_queue_message()

        log.info(
            "Staff %s (%d) resumed the queue.",
            interaction.user.name,
            interaction.user.id,
        )

        try:
            await send_log(
                self.bot,
                title="Queue Resumed",
                description=f"{interaction.user.mention} resumed the queue. Players can now join.",
                colour=discord.Colour.green(),
                fields=[
                    ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                ],
                guild_id=interaction.guild_id,
            )
        except Exception as e:
            log.debug("Failed to send start queue log: %s", e)

        await interaction.followup.send(
            "Queue has been resumed! Players can now join via the panel.",
            ephemeral=True,
        )

    # ── /submit-result ────────────────────────────────────────────────────────

    @app_commands.command(
        name="submit-result",
        description="Submit the match scoreboard screenshot for OCR analysis and stats recording.",
    )
    @app_commands.describe(
        screenshot="The Valorant match end-screen scoreboard screenshot (PNG/JPG/WEBP)."
    )
    async def submit_result_hyphen_cmd(
        self,
        interaction: discord.Interaction,
        screenshot: discord.Attachment,
    ) -> None:
        """Submit match results screenshot (hyphen version)."""
        await self._handle_submit_result(interaction, screenshot)

    @app_commands.command(
        name="submit_result",
        description="Submit the match scoreboard screenshot for OCR analysis and stats recording.",
    )
    @app_commands.describe(
        screenshot="The Valorant match end-screen scoreboard screenshot (PNG/JPG/WEBP)."
    )
    async def submit_result_underscore_cmd(
        self,
        interaction: discord.Interaction,
        screenshot: discord.Attachment,
    ) -> None:
        """Submit match results screenshot (underscore version)."""
        await self._handle_submit_result(interaction, screenshot)

    async def _handle_submit_result(
        self,
        interaction: discord.Interaction,
        screenshot: discord.Attachment,
    ) -> None:
        """Core logic for analyzing match screenshots, blocking race conditions, and updating stats/ELO."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        # 1. Verify this is an active solo match channel
        match = await db.get_solo_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.response.send_message(
                "❌ This channel is not an active 10-man match lobby.",
                ephemeral=True,
            )
            return

        t1_pids = list(match.get("team1_player_ids") or [])
        t2_pids = list(match.get("team2_player_ids") or [])
        all_match_pids = list(set(t1_pids + t2_pids))

        # 2. Verify authorization (match participant or staff)
        is_participant = interaction.user.id in all_match_pids
        is_staff = _is_admin(interaction.user)
        if not is_participant and not is_staff:
            await interaction.response.send_message(
                "❌ Only players participating in this match (or staff) can submit results.",
                ephemeral=True,
            )
            return

        # 3. Validate image format
        if not screenshot.content_type or not screenshot.content_type.startswith("image/"):
            await interaction.response.send_message(
                "❌ Please upload a valid scoreboard image (PNG, JPG, or WEBP).",
                ephemeral=True,
            )
            return

        # 4. Atomic concurrency claim / lock
        claimed, err_reason, _ = await db.claim_solo_match_result_submission(
            match["id"], interaction.user.id
        )
        if not claimed:
            await interaction.response.send_message(f"❌ {err_reason}", ephemeral=True)
            return

        # 5. Send public calculating message
        await interaction.response.send_message("Calculating result, please wait...")

        # 6. Read attachment image bytes
        try:
            image_bytes = await screenshot.read()
        except Exception as exc:
            await db.release_solo_match_result_submission(match["id"])
            await interaction.edit_original_response(content=f"❌ Failed to read attached image: {exc}")
            return

        # 7. Run OCR Pipeline (Ollama qwen2.5vl:3b -> OpenRouter -> Tesseract fallback)
        from utils.match_ocr import process_match_screenshot, PlayerRowStats, get_agent_emoji
        result = await process_match_screenshot(image_bytes)

        if not result.success or (not result.team1_players and not result.team2_players):
            await db.release_solo_match_result_submission(match["id"])
            await interaction.edit_original_response(
                content=(
                    f"❌ **Scoreboard Analysis Failed**: {result.error or 'Could not detect scoreboard table.'}\n"
                    f"• Engine: `{result.engine}`\n"
                    f"• Time: `{result.processing_time_ms} ms`\n\n"
                    "Please make sure the entire Valorant match scoreboard is clearly visible and try again."
                )
            )
            return

        # 8. Map name normalization
        MAP_TRANSLATIONS = {
            "源工重镇": "Bind",
            "亚海悬城": "Ascent",
            "莲华古城": "Lotus",
            "深海明珠": "Pearl",
            "微风岛屿": "Breeze",
            "隐世修所": "Haven",
            "天堂": "Haven",
            "霓虹町": "Split",
            "分裂": "Split",
            "森寒冬港": "Icebox",
            "极地寒港": "Icebox",
            "冰箱": "Icebox",
            "裂变峡谷": "Fracture",
            "碎片": "Fracture",
            "日落之城": "Sunset",
            "日落": "Sunset",
            "幽邃地窟": "Abyss",
            "深渊": "Abyss",
        }
        raw_map = result.map_name or match.get("selected_map") or "Unknown"
        map_name = MAP_TRANSLATIONS.get(raw_map, raw_map)

        # 9. Fetch registered player records for the 10 lobby players
        lobby_player_records: dict[int, dict] = {}
        for pid in all_match_pids:
            p_rec = await db.get_player(pid)
            if p_rec:
                lobby_player_records[pid] = dict(p_rec)

        import re
        import difflib

        def _clean_str(s: str) -> str:
            if not s:
                return ""
            s = s.split("#")[0]
            s = re.sub(r"[^\w\u4e00-\u9fff]", "", s, flags=re.UNICODE)
            return s.lower()

        clean_to_pid: dict[str, int] = {}
        for pid, prec in lobby_player_records.items():
            ign_clean = _clean_str(prec.get("ign", ""))
            if ign_clean:
                clean_to_pid[ign_clean] = pid
            user_clean = _clean_str(prec.get("discord_username", ""))
            if user_clean and user_clean not in clean_to_pid:
                clean_to_pid[user_clean] = pid

        def _find_best_player(ocr_ign: str, candidate_pids: set[int]) -> Optional[int]:
            ocr_c = _clean_str(ocr_ign)
            if not ocr_c:
                return None
            # Exact match
            for p_c, pid in clean_to_pid.items():
                if pid in candidate_pids and p_c == ocr_c:
                    return pid
            # Substring match (min len 3)
            if len(ocr_c) >= 3:
                for p_c, pid in clean_to_pid.items():
                    if pid in candidate_pids and (ocr_c in p_c or p_c in ocr_c):
                        return pid
            # Fuzzy match (ratio >= 0.70)
            best_pid = None
            best_sim = 0.0
            for p_c, pid in clean_to_pid.items():
                if pid in candidate_pids:
                    sim = difflib.SequenceMatcher(None, ocr_c, p_c).ratio()
                    if sim > best_sim:
                        best_sim = sim
                        best_pid = pid
            if best_sim >= 0.70:
                return best_pid
            return None

        # 10. Check Team Alignment (determine if OCR Team 1 is Match Team 1 or Team 2)
        t1_set = set(t1_pids)
        t2_set = set(t2_pids)
        ocr_t1_in_lobby_t1 = 0
        ocr_t1_in_lobby_t2 = 0

        for p in result.team1_players:
            matched = _find_best_player(p.ign, set(all_match_pids))
            if matched:
                if matched in t1_set:
                    ocr_t1_in_lobby_t1 += 1
                elif matched in t2_set:
                    ocr_t1_in_lobby_t2 += 1

        # If OCR Team 1 matched more players from Lobby Team 2, swap OCR teams & scores
        if ocr_t1_in_lobby_t2 > ocr_t1_in_lobby_t1:
            result.team1_players, result.team2_players = result.team2_players, result.team1_players
            result.team1_score, result.team2_score = result.team2_score, result.team1_score

        # 11. Pair each team's OCR rows to Lobby players
        matched_stats_by_pid: dict[int, PlayerRowStats] = {}
        avail_t1 = set(t1_pids)
        avail_t2 = set(t2_pids)

        for p in result.team1_players:
            mpid = _find_best_player(p.ign, avail_t1)
            if mpid:
                avail_t1.discard(mpid)
                matched_stats_by_pid[mpid] = p

        for p in result.team2_players:
            mpid = _find_best_player(p.ign, avail_t2)
            if mpid:
                avail_t2.discard(mpid)
                matched_stats_by_pid[mpid] = p

        # Assign any remaining unmatched OCR rows to remaining lobby players on that team
        unmatched_ocr_t1 = [p for p in result.team1_players if p not in matched_stats_by_pid.values()]
        for pid in list(avail_t1):
            if unmatched_ocr_t1:
                matched_stats_by_pid[pid] = unmatched_ocr_t1.pop(0)
                avail_t1.remove(pid)

        unmatched_ocr_t2 = [p for p in result.team2_players if p not in matched_stats_by_pid.values()]
        for pid in list(avail_t2):
            if unmatched_ocr_t2:
                matched_stats_by_pid[pid] = unmatched_ocr_t2.pop(0)
                avail_t2.remove(pid)

        # 12. Determine match outcome
        t1_score = result.team1_score or 0
        t2_score = result.team2_score or 0
        is_draw = (t1_score == t2_score)
        winning_team = 0 if is_draw else (1 if t1_score > t2_score else 2)

        # 13. Calculate ELO & stats for all 10 players
        scoring_mode = await get_solo_scoring_mode()
        player_updates: list[dict] = []
        overall_mvp_pid = None

        for pid in all_match_pids:
            is_t1 = (pid in t1_pids)
            is_win = (not is_draw) and ((is_t1 and winning_team == 1) or ((not is_t1) and winning_team == 2))

            stats = matched_stats_by_pid.get(pid)
            kills = stats.kills if stats else 0
            deaths = stats.deaths if stats else 0
            assists = stats.assists if stats else 0
            acs = stats.acs if stats else 0
            damage = stats.damage if stats else 0
            fb = stats.first_bloods if stats else 0
            is_mvp = stats.is_mvp if stats else False
            mvp_type = stats.mvp_type if stats else None

            is_match_mvp = is_mvp and (mvp_type == "Match MVP" or "match" in str(mvp_type).lower())
            if is_match_mvp:
                overall_mvp_pid = pid

            elo_delta = calculate_player_elo(
                scoring_mode=scoring_mode,
                is_winner=is_win,
                is_draw=is_draw,
                is_mvp=is_mvp,
                mvp_type=mvp_type,
                kills=kills,
                deaths=deaths,
                assists=assists,
                acs=acs,
                damage=damage,
                first_bloods=fb,
            )

            player_updates.append({
                "discord_id": pid,
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "is_winner": is_win,
                "is_mvp": is_match_mvp or is_mvp,
                "elo_delta": elo_delta,
                "stats_obj": stats,
            })

        # 1. Determine Match MVP: Highest ACS player across all 10 players is ALWAYS Match MVP
        overall_top_u = max(
            player_updates,
            key=lambda x: (
                x.get("stats_obj").acs if x.get("stats_obj") else 0,
                x.get("kills", 0),
            ),
        ) if player_updates else None
        overall_mvp_pid = overall_top_u["discord_id"] if overall_top_u else None

        # 2. Determine Team 1 MVP (identified by 我方-最佳 or top ACS on Team 1)
        t1_updates = [u for u in player_updates if u["discord_id"] in t1_pids]
        t1_tagged = [u for u in t1_updates if u.get("stats_obj") and u.get("stats_obj").is_mvp]
        if t1_tagged:
            t1_mvp_u = max(t1_tagged, key=lambda x: (x.get("stats_obj").acs if x.get("stats_obj") else 0, x.get("kills", 0)))
        elif t1_updates:
            t1_mvp_u = max(t1_updates, key=lambda x: (x.get("stats_obj").acs if x.get("stats_obj") else 0, x.get("kills", 0)))
        else:
            t1_mvp_u = None
        t1_mvp_pid = t1_mvp_u["discord_id"] if t1_mvp_u else None

        # 3. Determine Team 2 MVP (identified by 敌方-最佳 or top ACS on Team 2)
        t2_updates = [u for u in player_updates if u["discord_id"] in t2_pids]
        t2_tagged = [u for u in t2_updates if u.get("stats_obj") and u.get("stats_obj").is_mvp]
        if t2_tagged:
            t2_mvp_u = max(t2_tagged, key=lambda x: (x.get("stats_obj").acs if x.get("stats_obj") else 0, x.get("kills", 0)))
        elif t2_updates:
            t2_mvp_u = max(t2_updates, key=lambda x: (x.get("stats_obj").acs if x.get("stats_obj") else 0, x.get("kills", 0)))
        else:
            t2_mvp_u = None
        t2_mvp_pid = t2_mvp_u["discord_id"] if t2_mvp_u else None

        # 4. Mark is_mvp = True for all MVPs (Match MVP and both Team MVPs) so their mvp_count increments in player stats!
        for u in player_updates:
            pid = u["discord_id"]
            if pid in (overall_mvp_pid, t1_mvp_pid, t2_mvp_pid):
                u["is_mvp"] = True

        # 14. Build comprehensive result embed matching the reference UI
        c1_id = match.get("captain1_id")
        c2_id = match.get("captain2_id")
        c1_name = lobby_player_records.get(c1_id, {}).get("ign") or lobby_player_records.get(c1_id, {}).get("discord_username") or "1"
        c2_name = lobby_player_records.get(c2_id, {}).get("ign") or lobby_player_records.get(c2_id, {}).get("discord_username") or "2"
        t1_team_name = f"Team {c1_name}"
        t2_team_name = f"Team {c2_name}"

        def _format_team_lines(team_pids: list[int]) -> list[str]:
            lines = []
            t_updates = [u for u in player_updates if u["discord_id"] in team_pids]
            t_updates.sort(
                key=lambda x: (x["kills"], x.get("stats_obj").acs if x.get("stats_obj") else 0),
                reverse=True,
            )

            for u in t_updates:
                pid = u["discord_id"]
                k, d, a = u["kills"], u["deaths"], u["assists"]
                delta = u["elo_delta"]
                elo_str = f"+{delta} Elo" if delta >= 0 else f"{delta} Elo"

                # Rating formula
                rating = round((k + a * 0.25) / max(1, d), 2)

                badges = []
                if pid == overall_mvp_pid:
                    badges.append("👑 `Match MVP`")
                if pid in (t1_mvp_pid, t2_mvp_pid):
                    if pid != overall_mvp_pid:
                        badges.append("⭐ `Team MVP`")

                mvp_badge = f" {' '.join(badges)}" if badges else ""
                stats_obj = u.get("stats_obj")
                agent_name = stats_obj.agent if stats_obj else None
                agent_emoji = get_agent_emoji(self.bot, agent_name, interaction.guild)
                prefix = f"{agent_emoji} " if agent_emoji else ""

                lines.append(f"{prefix}<@{pid}>{mvp_badge}")
                lines.append(f"└ [{k}/{d}/{a}] {rating:.2f}r {elo_str}")
            return lines

        t1_player_lines = _format_team_lines(t1_pids)
        t2_player_lines = _format_team_lines(t2_pids)

        desc_parts = [
            "**Score**",
            f"{t1_team_name} [{t1_score}]",
            f"{t2_team_name} [{t2_score}]",
        ]
        if overall_mvp_pid:
            desc_parts.append(f"👑 **Match MVP:** <@{overall_mvp_pid}>")
        if t1_mvp_pid:
            t1_extra = " *(Match MVP)*" if t1_mvp_pid == overall_mvp_pid else ""
            desc_parts.append(f"⭐ **{t1_team_name} MVP:** <@{t1_mvp_pid}>{t1_extra}")
        if t2_mvp_pid:
            t2_extra = " *(Match MVP)*" if t2_mvp_pid == overall_mvp_pid else ""
            desc_parts.append(f"⭐ **{t2_team_name} MVP:** <@{t2_mvp_pid}>{t2_extra}")

        desc_parts.extend([
            "",
            f"**{t1_team_name}**",
            "\n".join(t1_player_lines) if t1_player_lines else "*No players detected*",
            "",
            f"**{t2_team_name}**",
            "\n".join(t2_player_lines) if t2_player_lines else "*No players detected*",
        ])

        result_embed = discord.Embed(
            title=f"Match {match['id']} Results",
            description="\n".join(desc_parts),
            colour=discord.Colour(0xE74C3C),
        )

        clean_map = (map_name or "").strip().lower()
        if clean_map:
            map_path = os.path.join(MAPS_DIR, f"{clean_map}.png")
            if os.path.exists(map_path):
                result_embed.set_thumbnail(url=f"attachment://{clean_map}.png")

        result_embed.set_image(url="attachment://scoreboard.png")

        # 15. Handlers for voting resolution
        async def _on_confirmed(btn_interaction: discord.Interaction) -> None:
            # Commit to database atomically
            await db.complete_solo_match_with_stats(
                match_id=match["id"],
                winning_team=winning_team,
                team1_score=t1_score,
                team2_score=t2_score,
                map_name=map_name,
                submitted_by=interaction.user.id,
                screenshot_url=screenshot.url,
                mvp_player_id=overall_mvp_pid,
                player_updates=player_updates,
                all_lobby_player_ids=all_match_pids,
            )

            # Post to dedicated results channel if configured
            results_ch_id = await get_solo_results_channel_id()
            if results_ch_id and interaction.guild:
                results_channel = interaction.guild.get_channel(results_ch_id)
                if isinstance(results_channel, discord.TextChannel):
                    try:
                        f_map_res = get_solo_map_file(map_name)
                        f_sc_res = discord.File(io.BytesIO(image_bytes), filename="scoreboard.png")
                        res_files = [f for f in [f_map_res, f_sc_res] if f]
                        await results_channel.send(embed=result_embed, files=res_files)
                        log.info("Posted match #%d result to results channel #%s.", match["id"], results_channel.name)
                    except Exception as e:
                        log.warning("Could not post match result to results channel %d: %s", results_ch_id, e)

            # Announce 10-second countdown in lobby channel
            if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.send(
                    "🎉 **Result confirmed!** Stats and ELO have been updated.\n"
                    "Deleting channel in 10 seconds..."
                )

            # Wait 10 seconds
            await asyncio.sleep(10)

            # Cleanup voice channels, text channel, and match category
            cat_to_delete: Optional[discord.CategoryChannel] = None
            if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
                cat = interaction.channel.category
                if cat and cat.id != SOLO_MATCH_CATEGORY_ID and f"#{match['id']}" in cat.name:
                    cat_to_delete = cat

            if interaction.guild:
                v_lobby_id = match.get("voice_lobby_id")
                v1_id = match.get("voice_team1_id")
                v2_id = match.get("voice_team2_id")
                for vid in (v_lobby_id, v1_id, v2_id):
                    if vid:
                        vch = interaction.guild.get_channel(vid)
                        if isinstance(vch, discord.VoiceChannel):
                            try:
                                await vch.delete(reason=f"Queue #{match['id']} concluded")
                            except Exception:
                                pass

            if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
                try:
                    await interaction.channel.delete(reason=f"Queue #{match['id']} concluded")
                except Exception as e:
                    log.error("Failed to delete queue channel: %s", e)

            if cat_to_delete:
                try:
                    await cat_to_delete.delete(reason=f"Queue #{match['id']} concluded")
                except Exception as e:
                    log.debug("Failed to delete queue category: %s", e)

        async def _on_declined(btn_interaction: Optional[discord.Interaction]) -> None:
            await db.release_solo_match_result_submission(match["id"])
            if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.send(
                    "❌ **Result submission declined.** Please take a clearer scoreboard screenshot and use `/submit_result` again."
                )

        vote_view = MatchResultVoteView(
            match=match,
            all_player_ids=all_match_pids,
            result_embed=result_embed,
            on_confirmed_callback=_on_confirmed,
            on_declined_callback=_on_declined,
        )

        f_map = get_solo_map_file(map_name)
        f_sc = discord.File(io.BytesIO(image_bytes), filename="scoreboard.png")
        files_to_send = [f for f in [f_map, f_sc] if f]

        edited = False
        try:
            await interaction.edit_original_response(
                content="**Result Verification Vote** — 6 confirm votes needed to finalize results.",
                embed=result_embed,
                view=vote_view,
                attachments=files_to_send,
            )
            edited = True
        except Exception as e:
            log.warning("Could not edit_original_response with attachments in _handle_submit_result: %s", e)

        if not edited and interaction.channel and hasattr(interaction.channel, "send"):
            try:
                await interaction.edit_original_response(content="Result calculated. Vote below:")
            except Exception:
                pass
            f_map2 = get_solo_map_file(map_name)
            f_sc2 = discord.File(io.BytesIO(image_bytes), filename="scoreboard.png")
            files_to_send2 = [f for f in [f_map2, f_sc2] if f]
            await interaction.channel.send(
                content="**Result Verification Vote** — 6 confirm votes needed to finalize results.",
                embed=result_embed,
                view=vote_view,
                files=files_to_send2,
            )

    @app_commands.command(
        name="cancel",
        description="Cancel the current queue match and release all players to IDLE (Staff only).",
    )
    @app_commands.describe(
        match_id="Optional match ID to cancel if running outside the match channel"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def cancel_command(
        self,
        interaction: discord.Interaction,
        match_id: Optional[int] = None,
    ) -> None:
        await self._handle_cancel_match(interaction, match_id=match_id)

    @app_commands.command(
        name="cancel-match",
        description="Cancel the current queue match and release all players to IDLE (Staff only).",
    )
    @app_commands.describe(
        match_id="Optional match ID to cancel if running outside the match channel"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def cancel_match_command(
        self,
        interaction: discord.Interaction,
        match_id: Optional[int] = None,
    ) -> None:
        await self._handle_cancel_match(interaction, match_id=match_id)

    @app_commands.command(
        name="cancel-queue",
        description="Cancel the current queue match and release all players to IDLE (Staff only).",
    )
    @app_commands.describe(
        match_id="Optional match ID to cancel if running outside the match channel"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def cancel_queue_command(
        self,
        interaction: discord.Interaction,
        match_id: Optional[int] = None,
    ) -> None:
        await self._handle_cancel_match(interaction, match_id=match_id)

    @app_commands.command(
        name="cancel_solo_match",
        description="Cancel the current queue match and release all players to IDLE (Staff only).",
    )
    @app_commands.describe(
        match_id="Optional match ID to cancel if running outside the match channel"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def cancel_solo_match_command(
        self,
        interaction: discord.Interaction,
        match_id: Optional[int] = None,
    ) -> None:
        await self._handle_cancel_match(interaction, match_id=match_id)

    async def _handle_cancel_match(
        self,
        interaction: discord.Interaction,
        match_id: Optional[int] = None,
    ) -> None:
        """Cancel an active queue match or scrim match, clean database, and release players."""
        await interaction.response.defer(ephemeral=True)

        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions to cancel matches.", ephemeral=True)
            return

        match = None
        if match_id is not None:
            match = await db.get_solo_match_by_id(match_id)
        else:
            match = await db.get_solo_match_by_channel(interaction.channel_id)

        if not match:
            # Check if this is a team scrim match channel
            scrim = await db.get_scrim_match_by_channel(interaction.channel_id)
            if scrim:
                await db.cancel_scrim_match(scrim["id"])
                await interaction.followup.send(
                    f"Scrim match #{scrim['id']} cancelled in database. Deleting channel in 5 seconds...",
                    ephemeral=True,
                )
                if isinstance(interaction.channel, discord.TextChannel):
                    try:
                        await interaction.channel.send("⚠️ **This scrim match has been cancelled by staff.** Deleting channel in 5 seconds...")
                    except Exception:
                        pass
                await asyncio.sleep(5)
                if isinstance(interaction.channel, discord.TextChannel):
                    try:
                        await interaction.channel.delete(reason=f"Scrim match #{scrim['id']} cancelled by {interaction.user.name}")
                    except Exception as e:
                        log.error("Failed to delete scrim channel: %s", e)
                return

            await interaction.followup.send("No active queue match found for this channel or match ID.", ephemeral=True)
            return

        if match.get("status") in ("COMPLETED", "CANCELLED"):
            await interaction.followup.send(f"Queue #{match['id']} is already {match['status']}.", ephemeral=True)
            return

        # Release players to IDLE and remove from queue
        all_pids = list(dict.fromkeys(
            match.get("team1_player_ids", [])
            + match.get("team2_player_ids", [])
            + match.get("available_player_ids", [])
            + [match.get("captain1_id"), match.get("captain2_id")]
        ))
        all_pids = [pid for pid in all_pids if pid]

        for pid in all_pids:
            try:
                await db.set_player_status(pid, "IDLE")
                await db.remove_player_from_solo_queue(pid)
            except Exception as e:
                log.debug("Error resetting player %s status: %s", pid, e)

        # Cancel match in database
        await db.cancel_solo_match(match["id"])

        # Delete voice channels if created
        v_lobby_id = match.get("voice_lobby_id")
        v1_id = match.get("voice_team1_id")
        v2_id = match.get("voice_team2_id")
        for vid in (v_lobby_id, v1_id, v2_id):
            if vid and interaction.guild:
                vch = interaction.guild.get_channel(vid)
                if isinstance(vch, discord.VoiceChannel):
                    try:
                        await vch.delete(reason=f"Queue #{match['id']} cancelled by {interaction.user.name}")
                    except Exception as e:
                        log.debug("Failed to delete temporary match voice channel: %s", e)

        # Refresh the main queue embed to show updated counts/states
        asyncio.create_task(self.refresh_queue_message())

        # Locate the match text channel to announce & delete
        match_ch_id = match.get("channel_id")
        target_ch = interaction.guild.get_channel(match_ch_id) if (interaction.guild and match_ch_id) else None
        if not target_ch and isinstance(interaction.channel, discord.TextChannel):
            target_ch = interaction.channel

        await interaction.followup.send(
            f"Queue #{match['id']} cancelled in database. All {len(all_pids)} players returned to IDLE.",
            ephemeral=True,
        )

        cat_to_delete: Optional[discord.CategoryChannel] = None
        if target_ch and target_ch.category and target_ch.category.id != SOLO_MATCH_CATEGORY_ID and f"#{match['id']}" in target_ch.category.name:
            cat_to_delete = target_ch.category

        if target_ch and isinstance(target_ch, discord.TextChannel):
            try:
                await target_ch.send(
                    f"⚠️ **Queue #{match['id']} has been cancelled by {interaction.user.mention}.**\n"
                    "Match cancelled in database and players returned to IDLE. Deleting channel in 5 seconds..."
                )
            except Exception:
                pass
            await asyncio.sleep(5)
            try:
                await target_ch.delete(reason=f"Queue #{match['id']} cancelled by {interaction.user.name}")
            except Exception as e:
                log.error("Failed to delete match channel: %s", e)

        if cat_to_delete:
            try:
                await cat_to_delete.delete(reason=f"Queue #{match['id']} cancelled by {interaction.user.name}")
            except Exception as e:
                log.debug("Failed to delete match category: %s", e)

    @app_commands.command(
        name="admin-change-command",
        description="Admin command to replace a captain in the current queue lobby.",
    )
    @app_commands.describe(
        old_captain="The current captain to be replaced",
        new_captain="The player to promote to captain",
    )
    @app_commands.default_permissions(manage_channels=True)
    async def admin_change_command_alt(
        self,
        interaction: discord.Interaction,
        old_captain: discord.Member,
        new_captain: discord.Member,
    ) -> None:
        await self._handle_admin_change_captain(interaction, old_captain, new_captain)

    @app_commands.command(
        name="admin-change-captain",
        description="Admin command to replace a captain in the current queue lobby.",
    )
    @app_commands.describe(
        old_captain="The current captain to be replaced",
        new_captain="The player to promote to captain",
    )
    @app_commands.default_permissions(manage_channels=True)
    async def admin_change_captain_command(
        self,
        interaction: discord.Interaction,
        old_captain: discord.Member,
        new_captain: discord.Member,
    ) -> None:
        await self._handle_admin_change_captain(interaction, old_captain, new_captain)

    async def _handle_admin_change_captain(
        self,
        interaction: discord.Interaction,
        old_captain: discord.Member,
        new_captain: discord.Member,
    ) -> None:
        """Handle admin captain replacement in a queue match lobby."""
        await interaction.response.defer(ephemeral=False)

        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions.", ephemeral=True)
            return

        match = await db.get_solo_match_by_channel(interaction.channel_id)
        if not match or match.get("status") in ("COMPLETED", "CANCELLED"):
            await interaction.followup.send("This command can only be used inside an active queue channel.", ephemeral=True)
            return

        if old_captain.id == new_captain.id:
            await interaction.followup.send("Old captain and new captain cannot be the same person.", ephemeral=True)
            return

        c1_id = match["captain1_id"]
        c2_id = match["captain2_id"]

        if old_captain.id not in (c1_id, c2_id):
            await interaction.followup.send(f"<@{old_captain.id}> is not currently a captain in this queue.", ephemeral=True)
            return

        if new_captain.id in (c1_id, c2_id):
            await interaction.followup.send(f"<@{new_captain.id}> is already a captain in this queue.", ephemeral=True)
            return

        t1_ids = list(match.get("team1_player_ids", []))
        t2_ids = list(match.get("team2_player_ids", []))
        avail_ids = list(match.get("available_player_ids", []))
        all_pids = set(t1_ids + t2_ids + avail_ids + [c1_id, c2_id])

        if new_captain.id not in all_pids:
            await interaction.followup.send(f"<@{new_captain.id}> is not a player in this queue.", ephemeral=True)
            return

        is_cap1 = (old_captain.id == c1_id)
        new_c1 = new_captain.id if is_cap1 else c1_id
        new_c2 = new_captain.id if not is_cap1 else c2_id

        if is_cap1:
            if new_captain.id in avail_ids:
                avail_ids.remove(new_captain.id)
                avail_ids.append(old_captain.id)
                if old_captain.id in t1_ids:
                    t1_ids.remove(old_captain.id)
                if new_captain.id not in t1_ids:
                    t1_ids.insert(0, new_captain.id)
            elif new_captain.id in t2_ids:
                t2_ids.remove(new_captain.id)
                t2_ids.append(old_captain.id)
                if old_captain.id in t1_ids:
                    t1_ids.remove(old_captain.id)
                if new_captain.id not in t1_ids:
                    t1_ids.insert(0, new_captain.id)
            elif new_captain.id in t1_ids:
                t1_ids = [new_captain.id] + [p for p in t1_ids if p != new_captain.id]
                if old_captain.id not in t1_ids:
                    t1_ids.append(old_captain.id)
            else:
                t1_ids = [new_captain.id] + [p for p in t1_ids if p not in (new_captain.id, old_captain.id)] + [old_captain.id]
        else:
            if new_captain.id in avail_ids:
                avail_ids.remove(new_captain.id)
                avail_ids.append(old_captain.id)
                if old_captain.id in t2_ids:
                    t2_ids.remove(old_captain.id)
                if new_captain.id not in t2_ids:
                    t2_ids.insert(0, new_captain.id)
            elif new_captain.id in t1_ids:
                t1_ids.remove(new_captain.id)
                t1_ids.append(old_captain.id)
                if old_captain.id in t2_ids:
                    t2_ids.remove(old_captain.id)
                if new_captain.id not in t2_ids:
                    t2_ids.insert(0, new_captain.id)
            elif new_captain.id in t2_ids:
                t2_ids = [new_captain.id] + [p for p in t2_ids if p != new_captain.id]
                if old_captain.id not in t2_ids:
                    t2_ids.append(old_captain.id)
            else:
                t2_ids = [new_captain.id] + [p for p in t2_ids if p not in (new_captain.id, old_captain.id)] + [old_captain.id]

        turn_id = match.get("current_turn_captain_id")
        if turn_id == old_captain.id:
            turn_id = new_captain.id

        updated_match = await db.update_solo_match_captains(
            match_id=match["id"],
            captain1_id=new_c1,
            captain2_id=new_c2,
            team1_player_ids=t1_ids,
            team2_player_ids=t2_ids,
            available_player_ids=avail_ids,
            current_turn_captain_id=turn_id,
        )
        if not updated_match:
            updated_match = dict(match)
            updated_match["captain1_id"] = new_c1
            updated_match["captain2_id"] = new_c2
            updated_match["team1_player_ids"] = t1_ids
            updated_match["team2_player_ids"] = t2_ids
            updated_match["available_player_ids"] = avail_ids
            updated_match["current_turn_captain_id"] = turn_id

        # Update lobby panel embed/view if active
        try:
            panel_msg_id = updated_match.get("panel_message_id")
            ch = None
            ch_id = updated_match.get("channel_id") or interaction.channel_id
            if interaction.guild and ch_id:
                ch = interaction.guild.get_channel(ch_id)
            if not ch:
                ch = interaction.channel
            if panel_msg_id and ch and hasattr(ch, "fetch_message"):
                panel_msg = await ch.fetch_message(panel_msg_id)
                match_pids = list(set(t1_ids + t2_ids + avail_ids + [new_c1, new_c2]))
                fetched = await db.get_players_bulk(match_pids)
                players_by_id = {p["discord_id"]: p for p in fetched}
                colour = await get_solo_embed_colour()
                status = updated_match.get("status")

                if status == "DRAFTING":
                    embed = build_solo_draft_embed(updated_match, players_by_id, colour=colour)
                    avail_players = [players_by_id[pid] for pid in avail_ids if pid in players_by_id]
                    draft_mode = await get_solo_draft_mode()
                    view = SoloDraftView(updated_match, avail_players, players_by_id=players_by_id, colour=colour, draft_mode=draft_mode)
                    await panel_msg.edit(embed=embed, view=view)
                elif status == "MAP_VETO":
                    embed = build_solo_map_veto_embed(updated_match, players_by_id, colour=colour)
                    veto_mode = await get_solo_veto_mode()
                    if veto_mode not in ("MAP_VOTE", "VOTE"):
                        view = SoloMapVetoView(updated_match, players_by_id, veto_mode=veto_mode, colour=colour)
                        await panel_msg.edit(embed=embed, view=view)
                    else:
                        await panel_msg.edit(embed=embed)
                elif status == "VOICE_CHECKIN":
                    v_lobby_id = updated_match.get("voice_lobby_id")
                    lobby_vc = interaction.guild.get_channel(v_lobby_id) if (v_lobby_id and interaction.guild) else None
                    connected_pids = {m.id for m in lobby_vc.members if m.id in match_pids} if isinstance(lobby_vc, discord.VoiceChannel) else set()
                    embed = build_solo_checkin_embed(updated_match, players_by_id, connected_pids, v_lobby_id or 0, colour=colour)
                    await panel_msg.edit(embed=embed)
                elif status == "IN_PROGRESS":
                    embed = build_solo_map_veto_embed(updated_match, players_by_id, colour=colour)
                    map_file = get_solo_map_file(updated_match.get("selected_map"))
                    edit_kwargs = {"embed": embed}
                    if map_file:
                        edit_kwargs["attachments"] = [map_file]
                    await panel_msg.edit(**edit_kwargs)
        except Exception as e:
            log.debug("Failed to update panel message on captain change: %s", e)

        try:
            await interaction.followup.send(
                f"Captain updated: <@{new_captain.id}> has replaced <@{old_captain.id}> as captain."
            )
        except Exception:
            if isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.send(
                    f"Captain updated: <@{new_captain.id}> has replaced <@{old_captain.id}> as captain."
                )

    async def _handle_set_elo_template(
        self,
        interaction: discord.Interaction,
        template: app_commands.Choice[str],
    ) -> None:
        """Helper to update the global ELO scoring template."""
        await interaction.response.defer(ephemeral=True)
        if not _is_admin(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions.", ephemeral=True)
            return
        await db.set_config(CONFIG_KEY_SCORING_MODE, template.value)
        await interaction.followup.send(
            f"ELO scoring template updated to **`{template.value}`** ({template.name}).",
            ephemeral=True,
        )

    @app_commands.command(
        name="set-elo-template",
        description="Set the matchmaking ELO scoring template (Staff only).",
    )
    @app_commands.choices(template=[
        app_commands.Choice(name="Default Flat ELO (+25 Win / -20 Loss / +5 MVP)", value="DEFAULT"),
        app_commands.Choice(name="Performance Combat-Based ELO (Stats Scaling)", value="PERFORMANCE"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def set_elo_template_command(
        self,
        interaction: discord.Interaction,
        template: app_commands.Choice[str],
    ) -> None:
        """Change the global ELO template."""
        await self._handle_set_elo_template(interaction, template)

    @app_commands.command(
        name="set-elo-system",
        description="Set the matchmaking ELO scoring template (Staff only).",
    )
    @app_commands.choices(template=[
        app_commands.Choice(name="Default Flat ELO (+25 Win / -20 Loss / +5 MVP)", value="DEFAULT"),
        app_commands.Choice(name="Performance Combat-Based ELO (Stats Scaling)", value="PERFORMANCE"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def set_elo_system_command(
        self,
        interaction: discord.Interaction,
        template: app_commands.Choice[str],
    ) -> None:
        """Alias for set-elo-template."""
        await self._handle_set_elo_template(interaction, template)


async def setup(bot: commands.Bot) -> None:
    cog = SoloQueueCog(bot)
    await bot.add_cog(cog)
    bot.add_view(SoloQueueView(cog))
