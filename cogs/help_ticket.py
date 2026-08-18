"""
cogs/help_ticket.py
Private help ticket & in-channel AI support cog — /help command, OpenRouter AI assistant
chatting directly in the ticket channel, and staff escalation.
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

SYSTEM_PROMPT = (
    "You are the official Vega Scrims AI Support Assistant on Discord.\n"
    "Your mission is to help players and team captains with questions about the Vega Scrims bot, "
    "queue system, player registration, team creation, team management, and commands.\n\n"
    "=== VEGA SCRIMS COMPLETE SYSTEM REFERENCE ===\n\n"
    "1. PLAYER & PROFILE COMMANDS:\n"
    "• `/register ign:<ign> region:<region>`: Register player profile (India, APAC, EMEA, Americas). Must be used in the registration channel.\n"
    "• `/unregister`: Unregister profile. Stats/history are preserved. When re-registering, players can resume old profile or start fresh.\n"
    "• `/profile [player:@user]`: View stats, ELO (starts at 1000), K/D/A, matches, and regional ranking.\n"
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
    "• `/invite player:<@user>`: Invite a player as Player, Manager, Coach, or Substitute via DM. Captain/Manager only. 24h expiry.\n"
    "• `/invite_cancel player:<@user>`: Revoke a pending invite before player accepts. Captain/Manager only.\n"
    "• `/invite_cancel_all`: Cancel every active pending invite sent by your team. Captain/Manager only.\n"
    "• `/invites_pending`: List all active, unexpired invites with roles and expiration countdowns. Captain/Manager only.\n"
    "• `/team_set_role player:<@user> role:<Player|Manager|Coach|Substitute>`: Update a roster member's role. Captain/Manager only.\n"
    "• `/kick player:<@user>`: Kick a member from your team. Captain/Manager only. Captain cannot be kicked.\n"
    "• `/leave`: Leave your current team (Players, Managers, Coaches only; Captains must use `/transfer_captain` or `/disband`).\n\n"
    "4. SUPPORT & TICKETS:\n"
    "• `/help [issue]`: Open a private support ticket with AI troubleshooting.\n\n"
    "5. STAFF & ADMIN COMMANDS:\n"
    "• `/admin player_ban user:<@user> [duration_hours:<int>] reason:<text>`: Ban a player from matchmaking and queues (temporary or permanent).\n"
    "• `/admin player_unban user:<@user>`: Unban a player, clear cooldown penalties, and restore queue access.\n"
    "• `/help_admin`: Display the admin commands overview panel.\n\n"
    "GUIDELINES:\n"
    "1. Give direct, actionable, step-by-step guidance.\n"
    "2. Format commands in markdown `code` blocks (e.g. `/team_change_logo`).\n"
    "3. Be friendly, polite, and concise (keep answers under 1500 characters).\n"
    "4. If a problem requires human staff (bans, server bugs, match disputes), tell the user they can click the 'Request Staff' button in the channel."
)


FALLBACK_MODELS = [
    "openai/gpt-4o-mini",
    "google/gemini-flash-1.5",
    "meta-llama/llama-3.3-70b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free",
]


async def _query_openrouter_messages(messages: list[dict]) -> Optional[str]:
    """
    Query OpenRouter chat completion API with a list of messages.
    Tries user-configured model first, then falls back to reliable models if 404 or errors occur.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        log.info("OPENROUTER_API_KEY not configured — skipping AI response.")
        return None

    models_to_try = []
    env_model = os.environ.get("OPENROUTER_MODEL", "").strip()
    if env_model:
        models_to_try.append(env_model)

    for fallback in FALLBACK_MODELS:
        if fallback not in models_to_try:
            models_to_try.append(fallback)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/KartikSakhuja02/Vega-Teams-Queue",
        "X-Title": "Vega Scrims Bot",
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for model in models_to_try:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": 600,
                "temperature": 0.3,
            }
            try:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if choices and "message" in choices[0]:
                            content = choices[0]["message"].get("content", "").strip()
                            if content:
                                return content
                    else:
                        err_text = await resp.text()
                        log.warning(
                            "OpenRouter API with model '%s' returned status %d: %s. Trying next model...",
                            model,
                            resp.status,
                            err_text,
                        )
            except Exception as e:
                log.warning("OpenRouter request failed for model %s: %s", model, e)

    return None


# ---------------------------------------------------------------------------
# Channel & Overwrite Helpers
# ---------------------------------------------------------------------------

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


def _build_ticket_embed(opener: discord.Member, initial_question: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title="🎫 Vega Scrims Support Ticket",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        f"Welcome to your private support channel, {opener.mention}!\n\n"
        "💬 **Ask our AI Assistant anything** by typing in this channel.\n"
        "🙋 If you need human staff, click **Request Staff** below."
    )
    embed.add_field(name="Opened By", value=opener.mention, inline=True)
    embed.add_field(name="Status",    value="🟢 Active (AI Support)", inline=True)
    if initial_question:
        embed.add_field(name="Initial Question", value=initial_question[:1024], inline=False)
    embed.set_footer(text="Vega Queue Support • Close ticket below when finished")
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
    embed.description = f"{user.mention} opened a help ticket in {channel.mention}."
    embed.add_field(name="User",    value=str(user), inline=True)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    if initial_question:
        embed.add_field(name="Question", value=initial_question[:1024], inline=False)
    if role_names:
        embed.add_field(name="Target Roles", value=", ".join(role_names), inline=False)
    embed.set_footer(text="Vega Queue Support")
    return embed


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
# Persistent Ticket View
# ---------------------------------------------------------------------------

class HelpTicketView(discord.ui.View):
    """View inside ticket channels with 'Request Staff' and 'Close Ticket' buttons."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Request Staff",
        style=discord.ButtonStyle.primary,
        emoji="🙋",
        custom_id="help_ticket_request_staff",
    )
    async def request_staff_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This button only works in ticket channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Notify admins in channel
        admin_mentions = []
        if interaction.guild:
            for role_id in HELP_ADMIN_ROLE_IDS:
                role = interaction.guild.get_role(role_id)
                if role:
                    admin_mentions.append(role.mention)

        mention_str = " ".join(admin_mentions) if admin_mentions else "@Staff"
        await channel.send(
            f"🔔 {mention_str} — {interaction.user.mention} has requested human staff assistance for this ticket."
        )

        button.label = "Staff Requested"
        button.disabled = True
        button.style = discord.ButtonStyle.secondary
        with suppress(Exception):
            await interaction.message.edit(view=self)

        await interaction.followup.send("Human staff has been alerted and will assist you shortly.", ephemeral=True)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="help_ticket_close",
    )
    async def close_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await _close_ticket(interaction)


# ---------------------------------------------------------------------------
# HelpTicket Cog
# ---------------------------------------------------------------------------

class HelpTicketCog(commands.Cog, name="HelpTicket"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="help",
        description="Open a private support ticket with AI troubleshooting and staff assistance.",
    )
    @app_commands.describe(
        issue="Optional summary of your question or issue to get started."
    )
    async def help_ticket(
        self,
        interaction: discord.Interaction,
        issue: Optional[str] = None,
    ) -> None:
        """Open a private ticket channel immediately and start AI assistance."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # 1. Check if user already has an active ticket channel
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

        # 2. Create the ticket channel with private overwrites
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
                "I do not have permission to create channels in this server. Please contact an admin.",
                ephemeral=True,
            )
            return

        # 3. Post welcome embed & view in the ticket channel
        embed = _build_ticket_embed(interaction.user, initial_question=issue)
        await ticket_channel.send(embed=embed, view=HelpTicketView())

        # 4. Inform user ephemerally
        await interaction.followup.send(
            f"🎫 Your private support ticket is ready: {ticket_channel.mention}",
            ephemeral=True,
        )

        # 5. Notify staff via DM
        if HELP_ADMIN_ROLE_IDS:
            admin_members = self._get_admin_members(interaction.guild, HELP_ADMIN_ROLE_IDS)
            await asyncio.gather(
                *(
                    self._notify_admin(
                        member,
                        interaction.user,
                        ticket_channel,
                        self._member_role_names(member, HELP_ADMIN_ROLE_IDS),
                        initial_question=issue,
                    )
                    for member in admin_members
                ),
                return_exceptions=True,
            )

        # 6. If user provided an issue right away, post it and generate initial AI answer
        if issue:
            await ticket_channel.send(f"**{interaction.user.mention} asked:**\n> {issue}")
            await self._answer_ticket_with_ai(ticket_channel, interaction.user, issue)

    async def _answer_ticket_with_ai(
        self,
        channel: discord.TextChannel,
        user: discord.abc.User,
        latest_question: str,
    ) -> None:
        """Fetch channel conversation history and reply via OpenRouter AI."""
        async with channel.typing():
            messages_payload = [{"role": "system", "content": SYSTEM_PROMPT}]

            # Fetch recent message history (up to last 6 messages)
            history_msgs = []
            async for h in channel.history(limit=8, oldest_first=True):
                if not h.content and h.embeds:
                    continue
                if h.content.startswith("🔔") or h.content.startswith("🔒"):
                    continue
                role = "assistant" if h.author == self.bot.user else "user"
                history_msgs.append({"role": role, "content": h.clean_content})

            for hm in history_msgs:
                messages_payload.append(hm)

            # Query AI
            ai_reply = await _query_openrouter_messages(messages_payload)

            if ai_reply:
                if len(ai_reply) > 2000:
                    ai_reply = ai_reply[:1990] + "…"
                await channel.send(ai_reply)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Listen for messages inside ticket channels to trigger AI assistance."""
        if message.author.bot or not message.guild:
            return

        if not isinstance(message.channel, discord.TextChannel):
            return

        opener_id = _get_opener_id(message.channel)
        if opener_id is None:
            return

        # Ignore slash commands or bot commands
        if message.content.startswith("/") or message.content.startswith("!"):
            return

        # AI responds to chat in the ticket channel
        await self._answer_ticket_with_ai(message.channel, message.author, message.clean_content)

    def _get_admin_members(self, guild: discord.Guild, role_ids: Iterable[int]) -> list[discord.Member]:
        members: list[discord.Member] = []
        seen_ids: set[int] = set()
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None:
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