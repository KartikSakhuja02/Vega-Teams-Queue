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
HELP_ADMIN_ROLE_IDS_RAW = os.environ.get("HELP_ADMIN_ROLE_IDS", "")


def _parse_admin_role_ids(raw_value: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw_value.split(","):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        try:
            ids.append(int(cleaned))
        except ValueError:
            log.warning("Ignoring invalid HELP_ADMIN_ROLE_IDS value: %s", cleaned)
    return ids


HELP_ADMIN_ROLE_IDS = _parse_admin_role_ids(HELP_ADMIN_ROLE_IDS_RAW)


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


def _build_admin_dm_embed(user: discord.abc.User, channel: discord.TextChannel, role_names: list[str]) -> discord.Embed:
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
    if role_names:
        embed.add_field(name="Target Roles", value=", ".join(role_names), inline=False)
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
    admin_role_ids: Iterable[int],
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

    for admin_role_id in admin_role_ids:
        role = guild.get_role(admin_role_id)
        if role is None:
            continue
        target = role
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

    allowed = interaction.user.id == opener_id or any(
        role.id in HELP_ADMIN_ROLE_IDS for role in getattr(interaction.user, "roles", [])
    )
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
            HELP_ADMIN_ROLE_IDS,
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

        if not HELP_ADMIN_ROLE_IDS:
            log.warning(
                "HELP_ADMIN_ROLE_IDS is empty — no direct admin DMs will be sent for ticket %s.",
                ticket_channel.id,
            )

        if HELP_ADMIN_ROLE_IDS:
            admin_members = self._get_admin_members(interaction.guild, HELP_ADMIN_ROLE_IDS)
            await asyncio.gather(
                *(
                    self._notify_admin(
                        member,
                        interaction.user,
                        ticket_channel,
                        self._member_role_names(member, HELP_ADMIN_ROLE_IDS),
                    )
                    for member in admin_members
                ),
                return_exceptions=True,
            )

        log.info(
            "Help ticket created — channel_id=%d opener_id=%d admins_notified=%d",
            ticket_channel.id,
            interaction.user.id,
            len(self._get_admin_members(interaction.guild, HELP_ADMIN_ROLE_IDS)),
        )

    def _get_admin_members(self, guild: discord.Guild, role_ids: Iterable[int]) -> list[discord.Member]:
        members: list[discord.Member] = []
        seen_ids: set[int] = set()
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None:
                log.warning("Configured admin role %d could not be found in guild %s.", role_id, guild.id)
                continue
            for member in role.members:
                if member.id in seen_ids:
                    continue
                seen_ids.add(member.id)
                members.append(member)
        return members

    def _member_role_names(
        self,
        member: discord.Member,
        role_ids: Iterable[int],
    ) -> list[str]:
        role_id_set = set(role_ids)
        return [role.name for role in member.roles if role.id in role_id_set]

    async def _notify_admin(
        self,
        admin_member: discord.Member,
        user: discord.User,
        channel: discord.TextChannel,
        role_names: list[str],
    ) -> None:
        try:
            await admin_member.send(embed=_build_admin_dm_embed(user, channel, role_names))
        except discord.Forbidden:
            log.warning("Could not DM configured admin member %s.", admin_member)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(HelpTicketView())
    await bot.add_cog(HelpTicketCog(bot))