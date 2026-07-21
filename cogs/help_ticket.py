"""
cogs/help_ticket.py
Private help ticket cog — /help command, ticket channel creation, and close button handling.
"""

import asyncio
import logging
import os
import re
from contextlib import suppress
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")
HELP_ADMIN_USER_IDS_RAW = os.environ.get("HELP_ADMIN_USER_IDS", "")


def _parse_admin_user_ids(raw_value: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw_value.split(","):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        try:
            ids.append(int(cleaned))
        except ValueError:
            log.warning("Ignoring invalid HELP_ADMIN_USER_IDS value: %s", cleaned)
    return ids


HELP_ADMIN_USER_IDS = _parse_admin_user_ids(HELP_ADMIN_USER_IDS_RAW)


def _build_ticket_embed(interaction: discord.Interaction) -> discord.Embed:
    embed = discord.Embed(
        title="Private Help Ticket",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        "You have opened a private help ticket with the admin team. Use this channel to "
        "describe your issue clearly so the team can assist you."
    )
    embed.add_field(
        name="Opened By",
        value=interaction.user.mention,
        inline=True,
    )
    embed.add_field(
        name="Status",
        value="Open",
        inline=True,
    )
    embed.add_field(
        name="Close",
        value="If this ticket was opened by mistake, use the button below to close it.",
        inline=False,
    )
    embed.set_footer(text="Vega Queue Support")
    return embed


def _build_admin_dm_embed(user: discord.abc.User, channel: discord.TextChannel) -> discord.Embed:
    embed = discord.Embed(
        title="Private Help Ticket Opened",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        "A user has opened a private ticket and needs assistance. Please move the discussion "
        "to the private channel below."
    )
    embed.add_field(name="User", value=str(user), inline=True)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.set_footer(text="Vega Queue Support")
    return embed


def _normalise_channel_name(base_name: str) -> str:
    cleaned = base_name.lower().strip()
    cleaned = re.sub(r"[^a-z0-9-]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "user"


def _build_channel_name(user: discord.User) -> str:
    display_name = getattr(user, "display_name", user.name)
    return f"help-{_normalise_channel_name(display_name)}"


def _is_ticket_channel(channel: discord.TextChannel, opener_id: int) -> bool:
    return channel.topic == f"help-ticket:{opener_id}"


def _get_opener_id(channel: discord.TextChannel) -> int | None:
    if not channel.topic:
        return None
    prefix = "help-ticket:"
    if not channel.topic.startswith(prefix):
        return None
    try:
        return int(channel.topic.removeprefix(prefix))
    except ValueError:
        return None


def _build_channel_overwrites(
    guild: discord.Guild,
    opener: discord.Member,
    admin_ids: Iterable[int],
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }

    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
        )

    for admin_id in admin_ids:
        member = guild.get_member(admin_id)
        target = member if member is not None else discord.Object(id=admin_id)
        overwrites[target] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )

    return overwrites


async def _close_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "This button only works inside a help ticket channel.",
            ephemeral=True,
        )
        return

    opener_id = _get_opener_id(channel)
    if opener_id is None:
        await interaction.response.send_message(
            "This channel is not a recognised help ticket.",
            ephemeral=True,
        )
        return

    allowed = interaction.user.id == opener_id or interaction.user.id in HELP_ADMIN_USER_IDS
    if isinstance(interaction.user, discord.Member):
        allowed = allowed or interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_channels

    if not allowed:
        await interaction.response.send_message(
            "You do not have permission to close this ticket.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Ticket closed. This channel will be removed shortly.",
        ephemeral=True,
    )

    with suppress(discord.Forbidden):
        await channel.send("This ticket is now closed.")

    await asyncio.sleep(2)
    try:
        await channel.delete(reason=f"Help ticket closed by {interaction.user}")
    except discord.Forbidden:
        log.warning("Missing permission to delete ticket channel %s.", channel.id)


class HelpTicketView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="help_ticket_close",
    )
    async def close_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await _close_ticket(interaction)


class HelpTicketCog(commands.Cog, name="HelpTicket"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="help",
        description="Open a private help ticket for the admin team.",
    )
    async def help_ticket(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        existing_channel = next(
            (
                channel
                for channel in interaction.guild.text_channels
                if _is_ticket_channel(channel, interaction.user.id)
            ),
            None,
        )
        if existing_channel is not None:
            await interaction.followup.send(
                f"You already have an open ticket: {existing_channel.mention}",
                ephemeral=True,
            )
            return

        overwrites = _build_channel_overwrites(
            interaction.guild,
            interaction.user,
            HELP_ADMIN_USER_IDS,
        )

        channel_name = _build_channel_name(interaction.user)
        ticket_channel = await interaction.guild.create_text_channel(
            name=channel_name,
            overwrites=overwrites,
            topic=f"help-ticket:{interaction.user.id}",
            reason=f"Help ticket opened by {interaction.user}",
        )

        embed = _build_ticket_embed(interaction)
        await ticket_channel.send(embed=embed, view=HelpTicketView())

        await interaction.followup.send(
            f"Your private help ticket is ready: {ticket_channel.mention}",
            ephemeral=True,
        )

        if not HELP_ADMIN_USER_IDS:
            log.warning(
                "HELP_ADMIN_USER_IDS is empty — no direct admin DMs will be sent for ticket %s.",
                ticket_channel.id,
            )

        if HELP_ADMIN_USER_IDS:
            await asyncio.gather(
                *(
                    self._notify_admin(admin_id, interaction.user, ticket_channel)
                    for admin_id in HELP_ADMIN_USER_IDS
                ),
                return_exceptions=True,
            )

        log.info(
            "Help ticket created — channel_id=%d opener_id=%d admins_notified=%d",
            ticket_channel.id,
            interaction.user.id,
            len(HELP_ADMIN_USER_IDS),
        )

    async def _notify_admin(
        self,
        admin_id: int,
        user: discord.User,
        channel: discord.TextChannel,
    ) -> None:
        try:
            admin_user = await self.bot.fetch_user(admin_id)
        except discord.NotFound:
            log.warning("Configured admin user %d could not be found.", admin_id)
            return

        try:
            await admin_user.send(embed=_build_admin_dm_embed(user, channel))
        except discord.Forbidden:
            log.warning("Could not DM configured admin %s.", admin_user)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(HelpTicketView())
    await bot.add_cog(HelpTicketCog(bot))