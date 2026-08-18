"""
cogs/player_status.py
---------------------
/player_status command.

Displays the current system state of the calling player:
  IDLE              — registered and not doing anything
  IN_QUEUE          — waiting in a match queue
  IN_MATCH          — currently in an active match
  PENALTY_COOLDOWN  — serving a cooldown penalty (shows time remaining)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from database import db

log = logging.getLogger(__name__)

# ── Status metadata ─────────────────────────────────────────────────────────

_STATUS_META: dict[str, dict] = {
    "IDLE": {
        "label":       "Idle",
        "emoji":       "⚪",
        "colour":      discord.Colour.from_str("#7289DA"),
        "description": "You are not currently in a queue or match.",
    },
    "IN_QUEUE": {
        "label":       "In Queue",
        "emoji":       "🟡",
        "colour":      discord.Colour.from_str("#FAA61A"),
        "description": "You are waiting in a match queue.",
    },
    "IN_MATCH": {
        "label":       "In Match",
        "emoji":       "🟢",
        "colour":      discord.Colour.from_str("#43B581"),
        "description": "You are currently in an active match.",
    },
    "PENALTY_COOLDOWN": {
        "label":       "Penalty Cooldown",
        "emoji":       "🔴",
        "colour":      discord.Colour.from_str("#F04747"),
        "description": "You are serving a penalty cooldown and cannot queue.",
    },
}


def _fmt_duration(seconds: float) -> str:
    """Convert a number of seconds into a human-readable string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"


def _fmt_ts(dt: datetime | None) -> str:
    """Format a datetime as a Discord relative timestamp, or 'N/A'."""
    if dt is None:
        return "N/A"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix = int(dt.timestamp())
    return f"<t:{unix}:R>"  # Discord relative: "3 minutes ago"


# ── Cog ─────────────────────────────────────────────────────────────────────

class PlayerStatusCog(commands.Cog, name="PlayerStatus"):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="player_status",
        description="Check a player's current system state. Leave @user blank to check your own.",
    )
    @app_commands.describe(player="The player to look up (leave blank to check yourself).")
    async def player_status(
        self,
        interaction: discord.Interaction,
        player: discord.User | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # Resolve target — defaults to the caller
        target = player or interaction.user
        is_self = target.id == interaction.user.id

        record = await db.get_player(target.id)
        if not record or not record["is_active"]:
            msg = (
                "You are not registered. Use `/register` to create a profile."
                if is_self else
                f"{target.mention} is not registered in Vega Scrims."
            )
            await interaction.followup.send(msg, ephemeral=True)
            return

        # Check ban status
        is_banned, ban_reason, banned_until, _ = await db.get_player_ban_status(target.id)
        if is_banned:
            title_suffix = "" if is_self else f" — {target.display_name}"
            embed = discord.Embed(
                title=f"🚫 Banned{title_suffix}",
                description="This player is currently banned from matchmaking and queues.",
                colour=discord.Colour.from_str("#E74C3C"),
            )
            embed.add_field(name="IGN",    value=record["ign"],    inline=True)
            embed.add_field(name="Region", value=record["region"], inline=True)
            embed.add_field(name="Ban Reason", value=ban_reason or "No reason provided.", inline=False)
            if banned_until:
                if banned_until.tzinfo is None:
                    banned_until = banned_until.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                rem = (banned_until - now).total_seconds()
                embed.add_field(
                    name="Ban Expires",
                    value=f"{_fmt_ts(banned_until)} (`{_fmt_duration(max(0, rem))}` remaining)",
                    inline=True,
                )
            else:
                embed.add_field(name="Ban Duration", value="`Permanent`", inline=True)
            embed.set_footer(text="Vega Scrims Ban Enforcement")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        status_key: str = record.get("status", "IDLE") or "IDLE"
        meta = _STATUS_META.get(status_key, _STATUS_META["IDLE"])

        title_suffix = "" if is_self else f" — {target.display_name}"
        embed = discord.Embed(
            title=f"{meta['emoji']} {meta['label']}{title_suffix}",
            description=meta["description"],
            colour=meta["colour"],
        )

        embed.add_field(name="IGN",    value=record["ign"],    inline=True)
        embed.add_field(name="Region", value=record["region"], inline=True)
        embed.add_field(name="\u200b", value="\u200b",         inline=True)  # spacer


        # Status-since timestamp
        embed.add_field(
            name="Status Since",
            value=_fmt_ts(record.get("status_since")),
            inline=True,
        )

        # Penalty-specific block
        if status_key == "PENALTY_COOLDOWN":
            penalty_end: datetime | None = record.get("penalty_ends_at")
            if penalty_end:
                if penalty_end.tzinfo is None:
                    penalty_end = penalty_end.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                remaining = (penalty_end - now).total_seconds()
                if remaining > 0:
                    embed.add_field(
                        name="Cooldown Ends",
                        value=f"{_fmt_ts(penalty_end)}  (`{_fmt_duration(remaining)}` remaining)",
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name="Cooldown Ends",
                        value="Penalty has expired — status will reset shortly.",
                        inline=False,
                    )
            else:
                embed.add_field(
                    name="Cooldown Ends",
                    value="Unknown (contact an admin).",
                    inline=False,
                )

        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"Discord ID: {target.id}")


        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayerStatusCog(bot))
