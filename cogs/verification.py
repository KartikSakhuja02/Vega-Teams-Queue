"""
cogs/verification.py
--------------------
Matchmaking verification cog with automated screenshot OCR via Ollama,
interactive region selection, moderator emoji approval (✅), Matchmaking Verified
role assignment, and Server B logging.
"""

from __future__ import annotations

import asyncio
import logging
import os
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
TEAM_MOD_ROLE_IDS_RAW: str = (
    os.environ.get("TEAM_MOD_ROLE_IDS", "").strip()
    or os.environ.get("HELP_ADMIN_ROLE_IDS", "")
)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")
_SUPPORTED_IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def _parse_role_ids(raw_value: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw_value.split(","):
        cleaned = chunk.strip()
        if cleaned.isdigit():
            ids.append(int(cleaned))
    return ids


STAFF_ROLE_IDS: list[int] = _parse_role_ids(TEAM_MOD_ROLE_IDS_RAW)


def _is_staff(member: discord.Member) -> bool:
    """Check if member has moderation privileges."""
    if (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
        or member.guild_permissions.manage_roles
    ):
        return True
    return any(r.id in STAFF_ROLE_IDS for r in member.roles)


def _is_image_attachment(att: discord.Attachment) -> bool:
    ct = (att.content_type or "").lower()
    if any(ct.startswith(m) for m in _SUPPORTED_IMAGE_MIMES):
        return True
    ext = os.path.splitext(att.filename.lower())[1]
    return ext in (".png", ".jpg", ".jpeg", ".webp")


# ── UI Components ─────────────────────────────────────────────────────────────

class EditIGNModal(discord.ui.Modal, title="Correct Your In-Game Name"):
    """Modal to let the player manually correct their detected IGN."""

    ign_input = discord.ui.TextInput(
        label="In-Game Name (IGN)",
        placeholder="Enter your exact IGN (e.g. Player#TAG)",
        max_length=64,
        required=True,
    )

    def __init__(self, view: "VerificationSelectView", current_ign: str) -> None:
        super().__init__()
        self.view_ref = view
        self.ign_input.default = current_ign

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_ign = str(self.ign_input.value).strip()
        if not new_ign:
            await interaction.response.send_message("IGN cannot be empty.", ephemeral=True)
            return

        self.view_ref.ign = new_ign
        embed = self.view_ref.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.view_ref)


class VerificationRegionSelect(discord.ui.Select):
    """Dropdown for user to select their region."""

    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="India", value="India", description="India region (IST)"),
            discord.SelectOption(label="APAC", value="APAC", description="Asia-Pacific region (SGT)"),
            discord.SelectOption(label="EMEA", value="EMEA", description="Europe, Middle East, Africa (CET)"),
            discord.SelectOption(label="Americas", value="Americas", description="Americas region (EST)"),
        ]
        super().__init__(
            placeholder="Choose your region",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="verify_region_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: VerificationSelectView = self.view  # type: ignore[assignment]
        if interaction.user.id != view.player_id:
            await interaction.response.send_message(
                "Only the player who posted the screenshot can select the region.",
                ephemeral=True,
            )
            return

        region = self.values[0]
        await view.submit_verification(interaction, region)


class VerificationSelectView(discord.ui.View):
    """View attached to the bot's reply asking for region and offering IGN correction."""

    def __init__(
        self,
        cog: "VerificationCog",
        player_id: int,
        player_name: str,
        ign: str,
        orig_message_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.player_id = player_id
        self.player_name = player_name
        self.ign = ign
        self.orig_message_id = orig_message_id
        self.add_item(VerificationRegionSelect())

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Matchmaking Profile Verification",
            description=(
                f"> **Player:** <@{self.player_id}>\n"
                f"> **Detected IGN:** **`{self.ign}`**\n\n"
                "Please choose your region from the dropdown below.\n"
                "If the detected IGN is incorrect, click **Edit IGN** first."
            ),
            colour=EMBED_COLOUR,
        )
        embed.set_footer(text="Select your region to submit for moderator verification.")
        return embed

    @discord.ui.button(label="Edit IGN", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit_ign_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "Only the player who posted the screenshot can edit the IGN.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(EditIGNModal(self, self.ign))

    async def submit_verification(self, interaction: discord.Interaction, region: str) -> None:
        """Called when region is selected."""
        reply_id = interaction.message.id if interaction.message else 0
        pending_data = {
            "player_id": self.player_id,
            "player_name": self.player_name,
            "ign": self.ign,
            "region": region,
            "orig_message_id": self.orig_message_id,
            "reply_message_id": reply_id,
            "channel_id": interaction.channel_id,
            "guild_id": interaction.guild_id,
        }

        # Store pending under both message IDs so mods can react to either
        self.cog.pending_verifications[self.orig_message_id] = pending_data
        if reply_id:
            self.cog.pending_verifications[reply_id] = pending_data

        # Update message to pending status with detected IGN & Region clearly shown
        embed = discord.Embed(
            title="Matchmaking Verification — Pending Review",
            description=(
                f"> **Player:** <@{self.player_id}>\n"
                f"> **Detected IGN:** **`{self.ign}`**\n"
                f"> **Region:** **`{region}`**\n\n"
                "⏳ **Please wait for a moderator to verify.**\n"
                "A moderator will click the ✅ emoji on your screenshot to approve."
            ),
            colour=COL_WARNING,
        )
        embed.set_footer(text="Staff: React with ✅ on the screenshot message above to approve.")

        # Disable buttons
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]

        await interaction.response.edit_message(embed=embed, view=None)

        # Add tick mark emoji reaction to the player's original screenshot message
        try:
            channel = interaction.channel
            if channel:
                orig_msg = await channel.fetch_message(self.orig_message_id)
                await orig_msg.add_reaction("✅")
        except Exception as e:
            log.warning("Could not add reaction to original screenshot message %d: %s", self.orig_message_id, e)


class EnterIGNModal(discord.ui.Modal, title="Enter Your In-Game Name"):
    """Modal shown when automatic IGN detection could not find an IGN."""

    ign_input = discord.ui.TextInput(
        label="In-Game Name (IGN)",
        placeholder="Enter your exact IGN (e.g. VIP8R)",
        max_length=64,
        required=True,
    )

    def __init__(
        self,
        cog: "VerificationCog",
        player_id: int,
        player_name: str,
        orig_message_id: int,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.player_id = player_id
        self.player_name = player_name
        self.orig_message_id = orig_message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_ign = str(self.ign_input.value).strip()
        if not new_ign:
            await interaction.response.send_message("IGN cannot be empty.", ephemeral=True)
            return

        # Now that IGN is confirmed, send the UI of the IGN with region selection
        view = VerificationSelectView(
            cog=self.cog,
            player_id=self.player_id,
            player_name=self.player_name,
            ign=new_ign,
            orig_message_id=self.orig_message_id,
        )
        embed = view.build_embed()
        await interaction.response.edit_message(content=None, embed=embed, view=view)


class EnterIGNView(discord.ui.View):
    """View presented when IGN is not yet detected, with ONLY an Enter IGN button."""

    def __init__(
        self,
        cog: "VerificationCog",
        player_id: int,
        player_name: str,
        orig_message_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.player_id = player_id
        self.player_name = player_name
        self.orig_message_id = orig_message_id

    @discord.ui.button(label="Enter IGN", style=discord.ButtonStyle.primary, emoji="✏️")
    async def enter_ign_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "Only the player who posted the screenshot can enter the IGN.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            EnterIGNModal(self.cog, self.player_id, self.player_name, self.orig_message_id)
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
        # Mapping: orig_message_id -> pending verification dict
        self.pending_verifications: dict[int, dict] = {}

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

        # Once the IGN is detected, then only send the UI of the IGN with region selection.
        if detected_ign:
            view = VerificationSelectView(
                cog=self,
                player_id=message.author.id,
                player_name=str(message.author),
                ign=detected_ign,
                orig_message_id=message.id,
            )
            embed = view.build_embed()
            try:
                await status_msg.edit(content=None, embed=embed, view=view)
            except Exception as e:
                log.error("Failed to edit status message with verification view: %s", e)
        else:
            # IGN could not be automatically detected: do NOT send region UI or default to 'Player'!
            view = EnterIGNView(
                cog=self,
                player_id=message.author.id,
                player_name=str(message.author),
                orig_message_id=message.id,
            )
            embed = discord.Embed(
                title="In-Game Name Not Detected",
                description=(
                    f"> **Player:** {message.author.mention}\n\n"
                    "⚠️ **Could not automatically detect your In-Game Name from the screenshot.**\n\n"
                    "Please click **Enter IGN** below to type your in-game name, or upload a clearer profile screenshot."
                ),
                colour=COL_WARNING,
            )
            embed.set_footer(text="Once your IGN is entered, you will be prompted to select your region.")
            try:
                await status_msg.edit(content=None, embed=embed, view=view)
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
        pending = self.pending_verifications.get(message_id)
        if not pending:
            return

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
        player_id = pending["player_id"]
        player_name = pending["player_name"]
        ign = pending["ign"]
        region = pending["region"]
        reply_message_id = pending.get("reply_message_id", 0)

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

        # 1. Update/Register in database
        existing = await db.get_player(player_id)
        if existing:
            if not existing.get("is_active"):
                await db.reset_and_reactivate_player(
                    discord_id=player_id,
                    new_username=player_name,
                    new_ign=ign,
                    new_region=region,
                )
            else:
                await db.admin_update_player_ign(player_id, ign)
                await db.update_player_region(player_id, region)
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

        # 6. Cleanup pending state
        orig_id = pending.get("orig_message_id", message_id)
        self.pending_verifications.pop(orig_id, None)
        self.pending_verifications.pop(message_id, None)
        if reply_message_id:
            self.pending_verifications.pop(reply_message_id, None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VerificationCog(bot))
