"""
cogs/registration.py
Player registration cog — /register command and the persistent info message.
"""

import os
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment config
# ---------------------------------------------------------------------------

REGISTRATION_CHANNEL_ID: int = int(os.environ.get("REGISTRATION_CHANNEL_ID", "0"))
REGISTRATION_VIDEO_URL: str  = os.environ.get("REGISTRATION_VIDEO_URL", "").strip()

# Deep indigo — consistent brand colour, no harsh primaries.
EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")


# ---------------------------------------------------------------------------
# Persistent message builder
# ---------------------------------------------------------------------------

def _build_info_embed() -> discord.Embed:
    """Build the always-on registration guide embed."""
    embed = discord.Embed(
        title="Vega Scrims Queue — Player Registration",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        "To participate in Vega Scrims you must first register a player profile "
        "using the slash command below. Registration is free and takes under a minute."
    )
    embed.add_field(
        name="Command",
        value="`/register ign:<your_ign> region:<region>`",
        inline=False,
    )
    embed.add_field(
        name="Parameters",
        value=(
            "**ign** — Your exact in-game name (including tag, e.g. `PlayerName#TAG`).\n"
            "**region** — Select one of: `India` / `APAC` / `EMEA` / `Americas`."
        ),
        inline=False,
    )
    embed.add_field(
        name="Notes",
        value=(
            "- You may only register once per Discord account.\n"
            "- Your Discord account is automatically linked to your player profile.\n"
            "- To update your IGN or region after registration, contact an admin.\n"
            "- Registration is a prerequisite for joining a team and entering the queue."
        ),
        inline=False,
    )
    embed.set_footer(text="Vega Scrims — Do not delete this message.")
    return embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class RegistrationCog(commands.Cog, name="Registration"):
    """Handles player registration for the Vega Scrims Queue."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Guard against on_ready firing multiple times (e.g. on reconnect).
        self._info_message_posted: bool = False

    # ------------------------------------------------------------------
    # Lifecycle — post/refresh the persistent info card
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._info_message_posted:
            return
        self._info_message_posted = True
        await self._ensure_info_message()

    async def _ensure_info_message(self) -> None:
        """
        Post the registration info card to the configured channel, or edit
        the existing one if we already sent it in a previous session.
        """
        if not REGISTRATION_CHANNEL_ID:
            log.warning(
                "REGISTRATION_CHANNEL_ID is not configured — skipping info message."
            )
            return

        channel = self.bot.get_channel(REGISTRATION_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            log.error(
                "Channel %d not found or is not a TextChannel.", REGISTRATION_CHANNEL_ID
            )
            return

        embed = _build_info_embed()
        # If a video URL is set, include it as message content so Discord auto-previews it.
        video_content: Optional[str] = REGISTRATION_VIDEO_URL or None

        # Check whether we already have a stored message ID in the database.
        stored_id = await db.get_config("registration_message_id")
        if stored_id:
            try:
                existing_msg = await channel.fetch_message(int(stored_id))
                await existing_msg.edit(content=video_content, embed=embed)
                log.info("Registration info message refreshed (ID: %s).", stored_id)
                return
            except discord.NotFound:
                log.warning(
                    "Stored message ID %s was deleted — sending a new one.", stored_id
                )

        # Send a fresh message and pin it.
        msg = await channel.send(content=video_content, embed=embed)
        try:
            await msg.pin()
        except discord.Forbidden:
            log.warning("Missing Manage Messages permission — could not pin info message.")

        await db.set_config("registration_message_id", str(msg.id))
        log.info("Registration info message sent and pinned (ID: %d).", msg.id)

    # ------------------------------------------------------------------
    # /register command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="register",
        description="Register your player profile for Vega Scrims.",
    )
    @app_commands.describe(
        ign="Your in-game name exactly as it appears (e.g. PlayerName#TAG).",
        region="The region you compete in.",
    )
    @app_commands.choices(region=[
        app_commands.Choice(name="India",    value="India"),
        app_commands.Choice(name="APAC",     value="APAC"),
        app_commands.Choice(name="EMEA",     value="EMEA"),
        app_commands.Choice(name="Americas", value="Americas"),
    ])
    async def register(
        self,
        interaction: discord.Interaction,
        ign: str,
        region: app_commands.Choice[str],
    ) -> None:
        """Register a Discord user as a Vega Scrims player."""

        # Enforce channel restriction.
        if REGISTRATION_CHANNEL_ID and interaction.channel_id != REGISTRATION_CHANNEL_ID:
            await interaction.response.send_message(
                "This command can only be used in the designated registration channel.",
                ephemeral=True,
            )
            return

        # Defer so we have time for DB operations.
        await interaction.response.defer(ephemeral=True)

        discord_id = interaction.user.id
        discord_username = str(interaction.user)

        # Check if already registered.
        existing = await db.get_player(discord_id)
        if existing:
            registered_at = existing["registered_at"].strftime("%Y-%m-%d %H:%M UTC")
            await interaction.followup.send(
                "You are already registered.\n\n"
                f"IGN        : {existing['ign']}\n"
                f"Region     : {existing['region']}\n"
                f"Registered : {registered_at}\n\n"
                "Contact an admin if you need to update your details.",
                ephemeral=True,
            )
            return

        # Insert the player.
        player = await db.register_player(
            discord_id=discord_id,
            discord_username=discord_username,
            ign=ign,
            region=region.value,
        )

        if player is None:
            # UniqueViolation — edge case (race condition or stale cache).
            await interaction.followup.send(
                "Registration could not be completed. "
                "You may already be registered. Please contact an admin.",
                ephemeral=True,
            )
            return

        registered_at = player["registered_at"].strftime("%Y-%m-%d %H:%M UTC")

        # Ephemeral success reply in the channel.
        await interaction.followup.send(
            "Registration successful.\n\n"
            f"IGN        : {ign}\n"
            f"Region     : {region.value}\n"
            f"Registered : {registered_at}",
            ephemeral=True,
        )

        # Send a welcome DM.
        await self._send_welcome_dm(interaction.user, player)
        log.info(
            "Player registered — discord_id=%d  ign=%s  region=%s",
            discord_id,
            ign,
            region.value,
        )

    # ------------------------------------------------------------------
    # DM helper
    # ------------------------------------------------------------------

    async def _send_welcome_dm(
        self,
        user: discord.User,
        player: dict,
    ) -> None:
        """Send a welcome DM to the newly registered player."""
        registered_at = player["registered_at"].strftime("%Y-%m-%d %H:%M UTC")
        separator = "-" * 44

        dm_lines = [
            "Welcome to Vega Scrims Queue.",
            "",
            "Your player profile has been registered successfully.",
            "",
            "Player Details",
            separator,
            f"Discord    : {user}",
            f"IGN        : {player['ign']}",
            f"Region     : {player['region']}",
            f"Registered : {registered_at}",
            "",
            "You are now eligible to join a team and participate in scrims.",
            "Keep an eye on the server for team recruitment announcements.",
            "",
            "- Vega Scrims Staff",
        ]

        try:
            await user.send("\n".join(dm_lines))
        except discord.Forbidden:
            # User has DMs disabled — not a fatal error.
            log.warning(
                "Could not send welcome DM to %s (DMs are disabled).", user
            )


# ---------------------------------------------------------------------------
# Cog setup entry point
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RegistrationCog(bot))
