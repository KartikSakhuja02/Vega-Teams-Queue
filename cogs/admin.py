"""
cogs/admin.py
-------------
Administrative moderation cog — /admin command group (player_ban, player_unban),
/help_admin command, and persistent admin commands overview panel in the admin channel.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from cogs.bot_logger import send_log, COL_SUCCESS, COL_DANGER

log = logging.getLogger(__name__)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")
ADMIN_COMMANDS_CHANNEL_ID: int = int(
    os.environ.get("ADMIN_COMMANDS_CHANNEL_ID", "0") or os.environ.get("ADMIN_CHANNEL_ID", "0")
)
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
            pass
    return ids


HELP_ADMIN_ROLE_IDS = _parse_admin_role_ids(HELP_ADMIN_ROLE_IDS_RAW)


def _is_admin(member: discord.Member) -> bool:
    """Check if a guild member has staff/admin permissions."""
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    return any(role.id in HELP_ADMIN_ROLE_IDS for role in member.roles)


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
        description="Comprehensive reference for staff and administrative commands.",
        colour=EMBED_COLOUR,
    )

    embed.add_field(
        name="🔨 Player Bans & Moderation",
        value=(
            "`/admin player_ban user:<@user> [duration_hours:<int>] reason:<text>`\n"
            "Ban a player from matchmaking and live queues (temporary or permanent).\n"
            "• Evicts the player from active queue/match states and logs the infraction.\n\n"
            "`/admin player_unban user:<@user>`\n"
            "Lift an active ban, clear cooldown penalties, and restore normal queue access."
        ),
        inline=False,
    )

    embed.add_field(
        name="🔍 Match Scoreboard OCR Testing",
        value=(
            "`/test_ss_ocr image:<attachment>`\n"
            "Test the high-performance OCR engine on an uploaded match end-screen screenshot.\n"
            "• Parses match score, map, duration, and all 10 players' ACS, K/D/A, DMG, FB, Plants, Defuses, and MVPs."
        ),
        inline=False,
    )

    embed.add_field(
        name="📋 Help & Staff Tools",
        value=(
            "`/help_admin`\n"
            "Display this administrative command overview panel anywhere on demand.\n\n"
            "`/player_status player:<@user>`\n"
            "Inspect any player's system state, active cooldowns, or ban details.\n\n"
            "**Ticket Management:**\n"
            "When users open `/help` tickets, staff can interact in `#help-<user>` channels or use the `[Close Ticket]` button to resolve."
        ),
        inline=False,
    )

    embed.add_field(
        name="⚙️ Audit Logging",
        value=(
            "All moderation events (bans, unbans, team renames, role transfers, etc.) are automatically streamed to the staff audit log channel."
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

    # ── /help_admin ─────────────────────────────────────────────────────────

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

    # ── /test_ss_ocr ────────────────────────────────────────────────────────

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

        embed = discord.Embed(
            title=f"🎮 Match Scoreboard OCR Results — {result.map_name}",
            description=(
                f"**Score:** 🟢 **{result.team1_score}**  vs  🔴 **{result.team2_score}** ({result.outcome})\n"
                f"**Duration:** `{result.duration}` • **Date:** `{result.match_date}`\n"
                f"**Engine:** `{result.engine}` • **Speed:** `{result.processing_time_ms} ms`"
            ),
            colour=COL_SUCCESS,
        )

        def _format_team_table(players: list[PlayerRowStats]) -> str:
            if not players:
                return "*No players detected*"
            lines = ["```", "IGN          ACS   K/D/A    DMG  FB PL DF"]
            for p in players:
                mvp_tag = "👑" if p.is_mvp else "  "
                ign_trimmed = p.ign[:10]
                lines.append(
                    f"{ign_trimmed:<10} {p.acs:>4} {p.kda_str:>8} {p.damage:>5} {p.first_bloods:>2} {p.plants:>2} {p.defuses:>2} {mvp_tag}"
                )
            lines.append("```")
            return "\n".join(lines)

        embed.add_field(
            name=f"🟢 Team 1 (Green) — Score: {result.team1_score}",
            value=_format_team_table(result.team1_players),
            inline=False,
        )

        embed.add_field(
            name=f"🔴 Team 2 (Red) — Score: {result.team2_score}",
            value=_format_team_table(result.team2_players),
            inline=False,
        )

        embed.set_footer(text=f"Vega Scrims OCR Engine • Processed in {result.processing_time_ms}ms")
        embed.set_thumbnail(url=image.url)

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

