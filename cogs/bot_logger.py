"""
cogs/bot_logger.py
------------------
Centralised action logger for the Vega Queue Bot.

Logs are posted in real-time to the Discord channel set by LOG_CHANNEL_ID
in the .env file.

Two logging layers:
  1. Automatic  — on_app_command_completion fires after EVERY slash command.
  2. Manual     — other cogs call `await bot_logger.send_log(bot, ...)` for
                  events that happen outside slash commands (DM events, etc.).

Embed colour legend:
  🟣 Purple  — general command use
  🟢 Green   — success / joins / accepts
  🔴 Red     — destructive actions (kick, disband, leave, decline)
  🟡 Yellow  — warnings / errors
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

LOG_CHANNEL_ID: int = int(os.environ.get("LOG_CHANNEL_ID", "0"))

# Colour palette
COL_DEFAULT  = discord.Colour.from_str("#5B4FCF")   # purple  — general
COL_SUCCESS  = discord.Colour.from_str("#2ECC71")   # green   — good events
COL_DANGER   = discord.Colour.from_str("#E74C3C")   # red     — destructive
COL_WARNING  = discord.Colour.from_str("#F39C12")   # yellow  — warnings


def _ts() -> str:
    """Return a short UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _colour_for_command(name: str) -> discord.Colour:
    """Pick a colour based on the command name."""
    if name in ("disband", "kick", "leave"):
        return COL_DANGER
    if name in ("invite", "register", "create_team"):
        return COL_SUCCESS
    return COL_DEFAULT


# ---------------------------------------------------------------------------
# Public helper — import and call from any cog
# ---------------------------------------------------------------------------

async def send_log(
    bot: commands.Bot,
    *,
    title: str,
    description: str,
    colour: discord.Colour = COL_DEFAULT,
    fields: list[tuple[str, str, bool]] | None = None,
) -> None:
    """
    Send a log embed to the log channel.

    Parameters
    ----------
    bot         : The running bot instance.
    title       : Bold heading of the embed.
    description : Main body text.
    colour      : Embed left-bar colour.
    fields      : Optional list of (name, value, inline) tuples.
    """
    if not LOG_CHANNEL_ID:
        return

    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            log.warning("Could not fetch log channel %d", LOG_CHANNEL_ID)
            return

    embed = discord.Embed(
        title=title,
        description=description,
        colour=colour,
        timestamp=datetime.now(timezone.utc),
    )
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)

    embed.set_footer(text="Vega Scrims — Action Log")

    try:
        await channel.send(embed=embed)
    except Exception as exc:
        log.warning("Failed to post log embed: %s", exc)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class BotLoggerCog(commands.Cog, name="BotLogger"):
    """Posts all slash-command events to the configured log channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Automatic: fires after every successful slash command ────────────────

    @commands.Cog.listener()
    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> None:
        """Auto-log every completed slash command."""
        if not LOG_CHANNEL_ID:
            return

        user = interaction.user
        channel = interaction.channel
        guild  = interaction.guild

        channel_str = f"<#{channel.id}>" if channel else "DM"
        guild_str   = guild.name if guild else "—"

        # Build a short options summary
        options_str = "—"
        if interaction.data:
            opts = interaction.data.get("options", [])
            if opts:
                parts = []
                for opt in opts:
                    parts.append(f"`{opt.get('name')}: {opt.get('value', '')}`")
                options_str = "  ".join(parts)

        colour = _colour_for_command(command.name)

        await send_log(
            self.bot,
            title=f"/{command.name}",
            description=f"{user.mention} used `/{command.name}` in {channel_str}",
            colour=colour,
            fields=[
                ("User",    f"{user} ({user.id})",  True),
                ("Server",  guild_str,               True),
                ("Options", options_str,             False),
            ],
        )

    # ── Error listener: logs failed commands ─────────────────────────────────

    @commands.Cog.listener()
    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Log command errors to the log channel."""
        if not LOG_CHANNEL_ID:
            return

        user    = interaction.user
        command = interaction.command
        cmd_str = f"/{command.name}" if command else "unknown"

        await send_log(
            self.bot,
            title=f"Command Error — {cmd_str}",
            description=f"{user.mention} triggered an error in `{cmd_str}`",
            colour=COL_WARNING,
            fields=[
                ("User",  f"{user} ({user.id})", True),
                ("Error", str(error)[:1000],      False),
            ],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BotLoggerCog(bot))
