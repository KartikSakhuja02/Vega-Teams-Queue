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
import re
from datetime import datetime, timezone
from typing import Optional, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import db
from cogs.bot_logger import send_log, COL_DEFAULT, COL_SUCCESS, COL_DANGER, COL_WARNING

log = logging.getLogger(__name__)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")
ADMIN_COMMANDS_CHANNEL_ID: int = int(
    os.environ.get("ADMIN_COMMANDS_CHANNEL_ID", "0") or os.environ.get("ADMIN_CHANNEL_ID", "0")
)
from utils.staff import is_staff, _is_admin, STAFF_ROLE_NAMES, get_staff_role_ids



BAN_ESCALATION_TIERS: dict[int, tuple[Optional[int], str]] = {
    1: (1, "1 hour"),
    2: (6, "6 hours"),
    3: (12, "12 hours"),
    4: (24, "24 hours"),
    5: (168, "7 days"),
    6: (720, "30 days"),
}


def get_escalated_ban_duration(next_ban_number: int) -> tuple[Optional[int], str]:
    """Returns (duration_hours, display_label) based on the ban tier (1-indexed)."""
    if next_ban_number in BAN_ESCALATION_TIERS:
        return BAN_ESCALATION_TIERS[next_ban_number]
    return (None, "Permanent")


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
            "`/admin player_ban user:<@user> reason:<text> [duration_hours:<int>] [permanent:bool]`\n"
            "Ban a player (auto-escalates: 1h -> 6h -> 12h -> 24h -> 7d -> 30d -> Permanent).\n\n"
            "`/admin player_unban user:<@user>`\n"
            "Lift an active ban, clear cooldown penalties, and restore normal queue access.\n\n"
            "`/admin check_bans user:<@user>`\n"
            "View a player's ban count history, status, and next escalation duration.\n\n"
            "`/admin set_bans user:<@user> count:<int>`\n"
            "Manually set a player's ban count to any specific number (e.g. 1, 2, 3).\n\n"
            "`/admin clear_bans user:<@user> [amount:<int>]`\n"
            "Clear/reduce a player's recorded ban count (resets to 0 if amount is omitted).\n\n"
            "`/admin blacklist words action:<add|remove|list|clear> [word:<text>] [reason:<text>]`\n"
            "Auto-ban players for abusive words in queue text channels (auto-escalates by ban count)."
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


def _get_queue_ban_channel_id() -> int:
    """Retrieve the designated Discord channel ID for matchmaking ban announcements."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    raw = (
        os.environ.get("QUEUE_BAN_CHANNEL_ID")
        or os.environ.get("BAN_CHANNEL_ID")
        or os.environ.get("BAN_LOG_CHANNEL_ID")
        or os.environ.get("QUEUE_BAN_LOG_CHANNEL_ID")
        or "0"
    )
    try:
        clean = raw.strip().split(",")[0].strip()
        return int(clean)
    except (ValueError, IndexError):
        return 0


def _build_queue_ban_embed(
    user: discord.User,
    player_record: dict,
    reason: str,
    duration_hours: Optional[int],
    banned_until_dt: Optional[datetime],
    banned_at_dt: Optional[datetime],
    admin: discord.User | discord.Member,
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    """Build an announcement embed for a banned player."""
    embed = discord.Embed(
        title="🔨 Matchmaking Ban Enacted",
        description=(
            f"A queue ban has been issued for {user.mention}.\n"
            f"Access to 10-man matchmaking, live queues, and team scrims has been revoked."
        ),
        colour=discord.Colour.from_str("#FF4655"),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    embed.add_field(
        name="👤 Banned Player",
        value=user.mention,
        inline=False,
    )

    now_ts = int(datetime.now(timezone.utc).timestamp())
    banned_at_ts = int(banned_at_dt.timestamp()) if isinstance(banned_at_dt, datetime) else now_ts

    if isinstance(banned_until_dt, datetime):
        banned_until_ts = int(banned_until_dt.timestamp())
        dur_label = _fmt_duration(duration_hours) if duration_hours else "Temporary"
        time_display = (
            f"• **Duration:** `{dur_label}`\n"
            f"• **Time Remaining:** <t:{banned_until_ts}:R>\n"
            f"• **Ban Expiration:** <t:{banned_until_ts}:F>"
        )
    else:
        time_display = (
            f"• **Duration:** `Permanent`\n"
            f"• **Time Remaining:** `Indefinite (Never)`\n"
            f"• **Ban Expiration:** `Never`"
        )

    embed.add_field(
        name="⏳ Ban Duration & Time",
        value=time_display,
        inline=False,
    )

    embed.add_field(
        name="📝 Reason",
        value=f"```{reason.strip()}```",
        inline=False,
    )

    icon_url = guild.icon.url if guild and guild.icon else None
    embed.set_footer(text="Vega Esports • Queue Moderation System", icon_url=icon_url)
    return embed


def _build_queue_unban_embed(
    user: discord.User,
    player_record: dict,
    admin: discord.User | discord.Member,
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    """Build a minimal announcement embed when a player's ban is lifted."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    embed = discord.Embed(
        title="🔓 Queue Ban Lifted",
        description=f"{user.mention}\nLifted: <t:{now_ts}:f>",
        colour=COL_SUCCESS,
        timestamp=datetime.now(timezone.utc),
    )
    icon_url = guild.icon.url if guild and guild.icon else None
    embed.set_footer(text="Vega Esports • Queue Moderation System", icon_url=icon_url)
    return embed


def _build_queue_expired_unban_embed(
    user_or_id: discord.User | discord.Member | int,
    player_record: dict,
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    """Build a minimal announcement embed when a player's temporary ban expires automatically."""
    mention = user_or_id.mention if hasattr(user_or_id, "mention") else f"<@{user_or_id}>"
    now_ts = int(datetime.now(timezone.utc).timestamp())

    embed = discord.Embed(
        title="🔓 Queue Ban Expired",
        description=f"{mention}\nExpired: <t:{now_ts}:f>",
        colour=COL_SUCCESS,
        timestamp=datetime.now(timezone.utc),
    )
    icon_url = guild.icon.url if guild and guild.icon else None
    embed.set_footer(text="Vega Esports • Queue Moderation System", icon_url=icon_url)
    return embed



class AdminCog(commands.Cog, name="Admin"):
    """Handles staff administration, moderation commands, and the admin command center."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._panel_posted: bool = False
        self._blacklisted_words: dict[str, dict] = {}
        self.check_expired_bans.start()
        self.check_expired_point_buffs.start()

    def cog_unload(self) -> None:
        self.check_expired_bans.cancel()
        self.check_expired_point_buffs.cancel()

    admin_group = app_commands.Group(
        name="admin",
        description="Administrative moderation and management commands.",
    )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._load_blacklisted_words()
        if self._panel_posted:
            return
        self._panel_posted = True
        await self._ensure_admin_commands_message()

    async def _load_blacklisted_words(self) -> None:
        """Load active blacklisted words from the database into in-memory cache."""
        try:
            words = await db.get_blacklisted_words()
            self._blacklisted_words = {w["word"].lower(): w for w in words}
            log.info("Loaded %d blacklisted words for queue auto-moderation.", len(self._blacklisted_words))
        except Exception as e:
            log.warning("Could not load blacklisted words from database: %s", e)

    async def _is_queue_text_channel(self, channel: discord.abc.GuildChannel) -> bool:
        """
        Check if a channel is a queue text channel (only in q text channels).
        Matches active 10-man solo match channels, scrim channels, and queue lobby channels.
        """
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False

        # 1. Configured Queue Channel IDs from .env
        solo_q_id = int(os.environ.get("SOLO_QUEUE_CHANNEL_ID", "0"))
        team_q_id = int(os.environ.get("TEAM_QUEUE_CHANNEL_ID", "0"))
        if channel.id in (solo_q_id, team_q_id) and channel.id != 0:
            return True

        # 2. Match Categories (SOLO_MATCH_CATEGORY_ID or SCRIM_CATEGORY_ID)
        solo_cat_id = int(os.environ.get("SOLO_MATCH_CATEGORY_ID", "0"))
        scrim_cat_id = int(os.environ.get("SCRIM_CATEGORY_ID", "0"))
        if channel.category_id and channel.category_id in (solo_cat_id, scrim_cat_id) and channel.category_id != 0:
            return True
        if channel.category and any(kw in channel.category.name.lower() for kw in ("queue #", "queue-", "solo match", "scrim")):
            return True

        # 3. Channel Name Patterns
        ch_name = channel.name.lower()
        if ch_name.startswith("queue-") or ch_name.startswith("q-") or ch_name == "queue" or ch_name == "solo-queue" or ch_name.startswith("scrim-"):
            return True

        # 4. Channel Topic Patterns
        topic = getattr(channel, "topic", None)
        if topic:
            topic_lower = topic.lower()
            if "10-man solo ranked queue" in topic_lower or "scrim match" in topic_lower or "queue #" in topic_lower:
                return True

        # 5. Database lookup for active solo or scrim matches
        try:
            solo_match = await db.get_solo_match_by_channel(channel.id)
            if solo_match:
                return True
            scrim_match = await db.get_scrim_match_by_channel(channel.id)
            if scrim_match:
                return True
        except Exception:
            pass

        return False

    async def _execute_blacklisted_word_auto_ban(
        self,
        message: discord.Message,
        matched_word: str,
        matched_entry: dict,
    ) -> None:
        """
        Auto-bans a user who used a blacklisted word in a queue text channel.
        - Deletes the abusive message immediately
        - Determines escalation duration from previous ban count
        - Bans the player in the database
        - Evicts them from active queues
        - Sends DM with infraction & duration
        - Announces ban in the designated bans channel
        - Logs to the bot audit log channel
        - Posts notification in the queue text channel
        """
        member = message.author
        if not isinstance(member, discord.Member):
            return
        channel = message.channel
        guild = message.guild

        # 1. Immediately delete message
        try:
            await message.delete()
        except Exception as e:
            log.debug("Could not delete blacklisted message: %s", e)

        # 2. Get player record to check ban tier
        player_record = await db.get_player(member.id)
        if not player_record:
            try:
                player_record = await db.register_player(
                    discord_id=member.id,
                    discord_username=member.name,
                    ign=member.display_name[:32],
                    region="India",
                )
            except Exception as e:
                log.warning("Could not auto-register player %d for ban tracking: %s", member.id, e)

        prev_ban_count = player_record.get("ban_count") or 0 if player_record else 0
        next_ban_tier = prev_ban_count + 1

        # 3. Determine ban duration (auto-escalation)
        effective_duration, dur_label = get_escalated_ban_duration(next_ban_tier)
        dur_text = f"`{_fmt_duration(effective_duration)}`" if effective_duration else "`Permanent`"

        # 4. Reason from blacklisted word entry
        word_reason = (matched_entry.get("reason") or "").strip()
        if word_reason:
            ban_reason = f"Used a blacklisted word ({word_reason})"
        else:
            ban_reason = "Used a blacklisted word"

        # 5. Apply ban in database
        bot_user_id = self.bot.user.id if self.bot.user else 0
        updated = await db.ban_player(
            discord_id=member.id,
            reason=ban_reason,
            banned_by=bot_user_id,
            duration_hours=effective_duration,
        )

        # 6. Evict from queues
        try:
            await db.clear_solo_queue([member.id])
            await self._refresh_solo_queue()
            await self._refresh_team_queue()
        except Exception as e:
            log.debug("Error during queue eviction for auto-banned user %d: %s", member.id, e)

        banned_until_dt = updated.get("banned_until") if updated else None
        banned_at_dt = updated.get("banned_at") if updated else datetime.now(timezone.utc)
        current_ban_count = updated.get("ban_count", next_ban_tier) if updated else next_ban_tier

        # 7. DM to banned user
        if isinstance(banned_until_dt, datetime):
            banned_until_ts = int(banned_until_dt.timestamp())
            dm_dur_str = f"{dur_text} (Expires: <t:{banned_until_ts}:F> • <t:{banned_until_ts}:R>)"
        else:
            dm_dur_str = "`Permanent`"

        try:
            dm_embed = discord.Embed(
                title="🔨 Account Banned from Matchmaking",
                description=(
                    f"You have been banned from Vega Scrims matchmaking queues for prohibited/abusive language in {channel.mention}.\n\n"
                    f"• **Reason:** {ban_reason}\n"
                    f"• **Duration:** {dm_dur_str}\n"
                    f"• **Ban Tier:** #{current_ban_count}\n\n"
                    "If you believe this is an error or wish to appeal, please contact server staff in help tickets."
                ),
                colour=COL_DANGER,
            )
            dm_embed.set_footer(text="Vega Scrims Auto-Moderation")
            await member.send(embed=dm_embed)
        except Exception:
            log.info("Could not send ban DM to user %d (DMs closed).", member.id)

        # 8. Announce in the bans channel
        try:
            ban_embed = _build_queue_ban_embed(
                user=member,
                player_record=player_record or {"ign": member.display_name},
                reason=ban_reason,
                duration_hours=effective_duration,
                banned_until_dt=banned_until_dt,
                banned_at_dt=banned_at_dt,
                admin=self.bot.user,
                guild=guild,
            )
            await self._send_ban_channel_ui(guild, ban_embed)
        except Exception as e:
            log.error("Failed to announce ban in bans channel for %d: %s", member.id, e)

        # 9. Audit log
        banned_at_ts = int(banned_at_dt.timestamp()) if isinstance(banned_at_dt, datetime) else int(datetime.now(timezone.utc).timestamp())
        banned_until_ts = int(banned_until_dt.timestamp()) if isinstance(banned_until_dt, datetime) else None
        desc_parts = [
            f"**User:** {member.mention} (`{member.id}`)",
            f"**IGN:** `{player_record.get('ign') if player_record else member.display_name}`",
            f"**Channel:** {channel.mention} (`#{channel.name}`)",
            f"**Ban Tier:** #{current_ban_count}",
            f"**Duration:** {dur_text}",
        ]
        if banned_until_ts:
            desc_parts.append(f"**Expires:** <t:{banned_until_ts}:F> (<t:{banned_until_ts}:R>)")
        else:
            desc_parts.append("**Expires:** `Never (Permanent)`")
        if word_reason:
            desc_parts.append(f"**Configured Reason:** `{word_reason}`")
        desc_parts.append(f"**Recorded Ban Reason:** `{ban_reason}`")

        await send_log(
            self.bot,
            title="🔨 Auto-Ban: Blacklisted Word in Queue Channel",
            description="\n".join(desc_parts),
            colour=COL_DANGER,
        )

        # 10. Notify in the queue text channel
        try:
            warn_embed = discord.Embed(
                title="🔨 Player Auto-Banned",
                description=(
                    f"{member.mention} has been auto-banned for prohibited/abusive language.\n"
                    f"• **Ban Tier:** #{current_ban_count} ({dur_text})\n"
                    f"• **Reason:** {word_reason or 'Blacklisted word violation'}"
                ),
                colour=COL_DANGER,
            )
            warn_embed.set_footer(text="Vega Scrims Auto-Moderation")
            await channel.send(embed=warn_embed)
        except Exception as e:
            log.warning("Could not send alert in queue channel: %s", e)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Auto-moderation listener that monitors queue text channels for blacklisted words."""
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return

        # Ensure blacklist cache is populated
        if not self._blacklisted_words:
            return

        # Check if the channel is a queue text channel (only in q text channels)
        if not await self._is_queue_text_channel(message.channel):
            return

        # Check message content against blacklisted words using regex word boundary
        content = message.content
        if not content:
            return

        matched_word = None
        matched_entry = None

        for w_key, w_data in list(self._blacklisted_words.items()):
            pattern = r'(?<!\w)' + re.escape(w_key) + r'(?!\w)'
            if re.search(pattern, content, re.IGNORECASE):
                matched_word = w_data.get("word") or w_key
                matched_entry = w_data
                break

        if matched_word and matched_entry:
            await self._execute_blacklisted_word_auto_ban(message, matched_word, matched_entry)


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

    # ── Helpers for Queue & Moderation Channels ─────────────────────────────

    async def _send_ban_channel_ui(
        self,
        guild: Optional[discord.Guild],
        embed: discord.Embed,
    ) -> Optional[discord.Message]:
        """Send the ban/unban UI card to the channel configured in .env."""
        ch_id = _get_queue_ban_channel_id()
        if not ch_id:
            return None

        channel = guild.get_channel(ch_id) if guild else None
        if channel is None:
            channel = self.bot.get_channel(ch_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(ch_id)
            except Exception as e:
                log.warning("Could not fetch queue ban channel %d: %s", ch_id, e)
                return None

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                msg = await channel.send(embed=embed)
                return msg
            except Exception as e:
                log.error("Failed to post ban announcement to channel %d: %s", ch_id, e)
        return None

    # ── Expired Bans Background Worker ──────────────────────────────────────

    @tasks.loop(seconds=30)
    async def check_expired_bans(self) -> None:
        """Background worker that periodically detects and clears expired matchmaking bans."""
        try:
            expired_players = await db.expire_pending_bans()
            if not expired_players:
                return

            for p in expired_players:
                user_id = p["discord_id"]
                log.info("Ban expired for player %d (%s) — sending unban notification.", user_id, p.get("ign"))

                # Try resolving the user/member
                user: Optional[discord.User] = self.bot.get_user(user_id)
                if not user:
                    try:
                        user = await self.bot.fetch_user(user_id)
                    except Exception:
                        user = None

                # Find relevant guild for guild icon if available
                guild: Optional[discord.Guild] = None
                ch_id = _get_queue_ban_channel_id()
                if ch_id:
                    ch = self.bot.get_channel(ch_id)
                    if isinstance(ch, (discord.TextChannel, discord.Thread)):
                        guild = ch.guild
                if not guild and self.bot.guilds:
                    guild = self.bot.guilds[0]

                target_entity = user if user else user_id

                # Post real-time unban notification to the same configured ban channel
                unban_embed = _build_queue_expired_unban_embed(
                    user_or_id=target_entity,
                    player_record=p,
                    guild=guild,
                )
                await self._send_ban_channel_ui(guild, unban_embed)

                # Send DM to player if possible
                if user:
                    try:
                        dm_embed = discord.Embed(
                            title="🔓 Matchmaking Ban Expired",
                            description=(
                                "Your temporary matchmaking ban on Vega Scrims has expired.\n"
                                "Your queue and matchmaking access has been fully restored. Welcome back!"
                            ),
                            colour=COL_SUCCESS,
                        )
                        dm_embed.set_footer(text="Vega Scrims Moderation")
                        await user.send(embed=dm_embed)
                    except Exception:
                        pass

                # Audit Log
                user_str = user.mention if user else f"<@{user_id}>"
                fields = [
                    ("Player", f"{user_str} (`{user_id}`)", True),
                    ("IGN", p.get("ign", "N/A"), True),
                    ("Status", "Ban Expired Automatically", True),
                    ("Original Reason", p.get("ban_reason") or "N/A", False),
                ]
                await send_log(
                    self.bot,
                    title="🔓 Ban Expired Automatically",
                    description=f"Matchmaking ban for {user_str} has expired. Queue access restored.",
                    colour=COL_SUCCESS,
                    fields=fields,
                )
        except Exception as e:
            log.exception("Error in check_expired_bans task: %s", e)

    @check_expired_bans.before_loop
    async def before_check_expired_bans(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh_solo_queue(self) -> None:
        """Helper to notify SoloQueueCog to refresh its persistent channel message."""
        solo_queue_cog = self.bot.get_cog("SoloQueueCog") or self.bot.get_cog("SoloQueue")
        if solo_queue_cog and hasattr(solo_queue_cog, "refresh_queue_message"):
            try:
                await solo_queue_cog.refresh_queue_message()
            except Exception as e:
                log.warning("Could not refresh solo queue message: %s", e)

    async def _refresh_team_queue(self) -> None:
        """Helper to notify TeamQueueCog to refresh its persistent channel message."""
        team_queue_cog = self.bot.get_cog("TeamQueue")
        if team_queue_cog and hasattr(team_queue_cog, "refresh_queue_message"):
            try:
                await team_queue_cog.refresh_queue_message()
            except Exception as e:
                log.warning("Could not refresh team queue message: %s", e)

    # ── Ban & Unban Handlers ────────────────────────────────────────────────

    async def _handle_player_ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str,
        duration_hours: Optional[int] = None,
        permanent: Optional[bool] = False,
    ) -> None:
        """Core logic for banning a player and posting the real-time UI card."""
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

        # 4. Determine ban duration & auto-escalation tier
        prev_ban_count = player_record.get("ban_count") or 0
        next_ban_tier = prev_ban_count + 1

        if permanent:
            effective_duration = None
        elif duration_hours is not None and duration_hours > 0:
            effective_duration = duration_hours
        else:
            effective_duration, _ = get_escalated_ban_duration(next_ban_tier)

        # 5. Apply ban in database
        updated = await db.ban_player(
            discord_id=user.id,
            reason=reason.strip(),
            banned_by=interaction.user.id,
            duration_hours=effective_duration,
        )
        if not updated:
            await interaction.followup.send("Failed to ban player due to a database error.", ephemeral=True)
            return

        # Evict player from active queues immediately
        try:
            await db.clear_solo_queue([user.id])
            await self._refresh_solo_queue()
            await self._refresh_team_queue()
        except Exception as e:
            log.debug("Error during queue eviction for banned user %d: %s", user.id, e)

        dur_text = f"`{_fmt_duration(effective_duration)}`" if effective_duration else "`Permanent`"
        banned_until_dt = updated.get("banned_until")
        banned_at_dt = updated.get("banned_at")
        current_ban_count = updated.get("ban_count", next_ban_tier)

        if isinstance(banned_until_dt, datetime):
            banned_until_ts = int(banned_until_dt.timestamp())
            dm_dur_str = f"{dur_text} (Expires: <t:{banned_until_ts}:F> • <t:{banned_until_ts}:R>)"
        else:
            dm_dur_str = "`Permanent`"

        # 6. Send DM to banned user
        try:
            dm_embed = discord.Embed(
                title="🔨 Account Banned from Matchmaking",
                description=(
                    f"You have been banned from Vega Scrims matchmaking queues.\n\n"
                    f"**Reason:** {reason.strip()}\n"
                    f"**Duration:** {dm_dur_str}\n\n"
                    "If you believe this is an error or wish to appeal, please contact server staff."
                ),
                colour=COL_DANGER,
            )
            dm_embed.set_footer(text="Vega Scrims Moderation")
            await user.send(embed=dm_embed)
        except Exception:
            log.info("Could not send ban DM to user %d (DMs may be closed).", user.id)

        # 7. Post real-time changing UI card to designated ban channel
        ban_embed = _build_queue_ban_embed(
            user=user,
            player_record=player_record,
            reason=reason,
            duration_hours=effective_duration,
            banned_until_dt=banned_until_dt,
            banned_at_dt=banned_at_dt,
            admin=interaction.user,
            guild=interaction.guild,
        )
        ban_msg = await self._send_ban_channel_ui(interaction.guild, ban_embed)

        # 8. Audit Log
        banned_at_ts = int(banned_at_dt.timestamp()) if isinstance(banned_at_dt, datetime) else None
        banned_until_ts = int(banned_until_dt.timestamp()) if isinstance(banned_until_dt, datetime) else None
        desc_parts = [f"{user.mention}"]
        desc_parts.append(f"Ban Count: #{current_ban_count}")
        if banned_at_ts:
            desc_parts.append(f"Issued: <t:{banned_at_ts}:F> (<t:{banned_at_ts}:R>)")
        if banned_until_ts:
            desc_parts.append(f"Expires: <t:{banned_until_ts}:F> (<t:{banned_until_ts}:R>)")
        else:
            desc_parts.append("Duration: Permanent")
        if reason:
            desc_parts.append(f"Reason: `{reason.strip()}`")
        await send_log(
            self.bot,
            title="🔨 Player Banned",
            description="\n".join(desc_parts),
            colour=COL_DANGER,
        )

        channel_note = f"\n• **UI Announcement Channel:** {ban_msg.channel.mention}" if ban_msg else ""
        await interaction.followup.send(
            f"✅ Successfully banned {user.mention} ({player_record.get('ign')}).\n"
            f"• **Ban Count:** #{current_ban_count}\n"
            f"• **Duration:** {dur_text}\n"
            f"• **Reason:** {reason.strip()}{channel_note}",
            ephemeral=True,
        )

    async def _handle_check_bans(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        """Check a player's ban history count and current status."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        player_record = await db.get_player(user.id)
        if not player_record:
            await interaction.followup.send(f"{user.mention} is not registered in the database.", ephemeral=True)
            return

        ban_count = player_record.get("ban_count") or 0
        is_banned, ban_reason, banned_until, _ = await db.get_player_ban_status(user.id)

        next_tier = ban_count + 1
        _, next_dur_label = get_escalated_ban_duration(next_tier)

        status_str = "🔴 Currently Banned" if is_banned else "🟢 Active (Not Banned)"
        
        embed = discord.Embed(
            title=f"📊 Ban Records for {user.display_name}",
            colour=COL_DANGER if is_banned else COL_SUCCESS,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Player", value=f"{user.mention} (`{player_record.get('ign', 'N/A')}`)", inline=False)
        embed.add_field(name="Ban Status", value=status_str, inline=True)
        embed.add_field(name="Total Bans Received", value=f"`{ban_count}`", inline=True)
        embed.add_field(name=f"Next Ban Tier (#{next_tier})", value=f"`{next_dur_label}`", inline=False)
        if is_banned:
            if banned_until:
                ts = int(banned_until.timestamp())
                embed.add_field(name="Current Ban Expires", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
            else:
                embed.add_field(name="Current Ban Duration", value="`Permanent`", inline=False)
            if ban_reason:
                embed.add_field(name="Current Reason", value=f"```{ban_reason}```", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_clear_bans(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: Optional[int] = None,
    ) -> None:
        """Clear or reduce a player's ban history count."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        player_record = await db.get_player(user.id)
        if not player_record:
            await interaction.followup.send(f"{user.mention} is not registered in the database.", ephemeral=True)
            return

        old_count = player_record.get("ban_count") or 0
        if old_count == 0:
            await interaction.followup.send(f"{user.mention} currently has 0 recorded bans.", ephemeral=True)
            return

        updated = await db.clear_player_bans(user.id, amount)
        new_count = updated.get("ban_count", 0) if updated else 0

        next_tier = new_count + 1
        _, next_dur_label = get_escalated_ban_duration(next_tier)

        if amount is not None and amount > 0:
            msg = (
                f"✅ Cleared `{amount}` ban(s) for {user.mention}.\n"
                f"• **Previous Ban Count:** `{old_count}`\n"
                f"• **New Ban Count:** `{new_count}`\n"
                f"• **Next Ban Tier (#{next_tier}):** `{next_dur_label}`"
            )
        else:
            msg = (
                f"✅ Cleared **all** ban records for {user.mention}.\n"
                f"• **Previous Ban Count:** `{old_count}`\n"
                f"• **New Ban Count:** `0`\n"
                f"• **Next Ban Tier (#1):** `1 hour`"
            )

        await send_log(
            self.bot,
            title="🧹 Ban History Cleared",
            description=(
                f"Staff {interaction.user.mention} cleared ban history for {user.mention}.\n"
                f"Bans: `{old_count}` ➔ `{new_count}`"
            ),
            colour=COL_SUCCESS,
        )

        await interaction.followup.send(msg, ephemeral=True)

    async def _handle_set_bans(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        count: int,
    ) -> None:
        """Manually set a player's ban history count."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        if count < 0:
            await interaction.response.send_message("Ban count must be 0 or greater.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        player_record = await db.get_player(user.id)
        if not player_record:
            await interaction.followup.send(f"{user.mention} is not registered in the database.", ephemeral=True)
            return

        old_count = player_record.get("ban_count") or 0
        updated = await db.set_player_ban_count(user.id, count)
        new_count = updated.get("ban_count", count) if updated else count

        next_tier = new_count + 1
        _, next_dur_label = get_escalated_ban_duration(next_tier)

        msg = (
            f"✅ Ban count for {user.mention} set to `{new_count}` (was `{old_count}`).\n"
            f"• **Next Ban Tier (#{next_tier}):** `{next_dur_label}`"
        )

        await send_log(
            self.bot,
            title="⚙️ Ban Count Set",
            description=(
                f"Staff {interaction.user.mention} updated ban count for {user.mention}.\n"
                f"Bans: `{old_count}` ➔ `{new_count}`"
            ),
            colour=COL_SUCCESS,
        )

        await interaction.followup.send(msg, ephemeral=True)

    async def _handle_player_unban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        """Core logic for unbanning a player and posting the lift notice."""
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

        # 5. Post to the designated ban channel
        unban_embed = _build_queue_unban_embed(
            user=user,
            player_record=player_record,
            admin=interaction.user,
            guild=interaction.guild,
        )
        unban_msg = await self._send_ban_channel_ui(interaction.guild, unban_embed)

        # 6. Audit Log
        await send_log(
            self.bot,
            title="🔓 Player Unbanned",
            description=f"{user.mention} ban lifted — queue access restored.",
            colour=COL_SUCCESS,
        )

        channel_note = f" Posted notice to {unban_msg.channel.mention}." if unban_msg else ""
        await interaction.followup.send(
            f"✅ Successfully unbanned {user.mention} ({player_record.get('ign')}). Queue access has been restored.{channel_note}",
            ephemeral=True,
        )

    # ── Blacklist Words Action Handler ──────────────────────────────────────

    async def _handle_blacklist_action(
        self,
        interaction: discord.Interaction,
        action: str,
        word: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Central router for blacklisted words moderation commands."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not _is_admin(interaction.user):
            await interaction.response.send_message("You do not have permission to use admin commands.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        action_clean = (action or "").lower().strip()

        if action_clean == "add":
            if not word or not word.strip():
                await interaction.followup.send("❌ Please specify the word or phrase to blacklist.", ephemeral=True)
                return
            clean_word = word.strip().lower()
            clean_reason = reason.strip() if reason and reason.strip() else None

            row = await db.add_blacklisted_word(
                word=clean_word,
                reason=clean_reason,
                added_by=interaction.user.id,
            )
            if not row:
                await interaction.followup.send("❌ Failed to save blacklisted word to database.", ephemeral=True)
                return

            self._blacklisted_words[clean_word] = dict(row)

            reason_disp = clean_reason if clean_reason else "None (Default auto-ban)"
            await send_log(
                self.bot,
                title="🛡️ Blacklisted Word Added",
                description=(
                    f"**Admin:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Word:** `||{clean_word}||`\n"
                    f"**Reason:** `{reason_disp}`\n"
                    f"**Target Channels:** Queue text channels only"
                ),
                colour=COL_SUCCESS,
            )

            await interaction.followup.send(
                f"✅ **Blacklisted Word Added**\n"
                f"• **Word:** `||{clean_word}||`\n"
                f"• **Reason:** `{reason_disp}`\n\n"
                f"Players using this word in **queue text channels** will be automatically banned according to their ban count tier, with notices sent to the bans channel.",
                ephemeral=True,
            )

        elif action_clean == "remove":
            if not word or not word.strip():
                await interaction.followup.send("❌ Please specify the word or phrase to remove from the blacklist.", ephemeral=True)
                return
            clean_word = word.strip().lower()
            removed = await db.remove_blacklisted_word(clean_word)
            self._blacklisted_words.pop(clean_word, None)

            if not removed:
                await interaction.followup.send(f"⚠️ Word `||{clean_word}||` was not found in the blacklist.", ephemeral=True)
                return

            await send_log(
                self.bot,
                title="🛡️ Blacklisted Word Removed",
                description=(
                    f"**Admin:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Removed Word:** `||{clean_word}||`"
                ),
                colour=COL_WARNING,
            )

            await interaction.followup.send(f"✅ Removed `||{clean_word}||` from blacklisted words.", ephemeral=True)

        elif action_clean == "list":
            words = await db.get_blacklisted_words()
            self._blacklisted_words = {w["word"].lower(): w for w in words}

            if not words:
                await interaction.followup.send(
                    "ℹ️ There are currently no blacklisted words configured.\n"
                    "Use `/admin blacklist words action:add word:<word> [reason:<text>]` to add one.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="🛡️ Queue Blacklisted Words & Moderation Rules",
                description=(
                    f"Total active blacklisted words: **{len(words)}**\n"
                    f"Monitored Channels: **Queue text channels only** (`queue-*`, scrims, solo queue)\n"
                    f"Enforcement: **Auto-escalating bans** (#1: 1h -> #2: 6h -> #3: 12h -> #4: 24h -> #5: 7d -> #6: 30d -> Permanent)"
                ),
                colour=EMBED_COLOUR,
            )

            lines = []
            for idx, w in enumerate(words[:25], start=1):
                reason_txt = f" *(Reason: {w['reason']})*" if w.get("reason") else ""
                lines.append(f"`{idx}.` `||{w['word']}||`{reason_txt}")

            embed.add_field(name="Blacklisted Words", value="\n".join(lines), inline=False)
            if len(words) > 25:
                embed.set_footer(text=f"Showing 25 of {len(words)} blacklisted words.")
            else:
                embed.set_footer(text="Vega Esports • Queue Auto-Moderation")

            await interaction.followup.send(embed=embed, ephemeral=True)

        elif action_clean == "clear":
            count = await db.clear_blacklisted_words()
            self._blacklisted_words.clear()

            await send_log(
                self.bot,
                title="🛡️ Blacklist Cleared",
                description=(
                    f"**Admin:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Words Cleared:** `{count}`"
                ),
                colour=COL_DANGER,
            )

            await interaction.followup.send(f"✅ Cleared all `{count}` blacklisted words from the database.", ephemeral=True)

        else:
            await interaction.followup.send(
                f"❌ Unknown action `{action}`. Valid options: `add`, `remove`, `list`, or `clear`.",
                ephemeral=True,
            )


    # ── Slash Commands (/admin player_ban & /admin_player_ban) ──────────────

    # ── Slash Commands (/admin player_ban & /admin_player_ban) ──────────────

    @admin_group.command(
        name="player_ban",
        description="Ban a player from matchmaking and live queues (auto-escalating or custom).",
    )
    @app_commands.describe(
        user="The player to ban from matchmaking.",
        reason="The infraction reason for this ban.",
        duration_hours="Optional manual ban duration in hours (leave empty for auto escalation).",
        permanent="Set to True to issue an explicit permanent ban.",
    )
    async def player_ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str,
        duration_hours: Optional[int] = None,
        permanent: Optional[bool] = False,
    ) -> None:
        """Ban a player from queues and matches."""
        await self._handle_player_ban(interaction, user, reason, duration_hours, permanent)

    @app_commands.command(
        name="admin_player_ban",
        description="Ban a player from matchmaking and live queues (auto-escalating or custom).",
    )
    @app_commands.describe(
        user="The player to ban from matchmaking.",
        reason="The infraction reason for this ban.",
        duration_hours="Optional manual ban duration in hours (leave empty for auto escalation).",
        permanent="Set to True to issue an explicit permanent ban.",
    )
    async def admin_player_ban_command(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str,
        duration_hours: Optional[int] = None,
        permanent: Optional[bool] = False,
    ) -> None:
        """Top-level command alias for /admin player_ban."""
        await self._handle_player_ban(interaction, user, reason, duration_hours, permanent)

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
        await self._handle_player_unban(interaction, user)

    @app_commands.command(
        name="admin_player_unban",
        description="Unban a player and restore queue access.",
    )
    @app_commands.describe(
        user="The player to unban.",
    )
    async def admin_player_unban_command(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        """Top-level command alias for /admin player_unban."""
        await self._handle_player_unban(interaction, user)

    @admin_group.command(
        name="check_bans",
        description="Check a player's ban history count, status, and next escalation tier.",
    )
    @app_commands.describe(
        user="The player to inspect.",
    )
    async def check_bans(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        """Check a player's ban records."""
        await self._handle_check_bans(interaction, user)

    @app_commands.command(
        name="admin_check_bans",
        description="Check a player's ban history count, status, and next escalation tier.",
    )
    @app_commands.describe(
        user="The player to inspect.",
    )
    async def admin_check_bans_command(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        """Top-level command alias for /admin check_bans."""
        await self._handle_check_bans(interaction, user)

    @admin_group.command(
        name="clear_bans",
        description="Clear or reduce a player's recorded ban count.",
    )
    @app_commands.describe(
        user="The player whose ban count to clear/reduce.",
        amount="Optional number of bans to clear (leave empty to clear all).",
    )
    async def clear_bans(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: Optional[int] = None,
    ) -> None:
        """Clear a player's ban count."""
        await self._handle_clear_bans(interaction, user, amount)

    @app_commands.command(
        name="admin_clear_bans",
        description="Clear or reduce a player's recorded ban count.",
    )
    @app_commands.describe(
        user="The player whose ban count to clear/reduce.",
        amount="Optional number of bans to clear (leave empty to clear all).",
    )
    async def admin_clear_bans_command(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: Optional[int] = None,
    ) -> None:
        """Top-level command alias for /admin clear_bans."""
        await self._handle_clear_bans(interaction, user, amount)

    @admin_group.command(
        name="set_bans",
        description="Set a player's recorded ban count to a specific number.",
    )
    @app_commands.describe(
        user="The player whose ban count to set.",
        count="The new ban count (e.g. 1, 2, 3).",
    )
    async def set_bans(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        count: int,
    ) -> None:
        """Set a player's ban count."""
        await self._handle_set_bans(interaction, user, count)

    @app_commands.command(
        name="admin_set_bans",
        description="Set a player's recorded ban count to a specific number.",
    )
    @app_commands.describe(
        user="The player whose ban count to set.",
        count="The new ban count (e.g. 1, 2, 3).",
    )
    async def admin_set_bans_command(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        count: int,
    ) -> None:
        """Top-level command alias for /admin set_bans."""
        await self._handle_set_bans(interaction, user, count)

    # ── Slash Commands (/admin blacklist ...) ───────────────────────────────

    blacklist_group = app_commands.Group(
        name="blacklist",
        description="Manage blacklisted words and moderation rules for queue channels.",
        parent=admin_group,
    )

    @blacklist_group.command(
        name="words",
        description="Manage blacklisted words for queue channels (add, remove, list, clear).",
    )
    @app_commands.describe(
        action="Choose action: add, remove, list, or clear.",
        word="The word or phrase to add or remove.",
        reason="Infraction reason for bot logs and ban card (e.g. Abusive Language, Slurs).",
    )
    async def blacklist_words_cmd(
        self,
        interaction: discord.Interaction,
        action: Literal["add", "remove", "list", "clear"],
        word: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Command /admin blacklist words."""
        await self._handle_blacklist_action(interaction, action, word, reason)

    @blacklist_group.command(
        name="add",
        description="Add a word or phrase to the auto-ban blacklist with optional reason.",
    )
    @app_commands.describe(
        word="The word or phrase to blacklist.",
        reason="Infraction reason recorded in the bot logs and ban announcements.",
    )
    async def blacklist_add_cmd(
        self,
        interaction: discord.Interaction,
        word: str,
        reason: Optional[str] = None,
    ) -> None:
        """Command /admin blacklist add."""
        await self._handle_blacklist_action(interaction, "add", word, reason)

    @blacklist_group.command(
        name="remove",
        description="Remove a word or phrase from the blacklist.",
    )
    @app_commands.describe(
        word="The word or phrase to remove.",
    )
    async def blacklist_remove_cmd(
        self,
        interaction: discord.Interaction,
        word: str,
    ) -> None:
        """Command /admin blacklist remove."""
        await self._handle_blacklist_action(interaction, "remove", word, None)

    @blacklist_group.command(
        name="list",
        description="List all currently blacklisted words and their configured reasons.",
    )
    async def blacklist_list_cmd(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Command /admin blacklist list."""
        await self._handle_blacklist_action(interaction, "list", None, None)

    @blacklist_group.command(
        name="clear",
        description="Clear all blacklisted words from the database.",
    )
    async def blacklist_clear_cmd(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Command /admin blacklist clear."""
        await self._handle_blacklist_action(interaction, "clear", None, None)

    @admin_group.command(
        name="blacklist_words",
        description="Manage blacklisted words for queue auto-moderation.",
    )
    @app_commands.describe(
        action="Choose action: add, remove, list, or clear.",
        word="The word or phrase to add or remove.",
        reason="Infraction reason for bot logs and ban card (e.g. Abusive Language, Slurs).",
    )
    async def admin_blacklist_words_cmd(
        self,
        interaction: discord.Interaction,
        action: Literal["add", "remove", "list", "clear"],
        word: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Top-level command alias /admin blacklist_words."""
        await self._handle_blacklist_action(interaction, action, word, reason)

    @app_commands.command(
        name="admin_blacklist_words",
        description="Manage blacklisted words for queue auto-moderation.",
    )
    @app_commands.describe(
        action="Choose action: add, remove, list, or clear.",
        word="The word or phrase to add or remove.",
        reason="Infraction reason for bot logs and ban card (e.g. Abusive Language, Slurs).",
    )
    async def top_level_admin_blacklist_words(
        self,
        interaction: discord.Interaction,
        action: Literal["add", "remove", "list", "clear"],
        word: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Global command alias /admin_blacklist_words."""
        await self._handle_blacklist_action(interaction, action, word, reason)

    async def _autocomplete_blacklisted_words(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current_lower = current.lower()
        choices = []
        for word in self._blacklisted_words.keys():
            if current_lower in word.lower():
                choices.append(app_commands.Choice(name=word[:100], value=word[:100]))
                if len(choices) >= 25:
                    break
        return choices

    @blacklist_words_cmd.autocomplete("word")
    async def blacklist_words_word_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_blacklisted_words(interaction, current)

    @blacklist_remove_cmd.autocomplete("word")
    async def blacklist_remove_word_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_blacklisted_words(interaction, current)

    @admin_blacklist_words_cmd.autocomplete("word")
    async def admin_blacklist_words_word_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_blacklisted_words(interaction, current)

    @top_level_admin_blacklist_words.autocomplete("word")
    async def top_level_blacklist_word_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_blacklisted_words(interaction, current)


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
            "Summit": "Summit",
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


    # ── Point Buff Event Management (/admin queue point_buff ...) ───────────

    @tasks.loop(minutes=1.0)
    async def check_expired_point_buffs(self) -> None:
        """Periodically check if an active point buff event has expired and clean up announcement."""
        try:
            pct_str = await db.get_config("point_buff_pct")
            until_str = await db.get_config("point_buff_until")
            if not pct_str or not until_str:
                return

            end_time = datetime.fromisoformat(until_str)
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            if now >= end_time:
                log.info("Point buff event of +%s%% has expired.", pct_str)
                await db.clear_point_buff()
                await self._update_buff_announcement_ended(float(pct_str))
        except Exception as e:
            log.warning("Error checking expired point buffs: %s", e)

    @check_expired_point_buffs.before_loop
    async def _before_check_expired_point_buffs(self) -> None:
        await self.bot.wait_until_ready()

    async def _update_buff_announcement_ended(self, pct: float) -> None:
        """Update the buff announcement embed to show that the event has ended."""
        try:
            msg_id_str = await db.get_config("point_buff_msg_id")
            ch_id_str = await db.get_config("point_buff_ch_id")
            await db.delete_config("point_buff_msg_id")
            await db.delete_config("point_buff_ch_id")

            if not ch_id_str:
                ch_env = os.environ.get("POINT_BUFF_CHANNEL_ID", "0")
                if ch_env != "0":
                    ch_id_str = ch_env

            if not ch_id_str:
                return

            ch = self.bot.get_channel(int(ch_id_str))
            if not ch and hasattr(self.bot, "fetch_channel"):
                try:
                    ch = await self.bot.fetch_channel(int(ch_id_str))
                except Exception:
                    ch = None

            if isinstance(ch, discord.TextChannel) and msg_id_str:
                try:
                    msg = await ch.fetch_message(int(msg_id_str))
                    embed = discord.Embed(
                        title="❌ QUEUE POINTS BUFF EVENT ENDED",
                        description=f"The **+{pct:g}% Points Buff** event has concluded!\nThank you to everyone who participated.",
                        colour=discord.Colour(0x7F8C8D),
                    )
                    embed.set_footer(text="Vega Queue Events • Event Ended")
                    await msg.edit(content="📢 **EVENT CONCLUDED**", embed=embed)
                except Exception as e:
                    log.warning("Could not edit ended buff announcement message: %s", e)
        except Exception as e:
            log.warning("Failed updating buff announcement on expiry: %s", e)

    admin_queue_group = app_commands.Group(
        name="queue",
        description="Queue event and administration commands.",
        parent=admin_group,
    )

    async def _handle_point_buff(
        self,
        interaction: discord.Interaction,
        amount: float,
        hours: float,
    ) -> None:
        if not (is_staff(interaction.user) or (interaction.user.guild_permissions and interaction.user.guild_permissions.administrator)):
            await interaction.response.send_message(
                "❌ Only staff members and admins can activate point buffs.",
                ephemeral=True,
            )
            return

        if amount <= 0:
            await interaction.response.send_message(
                "❌ Buff amount must be greater than 0%.",
                ephemeral=True,
            )
            return

        if hours <= 0:
            await interaction.response.send_message(
                "❌ Duration hours must be greater than 0.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        end_time = await db.set_point_buff(amount, hours)
        end_unix = int(end_time.timestamp())

        ch_id_str = os.environ.get("POINT_BUFF_CHANNEL_ID", "0")
        announcement_sent = False
        ch_mention = "configured channel"

        if ch_id_str and ch_id_str != "0":
            try:
                ch_id = int(ch_id_str)
                ch = interaction.guild.get_channel(ch_id) if interaction.guild else None
                if not ch:
                    ch = self.bot.get_channel(ch_id)
                if not ch and hasattr(self.bot, "fetch_channel"):
                    try:
                        ch = await self.bot.fetch_channel(ch_id)
                    except Exception:
                        ch = None

                if isinstance(ch, discord.TextChannel):
                    ch_mention = ch.mention
                    embed = discord.Embed(
                        title="🔥 QUEUE POINTS BUFF ACTIVATED!",
                        description=(
                            f"⚡ **+{amount:g}% Extra Points / ELO** is now active for all queue match winners!\n\n"
                            f"⏰ **Event Duration:** `{hours:g} Hours`\n"
                            f"⏳ **Event Ends:** <t:{end_unix}:R> (<t:{end_unix}:F>)\n\n"
                            f"Play ranked queue matches during this event to earn boosted score points!"
                        ),
                        colour=discord.Colour(0xFF6B00),
                    )
                    embed.set_footer(text="Vega Queue Events • Active Event")
                    if interaction.guild and interaction.guild.icon:
                        embed.set_thumbnail(url=interaction.guild.icon.url)

                    msg = await ch.send(content="🎉 @everyone **NEW EVENT ACTIVATED!**", embed=embed)
                    await db.set_config("point_buff_msg_id", str(msg.id))
                    await db.set_config("point_buff_ch_id", str(ch.id))
                    announcement_sent = True
            except Exception as e:
                log.warning("Could not post point buff announcement: %s", e)

        ch_info = f"in {ch_mention}" if announcement_sent else f"(Channel ID `{ch_id_str}` unreachable or not found)."
        await interaction.followup.send(
            f"✅ **Points Buff Activated!**\n"
            f"• **Bonus:** `+{amount:g}%`\n"
            f"• **Duration:** `{hours:g} hours` (Ends <t:{end_unix}:R>)\n"
            f"• **Announcement:** {ch_info}",
            ephemeral=True,
        )

    async def _handle_stop_point_buff(self, interaction: discord.Interaction) -> None:
        if not (is_staff(interaction.user) or (interaction.user.guild_permissions and interaction.user.guild_permissions.administrator)):
            await interaction.response.send_message(
                "❌ Only staff members and admins can stop point buffs.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        pct, _ = await db.get_active_point_buff()

        if pct <= 0:
            await interaction.followup.send(
                "ℹ️ There is currently no active points buff event.",
                ephemeral=True,
            )
            return

        await db.clear_point_buff()
        await self._update_buff_announcement_ended(pct)

        await interaction.followup.send(
            "✅ **Points Buff Stopped.** Active percentage buff has been canceled.",
            ephemeral=True,
        )

    async def _handle_point_buff_status(self, interaction: discord.Interaction) -> None:
        pct, end_time = await db.get_active_point_buff()
        if pct > 0 and end_time:
            end_unix = int(end_time.timestamp())
            await interaction.response.send_message(
                f"🔥 **Points Buff Status: ACTIVE**\n"
                f"• **Bonus:** `+{pct:g}%` extra points on match wins\n"
                f"• **Event Ends:** <t:{end_unix}:R> (<t:{end_unix}:F>)",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "ℹ️ **Points Buff Status: INACTIVE**\nNo point buff event is currently active.",
                ephemeral=True,
            )

    @admin_queue_group.command(
        name="point_buff",
        description="Activate a points/ELO buff event for queue match winners.",
    )
    @app_commands.describe(
        amount="Percentage buff amount (e.g. 20 for +20% bonus points).",
        hours="Duration of the buff event in hours (e.g. 2 or 2.5).",
    )
    async def queue_point_buff_cmd(
        self,
        interaction: discord.Interaction,
        amount: float,
        hours: float,
    ) -> None:
        """Slash command /admin queue point_buff <amount> <hours>."""
        await self._handle_point_buff(interaction, amount, hours)

    @admin_queue_group.command(
        name="stop_point_buff",
        description="Stop/cancel the active queue points buff event immediately.",
    )
    async def queue_stop_point_buff_cmd(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Slash command /admin queue stop_point_buff."""
        await self._handle_stop_point_buff(interaction)

    @admin_queue_group.command(
        name="point_buff_status",
        description="Check current active queue points buff event status.",
    )
    async def queue_point_buff_status_cmd(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Slash command /admin queue point_buff_status."""
        await self._handle_point_buff_status(interaction)

    @admin_group.command(
        name="point_buff",
        description="Activate a points/ELO buff event for queue match winners.",
    )
    @app_commands.describe(
        amount="Percentage buff amount (e.g. 20 for +20% bonus points).",
        hours="Duration of the buff event in hours (e.g. 2 or 2.5).",
    )
    async def admin_point_buff_cmd(
        self,
        interaction: discord.Interaction,
        amount: float,
        hours: float,
    ) -> None:
        """Top-level command alias /admin point_buff <amount> <hours>."""
        await self._handle_point_buff(interaction, amount, hours)

    @admin_group.command(
        name="stop_point_buff",
        description="Stop/cancel the active queue points buff event immediately.",
    )
    async def admin_stop_point_buff_cmd(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Top-level command alias /admin stop_point_buff."""
        await self._handle_stop_point_buff(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

