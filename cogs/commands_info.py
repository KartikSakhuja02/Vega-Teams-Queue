"""
cogs/commands_info.py
Commands information cog — posts and updates the persistent commands list embed.
"""

import os
import logging

import discord
from discord.ext import commands

from database import db

log = logging.getLogger(__name__)

COMMANDS_CHANNEL_ID: int = int(os.environ.get("COMMANDS_CHANNEL_ID", "0"))
EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")


def _build_commands_embed() -> discord.Embed:
    """Build the commands overview embed card."""
    embed = discord.Embed(
        title="Vega Scrims — Bot Commands",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        "Here is the list of available commands for players. All command interactions "
        "respond ephemerally (visible only to you) to keep the channels clean and organized."
    )
    
    embed.add_field(
        name="Player Registration",
        value=(
            "`/register ign:<ign> region:<region>`\n"
            "Register your player profile. Can only be used in the registration channel.\n\n"
            "`/unregister`\n"
            "Unregister your profile. Your stats and history are preserved — "
            "if you register again you can choose to restore your old profile or start completely fresh."
        ),
        inline=False,
    )
    
    embed.add_field(
        name="Player Profile",
        value=(
            "`/profile`\n"
            "View your own stats, ELO, overall K/D/A, matches played, and regional ranking.\n\n"
            "`/profile player:<@user>`\n"
            "View the profile and statistics of another registered player.\n\n"
            "`/edit-profile`\n"
            "Edit your registered profile details (In-Game Name or Region). "
            "Each change requires confirmation before it is saved.\n\n"
            "`/team-profile`\n"
            "View the profile, region, and roster of your own team.\n\n"
            "`/team-profile player:<@user>`\n"
            "View the team profile and roster for another player's team.\n\n"
            "`/player_status`\n"
            "Check your current system state: IDLE, IN_QUEUE, IN_MATCH, or PENALTY_COOLDOWN. "
            "Optionally pass @user to check another player's state.\n\n"
            "`/toggle_dms`\n"
            "Enable or disable bot DMs for queue pop alerts and match check-in pings. "
            "Toggled per-player and remembered across sessions."
        ),
        inline=False,
    )

    embed.add_field(
        name="Private Help Tickets",
        value=(
            "`/help`\n"
            "Open a private ticket for direct support from the admin team."
        ),
        inline=False,
    )

    embed.add_field(
        name="Team Creation",
        value=(
            "`/create_team`\n"
            "Open a private team setup thread and submit your team details.\n\n"
            "`/disband`\n"
            "Disband your current team. Your data is preserved — you can resume the old team "
            "or start fresh next time you use `/create_team`."
        ),
        inline=False,
    )

    embed.add_field(
        name="Team Management",
        value=(
            "`/invite player:<@user>`\n"
            "Invite a registered player to your active team. Captain/Manager only. "
            "Prompt selects role (Player, Manager, or Coach).\n\n"
            "`/invite_cancel player:<@user>`\n"
            "Revoke a pending team invite before the player accepts. Captain/Manager only.\n\n"
            "`/invite_cancel_all`\n"
            "Cancel every active pending invite sent by your team. Captain/Manager only.\n\n"
            "`/invites_pending`\n"
            "List all active, unexpired invites sent by your team. Captain/Manager only.\n\n"
            "`/kick player:<@user>`\n"
            "Kick a player from your team. Only Captains and Managers can do this.\n\n"
            "`/leave`\n"
            "Leave your current team (Players, Managers, and Coaches only).\n\n"
            "`/team_rename new_name:<name>`\n"
            "Rename the team. Unique across the database. Captain/Manager only. 2–50 characters.\n\n"
            "`/team_change_logo`\n"
            "Upload a new team logo via a private thread. Bot opens a 1-on-1 thread, you send the image "
            "(PNG/JPG/GIF/WEBP), and it's saved and applied automatically. Captain/Manager only.\n\n"
            "`/change_team_tag new_tag:<tag>`\n"
            "Change your team's tag (e.g. `VGA`). Captain only. 2–6 alphanumeric characters.\n\n"
            "`/team_change_region`\n"
            "Change the entire team's region via dropdown. Captain only. "
            "Also updates every team member's individual region and DMs all members.\n\n"
            "`/player_change_region`\n"
            "Change your own region via dropdown. Available to all registered players."
        ),
        inline=False,
    )


    embed.set_footer(text="Vega Scrims — Do not delete this message.")
    return embed


class CommandsInfoCog(commands.Cog, name="CommandsInfo"):
    """Handles posting and syncing the bot commands overview card."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._info_message_posted: bool = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._info_message_posted:
            return
        self._info_message_posted = True
        await self._ensure_info_message()

    async def _ensure_info_message(self) -> None:
        """
        Post the commands list embed to the configured commands channel,
        or update the existing one if we already sent it.
        """
        if not COMMANDS_CHANNEL_ID:
            log.warning(
                "COMMANDS_CHANNEL_ID is not configured — skipping commands list."
            )
            return

        channel = self.bot.get_channel(COMMANDS_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            log.error(
                "Channel %d not found or is not a TextChannel.", COMMANDS_CHANNEL_ID
            )
            return

        embed = _build_commands_embed()

        # Check for existing message ID
        stored_id = await db.get_config("commands_info_message_id")
        if stored_id:
            try:
                existing_msg = await channel.fetch_message(int(stored_id))
                await existing_msg.edit(embed=embed)
                log.info("Commands list message refreshed (ID: %s).", stored_id)
                return
            except discord.NotFound:
                log.warning(
                    "Stored commands message ID %s was deleted — sending new one.", stored_id
                )

        # Post new message and pin it
        msg = await channel.send(embed=embed)
        try:
            await msg.pin()
        except discord.Forbidden:
            log.warning("Missing Manage Messages permission — could not pin commands list.")

        await db.set_config("commands_info_message_id", str(msg.id))
        log.info("Commands list message sent and pinned (ID: %d).", msg.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommandsInfoCog(bot))
