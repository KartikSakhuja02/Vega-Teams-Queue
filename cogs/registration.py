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
from cogs.bot_logger import send_log, COL_SUCCESS, COL_DANGER

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
        "using one of the two methods below. Registration is free and takes under a minute.\n\n"
        "Two videos are attached below to show both registration flows."
    )
    embed.add_field(
        name="Registration Methods",
        value=(
            "`/register ign:<your_ign> region:<region>`\n"
            "Use the slash command in this channel.\n\n"
            "Open Registration Form button\n"
            "Use the button below to choose your region from a dropdown, then enter your IGN."
        ),
        inline=False,
    )
    embed.add_field(
        name="Videos",
        value=(
            "Both registration videos are attached below this message."
        ),
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


def _build_region_options() -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label="India", value="India", description="India region"),
        discord.SelectOption(label="APAC", value="APAC", description="APAC region"),
        discord.SelectOption(label="EMEA", value="EMEA", description="EMEA region"),
        discord.SelectOption(label="Americas", value="Americas", description="Americas region"),
    ]


class RegistrationModal(discord.ui.Modal, title="Register Player Profile"):
    ign = discord.ui.TextInput(
        label="In-Game Name",
        placeholder="Enter your exact IGN",
        max_length=100,
    )
    def __init__(self, cog: "RegistrationCog", region_value: str) -> None:
        super().__init__()
        self.cog = cog
        self.region_value = region_value

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._register_player(
            interaction=interaction,
            ign=str(self.ign.value).strip(),
            region_value=self.region_value,
        )


class RegistrationRegionSelect(discord.ui.Select):
    def __init__(self, cog: "RegistrationCog") -> None:
        super().__init__(
            placeholder="Choose your region",
            min_values=1,
            max_values=1,
            options=_build_region_options(),
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        region_value = self.values[0]
        await interaction.response.send_modal(RegistrationModal(self.cog, region_value))


class RegistrationRegionView(discord.ui.View):
    def __init__(self, cog: "RegistrationCog") -> None:
        super().__init__(timeout=180)
        self.add_item(RegistrationRegionSelect(cog))


class ResumeOrFreshView(discord.ui.View):
    """
    Shown when an unregistered (inactive) user tries to register again.
    Lets them keep their old profile or start completely fresh.

    new_ign / new_region are optional:
      - Provided   → came from the slash command; "Start fresh" uses them directly.
      - Not provided → came from the button form; "Start fresh" collects new details.
    """

    def __init__(
        self,
        cog: "RegistrationCog",
        existing: dict,
        new_ign: str | None = None,
        new_region: str | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.cog        = cog
        self.existing   = existing
        self.new_ign    = new_ign
        self.new_region = new_region

    async def _finish(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Continue with old profile", style=discord.ButtonStyle.primary, emoji="♻️")
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._finish(interaction)

        player = await db.reactivate_player(
            discord_id=interaction.user.id,
            new_username=str(interaction.user),
        )
        if not player:
            await interaction.followup.send("Something went wrong. Please contact an admin.", ephemeral=True)
            return

        registered_at = format_regional_time(player["registered_at"], player["region"])
        await interaction.followup.send(
            "Welcome back! Your old profile has been restored.\n\n"
            f"IGN        : {player['ign']}\n"
            f"Region     : {player['region']}\n"
            f"Registered : {registered_at}",
            ephemeral=True,
        )
        asyncio.create_task(self.cog._send_welcome_dm(interaction.user, player))
        await send_log(
            interaction.client,
            title="Player Re-registered (Resumed)",
            description=f"{interaction.user.mention} restored their old profile.",
            colour=COL_SUCCESS,
            fields=[
                ("User",   f"{interaction.user} ({interaction.user.id})", True),
                ("IGN",    player["ign"],                                  True),
                ("Region", player["region"],                               True),
            ],
        )

    @discord.ui.button(label="Start fresh", style=discord.ButtonStyle.danger, emoji="🆕")
    async def fresh_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.new_ign and self.new_region:
            # Details already supplied (slash command path) — reset immediately.
            await interaction.response.defer(ephemeral=True)
            await self._finish(interaction)

            player = await db.reset_and_reactivate_player(
                discord_id=interaction.user.id,
                new_username=str(interaction.user),
                new_ign=self.new_ign,
                new_region=self.new_region,
            )
            if not player:
                await interaction.followup.send("Something went wrong. Please contact an admin.", ephemeral=True)
                return

            registered_at = format_regional_time(player["registered_at"], player["region"])
            await interaction.followup.send(
                "Fresh profile created! All previous stats have been reset.\n\n"
                f"IGN        : {player['ign']}\n"
                f"Region     : {player['region']}\n"
                f"Registered : {registered_at}",
                ephemeral=True,
            )
            asyncio.create_task(self.cog._send_welcome_dm(interaction.user, player))
            await send_log(
                interaction.client,
                title="Player Re-registered (Fresh Start)",
                description=f"{interaction.user.mention} started a fresh profile (stats wiped).",
                colour=COL_SUCCESS,
                fields=[
                    ("User",   f"{interaction.user} ({interaction.user.id})", True),
                    ("IGN",    player["ign"],                                  True),
                    ("Region", player["region"],                               True),
                ],
            )
        else:
            # No details yet (button path) — collect them via region select + modal.
            await self._finish(interaction)
            await interaction.response.send_message(
                "Select your new region to continue.",
                view=FreshStartRegionView(self.cog),
                ephemeral=True,
            )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Fresh-start region select + modal  (used when "Start fresh" is clicked from
# the button form — no details pre-filled).
# ---------------------------------------------------------------------------

class FreshStartModal(discord.ui.Modal, title="Start Fresh — New Profile"):
    ign = discord.ui.TextInput(
        label="New In-Game Name",
        placeholder="Enter your exact IGN",
        max_length=100,
    )

    def __init__(self, cog: "RegistrationCog", region_value: str) -> None:
        super().__init__()
        self.cog          = cog
        self.region_value = region_value

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        player = await db.reset_and_reactivate_player(
            discord_id=interaction.user.id,
            new_username=str(interaction.user),
            new_ign=str(self.ign.value).strip(),
            new_region=self.region_value,
        )
        if not player:
            await interaction.followup.send(
                "Something went wrong during reset. Please contact an admin.",
                ephemeral=True,
            )
            return

        registered_at = format_regional_time(player["registered_at"], player["region"])
        await interaction.followup.send(
            "Fresh profile created! All previous stats have been reset.\n\n"
            f"IGN        : {player['ign']}\n"
            f"Region     : {player['region']}\n"
            f"Registered : {registered_at}",
            ephemeral=True,
        )
        asyncio.create_task(self.cog._send_welcome_dm(interaction.user, player))
        await send_log(
            interaction.client,
            title="Player Re-registered (Fresh Start)",
            description=f"{interaction.user.mention} started a fresh profile via button form (stats wiped).",
            colour=COL_SUCCESS,
            fields=[
                ("User",   f"{interaction.user} ({interaction.user.id})", True),
                ("IGN",    player["ign"],                                  True),
                ("Region", player["region"],                               True),
            ],
        )


class FreshStartRegionSelect(discord.ui.Select):
    def __init__(self, cog: "RegistrationCog") -> None:
        super().__init__(
            placeholder="Choose your new region",
            min_values=1,
            max_values=1,
            options=_build_region_options(),
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        region_value = self.values[0]
        await interaction.response.send_modal(FreshStartModal(self.cog, region_value))


class FreshStartRegionView(discord.ui.View):
    def __init__(self, cog: "RegistrationCog") -> None:
        super().__init__(timeout=180)
        self.add_item(FreshStartRegionSelect(cog))


class UnregisterConfirmView(discord.ui.View):
    """Asks the user to confirm before unregistering."""

    def __init__(self) -> None:
        super().__init__(timeout=60)

    async def _disable(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Yes, unregister me", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)

        result = await db.deactivate_player(interaction.user.id)
        if not result:
            await interaction.followup.send(
                "Could not unregister you — you may not be registered. Contact an admin if this is wrong.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "You have been unregistered from Vega Scrims.\n"
            "Your stats and history are preserved. "
            "If you register again you will be asked whether to restore your old profile or start fresh.",
            ephemeral=True,
        )
        await send_log(
            interaction.client,
            title="Player Unregistered",
            description=f"{interaction.user.mention} unregistered from Vega Scrims.",
            colour=COL_DANGER,
            fields=[
                ("User", f"{interaction.user} ({interaction.user.id})", True),
                ("IGN",  result["ign"],                                  True),
            ],
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)
        await interaction.followup.send("Cancelled. You are still registered.", ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


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

        # ── Check DB BEFORE collecting any details ─────────────────────────
        existing = await db.get_player(interaction.user.id)

        if existing and existing["is_active"]:
            registered_at = format_regional_time(existing["registered_at"], existing["region"])
            await interaction.response.send_message(
                f"You are already registered!\n\n"
                f"IGN        : {existing['ign']}\n"
                f"Region     : {existing['region']}\n"
                f"Registered : {registered_at}\n\n"
                "Use `/edit-profile` to update your IGN or region.",
                ephemeral=True,
            )
            return

        if existing and not existing["is_active"]:
            # Previously unregistered — ask resume or fresh start NOW
            embed = discord.Embed(
                title="Previous Profile Found",
                description=(
                    "You have previously unregistered from Vega Scrims, "
                    "but your old profile is still on record.\n\n"
                    "What would you like to do?"
                ),
                colour=EMBED_COLOUR,
            )
            embed.add_field(name="Old IGN",    value=existing["ign"],    inline=True)
            embed.add_field(name="Old Region", value=existing["region"], inline=True)
            embed.add_field(
                name="♻️ Continue with old profile",
                value="Restore your previous IGN, region, and all stats.",
                inline=False,
            )
            embed.add_field(
                name="🆕 Start fresh",
                value="Wipe all stats and register a brand-new profile.",
                inline=False,
            )
            # new_ign / new_region are None — "Start fresh" will collect them
            view = ResumeOrFreshView(cog=self.cog, existing=existing)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        # New user — normal registration flow
        await interaction.response.send_message(
            "Select your region to continue with registration.",
            view=RegistrationRegionView(self.cog),
            ephemeral=True,
        )


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

        discord_id       = interaction.user.id
        discord_username = str(interaction.user)

        # ── Check for an existing (possibly inactive) account ────────────
        existing = await db.get_player(discord_id)

        if existing and not existing["is_active"]:
            # User previously unregistered — ask resume or fresh start
            embed = discord.Embed(
                title="Previous Profile Found",
                description=(
                    "You have previously unregistered from Vega Scrims, "
                    "but your old profile is still on record.\n\n"
                    "What would you like to do?"
                ),
                colour=EMBED_COLOUR,
            )
            embed.add_field(name="Old IGN",    value=existing["ign"],    inline=True)
            embed.add_field(name="Old Region", value=existing["region"], inline=True)
            embed.add_field(
                name="♻️ Continue with old profile",
                value="Restore your previous IGN, region, and all stats.",
                inline=False,
            )
            embed.add_field(
                name="🆕 Start fresh",
                value=f"Wipe all stats and register as **{ign}** in **{region_value}**.",
                inline=False,
            )
            view = ResumeOrFreshView(
                cog=self,
                existing=existing,
                new_ign=ign,
                new_region=region_value,
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            return

        # ── Normal registration ──────────────────────────────────────────
        # Insert the player.
        player = await db.register_player(
            discord_id=discord_id,
            discord_username=discord_username,
            ign=ign,
            region=region_value,
        )

        if player is None:
            # UniqueViolation — already active.
            if existing and existing["is_active"]:
                registered_at = format_regional_time(existing["registered_at"], existing["region"])
                await interaction.followup.send(
                    "You are already registered.\n\n"
                    f"IGN        : {existing['ign']}\n"
                    f"Region     : {existing['region']}\n"
                    f"Registered : {registered_at}\n\n"
                    "Use `/edit-profile` to update your IGN or region.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                "Registration could not be completed. Please contact an admin.",
                ephemeral=True,
            )
            return

        registered_at = format_regional_time(player["registered_at"], player["region"])

        # Ephemeral success reply.
        await interaction.followup.send(
            "Registration successful.\n\n"
            f"IGN        : {ign}\n"
            f"Region     : {player['region']}\n"
            f"Registered : {registered_at}",
            ephemeral=True,
        )

        asyncio.create_task(self._send_welcome_dm(interaction.user, player))
        await send_log(
            interaction.client,
            title="Player Registered",
            description=f"{interaction.user.mention} registered for Vega Scrims.",
            colour=COL_SUCCESS,
            fields=[
                ("User",   f"{interaction.user} ({interaction.user.id})", True),
                ("IGN",    ign,                                            True),
                ("Region", player["region"],                               True),
            ],
        )
        log.info(
            "Player registered — discord_id=%d  ign=%s  region=%s",
            discord_id, ign, player["region"],
        )

    # ------------------------------------------------------------------
    # /unregister command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="unregister",
        description="Unregister your Vega Scrims profile. Your stats are preserved and can be restored later.",
    )
    async def unregister(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Soft-delete the calling user's player profile."""
        await interaction.response.defer(ephemeral=True)

        player = await db.get_player(interaction.user.id)
        if not player or not player["is_active"]:
            await interaction.followup.send(
                "You are not currently registered in Vega Scrims.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Unregister from Vega Scrims",
            description=(
                "Are you sure you want to unregister?\n\n"
                "Your stats and history **will be preserved**. "
                "If you register again you will be able to restore your old profile or start fresh."
            ),
            colour=discord.Colour.red(),
        )
        embed.add_field(name="IGN",    value=player["ign"],    inline=True)
        embed.add_field(name="Region", value=player["region"], inline=True)
        embed.set_footer(text="This action can be reversed by registering again.")

        view = UnregisterConfirmView()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

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
    cog = RegistrationCog(bot)
    await bot.add_cog(cog)
    bot.add_view(RegistrationView(cog))
