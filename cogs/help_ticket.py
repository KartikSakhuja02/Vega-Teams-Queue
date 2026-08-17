"""
cogs/help_ticket.py
Private help ticket & AI support cog — /help command, OpenRouter AI troubleshooting,
and escalation to private staff ticket channels.
"""

import asyncio
import logging
import os
import re
from contextlib import suppress
from typing import Iterable, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.bot_logger import send_log, COL_SUCCESS, COL_DANGER

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


# ---------------------------------------------------------------------------
# OpenRouter AI Assistant
# ---------------------------------------------------------------------------

async def _ask_openrouter_ai(user_question: str, user_name: str) -> Optional[str]:
    """
    Query OpenRouter chat completion API with the bot's system context.
    Returns the AI response string, or None if credits/key are missing or request fails.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        log.info("OPENROUTER_API_KEY not configured — skipping AI support.")
        return None

    model = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.0-flash-001").strip() or "google/gemini-2.0-flash-001"

    system_prompt = (
        "You are the Vega Scrims AI Assistant on Discord. Your mission is to provide fast, direct, and accurate "
        "guidance on using the Vega Scrims bot, queue system, player registration, team creation, and roster management.\n\n"
        "=== VEGA SCRIMS SYSTEM REFERENCE ===\n\n"
        "1. PLAYER COMMANDS:\n"
        "• `/register ign:<ign> region:<region>`: Register player profile (India, APAC, EMEA, Americas). Must be used in registration channel.\n"
        "• `/unregister`: Unregister profile. Stats/history are preserved. Re-registering lets you resume old profile or start fresh.\n"
        "• `/profile [player:@user]`: View player stats, ELO (starts at 1000), K/D/A, matches, and regional ranking.\n"
        "• `/edit-profile`: Interactive form to edit registered IGN or Region with confirmation buttons.\n"
        "• `/player_status [player:@user]`: View current system status (IDLE, IN_QUEUE, IN_MATCH, PENALTY_COOLDOWN).\n"
        "• `/player_change_region`: Change your personal matchmaking region via dropdown.\n"
        "• `/toggle_dms`: Toggle queue pop alerts and match check-in DMs on/off.\n\n"
        "2. TEAM CREATION & CUSTOMIZATION:\n"
        "• `/create_team`: Open a private setup thread in the team panel channel to set team name, tag (2-6 alphanumeric), and upload logo.\n"
        "• `/disband`: Disband your team (Captain/Manager). Stats are preserved and can be resumed or started fresh later.\n"
        "• `/team-profile [player:@user]`: View team profile, tag, region, logo, and active roster.\n"
        "• `/team_rename new_name:<name>`: Rename the team (2-50 chars). Enforces unique name across DB. Captain/Manager only.\n"
        "• `/team_change_logo`: Upload a new team logo (PNG/JPG/GIF/WEBP) via a private 1-on-1 thread. Captain/Manager only.\n"
        "• `/change_team_tag new_tag:<tag>`: Change team tag (2-6 chars). Captain only.\n"
        "• `/team_change_region`: Change entire team's region via dropdown and updates all member regions too. Captain only.\n"
        "• `/transfer_captain new_captain:<@user>`: Transfer ownership and captain permissions to an active team member. Captain only.\n\n"
        "3. TEAM ROSTER & INVITES:\n"
        "• ROSTER SLOTS: Exactly 5 Players (1 Captain + 4 Players), 2 Substitutes, 1 Coach, 2 Managers (Max 10 members total).\n"
        "• `/invite player:<@user>`: Invite a player as Player, Manager, Coach, or Substitute via DM. Captain/Manager only.\n"
        "• `/invite_cancel player:<@user>`: Revoke a pending invite before player accepts. Captain/Manager only.\n"
        "• `/invite_cancel_all`: Cancel every active pending invite sent by your team. Captain/Manager only.\n"
        "• `/invites_pending`: List all active, unexpired invites with roles and expiration countdowns. Captain/Manager only.\n"
        "• `/team_set_role player:<@user> role:<Player|Manager|Coach|Substitute>`: Update a roster member's role. Captain/Manager only.\n"
        "• `/kick player:<@user>`: Kick a member from your team. Captain/Manager only. Captain cannot be kicked.\n"
        "• `/leave`: Leave your current team (Players, Managers, Coaches only; Captains must use `/transfer_captain` or `/disband`).\n\n"
        "4. SUPPORT:\n"
        "• `/help [issue]`: Ask AI assistant for help or open a private staff ticket.\n\n"
        "GUIDELINES:\n"
        "1. Give direct, actionable steps. Highlight commands in markdown `code` format (e.g. `/team_change_logo`).\n"
        "2. If the user wants human staff help or their issue is beyond bot commands (e.g. disputes, server bugs, manual overrides), remind them to use the 'Talk to Staff' button.\n"
        "3. Keep responses concise, clear, and under 1500 characters."
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/KartikSakhuja02/Vega-Teams-Queue",
        "X-Title": "Vega Scrims Bot",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Player {user_name} asks:\n{user_question}"},
        ],
        "max_tokens": 600,
        "temperature": 0.3,
    }

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if choices and "message" in choices[0]:
                        return choices[0]["message"].get("content", "").strip()
                else:
                    err_text = await resp.text()
                    log.warning("OpenRouter API returned error %d: %s", resp.status, err_text)
                    return None
    except Exception as exc:
        log.error("Failed to query OpenRouter AI: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Embed Builders & Helpers
# ---------------------------------------------------------------------------

def _build_ticket_embed(interaction: discord.Interaction, initial_question: Optional[str] = None) -> discord.Embed:
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
    if initial_question:
        embed.add_field(
            name="Initial Issue / Query",
            value=initial_question[:1024],
            inline=False,
        )
    embed.add_field(
        name="Close",
        value="When your issue is resolved, click the button below to close this ticket.",
        inline=False,
    )
    embed.set_footer(text="Vega Queue Support")
    return embed


def _build_admin_dm_embed(
    user: discord.abc.User,
    channel: discord.TextChannel,
    role_names: list[str],
    initial_question: Optional[str] = None,
) -> discord.Embed:
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
    if initial_question:
        embed.add_field(name="User Query", value=initial_question[:1024], inline=False)
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
        overwrites[role] = discord.PermissionOverwrite(
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
        allowed = (
            allowed
            or interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_channels
        )

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


# ---------------------------------------------------------------------------
# Views & Modals
# ---------------------------------------------------------------------------

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


class _AIHelpResponseView(discord.ui.View):
    """View attached to an AI help answer giving option to escalate to human staff."""

    def __init__(self, cog: "HelpTicketCog", user_question: str) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_question = user_question

    @discord.ui.button(label="🙋 Talk to Staff", style=discord.ButtonStyle.primary, emoji="🎫")
    async def talk_to_staff(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]
        with suppress(Exception):
            await interaction.message.edit(view=self)

        await self.cog.create_ticket_channel(interaction, initial_question=self.user_question)

    @discord.ui.button(label="✅ Resolved", style=discord.ButtonStyle.success, emoji="👍")
    async def resolved(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]
        with suppress(Exception):
            await interaction.message.edit(view=self)
        await interaction.followup.send(
            "Glad we could help! Feel free to use `/help` anytime if you need more assistance.",
            ephemeral=True,
        )


class HelpQuestionModal(discord.ui.Modal, title="Vega Scrims Support"):
    question_input = discord.ui.TextInput(
        label="What do you need help with?",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. How do I invite substitutes to my team? / How do I change my IGN?",
        required=True,
        max_length=500,
    )

    def __init__(self, cog: "HelpTicketCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.cog.handle_help_request(interaction, self.question_input.value.strip())


# ---------------------------------------------------------------------------
# Cog Implementation
# ---------------------------------------------------------------------------

class HelpTicketCog(commands.Cog, name="HelpTicket"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="help",
        description="Ask AI assistant for instant help or open a private staff ticket.",
    )
    @app_commands.describe(
        issue="Describe what you need help with (leave empty to open form)."
    )
    async def help_ticket(
        self,
        interaction: discord.Interaction,
        issue: Optional[str] = None,
    ) -> None:
        """Ask AI for instant troubleshooting or open a private support ticket."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if issue:
            await interaction.response.defer(ephemeral=True)
            await self.handle_help_request(interaction, issue.strip())
        else:
            await interaction.response.send_modal(HelpQuestionModal(self))

    async def handle_help_request(self, interaction: discord.Interaction, user_question: str) -> None:
        """Try to answer via OpenRouter AI first; fallback to staff ticket if unavailable."""
        ai_response = await _ask_openrouter_ai(user_question, str(interaction.user))

        if ai_response:
            embed = discord.Embed(
                title="🤖 Vega Scrims Support Assistant",
                description=ai_response,
                colour=EMBED_COLOUR,
            )
            embed.add_field(
                name="Your Question",
                value=user_question[:500],
                inline=False,
            )
            embed.set_footer(text="Need human help? Click 'Talk to Staff' below to open a private ticket.")

            view = _AIHelpResponseView(self, user_question)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            # Fallback directly to opening staff ticket
            await interaction.followup.send(
                "Connecting you with human staff team...",
                ephemeral=True,
            )
            await self.create_ticket_channel(interaction, initial_question=user_question)

    async def create_ticket_channel(
        self,
        interaction: discord.Interaction,
        initial_question: Optional[str] = None,
    ) -> None:
        """Create the private text channel for human staff ticket support."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return

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
        try:
            ticket_channel = await interaction.guild.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                topic=f"help-ticket:{interaction.user.id}",
                reason=f"Help ticket opened by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have permission to create ticket channels in this server.",
                ephemeral=True,
            )
            return

        embed = _build_ticket_embed(interaction, initial_question=initial_question)
        await ticket_channel.send(embed=embed, view=HelpTicketView())

        await interaction.followup.send(
            f"🎫 Your private support ticket with staff is ready: {ticket_channel.mention}",
            ephemeral=True,
        )

        # Notify admin members via DM
        if HELP_ADMIN_ROLE_IDS:
            admin_members = self._get_admin_members(interaction.guild, HELP_ADMIN_ROLE_IDS)
            await asyncio.gather(
                *(
                    self._notify_admin(
                        member,
                        interaction.user,
                        ticket_channel,
                        self._member_role_names(member, HELP_ADMIN_ROLE_IDS),
                        initial_question=initial_question,
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
        initial_question: Optional[str] = None,
    ) -> None:
        try:
            await admin_member.send(
                embed=_build_admin_dm_embed(
                    user,
                    channel,
                    role_names,
                    initial_question=initial_question,
                )
            )
        except discord.Forbidden:
            log.warning("Could not DM configured admin member %s.", admin_member)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(HelpTicketView())
    await bot.add_cog(HelpTicketCog(bot))