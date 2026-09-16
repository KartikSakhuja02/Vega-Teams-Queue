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
CONFIG_KEY_MAP_POOL = "solo_map_pool"
CONFIG_KEY_THEME = "solo_embed_colour"

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
    """Construct an elevated, zero-emoji 10-man solo queue embed."""
    count = len(queued_players)
    embed = discord.Embed(
        title="VEGA QUEUE",
        description=(
            "> VEGA Queue\n"
            "> JOIN QUEUE BY CLICKING ON \"JOIN QUEUE\" BUTTON BELOW\n\n"
            f"> **Lobby Status:** `[ {count} / 10 Players Waiting ]`"
        ),
        colour=colour or EMBED_COLOUR,
    )

    if queued_players:
        lines: list[str] = []
        for idx, p in enumerate(queued_players, 1):
            ign = p.get("ign") or p.get("discord_username") or "Player"
            elo = p.get("elo", 1000)
            reg = f" `[{p.get('region', 'Global')}]`"
            ts = int(p["joined_at"].timestamp()) if p.get("joined_at") else 0
            time_str = f" • <t:{ts}:R>" if ts else ""
            lines.append(f"> `{idx}.` **{ign}** • ELO: `{elo}`{reg}{time_str}")
        embed.add_field(name="Queued Players", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Queued Players", value="> *No players currently in queue*", inline=False)

    embed.set_footer(text="Vega Matchmaking • Click buttons below to queue")
    return embed


def build_solo_draft_embed(
    match: dict,
    players_by_id: dict[int, dict],
    colour: Optional[discord.Colour] = None,
) -> discord.Embed:
    """Construct the draft phase embed."""
    c1_id = match["captain1_id"]
    c2_id = match["captain2_id"]
    turn_id = match["current_turn_captain_id"]
    step = match["draft_step"]

    t1_ids = match.get("team1_player_ids", [])
    t2_ids = match.get("team2_player_ids", [])
    avail_ids = match.get("available_player_ids", [])

    embed = discord.Embed(
        title=f"MATCH #{match['id']} — PLAYER DRAFT",
        description=(
            f"> **Phase:** Player Draft (Step {step} of 7)\n"
            f"> **Active Turn:** <@{turn_id}>, choose a player from the dropdown below."
        ),
        colour=colour or EMBED_COLOUR,
    )

    # Team 1 Roster
    t1_lines = [f"**Captain:** <@{c1_id}> (`{players_by_id.get(c1_id, {}).get('ign', 'Cap')}`)"]
    for pid in t1_ids:
        if pid != c1_id:
            p_data = players_by_id.get(pid, {})
            t1_lines.append(f"> • **{p_data.get('ign', 'Player')}** (ELO: `{p_data.get('elo', 1000)}`)")
    embed.add_field(name=f"Team 1 `[ {len(t1_ids)} / 5 ]`", value="\n".join(t1_lines), inline=True)

    # Team 2 Roster
    t2_lines = [f"**Captain:** <@{c2_id}> (`{players_by_id.get(c2_id, {}).get('ign', 'Cap')}`)"]
    for pid in t2_ids:
        if pid != c2_id:
            p_data = players_by_id.get(pid, {})
            t2_lines.append(f"> • **{p_data.get('ign', 'Player')}** (ELO: `{p_data.get('elo', 1000)}`)")
    embed.add_field(name=f"Team 2 `[ {len(t2_ids)} / 5 ]`", value="\n".join(t2_lines), inline=True)

    # Remaining Available Players
    if avail_ids:
        avail_lines = [
            f"> • **{players_by_id.get(pid, {}).get('ign', 'Player')}** • ELO: `{players_by_id.get(pid, {}).get('elo', 1000)}`"
            for pid in avail_ids
        ]
        embed.add_field(name=f"Available Draft Pool `[ {len(avail_ids)} Remaining ]`", value="\n".join(avail_lines), inline=False)

    embed.set_footer(text="Vega 10-Man System • Turn-based Player Draft")
    return embed


def build_solo_map_veto_embed(
    match: dict,
    players_by_id: dict[int, dict],
    colour: Optional[discord.Colour] = None,
) -> discord.Embed:
    """Construct the map veto phase embed."""
    status = match.get("status", "MAP_VETO")
    turn_id = match.get("current_turn_captain_id")
    selected_map = match.get("selected_map")
    avail_maps = match.get("available_maps", [])
    banned_maps = match.get("banned_maps", [])

    c1_id = match["captain1_id"]
    c2_id = match["captain2_id"]

    if status == "IN_PROGRESS":
        embed = discord.Embed(
            title=f"MATCH #{match['id']} — MATCH READY",
            description=(
                f"> **Selected Map:** **{selected_map}**\n"
                "> Map veto complete. Both teams please join voice channels and assemble in-game."
            ),
            colour=colour or EMBED_COLOUR,
        )
    else:
        is_pick_turn = len(avail_maps) == 2
        action_text = "**PICK** it as the decider map" if is_pick_turn else "**BAN** it"
        embed = discord.Embed(
            title=f"MATCH #{match['id']} — MAP VETO",
            description=(
                f"> **Phase:** Map Veto\n"
                f"> **Active Turn:** <@{turn_id}>, click a map button below to {action_text}."
            ),
            colour=colour or EMBED_COLOUR,
        )

    # Team Rosters
    t1_names = [players_by_id.get(pid, {}).get("ign", "Player") for pid in match.get("team1_player_ids", [])]
    t2_names = [players_by_id.get(pid, {}).get("ign", "Player") for pid in match.get("team2_player_ids", [])]

    embed.add_field(name=f"Team 1 (Captain: <@{c1_id}>)", value="\n".join(f"> • {n}" for n in t1_names), inline=True)
    embed.add_field(name=f"Team 2 (Captain: <@{c2_id}>)", value="\n".join(f"> • {n}" for n in t2_names), inline=True)

    # Map pool status
    if status != "IN_PROGRESS":
        embed.add_field(name="Available Maps", value=" • ".join(f"`{m}`" for m in avail_maps) if avail_maps else "None", inline=False)
    if banned_maps:
        embed.add_field(name="Banned Maps", value=" • ".join(f"~~`{m}`~~" for m in banned_maps), inline=False)

    embed.set_footer(text="Vega 10-Man System • Map Veto Phase")
    return embed


def build_solo_config_embed(
    captain_mode: str,
    draft_mode: str,
    veto_mode: str,
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
                        f"Teams have been finalized. The match will be played on **{final_map}**!\n\n"
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
                    f"Teams have been finalized. Starting Map Veto phase.\n"
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
                    f"Captains <@{c1_id}> and <@{c2_id}>: Please set up the custom lobby and invite all players."
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
                        f"Captains <@{c1_id}> and <@{c2_id}>: Please set up the custom lobby and invite all players."
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
            row=3,
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
        theme: str,
    ) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.add_item(SoloConfigCaptainSelect(captain_mode))
        self.add_item(SoloConfigDraftSelect(draft_mode))
        self.add_item(SoloConfigVetoSelect(veto_mode))
        self.add_item(SoloConfigThemeSelect(theme))

    async def refresh(self, interaction: discord.Interaction) -> None:
        captain_mode = await get_solo_captain_mode()
        draft_mode = await get_solo_draft_mode()
        veto_mode = await get_solo_veto_mode()
        theme_val = (await db.get_config(CONFIG_KEY_THEME)) or "PURPLE"
        map_pool = await get_solo_map_pool()
        colour = await get_solo_embed_colour()

        new_view = SoloConfigPanelView(self.cog, captain_mode, draft_mode, veto_mode, theme_val)
        embed = build_solo_config_embed(captain_mode, draft_mode, veto_mode, theme_val, map_pool, colour)
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
        """Fetch latest solo queue data from database and update or post the persistent queue message."""
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

            stored_id_str = await db.get_config(SOLO_QUEUE_MESSAGE_CONFIG_KEY)
            if stored_id_str:
                try:
                    stored_id = int(stored_id_str)
                    existing_msg = await channel.fetch_message(stored_id)
                    await existing_msg.edit(content=None, embed=embed, view=view, attachments=[])
                    log.info("Refreshed solo queue panel message (ID: %d).", stored_id)
                    return
                except discord.NotFound:
                    log.warning("Stored solo queue message %s was deleted. Sending new message.", stored_id_str)
                except Exception as e:
                    log.error("Error editing existing solo queue message %s: %s", stored_id_str, e)

            try:
                msg = await channel.send(embed=embed, view=view)
                try:
                    await msg.pin()
                except discord.Forbidden:
                    log.warning("Missing Manage Messages permission — could not pin solo queue message.")
                await db.set_config(SOLO_QUEUE_MESSAGE_CONFIG_KEY, str(msg.id))
                log.info("Sent and stored new solo queue panel message (ID: %d).", msg.id)
            except Exception as e:
                log.error("Failed to post solo queue panel message: %s", e)

    # =========================================================================
    # Queue Actions
    # =========================================================================

    async def handle_join_queue(self, interaction: discord.Interaction) -> None:
        """Handle player joining 10-man solo queue."""
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        player = await db.get_player(user_id)
        if not player:
            b_reg_id = int(os.environ.get("SERVER_B_REGISTRATION_CHANNEL_ID", "0") or "0")
            ch_hint = f" in <#{b_reg_id}>" if b_reg_id else ""
            await interaction.followup.send(
                f"You must register your player profile first using `/register`{ch_hint}.",
                ephemeral=True,
            )
            return

        if player.get("is_banned"):
            await interaction.followup.send("You are currently banned from competitive queues.", ephemeral=True)
            return

        if player.get("status") == "IN_MATCH":
            await interaction.followup.send("You are currently listed as in an active match.", ephemeral=True)
            return

        queued_players = await db.get_solo_queue()
        if any(p["discord_id"] == user_id for p in queued_players):
            await interaction.followup.send("You are already in the 10-man queue.", ephemeral=True)
            return

        await db.add_player_to_solo_queue(user_id)
        await db.set_player_status(user_id, "IN_QUEUE")
        await self.refresh_queue_message()

        await interaction.followup.send("You have joined the 10-man queue.", ephemeral=True)
        log.info("Player %s (%d) joined 10-man solo queue.", interaction.user.name, user_id)

        # Check if 10 players reached
        if interaction.guild:
            asyncio.create_task(self._check_and_create_solo_match(interaction.guild))

    async def handle_leave_queue(self, interaction: discord.Interaction) -> None:
        """Handle player leaving 10-man solo queue."""
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        removed = await db.remove_player_from_solo_queue(user_id)
        if removed:
            await db.set_player_status(user_id, "IDLE")
            await self.refresh_queue_message()
            await interaction.followup.send("You have left the 10-man queue.", ephemeral=True)
            log.info("Player %s (%d) left 10-man solo queue.", interaction.user.name, user_id)
        else:
            await interaction.followup.send("You were not in the 10-man queue.", ephemeral=True)

    # =========================================================================
    # Match Creation (10 Players)
    # =========================================================================

    async def _check_and_create_solo_match(self, guild: discord.Guild) -> None:
        """Check if 10 players are queued, and form a match."""
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

            await self.refresh_queue_message()

            try:
                # Dynamic configurations
                captain_mode = await get_solo_captain_mode()
                draft_mode = await get_solo_draft_mode()
                veto_mode = await get_solo_veto_mode()
                map_pool = await get_solo_map_pool()
                colour = await get_solo_embed_colour()

                # Captain selection via configured template
                cap1, cap2 = select_captains(match_players, mode=captain_mode)
                c1_id = cap1["discord_id"]
                c2_id = cap2["discord_id"]

                avail_ids = [pid for pid in player_ids if pid not in (c1_id, c2_id)]

                # Permission overwrites
                overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
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

                player_perm = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                )

                for pid in player_ids:
                    mem = await _get_or_fetch_member(guild, pid)
                    if mem:
                        overwrites[mem] = player_perm

                for role_id in STAFF_ROLE_IDS:
                    role = guild.get_role(role_id)
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                        )

                # Category
                category: Optional[discord.CategoryChannel] = None
                if SOLO_MATCH_CATEGORY_ID:
                    cat = guild.get_channel(SOLO_MATCH_CATEGORY_ID)
                    if isinstance(cat, discord.CategoryChannel):
                        category = cat

                # Create match channel
                channel = await guild.create_text_channel(
                    name=f"match-lobby-{random.randint(100, 999)}",
                    overwrites=overwrites,
                    category=category,
                    topic="10-Man Solo Ranked Match Lobby",
                )

                players_by_id = {p["discord_id"]: p for p in match_players}
                pings = " ".join(f"<@{pid}>" for pid in player_ids)

                # Handle AUTO_BALANCE draft mode
                if draft_mode == "AUTO_BALANCE":
                    t1_ids, t2_ids = auto_balance_teams(match_players)
                    c1_id = t1_ids[0]
                    c2_id = t2_ids[0]

                    if veto_mode == "RANDOM_MAP":
                        final_map = random.choice(map_pool)
                        match = await db.create_solo_match(
                            channel_id=channel.id,
                            captain1_id=c1_id,
                            captain2_id=c2_id,
                            available_player_ids=[],
                            available_maps=[],
                        )
                        await db.update_solo_match_draft(
                            match_id=match["id"],
                            team1_player_ids=t1_ids,
                            team2_player_ids=t2_ids,
                            available_player_ids=[],
                            current_turn_captain_id=None,
                            draft_step=8,
                            status="IN_PROGRESS",
                        )
                        await db.update_solo_match_map_veto(
                            match_id=match["id"],
                            available_maps=[],
                            banned_maps=[],
                            selected_map=final_map,
                            current_turn_captain_id=None,
                            status="IN_PROGRESS",
                        )
                        updated_match = await db.get_solo_match_by_id(match["id"])
                        embed = build_solo_map_veto_embed(updated_match, players_by_id, colour=colour)
                        panel_msg = await channel.send(
                            content=(
                                f"{pings}\n"
                                f"**10-MAN MATCH READY**\n"
                                f"Teams auto-balanced by ELO. Map randomly selected: **{final_map.upper()}**!\n\n"
                                f"Captains <@{c1_id}> and <@{c2_id}>: Please set up the custom lobby and invite all players."
                            ),
                            embed=embed,
                        )
                        await db.update_solo_match_panel(match["id"], panel_msg.id)
                    else:
                        match = await db.create_solo_match(
                            channel_id=channel.id,
                            captain1_id=c1_id,
                            captain2_id=c2_id,
                            available_player_ids=[],
                            available_maps=list(map_pool),
                        )
                        await db.update_solo_match_draft(
                            match_id=match["id"],
                            team1_player_ids=t1_ids,
                            team2_player_ids=t2_ids,
                            available_player_ids=[],
                            current_turn_captain_id=c1_id,
                            draft_step=8,
                            status="MAP_VETO",
                        )
                        updated_match = await db.get_solo_match_by_id(match["id"])
                        embed = build_solo_map_veto_embed(updated_match, players_by_id, colour=colour)
                        veto_view = SoloMapVetoView(updated_match, players_by_id, veto_mode=veto_mode)
                        panel_msg = await channel.send(
                            content=(
                                f"{pings}\n"
                                f"**10-MAN MATCH FOUND • TEAMS AUTO-BALANCED**\n"
                                f"Captains: <@{c1_id}> and <@{c2_id}>.\n"
                                f"<@{c1_id}> Please ban the first map below."
                            ),
                            embed=embed,
                            view=veto_view,
                        )
                        await db.update_solo_match_panel(match["id"], panel_msg.id)

                    log.info("Created auto-balanced 10-man match #%d in channel #%s.", match["id"], channel.name)
                    return

                # Normal drafting flow (SNAKE or ALTERNATING)
                match = await db.create_solo_match(
                    channel_id=channel.id,
                    captain1_id=c1_id,
                    captain2_id=c2_id,
                    available_player_ids=avail_ids,
                    available_maps=list(map_pool),
                )

                if not match:
                    log.error("Failed to insert solo match record.")
                    return

                avail_dicts = [players_by_id[pid] for pid in avail_ids]
                embed = build_solo_draft_embed(match, players_by_id, colour=colour)
                view = SoloDraftView(match, avail_dicts)

                panel_msg = await channel.send(
                    content=(
                        f"{pings}\n"
                        f"**10-MAN MATCH FOUND**\n"
                        f"Captains selected: <@{c1_id}> and <@{c2_id}>.\n"
                        f"<@{c1_id}> Please make the first draft pick below."
                    ),
                    embed=embed,
                    view=view,
                )

                await db.update_solo_match_panel(match["id"], panel_msg.id)
                log.info("Created 10-man solo match #%d in channel #%s.", match["id"], channel.name)

            except Exception as e:
                log.error("Error creating 10-man solo match: %s", e, exc_info=True)

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
        theme_val = (await db.get_config(CONFIG_KEY_THEME)) or "PURPLE"
        map_pool = await get_solo_map_pool()
        colour = await get_solo_embed_colour()

        embed = build_solo_config_embed(captain_mode, draft_mode, veto_mode, theme_val, map_pool, colour)
        view = SoloConfigPanelView(self, captain_mode, draft_mode, veto_mode, theme_val)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @solo_config.command(name="view", description="View all active 10-man solo queue configurations.")
    async def solo_config_view_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        captain_mode = await get_solo_captain_mode()
        draft_mode = await get_solo_draft_mode()
        veto_mode = await get_solo_veto_mode()
        theme_val = (await db.get_config(CONFIG_KEY_THEME)) or "PURPLE"
        map_pool = await get_solo_map_pool()
        colour = await get_solo_embed_colour()

        embed = build_solo_config_embed(captain_mode, draft_mode, veto_mode, theme_val, map_pool, colour)
        await interaction.followup.send(embed=embed, ephemeral=True)

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
