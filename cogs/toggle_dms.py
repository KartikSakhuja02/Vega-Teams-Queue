"""
cogs/toggle_dms.py
------------------
/toggle_dms command.

Lets any registered player enable or disable bot DMs for:
  - Queue pop alerts
  - Match check-in pings
  - Any other bot-initiated DM that respects this flag

The preference is stored as `dms_enabled` (BOOLEAN) on the players row.
Other cogs can read db.get_player(discord_id)["dms_enabled"] before sending DMs.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from cogs.bot_logger import send_log, COL_SUCCESS

log = logging.getLogger(__name__)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")


class ToggleDMsCog(commands.Cog, name="ToggleDMs"):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="toggle_dms",
        description="Enable or disable bot DMs for queue pop alerts and match check-in pings.",
    )
    async def toggle_dms(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # Must be registered
        record = await db.get_player(interaction.user.id)
        if not record or not record["is_active"]:
            await interaction.followup.send(
                "You are not registered. Use `/register` first.",
                ephemeral=True,
            )
            return

        current: bool = bool(record.get("dms_enabled", True))

        # Flip the flag
        updated = await db.toggle_player_dms(interaction.user.id)
        if not updated:
            await interaction.followup.send(
                "Something went wrong. Please try again or contact an admin.",
                ephemeral=True,
            )
            return

        new_state: bool = bool(updated["dms_enabled"])

        if new_state:
            emoji, label, colour = "🔔", "Enabled", discord.Colour.green()
            detail = (
                "You will now receive bot DMs for:\n"
                "• Queue pop alerts\n"
                "• Match check-in pings\n"
                "• Other important notifications"
            )
        else:
            emoji, label, colour = "🔕", "Disabled", discord.Colour.red()
            detail = (
                "You will **no longer** receive bot DMs.\n\n"
                "⚠️ You may miss queue pop alerts and match check-in pings.\n"
                "Use `/toggle_dms` again to re-enable them."
            )

        embed = discord.Embed(
            title=f"{emoji} Bot DMs {label}",
            description=detail,
            colour=colour,
        )
        embed.set_footer(text="Use /toggle_dms again to switch back.")

        await interaction.followup.send(embed=embed, ephemeral=True)

        await send_log(
            interaction.client,
            title="DM Preference Changed",
            description=(
                f"{interaction.user.mention} turned bot DMs **{label.lower()}** "
                f"(was {'enabled' if current else 'disabled'})."
            ),
            colour=COL_SUCCESS,
            fields=[
                ("User",      f"{interaction.user} ({interaction.user.id})", True),
                ("New State", label,                                          True),
            ],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ToggleDMsCog(bot))
