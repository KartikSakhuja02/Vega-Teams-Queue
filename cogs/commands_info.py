"""
cogs/commands_info.py
Commands information cog — posts and updates the persistent commands list embed with category pagination.
"""

import os
import logging

import discord
from discord.ext import commands

from database import db

log = logging.getLogger(__name__)

COMMANDS_CHANNEL_ID: int = int(os.environ.get("COMMANDS_CHANNEL_ID", "0"))
EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")


def _build_page_1_embed() -> discord.Embed:
    """Page 1: Player & Profile Commands."""
    embed = discord.Embed(
        title="Vega Scrims — Player Commands (Page 1/3)",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        "Commands for player registration, personal profiles, queue state, and direct support.\n"
        "All interactions respond ephemerally to keep server channels clean."
    )
    embed.add_field(
        name="Player Registration",
        value=(
            "`/register ign:<ign> region:<region>`\n"
            "Register your player profile. Used in the registration channel.\n\n"
            "`/unregister`\n"
            "Unregister your profile. Stats are preserved — you can resume or start fresh on re-register."
        ),
        inline=False,
    )
    embed.add_field(
        name="Player Profile & Preferences",
        value=(
            "`/profile [player:@user]`\n"
            "View player stats, ELO, K/D/A, matches, and regional ranking.\n\n"
            "`/edit-profile`\n"
            "Edit your registered profile details (IGN or Region) with confirmation.\n\n"
            "`/player_status [player:@user]`\n"
            "Check current system state (IDLE, IN_QUEUE, IN_MATCH, PENALTY_COOLDOWN).\n\n"
            "`/player_change_region`\n"
            "Change your own regional matchmaking zone via dropdown.\n\n"
            "`/toggle_dms`\n"
            "Enable or disable bot DMs for queue pop alerts and match check-in pings."
        ),
        inline=False,
    )
    embed.add_field(
        name="Support & Help",
        value=(
            "`/help [issue:<text>]`\n"
            "Ask our AI Assistant for instant troubleshooting or open a private staff ticket."
        ),
        inline=False,
    )

    embed.set_footer(text="Vega Scrims — Page 1 of 3 • Use buttons below to switch categories")
    return embed


def _build_page_2_embed() -> discord.Embed:
    """Page 2: Team Creation & Customization."""
    embed = discord.Embed(
        title="Vega Scrims — Team & Setup Commands (Page 2/3)",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        "Commands for creating, customizing, and managing your team setup.\n"
        "Captain and Manager permissions are enforced where applicable."
    )
    embed.add_field(
        name="Team Creation & Disband",
        value=(
            "`/create_team`\n"
            "Open a private team setup thread and submit your team details.\n\n"
            "`/disband`\n"
            "Disband your team. Data is preserved — you can resume or start fresh later."
        ),
        inline=False,
    )
    embed.add_field(
        name="Team Profiles",
        value=(
            "`/team-profile [player:@user]`\n"
            "View the profile, region, logo, and active roster of your own or another player's team."
        ),
        inline=False,
    )
    embed.add_field(
        name="Team Customization & Ownership",
        value=(
            "`/team_rename new_name:<name>`\n"
            "Rename the team. Enforces database-wide uniqueness. Captain/Manager only.\n\n"
            "`/transfer_captain new_captain:<@user>`\n"
            "Transfer ownership and captain permissions to a roster member. Captain only.\n\n"
            "`/change_team_tag new_tag:<tag>`\n"
            "Change team tag (2–6 chars). Captain only. DMs all members.\n\n"
            "`/team_change_logo`\n"
            "Upload a new logo in a private thread. Captain/Manager only.\n\n"
            "`/team_change_region`\n"
            "Change team region and update all member regions. Captain only."
        ),
        inline=False,
    )

    embed.set_footer(text="Vega Scrims — Page 2 of 3 • Use buttons below to switch categories")
    return embed


def _build_page_3_embed() -> discord.Embed:
    """Page 3: Roster & Invite Management."""
    embed = discord.Embed(
        title="Vega Scrims — Roster & Invite Commands (Page 3/3)",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        "Commands for inviting players, managing pending invites, and roster management."
    )
    embed.add_field(
        name="Team Invites",
        value=(
            "`/invite player:<@user>`\n"
            "Invite a player as Player, Manager, Coach, or Substitute. Captain/Manager only.\n\n"
            "`/invite_cancel player:<@user>`\n"
            "Revoke a pending invite before acceptance. Captain/Manager only.\n\n"
            "`/invite_cancel_all`\n"
            "Cancel all active pending invites sent by your team. Captain/Manager only.\n\n"
            "`/invites_pending`\n"
            "List all active, unexpired invites with roles, senders, and expiration countdowns."
        ),
        inline=False,
    )
    embed.add_field(
        name="Roster Management",
        value=(
            "`/team_set_role player:<@user> role:<role>`\n"
            "Change a team member's role (Player, Manager, Coach, Substitute). Captain/Manager only.\n\n"
            "`/kick player:<@user>`\n"
            "Kick a player from your team. Captain/Manager only.\n\n"
            "`/leave`\n"
            "Leave your current team (Players, Managers, and Coaches only; Captains must use `/disband`)."
        ),
        inline=False,
    )

    embed.set_footer(text="Vega Scrims — Page 3 of 3 • Use buttons below to switch categories")
    return embed


class CommandsPaginationView(discord.ui.View):
    """Persistent category navigation view for the bot commands overview."""

    def __init__(self, current_page: int = 1) -> None:
        super().__init__(timeout=None)
        self.current_page = current_page
        self._update_button_styles()

    def _update_button_styles(self) -> None:
        self.btn_page_1.style = discord.ButtonStyle.primary if self.current_page == 1 else discord.ButtonStyle.secondary
        self.btn_page_2.style = discord.ButtonStyle.primary if self.current_page == 2 else discord.ButtonStyle.secondary
        self.btn_page_3.style = discord.ButtonStyle.primary if self.current_page == 3 else discord.ButtonStyle.secondary

    @discord.ui.button(
        label="👤 Player & Profile",
        style=discord.ButtonStyle.primary,
        custom_id="commands_info:page_1",
    )
    async def btn_page_1(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_page = 1
        self._update_button_styles()
        await interaction.response.edit_message(embed=_build_page_1_embed(), view=self)

    @discord.ui.button(
        label="🛡️ Team & Setup",
        style=discord.ButtonStyle.secondary,
        custom_id="commands_info:page_2",
    )
    async def btn_page_2(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_page = 2
        self._update_button_styles()
        await interaction.response.edit_message(embed=_build_page_2_embed(), view=self)

    @discord.ui.button(
        label="📨 Roster & Invites",
        style=discord.ButtonStyle.secondary,
        custom_id="commands_info:page_3",
    )
    async def btn_page_3(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_page = 3
        self._update_button_styles()
        await interaction.response.edit_message(embed=_build_page_3_embed(), view=self)


class CommandsInfoCog(commands.Cog, name="CommandsInfo"):
    """Handles posting and syncing the paginated bot commands overview card."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._info_message_posted: bool = False

    async def cog_load(self) -> None:
        # Register persistent view so buttons work across restarts
        self.bot.add_view(CommandsPaginationView(current_page=1))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._info_message_posted:
            return
        self._info_message_posted = True
        await self._ensure_info_message()

    async def _ensure_info_message(self) -> None:
        """
        Post the paginated commands overview to the configured commands channel,
        or update the existing message.
        """
        if not COMMANDS_CHANNEL_ID:
            log.warning("COMMANDS_CHANNEL_ID is not configured — skipping commands list.")
            return

        channel = self.bot.get_channel(COMMANDS_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(COMMANDS_CHANNEL_ID)
                if isinstance(fetched, discord.TextChannel):
                    channel = fetched
            except Exception:
                pass

        if not isinstance(channel, discord.TextChannel):
            log.error("Channel %d not found or is not a TextChannel.", COMMANDS_CHANNEL_ID)
            return

        embed = _build_page_1_embed()
        view = CommandsPaginationView(current_page=1)

        # Check for existing message ID
        stored_id = await db.get_config("commands_info_message_id")
        if stored_id:
            try:
                existing_msg = await channel.fetch_message(int(stored_id))
                await existing_msg.edit(embed=embed, view=view)
                log.info("Commands list message refreshed (ID: %s).", stored_id)
                return
            except discord.NotFound:
                log.warning("Stored commands message ID %s was deleted — sending new one.", stored_id)

        # Post new message and pin it
        msg = await channel.send(embed=embed, view=view)
        try:
            await msg.pin()
        except discord.Forbidden:
            log.warning("Missing Manage Messages permission — could not pin commands list.")

        await db.set_config("commands_info_message_id", str(msg.id))
        log.info("Commands list message sent and pinned (ID: %d).", msg.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommandsInfoCog(bot))
