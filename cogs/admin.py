"""
cogs/admin.py
-------------
Administrative moderation cog — /admin command group (player_ban, player_unban),
/help_admin command, and persistent admin commands overview panel in the admin channel.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from cogs.bot_logger import send_log, COL_DEFAULT, COL_SUCCESS, COL_DANGER, COL_WARNING

log = logging.getLogger(__name__)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")
ADMIN_COMMANDS_CHANNEL_ID: int = int(
    os.environ.get("ADMIN_COMMANDS_CHANNEL_ID", "0") or os.environ.get("ADMIN_CHANNEL_ID", "0")
)
from utils.staff import is_staff, _is_admin, STAFF_ROLE_NAMES, get_staff_role_ids



def _fmt_duration(hours: int) -> str:
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days, rem_hours = divmod(hours, 24)
    if rem_hours == 0:
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{days}d {rem_hours}h"


def _build_admin_commands_embed() -> discord.Embed:
    """Build the rich embed for the admin commands overview panel."""
    embed = discord.Embed(
        title="🛡️ Vega Scrims — Admin Command Center",
        description=(
            "Comprehensive reference for staff moderation, player adjustments, and team administration.\n"
            "All commands require Staff or Administrator permissions."
        ),
        colour=EMBED_COLOUR,
    )

    embed.add_field(
        name="🔨 Player Moderation & Bans",
        value=(
            "`/admin player_ban user:<@user> [duration_hours:<int>] reason:<text>`\n"
            "Ban a player from matchmaking and live queues (temporary or permanent).\n\n"
            "`/admin player_unban user:<@user>`\n"
            "Lift an active ban, clear cooldown penalties, and restore normal queue access."
        ),
        inline=False,
    )

    embed.add_field(
        name="👤 Player Account & Stats Management",
        value=(
            "`/admin player_set_ign user:<@user> ign:<new_ign>` — Update IGN (syncs team captain if applicable)\n"
            "`/admin player_set_region user:<@user> region:<zone>` — Fix accidental region selection\n"
            "`/admin player_set_elo user:<@user> elo:<int>` — Directly set player ELO rating\n"
            "`/admin player_reset_stats user:<@user> [reset_elo:bool]` — Wipe kills/deaths/matches (optional ELO reset)\n"
            "`/admin player_reset_status user:<@user>` — Reset stuck queue/match states & clear cooldowns\n"
            "`/admin player_delete user:<@user>` — Unregister & delete player from database\n"
            "`/admin player_info user:<@user>` — Detailed inspection of raw profile, ELO, status, & team"
        ),
        inline=False,
    )

    embed.add_field(
        name="🛡️ Team & Roster Administration",
        value=(
            "`/admin team_rename team:<team> new_name:<name>` — Rename team (enforces uniqueness)\n"
            "`/admin team_set_tag team:<team> new_tag:<tag>` — Update team tag (2–6 chars)\n"
            "`/admin team_set_region team:<team> region:<zone> [sync_members:bool]` — Update team & member regions\n"
            "`/admin team_set_captain team:<team> new_captain:<@user>` — Transfer captaincy to any player\n"
            "`/admin team_add_member team:<team> user:<@user> role:<role>` — Force add player to roster\n"
            "`/admin team_remove_member team:<team> user:<@user>` — Force kick member from roster\n"
            "`/admin team_set_role team:<team> user:<@user> role:<role>` — Update member role (Player/Manager/Coach/Sub)\n"
            "`/admin team_disband team:<team>` — Force disband team & purge from queues\n"
            "`/admin team_reactivate team:<team>` — Reactivate a previously disbanded team\n"
            "`/admin team_info team:<team>` — Inspect full team roster, region, captain, & queue states"
        ),
        inline=False,
    )

    embed.add_field(
        name="⚡ Matchmaking & Queue Controls",
        value=(
            "`/admin team_dequeue team:<team> [queue_type:Both|REGIONAL|GLOBAL]` — Evict team from live queues\n"
            "`/post_team_queue` — Post or refresh persistent live matchmaking queue panel"
        ),
        inline=False,
    )

    embed.add_field(
        name="🎮 10-Man Solo Queue & Templates",
        value=(
            "`/solo_config panel` — Interactive configuration UI for captain, draft, veto, and styling\n"
            "`/solo_config view` — View all active 10-man solo queue template configurations\n"
            "`/solo_config captain_mode mode:<template>` — Set captain selection (`HIGHEST_ELO`, `RANDOM`, `FIRST_JOINED`, `HIGHEST_WINRATE`)\n"
            "`/solo_config draft_mode mode:<template>` — Set player draft (`SNAKE`, `ALTERNATING`, `AUTO_BALANCE`)\n"
            "`/solo_config veto_mode mode:<template>` — Set map veto format (`ALTERNATING_BAN`, `BAN_BAN_PICK`, `RANDOM_MAP`, `CAPTAIN_PICK`)\n"
            "`/solo_config theme [preset] [custom_hex]` — Set visual embed accent theme (`PURPLE`, `VALORANT_RED`, `CYBER_CYAN`, `GOLD`, `EMERALD`, or `#hex`)\n"
            "`/solo_config map_pool maps:<comma_list>` — Set active competitive map pool\n"
            "`/post_solo_queue` — Post or refresh persistent 10-man solo queue panel\n"
            "`/clear_solo_queue` *(or `/clear-solo-queue`)* — Clear all waiting players from queue and reset to IDLE\n"
            "`/cancel_solo_match` — Cancel active 10-man match lobby and release players"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔍 Match OCR Testing & Staff Tools",
        value=(
            "`/test_ss_ocr image:<attachment>` — Test scoreboard OCR extraction on match screenshot\n"
            "`/player_status player:<@user>` — Quick check of player status & cooldowns\n"
            "`/help_admin` — Display this administrative commands center on demand"
        ),
        inline=False,
    )

    embed.set_footer(text="Vega Scrims Administration • Staff Access Only")
    return embed



class AdminCog(commands.Cog, name="Admin"):
    """Handles staff administration, moderation commands, and the admin command center."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._panel_posted: bool = False

    admin_group = app_commands.Group(
        name="admin",
        description="Administrative moderation and management commands.",
    )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._panel_posted:
            return
        self._panel_posted = True
        await self._ensure_admin_commands_message()

    async def _ensure_admin_commands_message(self) -> None:
        """
        Post or update the admin commands overview card in the configured admin channel.
        """
        channel_id = int(
            os.environ.get("ADMIN_COMMANDS_CHANNEL_ID", "0") or os.environ.get("ADMIN_CHANNEL_ID", "0")
        )
        if not channel_id:
            log.info("ADMIN_COMMANDS_CHANNEL_ID is not configured — skipping admin panel posting.")
            return

        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(channel_id)
                if isinstance(fetched, discord.TextChannel):
                    channel = fetched
            except Exception:
                pass

        if not isinstance(channel, discord.TextChannel):
            log.warning("Admin commands channel %d not found or is not a TextChannel.", channel_id)
            return

        embed = _build_admin_commands_embed()

        # Check for existing message ID
        stored_id = await db.get_config("admin_commands_info_message_id")
        if stored_id:
            try:
                existing_msg = await channel.fetch_message(int(stored_id))
                await existing_msg.edit(embed=embed)
                log.info("Admin commands list message refreshed (ID: %s).", stored_id)
                return
            except discord.NotFound:
                log.warning("Stored admin commands message ID %s was deleted — sending new one.", stored_id)
            except Exception as e:
                log.warning("Could not refresh admin commands message: %s", e)

        # Post new message and pin it
        try:
            msg = await channel.send(embed=embed)
            try:
                await msg.pin()
            except discord.Forbidden:
                log.warning("Missing Manage Messages permission — could not pin admin commands card.")
            await db.set_config("admin_commands_info_message_id", str(msg.id))
            log.info("Admin commands list message sent and saved (ID: %d).", msg.id)
        except Exception as e:
            log.error("Failed to send admin commands panel: %s", e)

    # ── /admin player_ban ───────────────────────────────────────────────────

    @admin_group.command(
        name="player_ban",
        description="Ban a player from matchmaking and live queues.",
    )
    @app_commands.describe(
        user="The player to ban from matchmaking.",
        reason="The infraction reason for this ban.",
        duration_hours="Optional ban duration in hours (leave empty for permanent).",
    )
    async def player_ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str,
        duration_hours: Optional[int] = None,
    ) -> None:
        """Ban a player from queues and matches."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        # 1. Check admin permissions
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # 2. Guardrails
        if user.id == interaction.user.id:
            await interaction.followup.send("You cannot ban yourself.", ephemeral=True)
            return

        if user.bot:
            await interaction.followup.send("You cannot ban bots.", ephemeral=True)
            return

        # Check if target is a server administrator
        target_member = interaction.guild.get_member(user.id)
        if target_member and _is_admin(target_member) and target_member.id != interaction.user.id:
            if not interaction.user.guild_permissions.administrator and interaction.guild.owner_id != interaction.user.id:
                await interaction.followup.send("You cannot ban another staff member.", ephemeral=True)
                return

        # 3. Check if player exists in database
        player_record = await db.get_player(user.id)
        if not player_record:
            await interaction.followup.send(
                f"{user.mention} is not registered in Vega Scrims database.",
                ephemeral=True,
            )
            return

        # 4. Check duration validity
        if duration_hours is not None and duration_hours <= 0:
            await interaction.followup.send("Duration in hours must be a positive number.", ephemeral=True)
            return

        # 5. Apply ban in database
        updated = await db.ban_player(
            discord_id=user.id,
            reason=reason.strip(),
            banned_by=interaction.user.id,
            duration_hours=duration_hours,
        )
        if not updated:
            await interaction.followup.send("Failed to ban player due to a database error.", ephemeral=True)
            return

        dur_text = f"`{_fmt_duration(duration_hours)}`" if duration_hours else "`Permanent`"

        # 6. Send DM to banned user
        try:
            dm_embed = discord.Embed(
                title="🔨 Account Banned from Matchmaking",
                description=(
                    f"You have been banned from Vega Scrims matchmaking queues.\n\n"
                    f"**Reason:** {reason.strip()}\n"
                    f"**Duration:** {dur_text}\n\n"
                    "If you believe this is an error or wish to appeal, please contact server staff."
                ),
                colour=COL_DANGER,
            )
            dm_embed.set_footer(text="Vega Scrims Moderation")
            await user.send(embed=dm_embed)
        except Exception:
            log.info("Could not send ban DM to user %d (DMs may be closed).", user.id)

        # 7. Audit Log
        fields = [
            ("Player",   f"{user.mention} (`{user.id}`)",       True),
            ("IGN",      player_record.get("ign", "N/A"),       True),
            ("Duration", dur_text,                              True),
            ("Reason",   reason.strip(),                        False),
            ("Staff",    f"{interaction.user.mention} (`{interaction.user.id}`)", False),
        ]
        await send_log(
            self.bot,
            title="🔨 Player Banned",
            description=f"{user.mention} was banned from matchmaking by {interaction.user.mention}",
            colour=COL_DANGER,
            fields=fields,
        )

        await interaction.followup.send(
            f"✅ Successfully banned {user.mention} ({player_record.get('ign')}).\n"
            f"• **Duration:** {dur_text}\n"
            f"• **Reason:** {reason.strip()}",
            ephemeral=True,
        )

    # ── /admin player_unban ─────────────────────────────────────────────────

    @admin_group.command(
        name="player_unban",
        description="Unban a player and restore queue access.",
    )
    @app_commands.describe(
        user="The player to unban.",
    )
    async def player_unban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        """Unban a player and clear ban status."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        # 1. Check admin permissions
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # 2. Check if player exists & is banned
        player_record = await db.get_player(user.id)
        if not player_record:
            await interaction.followup.send(f"{user.mention} is not registered in the database.", ephemeral=True)
            return

        if not player_record.get("is_banned"):
            await interaction.followup.send(f"{user.mention} ({player_record.get('ign')}) is not currently banned.", ephemeral=True)
            return

        # 3. Unban in database
        updated = await db.unban_player(user.id)
        if not updated:
            await interaction.followup.send("Failed to unban player due to a database error.", ephemeral=True)
            return

        # 4. Send DM to player
        try:
            dm_embed = discord.Embed(
                title="🔓 Ban Lifted",
                description=(
                    "Your ban on Vega Scrims has been lifted by staff.\n"
                    "Your normal queue and matchmaking access has been fully restored."
                ),
                colour=COL_SUCCESS,
            )
            dm_embed.set_footer(text="Vega Scrims Moderation")
            await user.send(embed=dm_embed)
        except Exception:
            pass

        # 5. Audit Log
        fields = [
            ("Player", f"{user.mention} (`{user.id}`)",       True),
            ("IGN",    player_record.get("ign", "N/A"),       True),
            ("Staff",  f"{interaction.user.mention} (`{interaction.user.id}`)", False),
        ]
        await send_log(
            self.bot,
            title="🔓 Player Unbanned",
            description=f"{user.mention} was unbanned by {interaction.user.mention}",
            colour=COL_SUCCESS,
            fields=fields,
        )

        await interaction.followup.send(
            f"✅ Successfully unbanned {user.mention} ({player_record.get('ign')}). Queue access has been restored.",
            ephemeral=True,
        )

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _refresh_team_queue(self) -> None:
        """Helper to notify TeamQueueCog to refresh its persistent channel message."""
        team_queue_cog = self.bot.get_cog("TeamQueue")
        if team_queue_cog and hasattr(team_queue_cog, "refresh_queue_message"):
            try:
                await team_queue_cog.refresh_queue_message()
            except Exception as e:
                log.warning("Could not refresh team queue message: %s", e)

    async def _autocomplete_teams(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team parameter searching by name, tag, or numeric ID."""
        teams = await db.search_teams(current, limit=25)
        choices = []
        for t in teams:
            tag_str = f" [{t['team_tag']}]" if t.get("team_tag") else ""
            status_str = "" if t.get("is_active") else " (Disbanded)"
            label = f"{t['team_name']}{tag_str} ({t['region']}){status_str}"
            choices.append(app_commands.Choice(name=label[:100], value=str(t["id"])))
        return choices

    # ── /admin Player Management Commands ───────────────────────────────────

    @admin_group.command(
        name="player_set_ign",
        description="Update a player's in-game name (and syncs team captain IGN if applicable).",
    )
    @app_commands.describe(
        user="The player whose IGN to change.",
        ign="The new in-game name (e.g. RiotID#TAG).",
    )
    async def player_set_ign(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        ign: str,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        player = await db.get_player(user.id)
        if not player:
            await interaction.followup.send(f"{user.mention} is not registered in the system.", ephemeral=True)
            return

        old_ign = player.get("ign", "N/A")
        cleaned_ign = ign.strip()
        updated = await db.admin_update_player_ign(user.id, cleaned_ign)
        if not updated:
            await interaction.followup.send("Failed to update IGN due to a database error.", ephemeral=True)
            return

        await self._refresh_team_queue()

        # Audit log
        await send_log(
            self.bot,
            title="✏️ Player IGN Updated",
            description=f"Staff updated IGN for {user.mention}",
            colour=COL_DEFAULT,
            fields=[
                ("Player", f"{user.mention} (`{user.id}`)", True),
                ("Old IGN", old_ign, True),
                ("New IGN", cleaned_ign, True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(
            f"Successfully updated IGN for {user.mention} from `{old_ign}` to **`{cleaned_ign}`**.",
            ephemeral=True,
        )

    @admin_group.command(
        name="player_set_region",
        description="Change a player's registered matchmaking region.",
    )
    @app_commands.describe(
        user="The player whose region to change.",
        region="The new regional zone for this player.",
    )
    @app_commands.choices(region=[
        app_commands.Choice(name="India", value="India"),
        app_commands.Choice(name="APAC", value="APAC"),
        app_commands.Choice(name="EMEA", value="EMEA"),
        app_commands.Choice(name="Americas", value="Americas"),
    ])
    async def player_set_region(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        region: app_commands.Choice[str],
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        player = await db.get_player(user.id)
        if not player:
            await interaction.followup.send(f"{user.mention} is not registered in the system.", ephemeral=True)
            return

        old_region = player.get("region", "N/A")
        updated = await db.update_player_region(user.id, region.value)
        if not updated:
            await interaction.followup.send("Failed to update player region.", ephemeral=True)
            return

        # Check if captain
        captain_team = await db.get_team_by_captain(user.id)
        note_str = ""
        if captain_team:
            note_str = f"\n*Note: {user.mention} is captain of team '{captain_team['team_name']}'. Use `/admin team_set_region` if you wish to change the team's region as well.*"

        await send_log(
            self.bot,
            title="🌍 Player Region Updated",
            description=f"Staff updated region for {user.mention}",
            colour=COL_DEFAULT,
            fields=[
                ("Player", f"{user.mention} (`{user.id}`)", True),
                ("Old Region", str(old_region), True),
                ("New Region", region.value, True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(
            f"Successfully updated region for {user.mention} from `{old_region}` to **`{region.value}`**.{note_str}",
            ephemeral=True,
        )

    @admin_group.command(
        name="player_set_elo",
        description="Directly set a player's ELO matchmaking rating.",
    )
    @app_commands.describe(
        user="The player whose ELO to change.",
        elo="The new ELO score (e.g. 1000).",
    )
    async def player_set_elo(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        elo: int,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        player = await db.get_player(user.id)
        if not player:
            await interaction.followup.send(f"{user.mention} is not registered in the system.", ephemeral=True)
            return

        old_elo = player.get("elo", 1000)
        updated = await db.admin_update_player_elo(user.id, elo)
        if not updated:
            await interaction.followup.send("Failed to update player ELO.", ephemeral=True)
            return

        lb_cog = self.bot.get_cog("Leaderboard")
        if lb_cog and hasattr(lb_cog, "refresh_all_leaderboards"):
            asyncio.create_task(lb_cog.refresh_all_leaderboards())

        await send_log(
            self.bot,
            title="⭐ Player ELO Updated",
            description=f"Staff updated ELO for {user.mention}",
            colour=COL_DEFAULT,
            fields=[
                ("Player", f"{user.mention} (`{user.id}`)", True),
                ("Old ELO", str(old_elo), True),
                ("New ELO", str(elo), True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(
            f"Successfully updated ELO for {user.mention} ({player.get('ign')}) from `{old_elo}` to **`{elo}`**.",
            ephemeral=True,
        )

    @admin_group.command(
        name="player_reset_stats",
        description="Wipe player match statistics (kills, deaths, assists, matches, wins).",
    )
    @app_commands.describe(
        user="The player whose stats to reset.",
        reset_elo="Whether to reset ELO back to 1000 as well (Default: False).",
    )
    async def player_reset_stats(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reset_elo: bool = False,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        player = await db.get_player(user.id)
        if not player:
            await interaction.followup.send(f"{user.mention} is not registered in the system.", ephemeral=True)
            return

        updated = await db.admin_reset_player_stats(user.id, reset_elo=reset_elo)
        if not updated:
            await interaction.followup.send("Failed to reset player stats.", ephemeral=True)
            return

        lb_cog = self.bot.get_cog("Leaderboard")
        if lb_cog and hasattr(lb_cog, "refresh_all_leaderboards"):
            asyncio.create_task(lb_cog.refresh_all_leaderboards())

        elo_note = "ELO reset to 1000." if reset_elo else f"ELO preserved at `{player.get('elo', 1000)}`."
        await send_log(
            self.bot,
            title="🔄 Player Stats Reset",
            description=f"Staff reset match stats for {user.mention}",
            colour=COL_WARNING,
            fields=[
                ("Player", f"{user.mention} (`{user.id}`)", True),
                ("Details", elo_note, True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(
            f"Successfully reset match combat stats for {user.mention} ({player.get('ign')}). {elo_note}",
            ephemeral=True,
        )

    @admin_group.command(
        name="player_reset_status",
        description="Force-reset a stuck player state to IDLE and clear cooldown penalties.",
    )
    @app_commands.describe(
        user="The player whose status state to reset.",
    )
    async def player_reset_status(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        player = await db.get_player(user.id)
        if not player:
            await interaction.followup.send(f"{user.mention} is not registered in the system.", ephemeral=True)
            return

        old_status = player.get("status", "IDLE")
        updated = await db.admin_reset_player_status(user.id)
        if not updated:
            await interaction.followup.send("Failed to reset player status.", ephemeral=True)
            return

        await send_log(
            self.bot,
            title="🔄 Player Status Reset",
            description=f"Staff cleared active queue/cooldown state for {user.mention}",
            colour=COL_SUCCESS,
            fields=[
                ("Player", f"{user.mention} (`{user.id}`)", True),
                ("Previous Status", str(old_status), True),
                ("New Status", "IDLE", True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(
            f"Successfully reset status for {user.mention} ({player.get('ign')}) from `{old_status}` to **IDLE**. Any penalty cooldown was cleared.",
            ephemeral=True,
        )

    @admin_group.command(
        name="player_delete",
        description="Permanently delete or unregister a player record from the database.",
    )
    @app_commands.describe(
        user="The player record to delete.",
    )
    async def player_delete(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        player = await db.get_player(user.id)
        if not player:
            await interaction.followup.send(f"{user.mention} is not registered in the system.", ephemeral=True)
            return

        success, msg = await db.admin_delete_player(user.id)
        if not success:
            await interaction.followup.send(f"Could not delete player: {msg}", ephemeral=True)
            return

        await send_log(
            self.bot,
            title="🗑️ Player Deleted",
            description=f"Staff permanently deleted player record for {user.mention}",
            colour=COL_DANGER,
            fields=[
                ("Player", f"{user.mention} (`{user.id}`)", True),
                ("IGN", player.get("ign", "N/A"), True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(
            f"Successfully deleted player profile for {user.mention} ({player.get('ign')}). The player can re-register anytime.",
            ephemeral=True,
        )

    @admin_group.command(
        name="player_info",
        description="Inspect complete internal database details and team membership for a player.",
    )
    @app_commands.describe(
        user="The player to inspect.",
    )
    async def player_info(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        player = await db.get_player_profile(user.id)
        if not player:
            # Check inactive
            player = await db.get_player(user.id)
            if not player:
                await interaction.followup.send(f"{user.mention} is not registered in the system.", ephemeral=True)
                return

        team_member = await db.get_player_team_membership(user.id)
        captain_team = await db.get_team_by_captain(user.id)
        is_banned, ban_reason, banned_until, banned_by = await db.get_player_ban_status(user.id)

        embed = discord.Embed(
            title=f"Player Inspector — {player.get('ign', 'N/A')}",
            colour=EMBED_COLOUR,
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        embed.add_field(name="Discord User", value=f"{user.mention}\n`{user.name}` (`{user.id}`)", inline=True)
        embed.add_field(name="Region", value=f"`{player.get('region', 'N/A')}`", inline=True)
        embed.add_field(name="Rating / ELO", value=f"**{player.get('elo', 1000)}** ELO", inline=True)

        kda_str = f"{player.get('kills', 0)} / {player.get('deaths', 0)} / {player.get('assists', 0)}"
        embed.add_field(name="Combat Stats", value=f"K/D/A: `{kda_str}`\nMatches: `{player.get('matches_played', 0)}` (Wins: `{player.get('wins', 0)}`)", inline=True)
        embed.add_field(name="System Status", value=f"Status: `{player.get('status', 'IDLE')}`\nActive: `{player.get('is_active', True)}`", inline=True)
        embed.add_field(name="DMs Enabled", value=f"`{player.get('dms_enabled', True)}`", inline=True)

        if captain_team:
            embed.add_field(name="Team Ownership", value=f"**Captain** of **{captain_team['team_name']}** `[{captain_team['team_tag']}]` (ID: `{captain_team['id']}`)", inline=False)
        elif team_member:
            embed.add_field(name="Team Membership", value=f"**{team_member['role']}** on **{team_member['team_name']}** `[{team_member['team_tag']}]` (ID: `{team_member['team_id']}`)", inline=False)
        else:
            embed.add_field(name="Team", value="*Free Agent / No Team*", inline=False)

        if is_banned:
            until_str = f"<t:{int(banned_until.timestamp())}:R>" if banned_until else "Permanent"
            embed.add_field(name="Ban Status", value=f"**BANNED**\nReason: `{ban_reason}`\nExpires: {until_str}", inline=False)

        reg_ts = player.get("registered_at")
        if reg_ts:
            embed.set_footer(text=f"Registered on {reg_ts.strftime('%Y-%m-%d %H:%M UTC')}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin Team Management Commands ─────────────────────────────────────

    @admin_group.command(
        name="team_rename",
        description="Rename a team (enforces database-wide uniqueness).",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
        new_name="The new name for the team.",
    )
    async def team_rename(
        self,
        interaction: discord.Interaction,
        team: str,
        new_name: str,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        cleaned_name = new_name.strip()
        existing_conflict = await db.get_team_by_name_key(cleaned_name.lower())
        if existing_conflict and existing_conflict["id"] != team_record["id"]:
            await interaction.followup.send(f"A team named '{cleaned_name}' already exists.", ephemeral=True)
            return

        old_name = team_record["team_name"]
        updated = await db.update_team_name(team_record["id"], cleaned_name)
        if not updated:
            await interaction.followup.send("Failed to update team name due to conflict or error.", ephemeral=True)
            return

        await self._refresh_team_queue()

        await send_log(
            self.bot,
            title="🏷️ Team Renamed",
            description=f"Staff renamed team from **{old_name}** to **{cleaned_name}**",
            colour=COL_DEFAULT,
            fields=[
                ("Team ID", str(team_record["id"]), True),
                ("Old Name", old_name, True),
                ("New Name", cleaned_name, True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(
            f"Successfully renamed team from **{old_name}** to **{cleaned_name}**.",
            ephemeral=True,
        )

    @team_rename.autocomplete("team")
    async def team_rename_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_set_tag",
        description="Update a team's tag (2 to 6 uppercase alphanumeric characters).",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
        new_tag="The new team tag (e.g. VGA).",
    )
    async def team_set_tag(
        self,
        interaction: discord.Interaction,
        team: str,
        new_tag: str,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        cleaned_tag = new_tag.strip().upper()
        if len(cleaned_tag) < 2 or len(cleaned_tag) > 6:
            await interaction.followup.send("Team tag must be between 2 and 6 characters.", ephemeral=True)
            return

        conflict = await db.get_team_by_tag_key(cleaned_tag.lower())
        if conflict and conflict["id"] != team_record["id"]:
            await interaction.followup.send(f"A team with tag `[{cleaned_tag}]` already exists.", ephemeral=True)
            return

        old_tag = team_record["team_tag"]
        updated = await db.update_team_tag(team_record["id"], cleaned_tag)
        if not updated:
            await interaction.followup.send("Failed to update team tag due to conflict or error.", ephemeral=True)
            return

        await self._refresh_team_queue()

        await send_log(
            self.bot,
            title="🏷️ Team Tag Updated",
            description=f"Staff updated tag for **{team_record['team_name']}**",
            colour=COL_DEFAULT,
            fields=[
                ("Team", f"{team_record['team_name']} (`{team_record['id']}`)", True),
                ("Old Tag", f"`[{old_tag}]`", True),
                ("New Tag", f"`[{cleaned_tag}]`", True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(
            f"Successfully updated tag for **{team_record['team_name']}** from `[{old_tag}]` to **`[{cleaned_tag}]`**.",
            ephemeral=True,
        )

    @team_set_tag.autocomplete("team")
    async def team_set_tag_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_set_region",
        description="Change a team's region (and optionally synchronize all roster members).",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
        region="The new competition region for the team.",
        sync_members="Whether to update all team members' player region to match (Default: True).",
    )
    @app_commands.choices(region=[
        app_commands.Choice(name="India", value="India"),
        app_commands.Choice(name="APAC", value="APAC"),
        app_commands.Choice(name="EMEA", value="EMEA"),
        app_commands.Choice(name="Americas", value="Americas"),
    ])
    async def team_set_region(
        self,
        interaction: discord.Interaction,
        team: str,
        region: app_commands.Choice[str],
        sync_members: bool = True,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        old_region = team_record["region"]
        updated = await db.update_team_region(team_record["id"], region.value)
        if not updated:
            await interaction.followup.send("Failed to update team region.", ephemeral=True)
            return

        members_updated = 0
        if sync_members:
            members_updated = await db.bulk_update_team_members_region(team_record["id"], region.value)
            # also update captain's region in players
            await db.update_player_region(team_record["captain_discord_id"], region.value)

        # Update region in team_queue if queued
        try:
            await db.get_pool().execute(
                "UPDATE team_queue SET region = $1::region_enum WHERE team_id = $2",
                region.value,
                team_record["id"],
            )
        except Exception:
            pass

        await self._refresh_team_queue()

        await send_log(
            self.bot,
            title="🌍 Team Region Updated",
            description=f"Staff updated region for **{team_record['team_name']}**",
            colour=COL_DEFAULT,
            fields=[
                ("Team", f"{team_record['team_name']} (`{team_record['id']}`)", True),
                ("Old Region", str(old_region), True),
                ("New Region", region.value, True),
                ("Members Synced", str(members_updated), True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        sync_text = f" and synchronized {members_updated} member(s)" if sync_members else ""
        await interaction.followup.send(
            f"Successfully updated region for **{team_record['team_name']}** from `{old_region}` to **`{region.value}`**{sync_text}.",
            ephemeral=True,
        )

    @team_set_region.autocomplete("team")
    async def team_set_region_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_set_captain",
        description="Transfer team ownership and captain role to another registered player.",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
        new_captain="The registered player to assign as the new captain.",
    )
    async def team_set_captain(
        self,
        interaction: discord.Interaction,
        team: str,
        new_captain: discord.Member,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        if team_record["captain_discord_id"] == new_captain.id:
            await interaction.followup.send(f"{new_captain.mention} is already the captain of **{team_record['team_name']}**.", ephemeral=True)
            return

        new_captain_player = await db.get_player(new_captain.id)
        if not new_captain_player:
            await interaction.followup.send(f"{new_captain.mention} is not registered in the system.", ephemeral=True)
            return

        # Check if new captain is already captain of another team
        other_captain_team = await db.get_team_by_captain(new_captain.id)
        if other_captain_team:
            await interaction.followup.send(f"{new_captain.mention} is already the captain of another team ('{other_captain_team['team_name']}').", ephemeral=True)
            return

        # Remove from any previous team membership first
        await db.remove_team_member(team_record["id"], new_captain.id)

        old_captain_id = team_record["captain_discord_id"]
        updated_team = await db.transfer_team_captain(
            team_id=team_record["id"],
            old_captain_id=old_captain_id,
            new_captain_id=new_captain.id,
            new_captain_username=new_captain.name,
            new_captain_ign=new_captain_player["ign"],
            old_captain_new_role="Player",
        )

        if not updated_team:
            await interaction.followup.send("Failed to transfer captaincy due to a database error.", ephemeral=True)
            return

        await self._refresh_team_queue()

        await send_log(
            self.bot,
            title="👑 Team Captain Transferred",
            description=f"Staff transferred captaincy of **{team_record['team_name']}**",
            colour=COL_WARNING,
            fields=[
                ("Team", f"{team_record['team_name']} (`{team_record['id']}`)", True),
                ("Old Captain", f"<@{old_captain_id}> (`{old_captain_id}`)", True),
                ("New Captain", f"{new_captain.mention} (`{new_captain.id}`)", True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(
            f"Successfully transferred captaincy of **{team_record['team_name']}** to {new_captain.mention} ({new_captain_player['ign']}). Previous captain moved to Player role.",
            ephemeral=True,
        )

    @team_set_captain.autocomplete("team")
    async def team_set_captain_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_add_member",
        description="Force-add a player to a team roster with a specific role.",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
        user="The player to add to the team.",
        role="The role to assign (Player, Manager, Coach, Substitute).",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Player", value="Player"),
        app_commands.Choice(name="Manager", value="Manager"),
        app_commands.Choice(name="Coach", value="Coach"),
        app_commands.Choice(name="Substitute", value="Substitute"),
    ])
    async def team_add_member(
        self,
        interaction: discord.Interaction,
        team: str,
        user: discord.Member,
        role: app_commands.Choice[str],
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        success, msg = await db.admin_force_add_team_member(team_record["id"], user.id, role.value)
        if not success:
            await interaction.followup.send(f"Could not add member: {msg}", ephemeral=True)
            return

        await send_log(
            self.bot,
            title="➕ Team Member Added by Staff",
            description=f"Staff added {user.mention} to **{team_record['team_name']}**",
            colour=COL_SUCCESS,
            fields=[
                ("Team", f"{team_record['team_name']} (`{team_record['id']}`)", True),
                ("Member", f"{user.mention} (`{user.id}`)", True),
                ("Role", role.value, True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(f"Successfully added {user.mention} to **{team_record['team_name']}** as **{role.value}**.", ephemeral=True)

    @team_add_member.autocomplete("team")
    async def team_add_member_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_remove_member",
        description="Force-kick a member from a team roster.",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
        user="The team member to remove.",
    )
    async def team_remove_member(
        self,
        interaction: discord.Interaction,
        team: str,
        user: discord.Member,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        if team_record["captain_discord_id"] == user.id:
            await interaction.followup.send(f"Cannot kick {user.mention} because they are the team captain. Use `/admin team_set_captain` first or `/admin team_disband`.", ephemeral=True)
            return

        removed = await db.remove_team_member(team_record["id"], user.id)
        if not removed:
            await interaction.followup.send(f"{user.mention} was not found on the roster for **{team_record['team_name']}**.", ephemeral=True)
            return

        await send_log(
            self.bot,
            title="➖ Team Member Removed by Staff",
            description=f"Staff removed {user.mention} from **{team_record['team_name']}**",
            colour=COL_WARNING,
            fields=[
                ("Team", f"{team_record['team_name']} (`{team_record['id']}`)", True),
                ("Member", f"{user.mention} (`{user.id}`)", True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(f"Successfully removed {user.mention} from **{team_record['team_name']}**.", ephemeral=True)

    @team_remove_member.autocomplete("team")
    async def team_remove_member_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_set_role",
        description="Update a team member's role (Player, Manager, Coach, Substitute).",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
        user="The team member whose role to change.",
        role="The new role to assign.",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Player", value="Player"),
        app_commands.Choice(name="Manager", value="Manager"),
        app_commands.Choice(name="Coach", value="Coach"),
        app_commands.Choice(name="Substitute", value="Substitute"),
    ])
    async def team_set_role(
        self,
        interaction: discord.Interaction,
        team: str,
        user: discord.Member,
        role: app_commands.Choice[str],
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        if team_record["captain_discord_id"] == user.id:
            await interaction.followup.send(f"{user.mention} is the team Captain. Use `/admin team_set_captain` to change ownership.", ephemeral=True)
            return

        updated = await db.update_team_member_role(team_record["id"], user.id, role.value)
        if not updated:
            await interaction.followup.send(f"{user.mention} is not a member of team **{team_record['team_name']}**.", ephemeral=True)
            return

        await send_log(
            self.bot,
            title="🎭 Team Member Role Changed by Staff",
            description=f"Staff updated role for {user.mention} on **{team_record['team_name']}**",
            colour=COL_DEFAULT,
            fields=[
                ("Team", f"{team_record['team_name']} (`{team_record['id']}`)", True),
                ("Member", f"{user.mention} (`{user.id}`)", True),
                ("New Role", role.value, True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(f"Successfully set role for {user.mention} on **{team_record['team_name']}** to **{role.value}**.", ephemeral=True)

    @team_set_role.autocomplete("team")
    async def team_set_role_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_disband",
        description="Force-disband a team (sets inactive and purges from matchmaking queues).",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
    )
    async def team_disband(
        self,
        interaction: discord.Interaction,
        team: str,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        if not team_record["is_active"]:
            await interaction.followup.send(f"Team **{team_record['team_name']}** is already disbanded.", ephemeral=True)
            return

        await db.deactivate_team(team_record["captain_discord_id"])
        await self._refresh_team_queue()

        await send_log(
            self.bot,
            title="🛑 Team Disbanded by Staff",
            description=f"Staff force-disbanded team **{team_record['team_name']}**",
            colour=COL_DANGER,
            fields=[
                ("Team", f"{team_record['team_name']} (`{team_record['id']}`)", True),
                ("Captain", f"<@{team_record['captain_discord_id']}>", True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(f"Successfully disbanded team **{team_record['team_name']}** `[{team_record['team_tag']}]`. The team was removed from active queues.", ephemeral=True)

    @team_disband.autocomplete("team")
    async def team_disband_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_reactivate",
        description="Reactivate a previously disbanded team.",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
    )
    async def team_reactivate(
        self,
        interaction: discord.Interaction,
        team: str,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        if team_record["is_active"]:
            await interaction.followup.send(f"Team **{team_record['team_name']}** is already active.", ephemeral=True)
            return

        await db.get_pool().execute("UPDATE teams SET is_active = TRUE WHERE id = $1", team_record["id"])

        await send_log(
            self.bot,
            title="♻️ Team Reactivated by Staff",
            description=f"Staff reactivated team **{team_record['team_name']}**",
            colour=COL_SUCCESS,
            fields=[
                ("Team", f"{team_record['team_name']} (`{team_record['id']}`)", True),
                ("Captain", f"<@{team_record['captain_discord_id']}>", True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(f"Successfully reactivated team **{team_record['team_name']}** `[{team_record['team_tag']}]`.", ephemeral=True)

    @team_reactivate.autocomplete("team")
    async def team_reactivate_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_dequeue",
        description="Evict a team from the live matchmaking queue.",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
        queue_type="Which queue to remove them from (Both, Regional, or Global).",
    )
    @app_commands.choices(queue_type=[
        app_commands.Choice(name="Both Queues", value="Both"),
        app_commands.Choice(name="Regional Queue Only", value="REGIONAL"),
        app_commands.Choice(name="Global Queue Only", value="GLOBAL"),
    ])
    async def team_dequeue(
        self,
        interaction: discord.Interaction,
        team: str,
        queue_type: app_commands.Choice[str],
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        if queue_type.value == "Both":
            removed = await db.remove_team_from_queue(team_record["id"])
        else:
            removed = await db.remove_team_from_queue(team_record["id"], queue_type.value)

        await self._refresh_team_queue()

        if not removed:
            await interaction.followup.send(f"Team **{team_record['team_name']}** was not found in the {queue_type.name}.", ephemeral=True)
            return

        await send_log(
            self.bot,
            title="🚪 Team Evicted from Queue",
            description=f"Staff removed **{team_record['team_name']}** from matchmaking queue",
            colour=COL_WARNING,
            fields=[
                ("Team", f"{team_record['team_name']} (`{team_record['id']}`)", True),
                ("Queue Type", queue_type.name, True),
                ("Staff", f"{interaction.user.mention} (`{interaction.user.id}`)", False),
            ],
        )

        await interaction.followup.send(f"Successfully removed team **{team_record['team_name']}** from **{queue_type.name}**.", ephemeral=True)

    @team_dequeue.autocomplete("team")
    async def team_dequeue_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)

    @admin_group.command(
        name="team_info",
        description="Inspect complete team details, full roster by role, and live queue status.",
    )
    @app_commands.describe(
        team="Search for the team by name, tag, or ID.",
    )
    async def team_info(
        self,
        interaction: discord.Interaction,
        team: str,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        team_record = await db.get_team_by_identifier(team)
        if not team_record:
            await interaction.followup.send(f"Team `{team}` was not found.", ephemeral=True)
            return

        members = await db.get_team_members(team_record["id"])
        queues = await db.get_team_queues(team_record["id"])

        embed = discord.Embed(
            title=f"Team Inspector — {team_record['team_name']} [{team_record['team_tag']}]",
            colour=EMBED_COLOUR,
        )

        embed.add_field(name="Team ID", value=f"`{team_record['id']}`", inline=True)
        embed.add_field(name="Region", value=f"`{team_record['region']}`", inline=True)
        embed.add_field(name="Status", value="`ACTIVE`" if team_record['is_active'] else "`DISBANDED`", inline=True)

        captain_mention = f"<@{team_record['captain_discord_id']}>"
        embed.add_field(
            name="Captain",
            value=f"{captain_mention} (`{team_record['captain_username']}`)\nIGN: **{team_record['captain_ign']}**",
            inline=True,
        )

        queue_types = [q["queue_type"] for q in queues]
        if len(queue_types) == 2:
            q_status = "**Both Regional & Global**"
        elif queue_types:
            q_status = f"**{queue_types[0].capitalize()} Queue**"
        else:
            q_status = "*Not in Queue*"
        embed.add_field(name="Matchmaking Status", value=q_status, inline=True)
        embed.add_field(name="Setup Thread", value=f"<#{team_record['thread_id']}>", inline=True)

        # Format roster
        if members:
            roster_lines = []
            for m in members:
                ign = m.get("ign") or "Unknown"
                roster_lines.append(f"• <@{m['discord_id']}> ({ign}) — **{m['role']}**")
            embed.add_field(name=f"Roster ({len(members)} members + Captain)", value="\n".join(roster_lines), inline=False)
        else:
            embed.add_field(name="Roster", value="*Only Captain on roster*", inline=False)

        created_ts = team_record.get("created_at")
        if created_ts:
            embed.set_footer(text=f"Created on {created_ts.strftime('%Y-%m-%d %H:%M UTC')}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @team_info.autocomplete("team")
    async def team_info_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_teams(interaction, current)


    @app_commands.command(
        name="help_admin",
        description="Display the administrative commands overview panel.",
    )
    async def help_admin(self, interaction: discord.Interaction) -> None:
        """Display the admin commands overview UI."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to view admin commands.", ephemeral=True)
            return

        embed = _build_admin_commands_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /test_ss_ocr and /test-ss-ocr ───────────────────────────────────────

    @app_commands.command(
        name="test_ss_ocr",
        description="Test scoreboard OCR parsing on a match end-screen screenshot.",
    )
    @app_commands.describe(
        image="The match scoreboard screenshot image attachment (PNG/JPG/WEBP)."
    )
    async def test_ss_ocr(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
    ) -> None:
        """Test OCR extraction on an uploaded match screenshot."""
        await self._handle_test_ss_ocr(interaction, image)

    @app_commands.command(
        name="test-ocr",
        description="Test scoreboard OCR parsing on a match end-screen screenshot.",
    )
    @app_commands.describe(
        image="The match scoreboard screenshot image attachment (PNG/JPG/WEBP)."
    )
    async def test_ocr_hyphen(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
    ) -> None:
        """Test OCR extraction on an uploaded match screenshot (/test-ocr)."""
        await self._handle_test_ss_ocr(interaction, image)

    @app_commands.command(
        name="test_ocr",
        description="Test scoreboard OCR parsing on a match end-screen screenshot.",
    )
    @app_commands.describe(
        image="The match scoreboard screenshot image attachment (PNG/JPG/WEBP)."
    )
    async def test_ocr_underscore(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
    ) -> None:
        """Test OCR extraction on an uploaded match screenshot (/test_ocr)."""
        await self._handle_test_ss_ocr(interaction, image)

    @app_commands.command(
        name="test-ss-ocr",
        description="Test scoreboard OCR parsing on a match end-screen screenshot.",
    )
    @app_commands.describe(
        image="The match scoreboard screenshot image attachment (PNG/JPG/WEBP)."
    )
    async def test_ss_ocr_hyphen(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
    ) -> None:
        """Test OCR extraction on an uploaded match screenshot (hyphen version)."""
        await self._handle_test_ss_ocr(interaction, image)

    async def _handle_test_ss_ocr(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
    ) -> None:
        """Internal handler for OCR testing."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        if not image.content_type or not image.content_type.startswith("image/"):
            await interaction.response.send_message("Please upload a valid image file (PNG, JPG, or WEBP).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            image_bytes = await image.read()
        except Exception as e:
            await interaction.followup.send(f"Failed to read attached image: {e}", ephemeral=True)
            return

        from utils.match_ocr import process_match_screenshot, PlayerRowStats
        result = await process_match_screenshot(image_bytes)

        if not result.success:
            await interaction.followup.send(
                f"❌ **OCR Parsing Failed**: {result.error or 'Could not detect scoreboard table.'}\n"
                f"• Engine: `{result.engine}`\n"
                f"• Time: `{result.processing_time_ms} ms`",
                ephemeral=True,
            )
            return

        # Map name lookup
        MAP_TRANSLATIONS = {
            "源工重镇": "Bind",
            "亚海悬城": "Ascent",
            "莲华古城": "Lotus",
            "深海明珠": "Pearl",
            "微风岛屿": "Breeze",
            "隐世修所": "Haven",
            "天堂": "Haven",
            "霓虹町": "Split",
            "分裂": "Split",
            "森寒冬港": "Icebox",
            "极地寒港": "Icebox",
            "冰箱": "Icebox",
            "裂变峡谷": "Fracture",
            "碎片": "Fracture",
            "日落之城": "Sunset",
            "日落": "Sunset",
            "幽邃地窟": "Abyss",
            "深渊": "Abyss",
        }
        raw_map = result.map_name or "Unknown"
        map_name = MAP_TRANSLATIONS.get(raw_map, raw_map)

        t1_score = result.team1_score or 0
        t2_score = result.team2_score or 0

        # Outcome summary & Theme color
        if t1_score > t2_score:
            outcome = "🟢 Team 1 Victory"
            sidebar_color = discord.Colour.from_rgb(46, 204, 113)
        elif t2_score > t1_score:
            outcome = "🔴 Team 2 Victory"
            sidebar_color = discord.Colour.from_rgb(235, 66, 85)
        else:
            outcome = "🤝 Match Draw"
            sidebar_color = discord.Colour.gold()

        meta_parts = []
        if result.duration and result.duration != "Unknown":
            meta_parts.append(f"⏱️ {result.duration}")
        if result.match_date and result.match_date != "Unknown":
            meta_parts.append(f"📅 {result.match_date}")
        meta_str = f" • {' • '.join(meta_parts)}" if meta_parts else ""

        # Extract MVP info for header summary:
        # 1. Match MVP is the player with the highest ACS across the entire match
        all_p = result.team1_players + result.team2_players
        m_mvp = max(all_p, key=lambda x: (x.acs, x.kills)) if all_p else None

        # 2. Team 1 MVP (我方-最佳)
        t1_mvp = next((p for p in result.team1_players if p.is_mvp), None)
        if not t1_mvp and result.team1_players:
            t1_mvp = max(result.team1_players, key=lambda x: (x.acs, x.kills))

        # 3. Team 2 MVP (敌方-最佳)
        t2_mvp = next((p for p in result.team2_players if p.is_mvp), None)
        if not t2_mvp and result.team2_players:
            t2_mvp = max(result.team2_players, key=lambda x: (x.acs, x.kills))

        mvp_lines = []
        if m_mvp:
            m_team = "Team 1" if m_mvp in result.team1_players else "Team 2"
            mvp_lines.append(f"👑 **Match MVP:** **{m_mvp.ign}** ({m_team} • `{m_mvp.acs} ACS` • `{m_mvp.kills}/{m_mvp.deaths}/{m_mvp.assists}`)")
        if t1_mvp:
            t1_extra = " *(Match MVP)*" if t1_mvp == m_mvp else ""
            mvp_lines.append(f"⭐ **Team 1 MVP (我方最佳):** **{t1_mvp.ign}** (`{t1_mvp.acs} ACS`){t1_extra}")
        if t2_mvp:
            t2_extra = " *(Match MVP)*" if t2_mvp == m_mvp else ""
            mvp_lines.append(f"⭐ **Team 2 MVP (敌方最佳):** **{t2_mvp.ign}** (`{t2_mvp.acs} ACS`){t2_extra}")

        mvp_desc = ("\n" + "\n".join(mvp_lines)) if mvp_lines else ""

        embed = discord.Embed(
            title=f"Match Results — {map_name}",
            description=(
                f"**Score:** 🟢 Team 1 **[{t1_score}]** — 🔴 Team 2 **[{t2_score}]**\n"
                f"**Outcome:** {outcome}{meta_str}{mvp_desc}"
            ),
            colour=sidebar_color,
        )

        from utils.match_ocr import get_agent_emoji

        def _fmt_player_list(players: list[PlayerRowStats]) -> str:
            if not players:
                return "*No players detected*"

            lines = []
            for p in players:
                ign = p.ign or "Unknown"
                mvp_badge = ""
                if p.mvp_type == "Match MVP" or (p.is_mvp and "match" in str(p.mvp_type).lower()):
                    mvp_badge = " 👑 `Match MVP`"
                elif p.mvp_type == "Enemy MVP" or (p.is_mvp and "enemy" in str(p.mvp_type).lower()):
                    mvp_badge = " ⭐ `Enemy MVP`"
                elif p.mvp_type == "Team MVP" or p.is_mvp:
                    mvp_badge = " ⭐ `Team MVP`"

                agent_emoji = get_agent_emoji(self.bot, p.agent, interaction.guild)
                if agent_emoji:
                    agent_prefix = f"{agent_emoji} "
                elif p.agent:
                    agent_prefix = f"`[{p.agent}]` "
                else:
                    agent_prefix = ""

                lines.append(f"{agent_prefix}**{ign}**{mvp_badge}")

                # Format clean, informative pills: `16/11/4 KDA` • `285 ACS` • `2,400 DMG` • `3 FB`
                parts = [f"`{p.kills}/{p.deaths}/{p.assists} KDA`"]
                if p.acs > 0:
                    parts.append(f"`{p.acs} ACS`")
                if p.damage > 0:
                    parts.append(f"`{p.damage:,} DMG`")
                if p.first_bloods > 0:
                    parts.append(f"`{p.first_bloods} FB`")
                if p.plants > 0:
                    parts.append(f"`{p.plants} PL`")
                if p.defuses > 0:
                    parts.append(f"`{p.defuses} DF`")

                pill_row = " • ".join(parts)
                lines.append(f"└ {pill_row}")
            return "\n".join(lines)[:1024]

        t1_header = f"🟢 Team 1 — {t1_score} Rounds" + (" 🏆" if t1_score > t2_score else "")
        t2_header = f"🔴 Team 2 — {t2_score} Rounds" + (" 🏆" if t2_score > t1_score else "")

        embed.add_field(name=t1_header, value=_fmt_player_list(result.team1_players), inline=False)
        embed.add_field(name=t2_header, value=_fmt_player_list(result.team2_players), inline=False)

        embed.set_footer(
            text="ACS: Combat Score • KDA: Kills/Deaths/Assists • DMG: Damage • FB: First Bloods • PL/DF: Plants/Defuses"
        )
        embed.set_thumbnail(url=image.url)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="sync-agent-emojis",
        description="Upload Valorant agent icon emojis from agents/ to this Discord server.",
    )
    async def sync_agent_emojis(self, interaction: discord.Interaction) -> None:
        """Upload all agent icons from agents/*.png as custom emojis to the current guild."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        from utils.ocr.agent_detector import AGENTS_DIR, clean_agent_name, get_emoji_candidate_names
        import glob
        from pathlib import Path

        if not os.path.exists(AGENTS_DIR):
            await interaction.followup.send(f"❌ Agents directory not found at `{AGENTS_DIR}`.", ephemeral=True)
            return

        existing_names = {e.name.lower() for e in interaction.guild.emojis}
        uploaded = 0
        skipped = 0
        failed = []

        agent_files = sorted(glob.glob(os.path.join(AGENTS_DIR, "*.png")))
        if not agent_files:
            await interaction.followup.send("❌ No PNG files found in `agents/` folder.", ephemeral=True)
            return

        for fpath in agent_files:
            stem = Path(fpath).stem
            canonical = clean_agent_name(stem) or stem
            candidates = get_emoji_candidate_names(canonical)
            primary_name = candidates[0] if candidates else stem

            # Check if any variant of this emoji already exists in the server
            if any(c.lower() in existing_names for c in candidates):
                skipped += 1
                continue

            try:
                import io
                from PIL import Image

                with Image.open(fpath) as pil_img:
                    pil_img = pil_img.convert("RGBA")
                    pil_img = pil_img.resize((128, 128), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    pil_img.save(buf, format="PNG", optimize=True)
                    img_bytes = buf.getvalue()

                await interaction.guild.create_custom_emoji(
                    name=primary_name,
                    image=img_bytes,
                    reason="Sync Valorant agent emojis for queue result display",
                )
                existing_names.add(primary_name.lower())
                uploaded += 1
            except Exception as exc:
                log.warning("Failed to upload agent emoji %s: %s", primary_name, exc)
                failed.append(f"{primary_name}: {exc}")

        msg = f"**Agent Emojis Sync Complete**:\n• 🆕 Uploaded: `{uploaded}`\n• ⏩ Already present: `{skipped}`"
        if failed:
            msg += f"\n• ⚠️ Failed ({len(failed)}): " + ", ".join(failed[:5])
            if len(failed) > 5:
                msg += f" ...and {len(failed) - 5} more"

        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

