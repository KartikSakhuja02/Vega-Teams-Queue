"""
cogs/verification.py
--------------------
Matchmaking verification cog with automated screenshot OCR via Ollama,
persistent interactive region selection, moderator emoji approval (✅),
Matchmaking Verified role assignment, and Server B logging.

Fully persistent UI: views never timeout, withstand bot restarts, and
state is backed by PostgreSQL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from cogs.bot_logger import send_log, COL_DEFAULT, COL_SUCCESS, COL_DANGER, COL_WARNING
from utils import ollama_client

log = logging.getLogger(__name__)

# ── Environment Config ────────────────────────────────────────────────────────
MATCHMAKING_VERIFY_CHANNEL_ID: int = int(os.environ.get("MATCHMAKING_VERIFY_CHANNEL_ID", "0") or "0")
MATCHMAKING_VERIFIED_ROLE_ID: int = int(os.environ.get("MATCHMAKING_VERIFIED_ROLE_ID", "0") or "0")
from utils.staff import is_staff, STAFF_ROLE_NAMES, get_staff_role_ids

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")
_SUPPORTED_IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")

STAFF_ROLE_IDS: list[int] = list(get_staff_role_ids())
_is_staff = is_staff


def _is_image_attachment(att: discord.Attachment) -> bool:
    ct = (att.content_type or "").lower()
    if any(ct.startswith(m) for m in _SUPPORTED_IMAGE_MIMES):
        return True
    ext = os.path.splitext(att.filename.lower())[1]
    return ext in (".png", ".jpg", ".jpeg", ".webp")


# ── Embed Builders ────────────────────────────────────────────────────────────

def build_verification_select_embed(player_id: int, ign: str) -> discord.Embed:
    embed = discord.Embed(
        title="Matchmaking Profile Verification",
        description=(
            f"> **Player:** <@{player_id}>\n"
            f"> **Detected IGN:** **`{ign}`**\n\n"
            "Please choose your region from the dropdown below.\n"
            "If the detected IGN is incorrect, click **Edit IGN** first."
        ),
        colour=EMBED_COLOUR,
    )
    embed.set_footer(text="Select your region to submit for moderator verification.")
    return embed


def build_verification_pending_embed(player_id: int, ign: str, region: str) -> discord.Embed:
    embed = discord.Embed(
        title="Matchmaking Verification — Pending Review",
        description=(
            f"> **Player:** <@{player_id}>\n"
            f"> **Detected IGN:** **`{ign}`**\n"
            f"> **Region:** **`{region}`**\n\n"
            "⏳ **Please wait for a moderator to verify.**\n"
            "A moderator will click the ✅ emoji on your screenshot to approve."
        ),
        colour=COL_WARNING,
    )
    embed.set_footer(text="Staff: React with ✅ on the screenshot message above to approve.")
    return embed


def build_verification_enter_ign_embed(player_id: int) -> discord.Embed:
    embed = discord.Embed(
        title="In-Game Name Not Detected",
        description=(
            f"> **Player:** <@{player_id}>\n\n"
            "⚠️ **Could not automatically detect your In-Game Name from the screenshot.**\n\n"
            "Please click **Enter IGN** below to type your in-game name, or upload a clearer profile screenshot."
        ),
        colour=COL_WARNING,
    )
    embed.set_footer(text="Once your IGN is entered, you will be prompted to select your region.")
    return embed


# ── Session Resolution Helper ─────────────────────────────────────────────────

async def _resolve_verification_record(interaction: discord.Interaction) -> Optional[dict]:
    """
    Fetch the verification session from PostgreSQL by message ID.
    Falls back to parsing message embed if session was created prior to DB tracking.
    """
    if not interaction.message:
        return None

    msg_id = interaction.message.id
    record = await db.get_matchmaking_verification(msg_id)
    if record:
        return record

    if interaction.message.reference and interaction.message.reference.message_id:
        ref_id = interaction.message.reference.message_id
        record = await db.get_matchmaking_verification(ref_id)
        if record:
            return record

    # Fallback: recover session from embed description
    if interaction.message.embeds:
        embed = interaction.message.embeds[0]
        desc = embed.description or ""
        player_match = re.search(r"<@!?(\d+)>", desc)
        ign_match = re.search(r"Detected IGN:\*\* \*\*`([^`]+)`\*\*", desc)
        if player_match:
            player_id = int(player_match.group(1))
            ign = ign_match.group(1).strip() if ign_match else "Player"
            orig_msg_id = (
                interaction.message.reference.message_id
                if interaction.message.reference and interaction.message.reference.message_id
                else interaction.message.id
            )
            record = await db.save_matchmaking_verification(
                orig_message_id=orig_msg_id,
                reply_message_id=interaction.message.id,
                channel_id=interaction.channel_id or 0,
                guild_id=interaction.guild_id or 0,
                player_id=player_id,
                player_name=str(interaction.user),
                ign=ign,
                status="PENDING_REGION",
            )
            return record

    return None


# ── Modals ────────────────────────────────────────────────────────────────────

class EditIGNModal(discord.ui.Modal, title="Correct Your In-Game Name"):
    """Modal to let the player manually correct their detected IGN."""

    ign_input = discord.ui.TextInput(
        label="In-Game Name (IGN)",
        placeholder="Enter your exact IGN (e.g. Player#TAG)",
        max_length=64,
        required=True,
    )

    def __init__(self, orig_message_id: int, player_id: int, current_ign: str) -> None:
        super().__init__()
        self.orig_message_id = orig_message_id
        self.player_id = player_id
        self.ign_input.default = current_ign

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_ign = str(self.ign_input.value).strip()
        if not new_ign:
            await interaction.response.send_message("IGN cannot be empty.", ephemeral=True)
            return

        # Duplicate IGN check
        conflict = await db.get_player_by_ign(new_ign)
        if conflict and conflict.get("discord_id") != self.player_id:
            await interaction.response.send_message(
                f"⚠️ The In-Game Name **`{new_ign}`** is already registered by another player (<@{conflict['discord_id']}>).\n"
                "Players with the same IGN cannot be registered. Please enter your unique in-game name.",
                ephemeral=True,
            )
            return

        await db.update_matchmaking_verification_ign(self.orig_message_id, new_ign, "PENDING_REGION")
        embed = build_verification_select_embed(self.player_id, new_ign)
        await interaction.response.edit_message(embed=embed, view=VerificationSelectView())


class EnterIGNModal(discord.ui.Modal, title="Enter Your In-Game Name"):
    """Modal shown when automatic IGN detection could not find an IGN."""

    ign_input = discord.ui.TextInput(
        label="In-Game Name (IGN)",
        placeholder="Enter your exact IGN (e.g. VIP8R)",
        max_length=64,
        required=True,
    )

    def __init__(self, orig_message_id: int, player_id: int) -> None:
        super().__init__()
        self.orig_message_id = orig_message_id
        self.player_id = player_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_ign = str(self.ign_input.value).strip()
        if not new_ign:
            await interaction.response.send_message("IGN cannot be empty.", ephemeral=True)
            return

        # Duplicate IGN check
        conflict = await db.get_player_by_ign(new_ign)
        if conflict and conflict.get("discord_id") != self.player_id:
            await interaction.response.send_message(
                f"⚠️ The In-Game Name **`{new_ign}`** is already registered by another player (<@{conflict['discord_id']}>).\n"
                "Players with the same IGN cannot be registered. Please enter your unique in-game name.",
                ephemeral=True,
            )
            return

        await db.update_matchmaking_verification_ign(self.orig_message_id, new_ign, "PENDING_REGION")
        embed = build_verification_select_embed(self.player_id, new_ign)
        await interaction.response.edit_message(content=None, embed=embed, view=VerificationSelectView())


# ── Persistent UI Views ───────────────────────────────────────────────────────

class VerificationSelectView(discord.ui.View):
    """
    Persistent view for matchmaking profile verification:
    Dropdown for region selection and Edit IGN button.
    timeout=None guarantees UI never expires or times out.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.select(
        cls=discord.ui.Select,
        placeholder="Choose your region",
        min_values=1,
        max_values=1,
        custom_id="verify_region_select",
        options=[
            discord.SelectOption(label="India", value="India", description="India region (IST)"),
            discord.SelectOption(label="APAC", value="APAC", description="Asia-Pacific region (SGT)"),
            discord.SelectOption(label="EMEA", value="EMEA", description="Europe, Middle East, Africa (CET)"),
            discord.SelectOption(label="Americas", value="Americas", description="Americas region (EST)"),
        ],
    )
    async def region_select_callback(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        record = await _resolve_verification_record(interaction)
        if not record:
            await interaction.response.send_message(
                "Could not locate this verification session. Please upload a fresh screenshot.",
                ephemeral=True,
            )
            return

        player_id = record["player_id"]
        if interaction.user.id != player_id:
            await interaction.response.send_message(
                "Only the player who posted the screenshot can select the region.",
                ephemeral=True,
            )
            return

        region = select.values[0]
        ign = record["ign"]

        # 1. Reject if player is already active
        existing = await db.get_player(player_id)
        if existing and existing.get("is_active"):
            await interaction.response.send_message(
                f"⚠️ You are already registered as **`{existing['ign']}`** in **`{existing['region']}`**.\n"
                "Duplicate registrations are not permitted. Please use `/edit-profile` if you wish to change your details.",
                ephemeral=True,
            )
            return

        # 2. Reject if IGN belongs to another player
        conflict = await db.get_player_by_ign(ign)
        if conflict and conflict.get("discord_id") != player_id:
            await interaction.response.send_message(
                f"⚠️ The In-Game Name **`{ign}`** is already registered by another player (<@{conflict['discord_id']}>).\n"
                "Players with the same IGN cannot be registered. Click **Edit IGN** to correct it.",
                ephemeral=True,
            )
            return

        # Update in database to PENDING_APPROVAL
        orig_msg_id = record["orig_message_id"]
        await db.update_matchmaking_verification_region(orig_msg_id, region, status="PENDING_APPROVAL")

        # Update message to pending status with detected IGN & Region clearly shown
        embed = build_verification_pending_embed(player_id, ign, region)
        await interaction.response.edit_message(embed=embed, view=None)

        # Add tick mark emoji reaction to the player's original screenshot message
        try:
            channel = interaction.channel
            if channel:
                orig_msg = await channel.fetch_message(orig_msg_id)
                await orig_msg.add_reaction("✅")
        except Exception as e:
            log.warning("Could not add reaction to original screenshot message %d: %s", orig_msg_id, e)

    @discord.ui.button(
        label="Edit IGN",
        style=discord.ButtonStyle.secondary,
        emoji="✏️",
        custom_id="verify_edit_ign_btn",
    )
    async def edit_ign_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        record = await _resolve_verification_record(interaction)
        if not record:
            await interaction.response.send_message(
                "Could not locate this verification session. Please upload a fresh screenshot.",
                ephemeral=True,
            )
            return

        if interaction.user.id != record["player_id"]:
            await interaction.response.send_message(
                "Only the player who posted the screenshot can edit the IGN.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            EditIGNModal(record["orig_message_id"], record["player_id"], record["ign"])
        )


class EnterIGNView(discord.ui.View):
    """
    Persistent view presented when IGN is not yet detected, with ONLY an Enter IGN button.
    timeout=None guarantees UI never expires or times out.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Enter IGN",
        style=discord.ButtonStyle.primary,
        emoji="✏️",
        custom_id="verify_enter_ign_btn",
    )
    async def enter_ign_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        record = await _resolve_verification_record(interaction)
        if not record:
            await interaction.response.send_message(
                "Could not locate this verification session. Please upload a fresh screenshot.",
                ephemeral=True,
            )
            return

        if interaction.user.id != record["player_id"]:
            await interaction.response.send_message(
                "Only the player who posted the screenshot can enter the IGN.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            EnterIGNModal(record["orig_message_id"], record["player_id"])
        )


# ── Cog Implementation ────────────────────────────────────────────────────────

class VerificationCog(commands.Cog, name="Verification"):
    """
    Handles screenshot-based player verification in Server B.
    Runs OCR on profile screenshots, prompts player for region, and registers
    them when staff reacts with ✅.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # In-memory fallback dictionary
        self.pending_verifications: dict[int, dict] = {}

    async def cog_load(self) -> None:
        """Register persistent views so buttons and select menus never timeout across restarts."""
        self.bot.add_view(VerificationSelectView())
        self.bot.add_view(EnterIGNView())

    def is_verification_channel(self, channel: discord.abc.GuildChannel | None) -> bool:
        """Check if the given channel is designated for matchmaking verification."""
        if not channel or not isinstance(channel, discord.TextChannel):
            return False

        if MATCHMAKING_VERIFY_CHANNEL_ID and channel.id == MATCHMAKING_VERIFY_CHANNEL_ID:
            return True

        # Fallback: auto-match channel name if MATCHMAKING_VERIFY_CHANNEL_ID is not configured
        if not MATCHMAKING_VERIFY_CHANNEL_ID:
            name = channel.name.lower()
            return any(
                kw in name
                for kw in (
                    "matchmaking-verification",
                    "matchmaking-verif",
                    "mm-verif",
                    "verify-profile",
                    "verification",
                )
            )

        return False

    async def _get_verified_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        """Fetch or find the Matchmaking Verified role in the guild."""
        if MATCHMAKING_VERIFIED_ROLE_ID:
            role = guild.get_role(MATCHMAKING_VERIFIED_ROLE_ID)
            if role:
                return role

        # Fallback search by name
        for r in guild.roles:
            r_name = r.name.lower().replace("-", " ").strip()
            if r_name in ("matchmaking verified", "matchmaking verify", "verified"):
                return r
        return None

    # ── Message Listener ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Listen for profile screenshots in the verification channel."""
        if message.author.bot:
            return

        if not self.is_verification_channel(message.channel):  # type: ignore[arg-type]
            return

        image_attachments = [a for a in message.attachments if _is_image_attachment(a)]
        if not image_attachments:
            return

        # 1. Prevent duplicate registration if player is already active
        existing_player = await db.get_player(message.author.id)
        if existing_player and existing_player.get("is_active"):
            embed = discord.Embed(
                title="Already Registered",
                description=(
                    f"> **Player:** {message.author.mention}\n"
                    f"> **Current IGN:** **`{existing_player['ign']}`**\n"
                    f"> **Region:** **`{existing_player['region']}`**\n\n"
                    "ℹ️ **You are already registered in the matchmaking system.**\n"
                    "Duplicate registrations are not permitted.\n\n"
                    "If you need to update your IGN or Region, please use `/edit-profile`."
                ),
                colour=COL_WARNING,
            )
            embed.set_footer(text="Vega Scrims — Duplicate registration prevented.")
            await message.reply(embed=embed, mention_author=False)
            return

        target_att = image_attachments[0]
        log.info(
            "Verification: Image received from %s (%d) in #%s",
            message.author,
            message.author.id,
            getattr(message.channel, "name", message.channel.id),
        )

        # Inform player that OCR scan is in progress
        status_msg = await message.reply(
            "🔍 Scanning profile screenshot for your In-Game Name (IGN)...",
            mention_author=False,
        )

        # Acquire GPU inference semaphore
        detected_ign: Optional[str] = None
        async with ollama_client.inference_semaphore:
            try:
                img_bytes = await target_att.read()
                detected_ign = await ollama_client.extract_profile_ign(img_bytes)
            except Exception as e:
                log.error("Error running profile OCR: %s", e)

        # Persist session to PostgreSQL
        await db.save_matchmaking_verification(
            orig_message_id=message.id,
            reply_message_id=status_msg.id,
            channel_id=message.channel.id,
            guild_id=message.guild.id if message.guild else 0,
            player_id=message.author.id,
            player_name=str(message.author),
            ign=detected_ign or "",
            status="PENDING_REGION" if detected_ign else "PENDING_IGN",
        )

        # In-memory backup
        pending_payload = {
            "player_id": message.author.id,
            "player_name": str(message.author),
            "ign": detected_ign or "",
            "region": "",
            "orig_message_id": message.id,
            "reply_message_id": status_msg.id,
            "channel_id": message.channel.id,
            "guild_id": message.guild.id if message.guild else 0,
        }
        self.pending_verifications[message.id] = pending_payload
        self.pending_verifications[status_msg.id] = pending_payload

        # Once the IGN is detected, then only send the UI of the IGN with region selection.
        if detected_ign:
            # Check if this detected IGN is already registered by another player!
            conflict = await db.get_player_by_ign(detected_ign)
            if conflict and conflict.get("discord_id") != message.author.id:
                embed = discord.Embed(
                    title="Duplicate In-Game Name Detected",
                    description=(
                        f"> **Player:** {message.author.mention}\n"
                        f"> **Detected IGN:** **`{detected_ign}`**\n\n"
                        f"⚠️ **This In-Game Name is already registered to another player (<@{conflict['discord_id']}>).**\n"
                        "Players with the same IGN cannot be registered.\n\n"
                        "If your screenshot IGN was misdetected, click **Enter IGN** below to enter your correct unique in-game name."
                    ),
                    colour=COL_DANGER,
                )
                embed.set_footer(text="Duplicate players cannot be registered.")
                try:
                    await status_msg.edit(content=None, embed=embed, view=EnterIGNView())
                except Exception as e:
                    log.error("Failed to edit status message with duplicate IGN warning: %s", e)
                return

            embed = build_verification_select_embed(message.author.id, detected_ign)
            try:
                await status_msg.edit(content=None, embed=embed, view=VerificationSelectView())
            except Exception as e:
                log.error("Failed to edit status message with verification view: %s", e)
        else:
            # IGN could not be automatically detected: send Enter IGN view
            embed = build_verification_enter_ign_embed(message.author.id)
            try:
                await status_msg.edit(content=None, embed=embed, view=EnterIGNView())
            except Exception as e:
                log.error("Failed to edit status message with enter IGN view: %s", e)

    # ── Reaction Listener ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Listen for moderator ✅ reactions to approve pending verifications."""
        # Check emoji
        emoji_name = str(payload.emoji.name)
        if emoji_name not in ("✅", "\u2705"):
            return

        # Must not be a bot reaction
        if payload.user_id == self.bot.user.id:  # type: ignore[union-attr]
            return

        message_id = payload.message_id

        # ATOMIC APPROVAL IN DATABASE: transitions status from PENDING_APPROVAL -> APPROVED
        record = await db.approve_matchmaking_verification_atomic(message_id)

        if not record:
            # Check if this verification was already approved
            existing_rec = await db.get_matchmaking_verification(message_id)
            if existing_rec and existing_rec.get("status") == "APPROVED":
                # Already approved by another staff member, safely ignore
                return

            # Check in-memory fallback
            pending = self.pending_verifications.pop(message_id, None)
            if not pending:
                return
            record = pending
            orig_id = pending.get("orig_message_id")
            reply_id = pending.get("reply_message_id")
            if orig_id:
                self.pending_verifications.pop(orig_id, None)
            if reply_id:
                self.pending_verifications.pop(reply_id, None)

        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild:
            return

        # Fetch moderator member
        member = payload.member
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return

        # Check staff authorization
        if not _is_staff(member):
            log.info("Non-staff user %s tried to approve verification message %d.", member, message_id)
            return

        # Staff approval granted — extract pending details
        player_id = record["player_id"]
        player_name = record["player_name"]
        ign = record["ign"]
        region = record.get("region") or "India"
        reply_message_id = record.get("reply_message_id", 0)

        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)  # type: ignore[assignment]
            except Exception:
                channel = None  # type: ignore[assignment]

        log.info(
            "Moderator %s approved verification for player %s (IGN: %s, Region: %s).",
            member, player_name, ign, region,
        )

        # Check if IGN was registered by someone else in the meantime
        conflict = await db.get_player_by_ign(ign)
        if conflict and conflict.get("discord_id") != player_id:
            log.warning(
                "Verification aborted for %s: IGN '%s' already registered by player %d.",
                player_name, ign, conflict["discord_id"],
            )
            if channel and reply_message_id:
                try:
                    reply_msg = await channel.fetch_message(reply_message_id)
                    abort_embed = discord.Embed(
                        title="Verification Rejected — Duplicate IGN",
                        description=(
                            f"> **Player:** <@{player_id}>\n"
                            f"> **IGN:** **`{ign}`**\n\n"
                            f"❌ **This In-Game Name is already registered by another player (<@{conflict['discord_id']}>).**\n"
                            "Duplicate registrations are not allowed."
                        ),
                        colour=COL_DANGER,
                    )
                    await reply_msg.edit(embed=abort_embed, view=None)
                except Exception:
                    pass
            return

        # 1. Update/Register in database
        existing = await db.get_player(player_id)
        if existing and existing.get("is_active"):
            log.info("Player %s (%d) is already active. Updating profile rather than duplicate registering.", player_name, player_id)
            await db.admin_update_player_ign(player_id, ign)
            await db.update_player_region(player_id, region)
        elif existing and not existing.get("is_active"):
            await db.reset_and_reactivate_player(
                discord_id=player_id,
                discord_username=player_name,
                new_ign=ign,
                new_region=region,
            )
        else:
            await db.register_player(
                discord_id=player_id,
                discord_username=player_name,
                ign=ign,
                region=region,
            )

        # 2. Assign Matchmaking Verified role
        role_assigned_str = "Matchmaking Verified"
        try:
            player_member = guild.get_member(player_id)
            if not player_member:
                player_member = await guild.fetch_member(player_id)

            if player_member:
                role = await self._get_verified_role(guild)
                if role:
                    role_assigned_str = role.mention
                    if role not in player_member.roles:
                        await player_member.add_roles(
                            role,
                            reason=f"Matchmaking verified by moderator {member} ({member.id})"
                        )
                        log.info("Granted role %s to %s.", role.name, player_member)
        except Exception as e:
            log.warning("Could not grant verified role to player %d: %s", player_id, e)

        # 3. Update the bot's reply message
        if channel and reply_message_id:
            try:
                reply_msg = await channel.fetch_message(reply_message_id)
                success_embed = discord.Embed(
                    title="Matchmaking Verification Approved",
                    description=(
                        f"> **Player:** <@{player_id}>\n"
                        f"> **IGN:** **`{ign}`**\n"
                        f"> **Region:** **`{region}`**\n"
                        f"> **Role:** {role_assigned_str}\n\n"
                        f"✅ Verified and registered by {member.mention}!"
                    ),
                    colour=COL_SUCCESS,
                )
                success_embed.set_footer(text="Profile active. You may now queue for matches.")
                await reply_msg.edit(embed=success_embed, view=None)
            except Exception as e:
                log.warning("Could not edit reply message %d: %s", reply_message_id, e)

        # 4. DM the player confirmation
        try:
            player_user = self.bot.get_user(player_id) or await self.bot.fetch_user(player_id)
            if player_user:
                dm_embed = discord.Embed(
                    title="Matchmaking Verification Approved",
                    description=(
                        "🎉 **You are now registered for the queue!**\n\n"
                        "Your profile screenshot has been verified by staff and your competitive profile is active:\n\n"
                        f"• **IGN:** `{ign}`\n"
                        f"• **Region:** `{region}`\n"
                        f"• **Starting Rating:** `1000 ELO`\n"
                        f"• **Role Granted:** Matchmaking Verified\n\n"
                        "Head to the queue channel and click **Join Queue** to start competing!"
                    ),
                    colour=COL_SUCCESS,
                )
                dm_embed.set_footer(text="Vega Competitive Queue")
                await player_user.send(embed=dm_embed)
        except Exception:
            pass  # User DMs closed

        # 5. Log event to Server B action log channel
        await send_log(
            self.bot,
            title="Player Verified & Registered",
            description=f"{member.mention} approved matchmaking verification for <@{player_id}>.",
            colour=COL_SUCCESS,
            fields=[
                ("Player", f"<@{player_id}> (`{player_id}`)", True),
                ("IGN", ign, True),
                ("Region", region, True),
                ("Moderator", f"{member.mention} (`{member.id}`)", False),
            ],
            guild_id=guild.id,
        )


async def setup(bot: commands.Bot) -> None:
    cog = VerificationCog(bot)
    await bot.add_cog(cog)
    bot.add_view(VerificationSelectView())
    bot.add_view(EnterIGNView())
