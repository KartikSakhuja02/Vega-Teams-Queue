"""
cogs/profile.py
Player profile cog — /profile command to fetch player stats and rankings.
"""

import logging
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from utils.generate_profile import generate_profile_card

log = logging.getLogger(__name__)

# Deep indigo — consistent brand colour
EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")


class ProfileCog(commands.Cog, name="Profile"):
    """Handles player profile retrieval and statistics visualization."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="profile",
        description="View player profile, ELO, stats, and regional ranking.",
    )
    @app_commands.describe(
        player="The player whose profile you want to view (defaults to yourself)."
    )
    async def profile(
        self,
        interaction: discord.Interaction,
        player: Optional[discord.User] = None,
    ) -> None:
        """View a player's profile and scrim statistics. Ephemeral response."""
        # Always reply ephemerally to keep the commands channel clean
        await interaction.response.defer(ephemeral=True)

        target_user = player or interaction.user
        profile_data = await db.get_player_profile(target_user.id)

        if not profile_data:
            if target_user.id == interaction.user.id:
                await interaction.followup.send(
                    "You are not registered in the database. "
                    "Use the `/register` command first in the registration channel.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"The user {target_user.display_name} is not registered in the database.",
                    ephemeral=True,
                )
            return

        # Determine avatar URL (1024px PNG for quality)
        avatar_url = (
            target_user.display_avatar.replace(format="png", size=512).url
            if target_user.display_avatar
            else None
        )

        try:
            card_buf = await generate_profile_card(profile_data, avatar_url=avatar_url)
            await interaction.followup.send(
                file=discord.File(card_buf, filename="profile.png"),
                ephemeral=True,
            )
        except Exception:
            log.exception("Failed to generate profile card for %s", target_user.id)
            await interaction.followup.send(
                "An error occurred while generating the profile card. Please try again later.",
                ephemeral=True,
            )


    @app_commands.command(
        name="team-profile",
        description="View the profile and roster of a team.",
    )
    @app_commands.describe(
        player="A player whose team you want to view (defaults to your own team)."
    )
    async def team_profile(
        self,
        interaction: discord.Interaction,
        player: Optional[discord.User] = None,
    ) -> None:
        """View a team's profile and members. Ephemeral response."""
        await interaction.response.defer(ephemeral=True)

        target_user = player or interaction.user
        
        # Determine the user's team
        team = await db.get_team_by_captain(target_user.id)
        team_id = team["id"] if team else None
        
        if not team_id:
            membership = await db.get_player_team_membership(target_user.id)
            if membership:
                team_id = membership["team_id"]
                
        if not team_id:
            if target_user.id == interaction.user.id:
                await interaction.followup.send("You are not currently in an active team.")
            else:
                await interaction.followup.send(f"{target_user.display_name} is not currently in an active team.")
            return

        full_team = await db.get_team_by_id(team_id)
        if not full_team or not full_team.get("is_active"):
            await interaction.followup.send("This team is no longer active.")
            return

        members = await db.get_team_members(team_id)
        
        players = [f"<@{m['discord_id']}> : {m['ign']}" for m in members if m['role'] == 'Player']
        managers = [f"<@{m['discord_id']}> : {m['ign']}" for m in members if m['role'] == 'Manager']
        coaches = [f"<@{m['discord_id']}> : {m['ign']}" for m in members if m['role'] == 'Coach']

        embed = discord.Embed(
            title="Vega Scrims — Team Profile",
            colour=EMBED_COLOUR,
        )
        
        embed.add_field(name="Team", value=full_team['team_name'], inline=True)
        embed.add_field(name="Tag", value=full_team['team_tag'], inline=True)
        embed.add_field(name="Region", value=full_team['region'], inline=True)
        
        embed.add_field(name="Captain", value=f"<@{full_team['captain_discord_id']}> : {full_team['captain_ign']}", inline=False)
        
        if managers:
            embed.add_field(name="Managers", value="\n".join(managers), inline=True)
        if coaches:
            embed.add_field(name="Coaches", value="\n".join(coaches), inline=True)
        if players:
            embed.add_field(name="Players", value="\n".join(players), inline=False)

        embed.set_footer(text="Vega Scrims Teams")

        # Attach logo if available
        logo_path = full_team.get("team_logo_path")
        if logo_path:
            import os
            if os.path.exists(logo_path):
                file = discord.File(logo_path, filename="logo.png")
                embed.set_thumbnail(url="attachment://logo.png")
                await interaction.followup.send(embed=embed, file=file, ephemeral=True)
                return

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProfileCog(bot))
