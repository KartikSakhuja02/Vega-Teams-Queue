"""
cogs/team_queue.py
------------------
Matchmaking queue & scrim scheduling cog for teams.
Features:
- Persistent embed sent to the designated Discord channel on startup.
- Live view of teams in Regional and Global queues stored in PostgreSQL.
- Teams can join BOTH Regional and Global queues simultaneously.
- Automated matchmaking: when 2 teams are queued (Regional same-region or Global),
  both teams are dequeued and a private Discord text channel is created.
- Persistent Scrim Negotiation UI inside the match channel:
  - Propose Match Time (Modal)
  - Accept Time (Validated: opponent only)
  - Cancel Match
- Elevated, minimalistic design with strictly NO emojis in titles, descriptions, buttons, or footers.
- Restricted to active team captains and roster members.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db

log = logging.getLogger(__name__)

TEAM_QUEUE_CHANNEL_ID: int = int(os.environ.get("TEAM_QUEUE_CHANNEL_ID", "0"))
SCRIM_CATEGORY_ID: int = int(os.environ.get("SCRIM_CATEGORY_ID", "0"))
TEAM_QUEUE_MESSAGE_CONFIG_KEY: str = "team_queue_message_id"

from utils.staff import is_staff, _is_admin, STAFF_ROLE_NAMES, get_staff_role_ids, _matches_staff_role
_is_staff = is_staff
TEAM_MOD_ROLE_IDS: list[int] = list(get_staff_role_ids())


REGIONS: list[str] = ["India", "APAC", "EMEA", "Americas"]
EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")


def _normalize_tag_str(tag: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", tag).lower()
    return cleaned or "team"


async def _get_or_fetch_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except Exception:
        return None


# =============================================================================
# Embed Builders (Strictly ZERO Emojis)
# =============================================================================

def build_team_queue_embed(regional_teams: list[dict], global_teams: list[dict]) -> discord.Embed:
    """
    Construct an elevated, beautiful, zero-emoji team queue embed.
    """
    reg_team_ids = {t["team_id"] for t in regional_teams}
    glob_team_ids = {t["team_id"] for t in global_teams}
    dual_team_ids = reg_team_ids.intersection(glob_team_ids)
    all_team_ids = reg_team_ids.union(glob_team_ids)

    embed = discord.Embed(
        title="VEGA SCRIMS — TEAM MATCHMAKING",
        description=(
            "> **Matchmaking Lobby**\n"
            "> Teams can enter **Regional Queue**, **Global Queue**, or **Both** at the same time.\n"
            "> Only active team captains can manage queue participation.\n\n"
            f"> **Live Queue Overview:** Regional: `{len(regional_teams)}` • Global: `{len(global_teams)}` • Unique: `{len(all_team_ids)}`"
        ),
        colour=EMBED_COLOUR,
    )

    # Regional Queue Section
    regional_blocks: list[str] = []
    for reg in REGIONS:
        teams_in_reg = [t for t in regional_teams if t.get("region") == reg]
        if teams_in_reg:
            lines = [f"**{reg.upper()}** `[ {len(teams_in_reg)} Queued ]`"]
            for idx, t in enumerate(teams_in_reg, 1):
                tag_str = f" `[{t['team_tag']}]`" if t.get("team_tag") else ""
                captain_name = t.get("captain_ign") or t.get("captain_username") or "Captain"
                ts = int(t["joined_at"].timestamp()) if t.get("joined_at") else 0
                time_str = f" • <t:{ts}:R>" if ts else ""
                dual_str = " `[Dual]`" if t["team_id"] in dual_team_ids else ""
                lines.append(f"> `{idx}.` **{t['team_name']}**{tag_str} • Captain: **{captain_name}**{time_str}{dual_str}")
        else:
            lines = [f"**{reg.upper()}** `[ Empty ]`", "> *No teams waiting in queue*"]
        regional_blocks.append("\n".join(lines))

    regional_text = "\n\n".join(regional_blocks)
    if len(regional_text) > 1024:
        regional_text = regional_text[:1020] + "..."

    embed.add_field(
        name="Regional Matchmaking",
        value=regional_text,
        inline=False,
    )

    # Global Queue Section
    if global_teams:
        global_lines: list[str] = []
        for idx, t in enumerate(global_teams, 1):
            tag_str = f" `[{t['team_tag']}]`" if t.get("team_tag") else ""
            reg_str = f" `{t.get('region', 'Global')}`"
            captain_name = t.get("captain_ign") or t.get("captain_username") or "Captain"
            ts = int(t["joined_at"].timestamp()) if t.get("joined_at") else 0
            time_str = f" • <t:{ts}:R>" if ts else ""
            dual_str = " `[Dual]`" if t["team_id"] in dual_team_ids else ""
            global_lines.append(f"> `{idx}.` **{t['team_name']}**{tag_str} • {reg_str} • Captain: **{captain_name}**{time_str}{dual_str}")
        global_text = "\n".join(global_lines)
    else:
        global_text = "> *No teams waiting in global queue*"

    if len(global_text) > 1024:
        global_text = global_text[:1020] + "..."

    embed.add_field(
        name=f"Global Matchmaking `[ {len(global_teams)} Queued ]`",
        value=global_text,
        inline=False,
    )

    embed.set_footer(text="Vega Matchmaking System • Click buttons below to queue")
    return embed


def build_scrim_match_embed(
    match: dict,
    team1: dict,
    team2: dict,
    t1_members: Optional[list[dict]] = None,
    t2_members: Optional[list[dict]] = None,
) -> discord.Embed:
    """
    Construct a clean, zero-emoji negotiation embed for matched scrim teams.
    """
    status = match.get("status", "NEGOTIATING")
    match_type = match.get("match_type", "REGIONAL").capitalize()
    region = match.get("region", "Global")

    embed = discord.Embed(
        title="VEGA SCRIMS — MATCH NEGOTIATION",
        description=(
            f"> **Match ID:** `#{match['id']}` • **Queue:** `{match_type}` • **Region:** `{region}`\n"
            "> Both teams have been matched. Coordinate match timing using the options below."
        ),
        colour=EMBED_COLOUR,
    )

    # Team 1 Details
    t1_roster_count = (len(t1_members) + 1) if t1_members is not None else 1
    t1_value = (
        f"**Captain:** <@{team1['captain_discord_id']}> (`{team1['captain_ign']}`)\n"
        f"**Roster:** `{t1_roster_count}` registered player(s)"
    )
    embed.add_field(
        name=f"Team 1: {team1['team_name']} [{team1['team_tag']}]",
        value=t1_value,
        inline=True,
    )

    # Team 2 Details
    t2_roster_count = (len(t2_members) + 1) if t2_members is not None else 1
    t2_value = (
        f"**Captain:** <@{team2['captain_discord_id']}> (`{team2['captain_ign']}`)\n"
        f"**Roster:** `{t2_roster_count}` registered player(s)"
    )
    embed.add_field(
        name=f"Team 2: {team2['team_name']} [{team2['team_tag']}]",
        value=t2_value,
        inline=True,
    )

    # Status & Schedule Section
    if status == "CONFIRMED":
        confirmed_ts = (
            f"<t:{int(match['confirmed_at'].timestamp())}:F>"
            if match.get("confirmed_at")
            else "Just now"
        )
        status_text = (
            f"**Status:** `CONFIRMED`\n"
            f"**Scheduled Time:** **{match.get('proposed_time', 'N/A')}**\n"
            f"**Confirmed By:** <@{match.get('confirmed_by_user_id')}>\n"
            f"**Confirmed At:** {confirmed_ts}\n\n"
            "> Both teams please assemble in-game and exchange lobby details in this channel."
        )
    elif status == "CANCELLED":
        status_text = (
            "**Status:** `CANCELLED`\n"
            "> This match negotiation was cancelled. Both teams may rejoin the queue."
        )
    elif status == "COMPLETED":
        status_text = (
            "**Status:** `COMPLETED`\n"
            "> This scrim match has concluded."
        )
    else:  # NEGOTIATING
        if match.get("proposed_time"):
            proposer_team_id = match.get("proposed_by_team_id")
            if proposer_team_id == team1["id"]:
                proposer_name = team1["team_name"]
            elif proposer_team_id == team2["id"]:
                proposer_name = team2["team_name"]
            else:
                proposer_name = "Team"

            status_text = (
                f"**Status:** `AWAITING ACCEPTANCE`\n"
                f"**Proposed Time:** **{match['proposed_time']}**\n"
                f"**Proposed By:** **{proposer_name}** (<@{match.get('proposed_by_user_id')}>)\n\n"
                "> Opposing team: Click **Accept Time** to lock in this schedule, or click **Propose Match Time** to make a counter-offer."
            )
        else:
            status_text = (
                "**Status:** `AWAITING TIME PROPOSAL`\n"
                "> No time has been proposed yet.\n"
                "> Either team captain or player can click **Propose Match Time** below to initiate scheduling."
            )

    embed.add_field(
        name="Schedule & Match Status",
        value=status_text,
        inline=False,
    )

    embed.set_footer(text="Vega Scrims System • Persistent Match Lobby")
    return embed


# =============================================================================
# Scrim Negotiation Modal & Views
# =============================================================================

class ScrimTimeModal(discord.ui.Modal, title="Propose Match Time"):
    time_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Proposed Match Date & Time",
        placeholder="e.g. Today 8:30 PM IST / Tomorrow 9:00 PM UTC",
        min_length=3,
        max_length=100,
        required=True,
    )

    def __init__(self, match_id: int, user_team: dict) -> None:
        super().__init__()
        self.match_id = match_id
        self.user_team = user_team

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        proposed_time = self.time_input.value.strip()
        if not proposed_time:
            await interaction.followup.send("Please provide a valid time string.", ephemeral=True)
            return

        updated = await db.propose_scrim_time(
            match_id=self.match_id,
            proposed_time=proposed_time,
            team_id=self.user_team["id"],
            user_id=interaction.user.id,
        )
        if not updated:
            await interaction.followup.send("Failed to update scrim match. Please try again.", ephemeral=True)
            return

        full_match = await db.get_scrim_match_by_id(self.match_id)
        if not full_match:
            await interaction.followup.send("Error reloading match details.", ephemeral=True)
            return

        # Determine opponent captain
        if full_match["team1_id"] == self.user_team["id"]:
            opp_captain_id = full_match["team2_captain_id"]
        else:
            opp_captain_id = full_match["team1_captain_id"]

        # Re-render channel panel
        team1 = await db.get_team_by_id(full_match["team1_id"])
        team2 = await db.get_team_by_id(full_match["team2_id"])
        t1_members = await db.get_team_members(full_match["team1_id"])
        t2_members = await db.get_team_members(full_match["team2_id"])

        embed = build_scrim_match_embed(full_match, team1, team2, t1_members, t2_members)
        view = ScrimMatchView(match_status="NEGOTIATING")

        panel_msg_id = full_match.get("panel_message_id")
        if panel_msg_id and interaction.channel:
            try:
                panel_msg = await interaction.channel.fetch_message(panel_msg_id)
                await panel_msg.edit(embed=embed, view=view)
            except Exception as e:
                log.warning("Could not edit scrim panel message: %s", e)

        if interaction.channel:
            await interaction.channel.send(
                f"**{self.user_team['team_name']}** proposed a match time: **{proposed_time}**\n"
                f"<@{opp_captain_id}> Click **Accept Time** to confirm or **Propose Match Time** to make a counter-offer."
            )

        await interaction.followup.send(
            f"Successfully proposed match time: **{proposed_time}**.",
            ephemeral=True,
        )


class ScrimMatchView(discord.ui.View):
    """
    Persistent view for scrim timing negotiation inside private match channels.
    Strictly zero emojis.
    """

    def __init__(self, match_status: Optional[str] = None) -> None:
        super().__init__(timeout=None)
        if match_status == "CONFIRMED":
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.custom_id in (
                    "scrim:propose_time",
                    "scrim:accept_time",
                ):
                    child.disabled = True
        elif match_status in ("CANCELLED", "COMPLETED"):
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True

    @staticmethod
    async def _resolve_user_team(user_id: int, match: dict) -> Optional[dict]:
        """Check if user is captain or roster member of Team 1 or Team 2."""
        if user_id == match["team1_captain_id"]:
            return {
                "id": match["team1_id"],
                "team_name": match["team1_name"],
                "team_tag": match["team1_tag"],
                "is_captain": True,
            }
        if user_id == match["team2_captain_id"]:
            return {
                "id": match["team2_id"],
                "team_name": match["team2_name"],
                "team_tag": match["team2_tag"],
                "is_captain": True,
            }

        t1_members = await db.get_team_members(match["team1_id"])
        if any(m["discord_id"] == user_id for m in t1_members):
            return {
                "id": match["team1_id"],
                "team_name": match["team1_name"],
                "team_tag": match["team1_tag"],
                "is_captain": False,
            }

        t2_members = await db.get_team_members(match["team2_id"])
        if any(m["discord_id"] == user_id for m in t2_members):
            return {
                "id": match["team2_id"],
                "team_name": match["team2_name"],
                "team_tag": match["team2_tag"],
                "is_captain": False,
            }

        return None

    @discord.ui.button(
        label="Propose Match Time",
        style=discord.ButtonStyle.primary,
        custom_id="scrim:propose_time",
    )
    async def propose_time_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Action must be performed in a server.", ephemeral=True)
            return

        match = await db.get_scrim_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.response.send_message("No active scrim match found for this channel.", ephemeral=True)
            return

        if match["status"] in ("CANCELLED", "COMPLETED"):
            await interaction.response.send_message(f"This match is already marked as {match['status']}.", ephemeral=True)
            return

        if match["status"] == "CONFIRMED":
            await interaction.response.send_message(
                "This match schedule is already confirmed. Coordinate in chat if changes are needed.",
                ephemeral=True,
            )
            return

        user_team = await self._resolve_user_team(interaction.user.id, match)
        if not user_team:
            await interaction.response.send_message("Only members or captains of the matched teams may propose match time.", ephemeral=True)
            return

        modal = ScrimTimeModal(match_id=match["id"], user_team=user_team)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Accept Time",
        style=discord.ButtonStyle.success,
        custom_id="scrim:accept_time",
    )
    async def accept_time_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        match = await db.get_scrim_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.followup.send("No active scrim match found for this channel.", ephemeral=True)
            return

        if match["status"] == "CONFIRMED":
            await interaction.followup.send("This match schedule has already been confirmed.", ephemeral=True)
            return

        if match["status"] in ("CANCELLED", "COMPLETED"):
            await interaction.followup.send(f"This match is already marked as {match['status']}.", ephemeral=True)
            return

        if not match.get("proposed_time"):
            await interaction.followup.send("No match time has been proposed yet. Click 'Propose Match Time' first.", ephemeral=True)
            return

        user_team = await self._resolve_user_team(interaction.user.id, match)
        if not user_team:
            await interaction.followup.send("Only members or captains of the matched teams may accept a match time.", ephemeral=True)
            return

        # Proposing team cannot accept their own proposal
        if match.get("proposed_by_team_id") and user_team["id"] == match["proposed_by_team_id"]:
            await interaction.followup.send(
                "You cannot accept your own team's proposal. The opposing team must accept it.",
                ephemeral=True,
            )
            return

        updated = await db.accept_scrim_time(match["id"], interaction.user.id)
        if not updated:
            await interaction.followup.send("Failed to confirm match. Please try again.", ephemeral=True)
            return

        full_match = await db.get_scrim_match_by_id(match["id"])
        team1 = await db.get_team_by_id(full_match["team1_id"])
        team2 = await db.get_team_by_id(full_match["team2_id"])
        t1_members = await db.get_team_members(full_match["team1_id"])
        t2_members = await db.get_team_members(full_match["team2_id"])

        embed = build_scrim_match_embed(full_match, team1, team2, t1_members, t2_members)
        view = ScrimMatchView(match_status="CONFIRMED")

        panel_msg_id = full_match.get("panel_message_id")
        if panel_msg_id and interaction.channel:
            try:
                panel_msg = await interaction.channel.fetch_message(panel_msg_id)
                await panel_msg.edit(embed=embed, view=view)
            except Exception as e:
                log.warning("Could not edit scrim panel message: %s", e)

        if interaction.channel:
            await interaction.channel.send(
                f"**SCRIM MATCH SCHEDULE CONFIRMED**\n"
                f"<@{full_match['team1_captain_id']}> • <@{full_match['team2_captain_id']}>\n"
                f"Match time confirmed for: **{full_match['proposed_time']}**\n"
                f"Confirmed by: <@{interaction.user.id}>\n\n"
                f"Captains, please coordinate match lobby details here in this channel prior to match start."
            )

        await interaction.followup.send("Match time successfully confirmed.", ephemeral=True)

    @discord.ui.button(
        label="Cancel Match",
        style=discord.ButtonStyle.danger,
        custom_id="scrim:cancel_match",
    )
    async def cancel_match_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        match = await db.get_scrim_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.followup.send("No active scrim match found for this channel.", ephemeral=True)
            return

        if match["status"] in ("CANCELLED", "COMPLETED"):
            await interaction.followup.send(f"This match is already {match['status'].lower()}.", ephemeral=True)
            return

        user_team = await self._resolve_user_team(interaction.user.id, match)
        is_staff = _is_staff(interaction.user)

        if not user_team and not is_staff:
            await interaction.followup.send("Only captains of the matched teams or staff may cancel this match.", ephemeral=True)
            return

        if user_team and not user_team["is_captain"] and not is_staff:
            await interaction.followup.send("Only team captains or staff may cancel the match.", ephemeral=True)
            return

        await db.cancel_scrim_match(match["id"])
        full_match = await db.get_scrim_match_by_id(match["id"])
        team1 = await db.get_team_by_id(full_match["team1_id"])
        team2 = await db.get_team_by_id(full_match["team2_id"])
        t1_members = await db.get_team_members(full_match["team1_id"])
        t2_members = await db.get_team_members(full_match["team2_id"])

        embed = build_scrim_match_embed(full_match, team1, team2, t1_members, t2_members)
        view = ScrimMatchView(match_status="CANCELLED")

        panel_msg_id = full_match.get("panel_message_id")
        if panel_msg_id and interaction.channel:
            try:
                panel_msg = await interaction.channel.fetch_message(panel_msg_id)
                await panel_msg.edit(embed=embed, view=view)
            except Exception as e:
                log.warning("Could not edit scrim panel message: %s", e)

        if interaction.channel:
            await interaction.channel.send(
                f"**SCRIM MATCH CANCELLED**\n"
                f"This match negotiation was cancelled by <@{interaction.user.id}>."
            )

        await interaction.followup.send("Match negotiation has been cancelled.", ephemeral=True)


# =============================================================================
# Leave Queue & Main Queue Views
# =============================================================================

class LeaveQueueOptionsView(discord.ui.View):
    """
    Ephemeral view shown when a captain whose team is in BOTH queues clicks Leave Queue.
    Allows choosing which queue to leave (or both).
    """

    def __init__(self, cog: TeamQueueCog, team: dict) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.team = team

    @discord.ui.button(
        label="Leave Regional Queue",
        style=discord.ButtonStyle.secondary,
        custom_id="leave_choice:regional",
    )
    async def leave_regional(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await db.remove_team_from_queue(self.team["id"], "REGIONAL")
        await self.cog.refresh_queue_message()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(
            content=f"Your team **{self.team['team_name']}** has left the **Regional Queue**. Your team remains in the **Global Queue**.",
            view=self,
        )

    @discord.ui.button(
        label="Leave Global Queue",
        style=discord.ButtonStyle.secondary,
        custom_id="leave_choice:global",
    )
    async def leave_global(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await db.remove_team_from_queue(self.team["id"], "GLOBAL")
        await self.cog.refresh_queue_message()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(
            content=f"Your team **{self.team['team_name']}** has left the **Global Queue**. Your team remains in the **Regional Queue**.",
            view=self,
        )

    @discord.ui.button(
        label="Leave Both Queues",
        style=discord.ButtonStyle.danger,
        custom_id="leave_choice:both",
    )
    async def leave_both(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await db.remove_team_from_queue(self.team["id"])
        await self.cog.refresh_queue_message()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(
            content=f"Your team **{self.team['team_name']}** has left **Both Queues**.",
            view=self,
        )


class TeamQueueView(discord.ui.View):
    """
    Persistent view for team queue matchmaking actions.
    No emojis on button labels.
    """

    def __init__(self, cog: Optional[TeamQueueCog] = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    def _resolve_cog(self, interaction: discord.Interaction) -> Optional[TeamQueueCog]:
        if self.cog is not None:
            return self.cog
        return interaction.client.get_cog("TeamQueueCog")

    @discord.ui.button(
        label="Join Regional Queue",
        style=discord.ButtonStyle.primary,
        custom_id="team_queue:join_regional",
    )
    async def join_regional(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        cog = self._resolve_cog(interaction)
        if cog:
            await cog.handle_join_queue(interaction, "REGIONAL")
        else:
            await interaction.response.send_message(
                "Queue system is currently unavailable. Please try again shortly.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Join Global Queue",
        style=discord.ButtonStyle.secondary,
        custom_id="team_queue:join_global",
    )
    async def join_global(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        cog = self._resolve_cog(interaction)
        if cog:
            await cog.handle_join_queue(interaction, "GLOBAL")
        else:
            await interaction.response.send_message(
                "Queue system is currently unavailable. Please try again shortly.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Leave Queue",
        style=discord.ButtonStyle.danger,
        custom_id="team_queue:leave",
    )
    async def leave_queue(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        cog = self._resolve_cog(interaction)
        if cog:
            await cog.handle_leave_queue(interaction)
        else:
            await interaction.response.send_message(
                "Queue system is currently unavailable. Please try again shortly.",
                ephemeral=True,
            )


# =============================================================================
# TeamQueueCog
# =============================================================================

class TeamQueueCog(commands.Cog, name="TeamQueue"):
    """Manages the persistent team queue UI, matchmaking, and scrim negotiations."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._queue_message_posted: bool = False
        self._refresh_lock: asyncio.Lock = asyncio.Lock()
        self._matchmaking_lock: asyncio.Lock = asyncio.Lock()
        self._repost_bottom_task: Optional[asyncio.Task] = None

    async def cog_unload(self) -> None:
        if self._repost_bottom_task and not self._repost_bottom_task.done():
            self._repost_bottom_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._queue_message_posted:
            return
        self._queue_message_posted = True
        await self.refresh_queue_message()

    async def _get_channel(self) -> Optional[discord.TextChannel]:
        """Fetch the configured queue text channel."""
        if not TEAM_QUEUE_CHANNEL_ID:
            log.warning("TEAM_QUEUE_CHANNEL_ID is not configured in environment.")
            return None

        channel = self.bot.get_channel(TEAM_QUEUE_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            return channel

        try:
            fetched = await self.bot.fetch_channel(TEAM_QUEUE_CHANNEL_ID)
            if isinstance(fetched, discord.TextChannel):
                return fetched
        except Exception as e:
            log.error("Could not fetch team queue channel %d: %s", TEAM_QUEUE_CHANNEL_ID, e)

        return None

    async def refresh_queue_message(self, repost_at_bottom: bool = False) -> None:
        """
        Fetch latest queue data from database and update or post the persistent queue message.
        If repost_at_bottom is True, deletes previous panel and posts a new one
        at the bottom of the chat under the latest message.
        """
        async with self._refresh_lock:
            channel = await self._get_channel()
            if not channel:
                return

            try:
                regional_teams = await db.get_team_queue(queue_type="REGIONAL")
                global_teams = await db.get_team_queue(queue_type="GLOBAL")
            except Exception as e:
                log.error("Failed to query team queue from database: %s", e)
                return

            embed = build_team_queue_embed(regional_teams, global_teams)
            view = TeamQueueView(self)

            stored_id_str = await db.get_config(TEAM_QUEUE_MESSAGE_CONFIG_KEY)
            if stored_id_str:
                try:
                    stored_id = int(stored_id_str)
                    if not repost_at_bottom:
                        partial = channel.get_partial_message(stored_id)
                        await partial.edit(content=None, embed=embed, view=view, attachments=[])
                        log.info("Refreshed team queue panel message (ID: %d).", stored_id)
                        return
                    else:
                        old_partial = channel.get_partial_message(stored_id)
                        await old_partial.delete()
                except discord.NotFound:
                    log.warning("Stored team queue message %s was deleted. Sending a new message.", stored_id_str)
                except Exception as e:
                    log.error("Error with team queue message %s: %s", stored_id_str, e)

            # Create new persistent message
            try:
                msg = await channel.send(embed=embed, view=view)
                if not repost_at_bottom:
                    try:
                        await msg.pin()
                    except discord.Forbidden:
                        log.warning("Missing Manage Messages permission — could not pin team queue message.")
                await db.set_config(TEAM_QUEUE_MESSAGE_CONFIG_KEY, str(msg.id))
                log.info("Sent and stored new team queue panel message (ID: %d).", msg.id)
            except Exception as e:
                log.error("Failed to post team queue panel message: %s", e)

    def _schedule_repost_panel_at_bottom(self, delay: float = 0.5) -> None:
        """Debounced schedule to repost the team queue panel at the bottom of the channel."""
        if self._repost_bottom_task and not self._repost_bottom_task.done():
            self._repost_bottom_task.cancel()

        async def _runner() -> None:
            try:
                await asyncio.sleep(delay)
                await self.refresh_queue_message(repost_at_bottom=True)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error("Error reposting team queue panel at bottom: %s", e)

        self._repost_bottom_task = asyncio.create_task(_runner())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        When someone messages in the team queue channel, repost the Join/Leave queue UI
        directly under their message so it's always at the bottom of the chat.
        """
        if message.author.bot or (self.bot.user and message.author.id == self.bot.user.id):
            return
        if not message.guild:
            return

        channel = await self._get_channel()
        if not channel or message.channel.id != channel.id:
            return

        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        self._schedule_repost_panel_at_bottom()

    # =========================================================================
    # Matchmaking & Channel Creation
    # =========================================================================

    async def _check_and_create_matches(self, guild: discord.Guild) -> None:
        """
        Evaluate queued teams and automatically create matches and private scrim channels:
        1. Regional Queue: Pairs of 2 teams in the same region.
        2. Global Queue: Pairs of 2 teams in the global queue.
        """
        async with self._matchmaking_lock:
            matched_any = False

            # 1. Regional Matchmaking
            for reg in REGIONS:
                while True:
                    queued_reg = await db.get_team_queue(queue_type="REGIONAL")
                    same_reg = [t for t in queued_reg if t.get("region") == reg]
                    if len(same_reg) < 2:
                        break

                    t1_data = same_reg[0]
                    t2_data = same_reg[1]

                    # Dequeue both teams from all queues
                    await db.remove_team_from_queue(t1_data["team_id"])
                    await db.remove_team_from_queue(t2_data["team_id"])
                    matched_any = True

                    try:
                        await self._create_scrim_channel_and_match(
                            guild=guild,
                            t1_queue_data=t1_data,
                            t2_queue_data=t2_data,
                            match_type="REGIONAL",
                            region=reg,
                        )
                    except Exception as e:
                        log.error("Failed to create scrim match for teams %d and %d: %s", t1_data["team_id"], t2_data["team_id"], e)

            # 2. Global Matchmaking
            while True:
                queued_global = await db.get_team_queue(queue_type="GLOBAL")
                if len(queued_global) < 2:
                    break

                t1_data = queued_global[0]
                t2_data = queued_global[1]

                # Dequeue both teams from all queues
                await db.remove_team_from_queue(t1_data["team_id"])
                await db.remove_team_from_queue(t2_data["team_id"])
                matched_any = True

                try:
                    await self._create_scrim_channel_and_match(
                        guild=guild,
                        t1_queue_data=t1_data,
                        t2_queue_data=t2_data,
                        match_type="GLOBAL",
                        region=t1_data.get("region", "Global"),
                    )
                except Exception as e:
                    log.error("Failed to create global scrim match for teams %d and %d: %s", t1_data["team_id"], t2_data["team_id"], e)

            if matched_any:
                await self.refresh_queue_message()

    async def _create_scrim_channel_and_match(
        self,
        guild: discord.Guild,
        t1_queue_data: dict,
        t2_queue_data: dict,
        match_type: str,
        region: str,
    ) -> None:
        """Create a private text channel for matched teams and post the negotiation UI."""
        team1 = await db.get_team_by_id(t1_queue_data["team_id"])
        team2 = await db.get_team_by_id(t2_queue_data["team_id"])
        if not team1 or not team2:
            log.error("Cannot create scrim match: one or both team records missing.")
            return

        t1_members = await db.get_team_members(team1["id"])
        t2_members = await db.get_team_members(team2["id"])

        # Construct permission overwrites
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

        team_perm = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )

        # Team 1 Captain & Members
        cap1_member = await _get_or_fetch_member(guild, team1["captain_discord_id"])
        if cap1_member:
            overwrites[cap1_member] = team_perm

        for m in t1_members:
            mem = await _get_or_fetch_member(guild, m["discord_id"])
            if mem:
                overwrites[mem] = team_perm

        # Team 2 Captain & Members
        cap2_member = await _get_or_fetch_member(guild, team2["captain_discord_id"])
        if cap2_member:
            overwrites[cap2_member] = team_perm

        for m in t2_members:
            mem = await _get_or_fetch_member(guild, m["discord_id"])
            if mem:
                overwrites[mem] = team_perm

        # Staff Roles
        staff_perm = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
        for role_id in get_staff_role_ids():
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = staff_perm
        for role in guild.roles:
            if _matches_staff_role(role.name) or role.name.strip().lower() in STAFF_ROLE_NAMES:
                overwrites[role] = staff_perm

        # Determine Category
        category: Optional[discord.CategoryChannel] = None
        if SCRIM_CATEGORY_ID:
            cat = guild.get_channel(SCRIM_CATEGORY_ID)
            if isinstance(cat, discord.CategoryChannel):
                category = cat

        if not category and TEAM_QUEUE_CHANNEL_ID:
            q_channel = guild.get_channel(TEAM_QUEUE_CHANNEL_ID)
            if isinstance(q_channel, discord.TextChannel) and q_channel.category:
                category = q_channel.category

        # Clean channel name
        t1_tag_clean = _normalize_tag_str(team1["team_tag"])
        t2_tag_clean = _normalize_tag_str(team2["team_tag"])
        channel_name = f"scrim-{t1_tag_clean}-vs-{t2_tag_clean}"[:95]

        # Create private text channel
        channel = await guild.create_text_channel(
            name=channel_name,
            overwrites=overwrites,
            category=category,
            topic=f"Scrim Match: {team1['team_name']} vs {team2['team_name']} ({match_type})",
        )

        # Insert record into database
        scrim_match = await db.create_scrim_match(
            team1_id=team1["id"],
            team2_id=team2["id"],
            channel_id=channel.id,
            match_type=match_type,
            region=region,
        )

        if not scrim_match:
            log.error("Failed to insert scrim_matches record into database.")
            return

        # Build and send persistent negotiation UI
        embed = build_scrim_match_embed(scrim_match, team1, team2, t1_members, t2_members)
        view = ScrimMatchView(match_status="NEGOTIATING")

        pings = f"<@{team1['captain_discord_id']}> <@{team2['captain_discord_id']}>"
        msg = await channel.send(
            content=f"{pings} Match found! Coordinate scrim match timing below.",
            embed=embed,
            view=view,
        )

        await db.update_scrim_match_panel(scrim_match["id"], msg.id)
        log.info(
            "Created scrim match #%d and channel #%s for teams %s and %s.",
            scrim_match["id"],
            channel.name,
            team1["team_name"],
            team2["team_name"],
        )

    # =========================================================================
    # Queue Join & Leave Handlers
    # =========================================================================

    async def handle_join_queue(self, interaction: discord.Interaction, queue_type: str) -> None:
        """
        Handle a captain requesting to join either REGIONAL or GLOBAL queue.
        Teams can join both queues simultaneously.
        Triggers matchmaking check immediately upon joining.
        """
        await interaction.response.defer(ephemeral=True)

        captain_id = interaction.user.id
        team = await db.get_team_by_captain(captain_id)
        if not team:
            await interaction.followup.send(
                "Only team captains can manage queue status for their team.",
                ephemeral=True,
            )
            return

        # Check if already in this specific queue
        existing = await db.get_queued_team(team["id"], queue_type)
        if existing:
            await interaction.followup.send(
                f"Your team **{team['team_name']}** is already in the **{queue_type.capitalize()} Queue**.",
                ephemeral=True,
            )
            return

        success = await db.add_team_to_queue(
            team_id=team["id"],
            queue_type=queue_type,
            region=team["region"],
            captain_discord_id=captain_id,
        )

        if not success:
            await interaction.followup.send(
                "Failed to join the queue due to a database error. Please try again.",
                ephemeral=True,
            )
            return

        await self.refresh_queue_message()

        other_type = "GLOBAL" if queue_type == "REGIONAL" else "REGIONAL"
        is_also_in_other = await db.get_queued_team(team["id"], other_type)

        if is_also_in_other:
            await interaction.followup.send(
                f"Your team **{team['team_name']}** has joined the **{queue_type.capitalize()} Queue**.\n"
                "Your team is now actively waiting in **Both Regional & Global** queues.",
                ephemeral=True,
            )
        else:
            if queue_type == "REGIONAL":
                await interaction.followup.send(
                    f"Your team **{team['team_name']}** has joined the **Regional ({team['region']}) Queue**.\n"
                    "Tip: You can also join the Global Queue simultaneously by clicking **Join Global Queue**.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"Your team **{team['team_name']}** has joined the **Global Queue**.\n"
                    f"Tip: You can also join your Regional ({team['region']}) Queue simultaneously by clicking **Join Regional Queue**.",
                    ephemeral=True,
                )

        log.info(
            "Team %s (ID: %d, Region: %s) joined %s queue by captain %s (%d)",
            team["team_name"],
            team["id"],
            team["region"],
            queue_type,
            interaction.user.name,
            captain_id,
        )

        # Trigger matchmaking check
        if interaction.guild:
            asyncio.create_task(self._check_and_create_matches(interaction.guild))

    async def handle_leave_queue(self, interaction: discord.Interaction) -> None:
        """
        Handle a captain requesting to leave the queue.
        If in only one queue, leaves directly.
        If in both queues, prompts with options to leave either or both.
        """
        await interaction.response.defer(ephemeral=True)

        captain_id = interaction.user.id
        team = await db.get_team_by_captain(captain_id)
        if not team:
            await interaction.followup.send(
                "Only team captains can manage queue status for their team.",
                ephemeral=True,
            )
            return

        active_queues = await db.get_team_queues(team["id"])
        if not active_queues:
            await interaction.followup.send(
                f"Your team **{team['team_name']}** is not currently in any queue.",
                ephemeral=True,
            )
            return

        if len(active_queues) == 1:
            q_type = active_queues[0]["queue_type"]
            await db.remove_team_from_queue(team["id"], q_type)
            await self.refresh_queue_message()
            await interaction.followup.send(
                f"Your team **{team['team_name']}** has left the **{q_type.capitalize()} Queue**.",
                ephemeral=True,
            )
            log.info(
                "Team %s (ID: %d) left %s queue by captain %s (%d)",
                team["team_name"],
                team["id"],
                q_type,
                interaction.user.name,
                captain_id,
            )
            return

        # Team is in both queues — prompt captain with choices
        options_view = LeaveQueueOptionsView(self, team)
        await interaction.followup.send(
            f"Your team **{team['team_name']}** is currently waiting in **Both Regional & Global** queues.\n"
            "Select an option below:",
            view=options_view,
            ephemeral=True,
        )

    # =========================================================================
    # Admin Commands
    # =========================================================================

    @app_commands.command(
        name="post_team_queue",
        description="Post or refresh the team matchmaking queue panel in the configured channel (Staff only).",
    )
    async def post_team_queue_command(self, interaction: discord.Interaction) -> None:
        """Admin command to manually trigger or refresh the team queue message."""
        await interaction.response.defer(ephemeral=True)

        if not _is_staff(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions to manage the queue panel.", ephemeral=True)
            return

        if not TEAM_QUEUE_CHANNEL_ID:
            await interaction.followup.send(
                "TEAM_QUEUE_CHANNEL_ID is not configured in .env.",
                ephemeral=True,
            )
            return

        await self.refresh_queue_message()
        await interaction.followup.send(
            "Team matchmaking queue panel has been refreshed.",
            ephemeral=True,
        )

    @app_commands.command(
        name="matchmake_teams",
        description="Force check and create matches for currently queued teams (Staff only).",
    )
    async def matchmake_teams_command(self, interaction: discord.Interaction) -> None:
        """Admin command to manually evaluate queues and trigger matchmaking."""
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send("Must be executed in a server.", ephemeral=True)
            return

        if not _is_staff(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send("You do not have staff permissions to run matchmaking.", ephemeral=True)
            return

        await self._check_and_create_matches(interaction.guild)
        await interaction.followup.send("Matchmaking evaluation complete.", ephemeral=True)

    @app_commands.command(
        name="close_scrim",
        description="Close and delete the current scrim match channel (Staff or Captains if cancelled).",
    )
    async def close_scrim_command(self, interaction: discord.Interaction) -> None:
        """Close and delete the scrim channel."""
        await interaction.response.defer(ephemeral=True)

        match = await db.get_scrim_match_by_channel(interaction.channel_id)
        if not match:
            await interaction.followup.send("This channel is not an active scrim match channel.", ephemeral=True)
            return

        is_staff = _is_staff(interaction.user) if isinstance(interaction.user, discord.Member) else False

        is_captain = interaction.user.id in (match["team1_captain_id"], match["team2_captain_id"])

        if not is_staff and not (is_captain and match["status"] in ("CANCELLED", "COMPLETED")):
            await interaction.followup.send("Only staff or captains (after cancellation/completion) may close this channel.", ephemeral=True)
            return

        await interaction.followup.send("Closing scrim channel in 5 seconds...")
        await asyncio.sleep(5)
        try:
            if isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.delete(reason=f"Scrim match #{match['id']} closed by {interaction.user.name}")
        except Exception as e:
            log.error("Failed to delete scrim channel: %s", e)


async def setup(bot: commands.Bot) -> None:
    cog = TeamQueueCog(bot)
    await bot.add_cog(cog)
    bot.add_view(TeamQueueView(cog))
    bot.add_view(ScrimMatchView())
