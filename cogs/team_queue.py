"""
cogs/team_queue.py
------------------
Matchmaking queue cog for teams.
Features:
- Persistent embed sent to the designated Discord channel on startup.
- Live view of teams in Regional and Global queues stored in PostgreSQL.
- Minimalistic design with strictly NO emojis in titles, descriptions, buttons, or footers.
- 3 interactive buttons: Join Regional Queue, Join Global Queue, Leave Queue.
- Restricted to active team captains.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db

log = logging.getLogger(__name__)

TEAM_QUEUE_CHANNEL_ID: int = int(os.environ.get("TEAM_QUEUE_CHANNEL_ID", "0"))
TEAM_QUEUE_MESSAGE_CONFIG_KEY: str = "team_queue_message_id"

REGIONS: list[str] = ["India", "APAC", "EMEA", "Americas"]
EMBED_COLOUR = discord.Colour(0x2B2D31)


def build_team_queue_embed(regional_teams: list[dict], global_teams: list[dict]) -> discord.Embed:
    """
    Construct the minimalistic, zero-emoji team queue embed.
    """
    embed = discord.Embed(
        title="Team Matchmaking Queue",
        description=(
            "Matchmaking queue for active teams.\n"
            "Only team captains can manage queue status for their team."
        ),
        colour=EMBED_COLOUR,
    )

    # Regional Queue Section
    regional_blocks: list[str] = []
    for reg in REGIONS:
        teams_in_reg = [t for t in regional_teams if t.get("region") == reg]
        lines = [f"**{reg}** ({len(teams_in_reg)})"]
        if teams_in_reg:
            for idx, t in enumerate(teams_in_reg, 1):
                tag_str = f" [{t['team_tag']}]" if t.get("team_tag") else ""
                captain_name = t.get("captain_ign") or t.get("captain_username") or "Captain"
                lines.append(f"{idx}. {t['team_name']}{tag_str} - Captain: {captain_name}")
        else:
            lines.append("No teams queued")
        regional_blocks.append("\n".join(lines))

    regional_text = "\n\n".join(regional_blocks)
    if len(regional_text) > 1024:
        regional_text = regional_text[:1020] + "..."

    embed.add_field(
        name="Regional Queue",
        value=regional_text,
        inline=False,
    )

    # Global Queue Section
    if global_teams:
        global_lines: list[str] = []
        for idx, t in enumerate(global_teams, 1):
            tag_str = f" [{t['team_tag']}]" if t.get("team_tag") else ""
            reg_str = f" ({t.get('region')})" if t.get("region") else ""
            captain_name = t.get("captain_ign") or t.get("captain_username") or "Captain"
            global_lines.append(f"{idx}. {t['team_name']}{tag_str}{reg_str} - Captain: {captain_name}")
        global_text = "\n".join(global_lines)
    else:
        global_text = "No teams queued"

    if len(global_text) > 1024:
        global_text = global_text[:1020] + "..."

    embed.add_field(
        name=f"Global Queue ({len(global_teams)})",
        value=global_text,
        inline=False,
    )

    total_queued = len(regional_teams) + len(global_teams)
    embed.set_footer(text=f"Vega Matchmaking | Total Teams Queued: {total_queued}")

    return embed


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


class TeamQueueCog(commands.Cog, name="TeamQueue"):
    """Manages the persistent team queue UI and database operations."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._queue_message_posted: bool = False
        self._refresh_lock: asyncio.Lock = asyncio.Lock()

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

    async def refresh_queue_message(self) -> None:
        """
        Fetch latest queue data from database and update or post the persistent queue message.
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
                    existing_msg = await channel.fetch_message(stored_id)
                    await existing_msg.edit(content=None, embed=embed, view=view, attachments=[])
                    log.info("Refreshed team queue panel message (ID: %d).", stored_id)
                    return
                except discord.NotFound:
                    log.warning("Stored team queue message %s was deleted. Sending a new message.", stored_id_str)
                except Exception as e:
                    log.error("Error editing existing team queue message %s: %s", stored_id_str, e)

            # Create new persistent message
            try:
                msg = await channel.send(embed=embed, view=view)
                try:
                    await msg.pin()
                except discord.Forbidden:
                    log.warning("Missing Manage Messages permission — could not pin team queue message.")
                await db.set_config(TEAM_QUEUE_MESSAGE_CONFIG_KEY, str(msg.id))
                log.info("Sent and stored new team queue panel message (ID: %d).", msg.id)
            except Exception as e:
                log.error("Failed to post team queue panel message: %s", e)

    async def handle_join_queue(self, interaction: discord.Interaction, queue_type: str) -> None:
        """
        Handle a captain requesting to join either REGIONAL or GLOBAL queue.
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

        existing = await db.get_queued_team(team["id"])
        if existing:
            current_type = existing["queue_type"]
            if current_type == queue_type:
                await interaction.followup.send(
                    f"Your team '{team['team_name']}' is already in the {queue_type.capitalize()} Queue.",
                    ephemeral=True,
                )
                return
            else:
                await interaction.followup.send(
                    f"Your team '{team['team_name']}' is currently in the {current_type.capitalize()} Queue. "
                    "Please leave your current queue before joining a different one.",
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

        if queue_type == "REGIONAL":
            await interaction.followup.send(
                f"Your team '{team['team_name']}' has joined the Regional ({team['region']}) Queue.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Your team '{team['team_name']}' has joined the Global Queue.",
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

    async def handle_leave_queue(self, interaction: discord.Interaction) -> None:
        """
        Handle a captain requesting to leave whatever queue their team is currently in.
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

        existing = await db.get_queued_team(team["id"])
        if not existing:
            await interaction.followup.send(
                f"Your team '{team['team_name']}' is not currently in any queue.",
                ephemeral=True,
            )
            return

        await db.remove_team_from_queue(team["id"])
        await self.refresh_queue_message()

        await interaction.followup.send(
            f"Your team '{team['team_name']}' has left the {existing['queue_type'].capitalize()} Queue.",
            ephemeral=True,
        )

        log.info(
            "Team %s (ID: %d) left %s queue by captain %s (%d)",
            team["team_name"],
            team["id"],
            existing["queue_type"],
            interaction.user.name,
            captain_id,
        )

    @app_commands.command(
        name="post_team_queue",
        description="Post or refresh the team matchmaking queue panel in the configured channel.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def post_team_queue_command(self, interaction: discord.Interaction) -> None:
        """Admin command to manually trigger or refresh the team queue message."""
        await interaction.response.defer(ephemeral=True)

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


async def setup(bot: commands.Bot) -> None:
    cog = TeamQueueCog(bot)
    await bot.add_cog(cog)
    bot.add_view(TeamQueueView(cog))
