"""
cogs/registration.py
Player registration cog — /register command and the persistent info message.
"""

import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db

log = logging.getLogger(__name__)

# Timezone offsets and display names for player regions
REGION_TIMEZONES = {
    "India": (timedelta(hours=5, minutes=30), "IST"),
    "APAC": (timedelta(hours=8), "SGT"),
    "EMEA": (timedelta(hours=1), "CET"),
    "Americas": (timedelta(hours=-5), "EST")
}

def format_regional_time(dt: datetime, region: str) -> str:
    """Format a datetime to the player's local timezone based on their region."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    offset, tz_name = REGION_TIMEZONES.get(region, (timedelta(hours=0), "UTC"))
    local_dt = dt.astimezone(timezone(offset))
    return local_dt.strftime(f"%Y-%m-%d %H:%M {tz_name}")

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
        "using the slash command below. Registration is free and takes under a minute.\n\n"
        "A video example of how to register and use the command is provided below."
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
        Uses the local player_registration.mp4 file if present.
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
        
        # Check if local video file exists, otherwise fall back to URL
        video_filename = "player_registration.mp4"
        local_video_exists = os.path.exists(video_filename)
        video_content: Optional[str] = REGISTRATION_VIDEO_URL or None

        # Check whether we already have a stored message ID in the database.
        stored_id = await db.get_config("registration_message_id")
        if stored_id:
            try:
                existing_msg = await channel.fetch_message(int(stored_id))
                
                # If there's already an attachment or if we don't have a local video, just edit text/embed/content
                if existing_msg.attachments or not local_video_exists:
                    await existing_msg.edit(content=video_content, embed=embed)
                else:
                    # Upload the local video file and update the message
                    try:
                        discord_file = discord.File(video_filename)
                        await existing_msg.edit(content=None, embed=embed, attachments=[discord_file])
                    except discord.HTTPException as e:
                        if e.status == 413:
                            log.warning(
                                "Local video file %s is too large for bot upload (limit is 10MB/25MB). "
                                "Falling back to editing without attachment. Configure REGISTRATION_VIDEO_URL instead.",
                                video_filename
                            )
                            await existing_msg.edit(content=video_content, embed=embed)
                        else:
                            raise
                
                log.info("Registration info message refreshed (ID: %s).", stored_id)
                return
            except discord.NotFound:
                log.warning(
                    "Stored message ID %s was deleted — sending a new one.", stored_id
                )

        # Send a fresh message and pin it.
        msg = None
        if local_video_exists:
            try:
                discord_file = discord.File(video_filename)
                msg = await channel.send(embed=embed, file=discord_file)
            except discord.HTTPException as e:
                if e.status == 413:
                    log.warning(
                        "Local video file %s is too large for bot upload. "
                        "Falling back to sending without attachment.",
                        video_filename
                    )
                    msg = await channel.send(content=video_content, embed=embed)
                else:
                    raise
        else:
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
            registered_at = format_regional_time(existing["registered_at"], existing["region"])
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

        registered_at = format_regional_time(player["registered_at"], player["region"])

        # Ephemeral success reply in the channel.
        await interaction.followup.send(
            "Registration successful.\n\n"
            f"IGN        : {ign}\n"
            f"Region     : {region.value}\n"
            f"Registered : {registered_at}",
            ephemeral=True,
        )

        # Send welcome DM concurrently so the command interaction is finalized instantly.
        asyncio.create_task(self._send_welcome_dm(interaction.user, player))
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
        """Send a welcome DM to the newly registered player using a professional embed design."""
        registered_at = format_regional_time(player["registered_at"], player["region"])

        # Construct a beautiful, professional, emoji-free embed for the DM
        embed = discord.Embed(
            title="Vega Scrims - Registration Confirmed",
            description="Your player profile has been registered successfully.",
            colour=EMBED_COLOUR,
        )

        embed.add_field(
            name="Discord Account",
            value=str(user),
            inline=True,
        )
        embed.add_field(
            name="In-Game Name",
            value=player["ign"],
            inline=True,
        )
        embed.add_field(
            name="Region",
            value=player["region"],
            inline=True,
        )
        embed.add_field(
            name="Registered Time",
            value=registered_at,
            inline=False,
        )

        embed.description += (
            "\n\nYou are now eligible to join a team and participate in scrims. "
            "Please keep an eye on the server channels for team registration guidelines and queue access announcements."
        )

        embed.set_footer(text="Vega Scrims Staff")

        try:
            await user.send(embed=embed)
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
