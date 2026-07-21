"""
cogs/profile.py
Player profile cog — /profile command to fetch player stats and rankings.
"""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db

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
            # Handle user not found in the database
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

        # Calculate K/D ratio
        kills = profile_data["kills"]
        deaths = profile_data["deaths"]
        assists = profile_data["assists"]
        matches_played = profile_data["matches_played"]
        
        kd_ratio = kills / deaths if deaths > 0 else float(kills)

        # Build professional, emoji-free profile embed
        embed = discord.Embed(
            title="Vega Scrims — Player Profile",
            colour=EMBED_COLOUR,
        )
        
        embed.add_field(
            name="Player",
            value=f"{profile_data['ign']} ({target_user.mention})",
            inline=True,
        )
        embed.add_field(
            name="Region",
            value=profile_data["region"],
            inline=True,
        )
        embed.add_field(
            name="Matches Played",
            value=str(matches_played),
            inline=True,
        )
        
        embed.add_field(
            name="ELO rating",
            value=str(profile_data["elo"]),
            inline=True,
        )
        embed.add_field(
            name="Regional Rank",
            value=f"#{profile_data['regional_rank']}",
            inline=True,
        )
        embed.add_field(
            name="K/D Ratio",
            value=f"{kd_ratio:.2f}",
            inline=True,
        )
        
        embed.add_field(
            name="Overall K/D/A",
            value=f"Kills: {kills} | Deaths: {deaths} | Assists: {assists}",
            inline=False,
        )

        embed.set_footer(text="Vega Scrims Statistics")

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProfileCog(bot))
