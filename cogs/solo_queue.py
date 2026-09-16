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

MAP_POOL_RAW = os.environ.get(
    "MAP_POOL",
    "Ascent, Bind, Haven, Split, Sunset, Lotus, Abyss",
)
MAP_POOL: list[str] = [m.strip() for m in MAP_POOL_RAW.split(",") if m.strip()]

CAPTAIN_SELECTION_MODE = os.environ.get("CAPTAIN_SELECTION_MODE", "HIGHEST_ELO").upper()
DRAFT_MODE = os.environ.get("DRAFT_MODE", "SNAKE").upper()

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")


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

def build_solo_queue_embed(queued_players: list[dict]) -> discord.Embed:
    """Construct an elevated, zero-emoji 10-man solo queue embed."""
    count = len(queued_players)
    embed = discord.Embed(
        title="VEGA RANKED — 10-MAN SOLO QUEUE",
        description=(
            "> **Competitive Pick-Up Game (PUG)**\n"
            "> Queue up solo. When 10 players arrive, a private match lobby is generated with Captain Selection, Player Draft, and Map Veto.\n\n"
            f"> **Lobby Status:** `[ {count} / 10 Players Waiting ]`"
        ),
        colour=EMBED_COLOUR,
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
        colour=EMBED_COLOUR,
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
            colour=EMBED_COLOUR,
        )
    else:
        embed = discord.Embed(
            title=f"MATCH #{match['id']} — MAP VETO",
            description=(
                f"> **Phase:** Map Veto\n"
                f"> **Active Turn:** <@{turn_id}>, click a map button below to **BAN** it."
            ),
            colour=EMBED_COLOUR,
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

            # Build player cache
            all_match_pids = t1_ids + t2_ids
            players_by_id = {}
            for pid in all_match_pids:
                p_rec = await db.get_player(pid)
                if p_rec:
                    players_by_id[pid] = p_rec

            embed = build_solo_map_veto_embed(updated_match, players_by_id)
            veto_view = SoloMapVetoView(updated_match, players_by_id)

            if interaction.channel:
                await interaction.edit_original_response(embed=embed, view=veto_view)
                await interaction.channel.send(
                    f"**DRAFT COMPLETE**\n"
                    f"Teams have been finalized. Starting Map Veto phase.\n"
                    f"<@{c1_id}> Click a button below to BAN your first map."
                )
            return

        # Advance draft step
        next_turn_id = get_draft_active_captain_id(next_step, c1_id, c2_id)
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
        embed = build_solo_draft_embed(updated_match, players_by_id)
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
    """View holding dynamic map ban buttons during map veto."""

    def __init__(self, match: dict, players_by_id: dict[int, dict]) -> None:
        super().__init__(timeout=None)
        self.match = match
        self.players_by_id = players_by_id

        avail_maps = match.get("available_maps", [])
        if match.get("status") == "IN_PROGRESS" or len(avail_maps) <= 1:
            return

        for map_name in avail_maps:
            btn = discord.ui.Button(
                label=f"Ban {map_name}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"solo_map_ban:{map_name}",
            )
            btn.callback = self._create_map_ban_callback(map_name)
            self.add_item(btn)

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

                embed = build_solo_map_veto_embed(updated_match, self.players_by_id)
                final_view = discord.ui.View()  # empty view, no buttons

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

            embed = build_solo_map_veto_embed(updated_match, self.players_by_id)
            next_view = SoloMapVetoView(updated_match, self.players_by_id)

            if interaction.channel:
                await interaction.edit_original_response(embed=embed, view=next_view)
                await interaction.channel.send(
                    f"<@{interaction.user.id}> banned **{map_to_ban}**.\n"
                    f"<@{next_turn_id}> It is your turn to ban a map."
                )

        return callback


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
            await interaction.followup.send(
                "You must register your player profile first using `/register`.",
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
                # Captain selection via configured template
                cap1, cap2 = select_captains(match_players)
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

                # Create match DB record
                match = await db.create_solo_match(
                    channel_id=channel.id,
                    captain1_id=c1_id,
                    captain2_id=c2_id,
                    available_player_ids=avail_ids,
                    available_maps=list(MAP_POOL),
                )

                if not match:
                    log.error("Failed to insert solo match record.")
                    return

                players_by_id = {p["discord_id"]: p for p in match_players}
                avail_dicts = [players_by_id[pid] for pid in avail_ids]

                embed = build_solo_draft_embed(match, players_by_id)
                view = SoloDraftView(match, avail_dicts)

                pings = " ".join(f"<@{pid}>" for pid in player_ids)
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
    # Admin Commands
    # =========================================================================

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
