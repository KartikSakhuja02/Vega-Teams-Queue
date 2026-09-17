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
import logging
import os
import random
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
    if val and val.upper() in ("ALTERNATING_BAN", "BAN_BAN_PICK", "RANDOM_MAP", "CAPTAIN_PICK"):
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
    is_team_mvp = (not is_match_mvp) and (is_mvp or mvp_type == "Team MVP" or "team" in str(mvp_type).lower())

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
) -> discord.Embed:
    """Minimalist 10-man solo queue panel embed."""
    count = len(queued_players)
    if queued_players:
        names = " • ".join(
            p.get("ign") or p.get("discord_username") or "Player"
            for p in queued_players
        )
        body = f"`[ {count} / 10 ]`\n{names}"
    else:
        body = "`[ 0 / 10 ]`\n*Waiting for players...*"

    embed = discord.Embed(
        title="VEGA QUEUE",
        description=body,
        colour=colour or EMBED_COLOUR,
    )
    embed.set_footer(text="Click Join Queue or Leave Queue below")
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
        title=f"MATCH #{match['id']} — VOICE CHECK-IN",
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
        title=f"Match {match['id']}  —  Draft  [{step}/7]",
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
    """Ultra-minimalist map veto embed — no emojis, plain text only."""
    status = match.get("status", "MAP_VETO")
    turn_id = match.get("current_turn_captain_id")
    selected_map = match.get("selected_map")
    avail_maps = match.get("available_maps", [])
    banned_maps = match.get("banned_maps", [])
    c1_id = match["captain1_id"]
    c2_id = match["captain2_id"]

    def _names(ids: list) -> str:
        parts = [players_by_id.get(pid, {}).get("ign") or str(pid) for pid in ids]
        return ",  ".join(parts) or "-"

    t1 = _names(match.get("team1_player_ids", []))
    t2 = _names(match.get("team2_player_ids", []))

    if status == "IN_PROGRESS":
        desc = (
            f"Map: {selected_map}\n\n"
            f"Team A — {t1}\n"
            f"Team B — {t2}"
        )
        embed = discord.Embed(
            title=f"Match {match['id']}  —  Ready",
            description=desc,
            colour=colour or EMBED_COLOUR,
        )
    else:
        action = "Pick" if len(avail_maps) == 2 else "Ban"
        picker = players_by_id.get(turn_id, {}).get("ign") or str(turn_id)
        avail_str = ",  ".join(avail_maps)
        banned_str = ",  ".join(f"~~{m}~~" for m in banned_maps) if banned_maps else "-"
        desc = (
            f"{picker} — {action}\n\n"
            f"Maps: {avail_str}\n"
            f"Banned: {banned_str}\n\n"
            f"Team A — {t1}\n"
            f"Team B — {t2}"
        )
        embed = discord.Embed(
            title=f"Match {match['id']}  —  Map Veto",
            description=desc,
            colour=colour or EMBED_COLOUR,
        )
    return embed


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
        value=f"`{veto_mode}`\n*(Alternating Bans, Ban-Ban-Pick, Random Map, or Captain Pick)*",
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
    and automatically move any players currently connected to voice.
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

    if t1_vc:
        for pid in t1_ids:
            mem = await _get_or_fetch_member(guild, pid)
            if mem:
                try:
                    await t1_vc.set_permissions(mem, view_channel=True, connect=True, speak=True)
                except Exception as e:
                    log.debug("Could not set Team 1 voice permissions for %s: %s", mem.name, e)
                if mem.voice and mem.voice.channel:
                    try:
                        await mem.move_to(t1_vc, reason=f"Match #{match['id']} Team 1 VC")
                    except Exception as e:
                        log.debug("Could not move %s to Team 1 VC: %s", mem.name, e)

    if t2_vc:
        for pid in t2_ids:
            mem = await _get_or_fetch_member(guild, pid)
            if mem:
                try:
                    await t2_vc.set_permissions(mem, view_channel=True, connect=True, speak=True)
                except Exception as e:
                    log.debug("Could not set Team 2 voice permissions for %s: %s", mem.name, e)
                if mem.voice and mem.voice.channel:
                    try:
                        await mem.move_to(t2_vc, reason=f"Match #{match['id']} Team 2 VC")
                    except Exception as e:
                        log.debug("Could not move %s to Team 2 VC: %s", mem.name, e)


# =============================================================================
# Interactive Views & Dropdowns
# =============================================================================

class SoloQueueView(discord.ui.View):
    """
    Persistent view for joining/leaving the 10-man solo queue.
    Button labels strictly: 'Join Queue' and 'Leave Queue'.
    """

    def __init__(self, cog: Optional[SoloQueueCog] = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog

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

    def __init__(self, match_id: int, available_players: list[dict]) -> None:
        options = [
            discord.SelectOption(
                label=f"{p['ign']} (ELO: {p.get('elo', 1000)})",
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

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        match = await db.get_solo_match_by_id(self.match_id)
        if not match or match["status"] != "DRAFTING":
            await interaction.followup.send("Drafting is no longer active for this match.", ephemeral=True)
            return

        if interaction.user.id != match["current_turn_captain_id"]:
            await interaction.followup.send("It is not your turn to pick.", ephemeral=True)
            return

        picked_id = int(self.values[0])
        avail_ids = list(match["available_player_ids"])
        if picked_id not in avail_ids:
            await interaction.followup.send("That player is no longer available in the pool.", ephemeral=True)
            return

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
        if next_step > 7 or len(avail_ids) == 1:
            if avail_ids:
                last_player_id = avail_ids.pop(0)
                if len(t1_ids) < 5:
                    t1_ids.append(last_player_id)
                else:
                    t2_ids.append(last_player_id)

            if interaction.guild:
                asyncio.create_task(finalize_teams_and_move(interaction.client, match, interaction.guild, t1_ids, t2_ids))

            veto_mode = await get_solo_veto_mode()
            colour = await get_solo_embed_colour()
            map_pool = await get_solo_map_pool()

            # Build player cache
            all_match_pids = t1_ids + t2_ids
            players_by_id = {}
            for pid in all_match_pids:
                p_rec = await db.get_player(pid)
                if p_rec:
                    players_by_id[pid] = p_rec

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
                embed = build_solo_map_veto_embed(updated_match, players_by_id, colour=colour)
                final_view = discord.ui.View()
                if interaction.channel:
                    await interaction.edit_original_response(embed=embed, view=final_view)
                    await interaction.channel.send(
                        f"**DRAFT COMPLETE • MAP RANDOMLY SELECTED: {final_map.upper()}**\n"
                        f"Teams have been finalized and moved to Team A & B voice channels!\n"
                        f"The match will be played on **{final_map}**!\n\n"
                        f"Captains <@{c1_id}> and <@{c2_id}>: Please set up the custom lobby and invite all players."
                    )
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

            embed = build_solo_map_veto_embed(updated_match, players_by_id, colour=colour)
            veto_view = SoloMapVetoView(updated_match, players_by_id, veto_mode=veto_mode)

            if interaction.channel:
                await interaction.edit_original_response(embed=embed, view=veto_view)
                await interaction.channel.send(
                    f"**DRAFT COMPLETE**\n"
                    f"Teams have been finalized and moved to Team A & B voice channels!\n"
                    f"Starting Map Veto phase.\n"
                    f"<@{c1_id}> Click a button below to BAN your first map."
                )
            return

        # Advance draft step
        draft_mode = await get_solo_draft_mode()
        colour = await get_solo_embed_colour()
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
        players_by_id = {}
        for pid in all_match_pids:
            p_rec = await db.get_player(pid)
            if p_rec:
                players_by_id[pid] = p_rec

        avail_player_dicts = [players_by_id[pid] for pid in avail_ids if pid in players_by_id]
        embed = build_solo_draft_embed(updated_match, players_by_id, colour=colour)
        view = SoloDraftView(updated_match, avail_player_dicts)

        if interaction.channel:
            await interaction.edit_original_response(embed=embed, view=view)
            picked_p = players_by_id.get(picked_id, {})
            await interaction.channel.send(
                f"<@{interaction.user.id}> drafted **{picked_p.get('ign', 'Player')}**.\n"
                f"<@{next_turn_id}> It is your turn to pick."
            )


class SoloDraftView(discord.ui.View):
    """View holding the player selection dropdown during drafting."""

    def __init__(self, match: dict, available_players: list[dict]) -> None:
        super().__init__(timeout=None)
        if available_players:
            self.add_item(PlayerDraftSelect(match["id"], available_players))


class SoloMapVetoView(discord.ui.View):
    """View holding dynamic map ban/pick buttons during map veto."""

    def __init__(
        self,
        match: dict,
        players_by_id: dict[int, dict],
        veto_mode: str = "ALTERNATING_BAN",
    ) -> None:
        super().__init__(timeout=None)
        self.match = match
        self.players_by_id = players_by_id
        self.veto_mode = veto_mode

        avail_maps = match.get("available_maps", [])
        if match.get("status") == "IN_PROGRESS" or len(avail_maps) <= 1:
            return

        is_pick_phase = (self.veto_mode in ("BAN_BAN_PICK", "CAPTAIN_PICK")) and len(avail_maps) == 2

        for map_name in avail_maps:
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
            await interaction.response.defer()
            match = await db.get_solo_match_by_id(self.match["id"])
            if not match or match["status"] != "MAP_VETO":
                await interaction.followup.send("Map veto is not currently active.", ephemeral=True)
                return

            if interaction.user.id != match["current_turn_captain_id"]:
                await interaction.followup.send("It is not your turn to pick a map.", ephemeral=True)
                return

            avail_maps = list(match["available_maps"])
            banned_maps = list(match["banned_maps"])

            if chosen_map not in avail_maps:
                await interaction.followup.send("That map is not available.", ephemeral=True)
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

            colour = await get_solo_embed_colour()
            embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)
            final_view = discord.ui.View()

            if interaction.channel:
                await interaction.edit_original_response(embed=embed, view=final_view)
                await interaction.channel.send(
                    f"**MAP DECIDED: {chosen_map.upper()}**\n"
                    f"<@{interaction.user.id}> picked **{chosen_map}**!\n\n"
                    f"Captains <@{c1_id}> and <@{c2_id}>: Please set up the custom lobby and invite all players.\n\n"
                    f"📸 **Submit Result:** When the match concludes, any player in this channel can submit the scoreboard screenshot using `/submit-result`!"
                )
        return callback

    def _create_map_ban_callback(self, map_to_ban: str):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            match = await db.get_solo_match_by_id(self.match["id"])
            if not match or match["status"] != "MAP_VETO":
                await interaction.followup.send("Map veto is not currently active.", ephemeral=True)
                return

            if interaction.user.id != match["current_turn_captain_id"]:
                await interaction.followup.send("It is not your turn to ban a map.", ephemeral=True)
                return

            avail_maps = list(match["available_maps"])
            banned_maps = list(match["banned_maps"])

            if map_to_ban not in avail_maps:
                await interaction.followup.send("That map is already banned.", ephemeral=True)
                return

            avail_maps.remove(map_to_ban)
            banned_maps.append(map_to_ban)

            c1_id = match["captain1_id"]
            c2_id = match["captain2_id"]
            next_turn_id = c2_id if match["current_turn_captain_id"] == c1_id else c1_id
            colour = await get_solo_embed_colour()

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
                next_view = SoloMapVetoView(updated_match, self.players_by_id, veto_mode=self.veto_mode)

                if interaction.channel:
                    await interaction.edit_original_response(embed=embed, view=next_view)
                    await interaction.channel.send(
                        f"<@{interaction.user.id}> banned **{map_to_ban}**.\n"
                        f"<@{picker_id}> It is your turn to **PICK** the decider map."
                    )
                return

            # If only 1 map remains, it is the selected map!
            if len(avail_maps) == 1:
                final_map = avail_maps[0]
                updated_match = await db.update_solo_match_map_veto(
                    match_id=match["id"],
                    available_maps=[],
                    banned_maps=banned_maps,
                    selected_map=final_map,
                    current_turn_captain_id=None,
                    status="IN_PROGRESS",
                )

                embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)
                final_view = discord.ui.View()

                if interaction.channel:
                    await interaction.edit_original_response(embed=embed, view=final_view)
                    await interaction.channel.send(
                        f"**MAP DECIDED: {final_map.upper()}**\n"
                        f"<@{interaction.user.id}> banned **{map_to_ban}**.\n"
                        f"The match will be played on **{final_map}**!\n\n"
                        f"Captains <@{c1_id}> and <@{c2_id}>: Please set up the custom lobby and invite all players.\n\n"
                        f"📸 **Submit Result:** When the match concludes, any player in this channel can submit the scoreboard screenshot using `/submit-result`!"
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

            embed = build_solo_map_veto_embed(updated_match, self.players_by_id, colour=colour)
            next_view = SoloMapVetoView(updated_match, self.players_by_id, veto_mode=self.veto_mode)

            if interaction.channel:
                await interaction.edit_original_response(embed=embed, view=next_view)
                await interaction.channel.send(
                    f"<@{interaction.user.id}> banned **{map_to_ban}**.\n"
                    f"<@{next_turn_id}> It is your turn to ban a map."
                )

        return callback


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
        ]
        super().__init__(
            placeholder="Select Map Veto Template...",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
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
        view: SoloConfigPanelView = self.view  # type: ignore[assignment]
        new_theme = self.values[0]
        await db.set_config(CONFIG_KEY_THEME, new_theme)
        await view.cog.refresh_queue_message()
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
        captain_mode = await get_solo_captain_mode()
        draft_mode = await get_solo_draft_mode()
        veto_mode = await get_solo_veto_mode()
        scoring_mode = await get_solo_scoring_mode()
        results_ch_id = await get_solo_results_channel_id()
        theme_val = (await db.get_config(CONFIG_KEY_THEME)) or "PURPLE"
        map_pool = await get_solo_map_pool()
        colour = await get_solo_embed_colour()

        new_view = SoloConfigPanelView(self.cog, captain_mode, draft_mode, veto_mode, scoring_mode, theme_val)
        embed = build_solo_config_embed(captain_mode, draft_mode, veto_mode, scoring_mode, results_ch_id, theme_val, map_pool, colour)
        await interaction.response.edit_message(embed=embed, view=new_view)


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
        # Cache panel message ID in memory to avoid a DB round-trip on every refresh
        self._panel_message_id: Optional[int] = None

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

            embed = build_solo_queue_embed(queued_players)
            view = SoloQueueView(self)

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

            # Atomic dequeue
            await db.clear_solo_queue(player_ids)
            for pid in player_ids:
                await db.set_player_status(pid, "IN_MATCH")

            self._schedule_queue_panel_refresh(delay=0.1)

            try:
                # Dynamic configurations
                captain_mode = await get_solo_captain_mode()
                map_pool = await get_solo_map_pool()
                colour = await get_solo_embed_colour()

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

                for pid in player_ids:
                    mem = await _get_or_fetch_member(guild, pid)
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
                category: Optional[discord.CategoryChannel] = None
                if SOLO_MATCH_CATEGORY_ID:
                    cat = guild.get_channel(SOLO_MATCH_CATEGORY_ID)
                    if isinstance(cat, discord.CategoryChannel):
                        category = cat

                voice_category: Optional[discord.CategoryChannel] = category
                if SOLO_VOICE_CATEGORY_ID:
                    vcat = guild.get_channel(SOLO_VOICE_CATEGORY_ID)
                    if isinstance(vcat, discord.CategoryChannel):
                        voice_category = vcat

                # Create 1 text channel and 3 voice channels
                match_lobby_num = random.randint(100, 999)

                text_channel = await guild.create_text_channel(
                    name=f"match-lobby-{match_lobby_num}",
                    overwrites=text_overwrites,
                    category=category,
                    topic="10-Man Solo Ranked Match Lobby",
                )

                lobby_vc = await guild.create_voice_channel(
                    name=f"🔊 Match #{match_lobby_num} Lobby",
                    overwrites=voice_lobby_overwrites,
                    category=voice_category,
                )

                team_a_vc = await guild.create_voice_channel(
                    name=f"🔊 Match #{match_lobby_num} Team A",
                    overwrites=team_locked_overwrites,
                    category=voice_category,
                )

                team_b_vc = await guild.create_voice_channel(
                    name=f"🔊 Match #{match_lobby_num} Team B",
                    overwrites=team_locked_overwrites,
                    category=voice_category,
                )

                players_by_id = {p["discord_id"]: p for p in match_players}

                # Create match in DB with VOICE_CHECKIN status
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
                )

                if not match:
                    log.error("Failed to insert solo match record.")
                    return

                # Send DMs to each individual player
                dm_content = (
                    f"⚔️ **Your 10-Man Match #{match['id']} is Ready!**\n"
                    f"Text Lobby: {text_channel.mention}\n"
                    f"Voice Lobby: {lobby_vc.mention}\n\n"
                    f"Please join the **Voice Lobby** now. Team selection will begin once all 10 players join voice."
                )
                for pid in player_ids:
                    mem = await _get_or_fetch_member(guild, pid)
                    if mem:
                        try:
                            await mem.send(dm_content)
                        except Exception:
                            pass  # Direct messages may be disabled

                # Check initial voice connections
                connected_pids = {m.id for m in lobby_vc.members if m.id in player_ids}
                checkin_embed = build_solo_checkin_embed(
                    match, players_by_id, connected_pids, lobby_vc.id, colour=colour
                )

                pings = " ".join(f"<@{pid}>" for pid in player_ids)
                panel_msg = await text_channel.send(
                    content=(
                        f"{pings}\n"
                        f"**10-MAN MATCH FOUND — VOICE CHECK-IN**\n"
                        f"All 10 players please connect to {lobby_vc.mention} to begin!"
                    ),
                    embed=checkin_embed,
                )
                await db.update_solo_match_panel(match["id"], panel_msg.id)
                log.info("Created 10-man solo match #%d in channel #%s.", match["id"], text_channel.name)

                # If all 10 players are somehow already in voice, start immediately
                if len(connected_pids) >= 10:
                    await self._start_match_after_checkin(match, guild, text_channel)

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

        players_by_id = {}
        for pid in all_pids:
            p_rec = await db.get_player(pid)
            if p_rec:
                players_by_id[pid] = p_rec

        colour = await get_solo_embed_colour()
        embed = build_solo_checkin_embed(match, players_by_id, connected_pids, lobby_vc.id, colour=colour)

        if panel_msg_id:
            try:
                msg = await ch.fetch_message(panel_msg_id)
                await msg.edit(embed=embed)
            except Exception as e:
                log.debug("Could not edit checkin message: %s", e)

        if len(connected_pids) >= 10 and match.get("status") == "VOICE_CHECKIN":
            await self._start_match_after_checkin(match, guild, ch)

    async def _start_match_after_checkin(
        self,
        match: dict,
        guild: discord.Guild,
        channel: discord.TextChannel,
    ) -> None:
        """Advance match from VOICE_CHECKIN to DRAFTING or AUTO_BALANCE."""
        current_match = await db.get_solo_match_by_id(match["id"])
        if not current_match or current_match.get("status") != "VOICE_CHECKIN":
            return

        draft_mode = await get_solo_draft_mode()
        veto_mode = await get_solo_veto_mode()
        map_pool = await get_solo_map_pool()
        colour = await get_solo_embed_colour()

        all_pids = (
            current_match.get("team1_player_ids", [])
            + current_match.get("team2_player_ids", [])
            + current_match.get("available_player_ids", [])
        )
        all_pids = list(dict.fromkeys(all_pids))

        match_players = []
        players_by_id = {}
        for pid in all_pids:
            p_rec = await db.get_player(pid)
            if p_rec:
                match_players.append(p_rec)
                players_by_id[pid] = p_rec

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
                embed = build_solo_map_veto_embed(updated, players_by_id, colour=colour)
                panel_msg = await channel.send(
                    content=(
                        f"**ALL PLAYERS CHECKED IN • TEAMS AUTO-BALANCED**\n"
                        f"Map randomly selected: **{final_map.upper()}**!\n"
                        f"Teams have been moved to their respective voice channels.\n\n"
                        f"Captains <@{c1_id}> and <@{c2_id}>: Please set up the custom lobby and invite all players."
                    ),
                    embed=embed,
                )
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
                veto_view = SoloMapVetoView(updated, players_by_id, veto_mode=veto_mode)
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
        current_match = await db.get_solo_match_by_id(current_match["id"])
        avail_ids = current_match.get("available_player_ids", [])
        avail_dicts = [players_by_id[pid] for pid in avail_ids if pid in players_by_id]
        embed = build_solo_draft_embed(current_match, players_by_id, colour=colour)
        view = SoloDraftView(current_match, avail_dicts)

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

        # 5. Defer public response so players in the lobby see submission in progress
        await interaction.response.defer(thinking=True)

        # 6. Read attachment image bytes
        try:
            image_bytes = await screenshot.read()
        except Exception as exc:
            await db.release_solo_match_result_submission(match["id"])
            await interaction.followup.send(f"❌ Failed to read attached image: {exc}")
            return

        # 7. Run OCR Pipeline (Ollama qwen2.5vl:3b -> OpenRouter -> Tesseract fallback)
        from utils.match_ocr import process_match_screenshot, PlayerRowStats
        result = await process_match_screenshot(image_bytes)

        if not result.success or (not result.team1_players and not result.team2_players):
            await db.release_solo_match_result_submission(match["id"])
            await interaction.followup.send(
                f"❌ **Scoreboard Analysis Failed**: {result.error or 'Could not detect scoreboard table.'}\n"
                f"• Engine: `{result.engine}`\n"
                f"• Time: `{result.processing_time_ms} ms`\n\n"
                "Please make sure the entire Valorant match scoreboard is clearly visible and try again."
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

        # 14. Commit to database atomically
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

        # 15. Build comprehensive result embed matching /test_ss_ocr style
        if t1_score > t2_score:
            outcome_text = "🟢 Team 1 Victory"
            sidebar_color = discord.Colour.from_rgb(46, 204, 113)
        elif t2_score > t1_score:
            outcome_text = "🔴 Team 2 Victory"
            sidebar_color = discord.Colour.from_rgb(235, 66, 85)
        else:
            outcome_text = "🤝 Match Draw"
            sidebar_color = discord.Colour.gold()

        meta_parts = []
        if result.duration and result.duration != "Unknown":
            meta_parts.append(f"⏱️ {result.duration}")
        if result.match_date and result.match_date != "Unknown":
            meta_parts.append(f"📅 {result.match_date}")
        meta_str = f" • {' • '.join(meta_parts)}" if meta_parts else ""

        result_embed = discord.Embed(
            title=f"Match Results — {map_name}",
            description=(
                f"**Score:** 🟢 Team 1 **[{t1_score}]** — 🔴 Team 2 **[{t2_score}]**\n"
                f"**Outcome:** {outcome_text}{meta_str}"
            ),
            colour=sidebar_color,
        )

        def _format_team_lines(team_pids: list[int]) -> str:
            lines = []
            t_updates = [u for u in player_updates if u["discord_id"] in team_pids]
            t_updates.sort(
                key=lambda x: (x["kills"], x.get("stats_obj").acs if x.get("stats_obj") else 0),
                reverse=True,
            )

            for u in t_updates:
                pid = u["discord_id"]
                prec = lobby_player_records.get(pid, {})
                ign = prec.get("ign") or prec.get("discord_username") or f"Player {pid}"
                stats = u.get("stats_obj")

                mvp_badge = ""
                if stats and (stats.mvp_type == "Match MVP" or "match" in str(stats.mvp_type).lower()):
                    mvp_badge = " 👑 `Match MVP`"
                elif stats and (stats.mvp_type == "Team MVP" or stats.is_mvp):
                    mvp_badge = " ⭐ `Team MVP`"

                delta = u["elo_delta"]
                delta_str = f"`(+{delta} ELO)`" if delta > 0 else (f"`({delta} ELO)`" if delta < 0 else "`(= ELO)`")

                lines.append(f"**{ign}**{mvp_badge}")

                k, d, a = u["kills"], u["deaths"], u["assists"]
                parts = [f"`{k}/{d}/{a} KDA`"]
                if stats and stats.acs > 0:
                    parts.append(f"`{stats.acs} ACS`")
                if stats and stats.damage > 0:
                    parts.append(f"`{stats.damage:,} DMG`")
                if stats and stats.first_bloods > 0:
                    parts.append(f"`{stats.first_bloods} FB`")
                if stats and stats.plants > 0:
                    parts.append(f"`{stats.plants} PL`")
                if stats and stats.defuses > 0:
                    parts.append(f"`{stats.defuses} DF`")
                parts.append(delta_str)

                lines.append(f"└ {' • '.join(parts)}")
            return "\n".join(lines)[:1024] if lines else "*No players detected*"

        t1_header = f"🟢 Team 1 — {t1_score} Rounds" + (" 🏆" if t1_score > t2_score else "")
        t2_header = f"🔴 Team 2 — {t2_score} Rounds" + (" 🏆" if t2_score > t1_score else "")

        result_embed.add_field(name=t1_header, value=_format_team_lines(t1_pids), inline=False)
        result_embed.add_field(name=t2_header, value=_format_team_lines(t2_pids), inline=False)
        result_embed.set_footer(
            text=f"Scoring: {scoring_mode} • ACS: Combat Score • KDA: K/D/A • DMG: Damage • FB: First Bloods • PL/DF: Plants/Defuses"
        )
        result_embed.set_thumbnail(url=screenshot.url)

        # 16. Post to dedicated results channel if configured
        results_ch_id = await get_solo_results_channel_id()
        if results_ch_id and interaction.guild:
            results_channel = interaction.guild.get_channel(results_ch_id)
            if isinstance(results_channel, discord.TextChannel):
                try:
                    await results_channel.send(embed=result_embed)
                    log.info("Posted match #%d result to results channel #%s.", match["id"], results_channel.name)
                except Exception as e:
                    log.warning("Could not post match result to results channel %d: %s", results_ch_id, e)

        # 17. Post in active match lobby channel
        await interaction.followup.send(
            content=f"✅ **Match Results Finalized by {interaction.user.mention}!**",
            embed=result_embed,
        )
        await interaction.channel.send(
            "🎉 **Stats and ELO Updated!** All 10 players have been released back to **IDLE** and can join new queues.\n"
            "Staff can use `/cancel_solo_match` to close this channel when ready."
        )

        # 18. Cleanup voice channels if created
        v_lobby_id = match.get("voice_lobby_id")
        v1_id = match.get("voice_team1_id")
        v2_id = match.get("voice_team2_id")
        for vid in (v_lobby_id, v1_id, v2_id):
            if vid and interaction.guild:
                vch = interaction.guild.get_channel(vid)
                if isinstance(vch, discord.VoiceChannel):
                    try:
                        await vch.delete(reason=f"Match #{match['id']} concluded")
                    except Exception as e:
                        log.debug("Failed to delete temporary match voice channel: %s", e)

    @app_commands.command(
        name="cancel_solo_match",
        description="Cancel the current 10-man match lobby and return players to IDLE (Staff only).",
    )
    @app_commands.default_permissions(manage_channels=True)
    async def cancel_solo_match_command(self, interaction: discord.Interaction) -> None:
        """Cancel an active 10-man match and release players."""
        await interaction.response.defer(ephemeral=True)

        match = await db.get_solo_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.followup.send("This channel is not an active 10-man match lobby.", ephemeral=True)
            return

        # Release players to IDLE
        all_pids = (
            match.get("team1_player_ids", [])
            + match.get("team2_player_ids", [])
            + match.get("available_player_ids", [])
        )
        for pid in all_pids:
            await db.set_player_status(pid, "IDLE")

        # Delete voice channels if created
        v_lobby_id = match.get("voice_lobby_id")
        v1_id = match.get("voice_team1_id")
        v2_id = match.get("voice_team2_id")
        for vid in (v_lobby_id, v1_id, v2_id):
            if vid and interaction.guild:
                vch = interaction.guild.get_channel(vid)
                if isinstance(vch, discord.VoiceChannel):
                    try:
                        await vch.delete(reason=f"10-man match #{match['id']} cancelled")
                    except Exception as e:
                        log.debug("Failed to delete temporary match voice channel: %s", e)

        await db.cancel_solo_match(match["id"])
        await interaction.followup.send("10-man match cancelled. Deleting channel in 5 seconds...")
        await asyncio.sleep(5)
        try:
            if isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.delete(reason=f"10-man match #{match['id']} cancelled by {interaction.user.name}")
        except Exception as e:
            log.error("Failed to delete match channel: %s", e)


async def setup(bot: commands.Bot) -> None:
    cog = SoloQueueCog(bot)
    await bot.add_cog(cog)
    bot.add_view(SoloQueueView(cog))
