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
        "A video example of how to register and use the command is provided in this channel."
    )
    embed.add_field(
        name="Command",
        value="`/register ign:<your_ign> region:<region>`",
        inline=False,
    )
    embed.add_field(
        name="Parameters",
        value=(
            "**ign** — Your exact in-game name (eg. DarkWiz.Zr`).\n"
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


def _build_region_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name="India", value="India"),
        app_commands.Choice(name="APAC", value="APAC"),
        app_commands.Choice(name="EMEA", value="EMEA"),
        app_commands.Choice(name="Americas", value="Americas"),
    ]


class RegistrationModal(discord.ui.Modal, title="Register Player Profile"):
    ign = discord.ui.TextInput(
        label="In-Game Name",
        placeholder="Enter your exact IGN",
        max_length=100,
    )
    region = discord.ui.TextInput(
        label="Region",
        placeholder="India, APAC, EMEA, or Americas",
        max_length=20,
    )

    def __init__(self, cog: "RegistrationCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._register_player(
            interaction=interaction,
            ign=str(self.ign.value).strip(),
            region_value=str(self.region.value).strip(),
            source="modal",
        )


class RegistrationView(discord.ui.View):
    def __init__(self, cog: "RegistrationCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Open Registration Form",
        style=discord.ButtonStyle.primary,
        custom_id="registration_open_form",
    )
    async def open_form_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if REGISTRATION_CHANNEL_ID and interaction.channel_id != REGISTRATION_CHANNEL_ID:
            await interaction.response.send_message(
                "This form is only available in the registration channel.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(RegistrationModal(self.cog))


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

        # Check whether we already have a stored message ID in the database.
        stored_id = await db.get_config("registration_message_id")
        if stored_id:
            try:
                existing_msg = await channel.fetch_message(int(stored_id))
                
                # Edit and refresh embed. Pass content=None and attachments=[] to clear any old links/files
                await existing_msg.edit(content=None, embed=embed, attachments=[])
                log.info("Registration info message refreshed (ID: %s).", stored_id)
                return
            except discord.NotFound:
                log.warning(
                    "Stored message ID %s was deleted — sending a new one.", stored_id
                )

        # Send a fresh message and pin it.
        msg = await channel.send(embed=embed, view=RegistrationView(self))

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
        await self._register_player(
            interaction=interaction,
            ign=ign,
            region_value=region.value,
        )

    async def _register_player(
        self,
        interaction: discord.Interaction,
        ign: str,
        region_value: str,
    ) -> None:
        """Shared registration flow for the slash command and modal."""

        # Enforce channel restriction.
        if REGISTRATION_CHANNEL_ID and interaction.channel_id != REGISTRATION_CHANNEL_ID:
            await interaction.response.send_message(
                "This command can only be used in the designated registration channel.",
                ephemeral=True,
            )
            return

        # Defer so we have time for DB operations.
        if not interaction.response.is_done():
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
            region=region_value,
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
            f"Region     : {player['region']}\n"
            f"Registered : {registered_at}",
            ephemeral=True,
        )

        # Send welcome DM concurrently so the command interaction is finalized instantly.
        asyncio.create_task(self._send_welcome_dm(interaction.user, player))
        log.info(
            "Player registered — discord_id=%d  ign=%s  region=%s",
            discord_id,
            ign,
            player["region"],
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
