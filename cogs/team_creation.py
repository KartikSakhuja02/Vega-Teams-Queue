"""
cogs/team_creation.py
Private team creation cog — persistent panel, setup thread, and team finalization modal.
"""

import asyncio
import logging
import os
import re
from collections.abc import Iterable
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db

log = logging.getLogger(__name__)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")
TEAM_PANEL_CHANNEL_ID: int = int(os.environ.get("TEAM_PANEL_CHANNEL_ID", "0"))
TEAM_MOD_ROLE_IDS_RAW = os.environ.get("TEAM_MOD_ROLE_IDS", "").strip() or os.environ.get("HELP_ADMIN_ROLE_IDS", "")
TEAM_PANEL_MESSAGE_CONFIG_KEY = "team_creation_message_id"


def _parse_role_ids(raw_value: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw_value.split(","):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        try:
            ids.append(int(cleaned))
        except ValueError:
            log.warning("Ignoring invalid TEAM_MOD_ROLE_IDS value: %s", cleaned)
    return ids


TEAM_MOD_ROLE_IDS = _parse_role_ids(TEAM_MOD_ROLE_IDS_RAW)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_key(value: str) -> str:
    return _normalize_text(value).casefold()


def _normalize_tag(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return cleaned


def _normalize_thread_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", _normalize_key(value))
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "team"


def _build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Vega Scrims Queue — Team Creation",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        "Create a private team setup thread from here. The captain's region is locked to the "
        "region stored in their player profile, and the team setup will stay private between "
        "the captain and the mod team."
    )
    embed.add_field(
        name="Command",
        value="`/create_team`",
        inline=False,
    )
    embed.add_field(
        name="Button",
        value="Use the button below to open a private setup thread.",
        inline=False,
    )
    embed.add_field(
        name="Setup Flow",
        value=(
            "1. Create the private thread.\n"
            "2. Open the team details form inside the thread.\n"
            "3. Submit team name, team tag, and logo URL.\n"
            "4. The team is saved with your registered region."
        ),
        inline=False,
    )
    embed.set_footer(text="Vega Scrims — Do not delete this message.")
    return embed


def _build_thread_embed(region: str, captain: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title="Private Team Setup",
        colour=EMBED_COLOUR,
    )
    embed.description = (
        "This thread is private to the captain and the mod team. Use the button below to open "
        "the team details form."
    )
    embed.add_field(name="Captain", value=captain.mention, inline=True)
    embed.add_field(name="Locked Region", value=region, inline=True)
    embed.add_field(
        name="Logo",
        value="Provide a direct image URL in the form.",
        inline=False,
    )
    return embed


def _build_final_embed(team: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Team Created",
        colour=EMBED_COLOUR,
    )
    embed.add_field(name="Team Name", value=team["team_name"], inline=True)
    embed.add_field(name="Team Tag", value=team["team_tag"], inline=True)
    embed.add_field(name="Region", value=team["region"], inline=True)
    embed.add_field(name="Captain", value=team["captain_username"], inline=False)
    if team.get("team_logo_path"):
        embed.add_field(name="Logo", value="Saved to server storage.", inline=False)
    embed.set_footer(text="Vega Scrims Team Setup")
    return embed





def _is_allowed_mod(member: discord.Member, role_ids: Iterable[int]) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
        return True
    role_id_set = set(role_ids)
    return any(role.id in role_id_set for role in member.roles)


def _collect_mod_members(guild: discord.Guild, role_ids: Iterable[int]) -> list[discord.Member]:
    members: list[discord.Member] = []
    seen_ids: set[int] = set()
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is None:
            log.warning("Configured team mod role %d could not be found in guild %s.", role_id, guild.id)
            continue
        for member in role.members:
            if member.id in seen_ids:
                continue
            seen_ids.add(member.id)
            members.append(member)
    return members


class TeamDetailsModal(discord.ui.Modal, title="Finalize Team"):
    team_name = discord.ui.TextInput(
        label="Team Name",
        placeholder="Enter the team name",
        max_length=50,
    )
    team_tag = discord.ui.TextInput(
        label="Team Tag",
        placeholder="Enter the team tag, e.g. VQ",
        max_length=12,
    )

    def __init__(self, cog: "TeamCreationCog", session: dict[str, str]) -> None:
        super().__init__()
        self.cog = cog
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.complete_team_setup(
            interaction=interaction,
            session=self.session,
            team_name=str(self.team_name.value).strip(),
            team_tag=str(self.team_tag.value).strip(),
        )


class TeamSetupView(discord.ui.View):
    def __init__(self, cog: "TeamCreationCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Enter Team Details",
        style=discord.ButtonStyle.primary,
        custom_id="team_setup_enter_details",
    )
    async def enter_details_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "This button only works inside the team setup thread.",
                ephemeral=True,
            )
            return

        session = await db.get_team_setup_session_by_thread_id(interaction.channel.id)
        if session is None:
            await interaction.response.send_message(
                "This team setup session is no longer active.",
                ephemeral=True,
            )
            return

        if interaction.user.id != session["captain_discord_id"] and not _is_allowed_mod(interaction.user, TEAM_MOD_ROLE_IDS):
            await interaction.response.send_message(
                "Only the captain or a mod can continue this setup.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(TeamDetailsModal(self.cog, session))


class TeamCreationView(discord.ui.View):
    def __init__(self, cog: "TeamCreationCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Create Team",
        style=discord.ButtonStyle.success,
        custom_id="team_creation_open",
    )
    async def create_team_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.start_team_setup(interaction)


class TeamCreationCog(commands.Cog, name="TeamCreation"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._panel_message_posted: bool = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._panel_message_posted:
            return
        self._panel_message_posted = True
        await self._ensure_panel_message()

    async def _ensure_panel_message(self) -> None:
        if not TEAM_PANEL_CHANNEL_ID:
            log.warning("TEAM_PANEL_CHANNEL_ID is not configured — skipping team panel.")
            return

        channel = self.bot.get_channel(TEAM_PANEL_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(TEAM_PANEL_CHANNEL_ID)
            except discord.HTTPException:
                log.error("Team panel channel %d not found.", TEAM_PANEL_CHANNEL_ID)
                return
            if not isinstance(fetched, discord.TextChannel):
                log.error("Channel %d is not a TextChannel.", TEAM_PANEL_CHANNEL_ID)
                return
            channel = fetched

        embed = _build_panel_embed()
        stored_id = await db.get_config(TEAM_PANEL_MESSAGE_CONFIG_KEY)
        if stored_id:
            try:
                existing_msg = await channel.fetch_message(int(stored_id))
                await existing_msg.edit(embed=embed, view=TeamCreationView(self))
                log.info("Team panel message refreshed (ID: %s).", stored_id)
                return
            except discord.NotFound:
                log.warning("Stored team panel message ID %s was deleted — sending new one.", stored_id)

        msg = await channel.send(embed=embed, view=TeamCreationView(self))
        try:
            await msg.pin()
        except discord.Forbidden:
            log.warning("Missing Manage Messages permission — could not pin team panel.")

        await db.set_config(TEAM_PANEL_MESSAGE_CONFIG_KEY, str(msg.id))
        log.info("Team panel message sent and pinned (ID: %d).", msg.id)

    @app_commands.command(
        name="create_team",
        description="Create a private team setup thread and register your team.",
    )
    async def create_team(self, interaction: discord.Interaction) -> None:
        await self.start_team_setup(interaction)

    async def start_team_setup(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        player = await db.get_player(interaction.user.id)
        if player is None:
            await interaction.followup.send(
                "You are not registered yet. Please complete player registration first.",
                ephemeral=True,
            )
            return

        existing_team = await db.get_team_by_captain(interaction.user.id)
        if existing_team is not None:
            await interaction.followup.send(
                f"You already have a team setup: **{existing_team['team_name']}**.",
                ephemeral=True,
            )
            return

        existing_session = await db.get_team_setup_session_by_captain(interaction.user.id)
        if existing_session is not None:
            # Check whether the thread still actually exists in Discord.
            # If it was deleted externally the session is orphaned — clean it up and let the
            # user create a fresh one rather than blocking them permanently.
            existing_thread = interaction.guild.get_thread(existing_session["thread_id"])
            if existing_thread is None:
                log.warning(
                    "Orphaned team setup session found for captain %d (thread %d missing) "
                    "— purging session and continuing.",
                    interaction.user.id,
                    existing_session["thread_id"],
                )
                await db.delete_team_setup_session(existing_session["thread_id"])
                # Fall through and create a new thread.
            else:
                await interaction.followup.send(
                    f"You already have an active team setup thread: {existing_thread.mention}",
                    ephemeral=True,
                )
                return

        if not TEAM_PANEL_CHANNEL_ID:
            await interaction.followup.send(
                "Team panel is not configured yet. Please contact an admin.",
                ephemeral=True,
            )
            return

        panel_channel = self.bot.get_channel(TEAM_PANEL_CHANNEL_ID)
        if not isinstance(panel_channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(TEAM_PANEL_CHANNEL_ID)
            except discord.HTTPException:
                await interaction.followup.send(
                    "Team panel channel could not be found.",
                    ephemeral=True,
                )
                return
            if not isinstance(fetched, discord.TextChannel):
                await interaction.followup.send(
                    "Team panel channel is invalid.",
                    ephemeral=True,
                )
                return
            panel_channel = fetched

        thread_name = f"team-setup-{_normalize_thread_name(interaction.user.display_name)[:32]}"
        try:
            thread = await panel_channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                reason=f"Team setup requested by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have permission to create private team threads.",
                ephemeral=True,
            )
            return

        session = await db.create_team_setup_session(
            thread_id=thread.id,
            captain_discord_id=interaction.user.id,
            captain_username=str(interaction.user),
            captain_ign=player["ign"],
            region=player["region"],
        )
        if session is None:
            await thread.delete()
            await interaction.followup.send(
                "I could not start the team setup session. Please try again.",
                ephemeral=True,
            )
            return

        participants: list[discord.Member] = [interaction.user]
        participants.extend(_collect_mod_members(interaction.guild, TEAM_MOD_ROLE_IDS))

        await asyncio.gather(
            *(thread.add_user(member) for member in participants),
            return_exceptions=True,
        )

        await thread.send(
            embed=_build_thread_embed(player["region"], interaction.user),
            view=TeamSetupView(self),
        )

        await interaction.followup.send(
            f"Your private team setup thread is ready: {thread.mention}",
            ephemeral=True,
        )

        log.info(
            "Team setup thread created — thread_id=%d captain_id=%d region=%s",
            thread.id,
            interaction.user.id,
            player["region"],
        )

    async def complete_team_setup(
        self,
        interaction: discord.Interaction,
        session: dict,
        team_name: str,
        team_tag: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "This form can only be used inside the private team setup thread.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        if interaction.user.id != session["captain_discord_id"] and not _is_allowed_mod(interaction.user, TEAM_MOD_ROLE_IDS):
            await interaction.followup.send(
                "Only the captain or a mod can finalize this team.",
                ephemeral=True,
            )
            return

        normalized_name = _normalize_text(team_name)
        normalized_name_key = _normalize_key(team_name)
        normalized_tag = _normalize_tag(team_tag)
        normalized_tag_key = normalized_tag

        if len(normalized_name) < 2:
            await interaction.followup.send(
                "Team name must be at least 2 characters long.",
                ephemeral=True,
            )
            return

        if len(normalized_tag) < 2 or len(normalized_tag) > 8:
            await interaction.followup.send(
                "Team tag must be between 2 and 8 alphanumeric characters.",
                ephemeral=True,
            )
            return

        captain_team = await db.get_team_by_captain(session["captain_discord_id"])
        if captain_team is not None:
            await db.delete_team_setup_session(interaction.channel.id)
            await interaction.followup.send(
                f"You already have a team: **{captain_team['team_name']}**.",
                ephemeral=True,
            )
            return

        name_conflict = await db.get_team_by_name_key(normalized_name_key)
        if name_conflict is not None:
            await interaction.followup.send(
                "That team name is already taken.",
                ephemeral=True,
            )
            return

        tag_conflict = await db.get_team_by_tag_key(normalized_tag_key)
        if tag_conflict is not None:
            await interaction.followup.send(
                "That team tag is already taken.",
                ephemeral=True,
            )
            return

        # Silently start waiting — the captain just sends the image in the thread.
        asyncio.create_task(
            self._await_logo_upload(
                thread=interaction.channel,
                session=session,
                team_name=normalized_name,
                team_name_key=normalized_name_key,
                team_tag=normalized_tag,
                team_tag_key=normalized_tag_key,
            )
        )

        await interaction.followup.send(
            "Team details saved. Send your logo image in this thread to complete registration.",
            ephemeral=True,
        )

    async def _await_logo_upload(
        self,
        thread: discord.Thread,
        session: dict,
        team_name: str,
        team_name_key: str,
        team_tag: str,
        team_tag_key: str,
    ) -> None:
        """Wait for the captain to post an image attachment in the setup thread."""
        captain_id: int = session["captain_discord_id"]
        _IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

        def _is_image_attachment(a: discord.Attachment) -> bool:
            if a.content_type and a.content_type.split(";")[0].strip() in _IMAGE_TYPES:
                return True
            ext = a.filename.rsplit(".", 1)[-1].lower() if "." in a.filename else ""
            return ext in {"png", "jpg", "jpeg", "gif", "webp"}

        def _check(m: discord.Message) -> bool:
            # Only accept an image from the captain themselves — not mods — so that
            # a mod watching the thread cannot accidentally finalize someone else's team.
            if m.channel.id != thread.id:
                return False
            if m.author.id != captain_id:
                return False
            return any(_is_image_attachment(a) for a in m.attachments)

        try:
            try:
                message: discord.Message = await self.bot.wait_for(
                    "message", check=_check, timeout=300.0
                )
            except asyncio.TimeoutError:
                await thread.send(
                    "Logo upload timed out after 5 minutes. Team setup has been cancelled. "
                    "Use the Create Team button again to restart."
                )
                await db.delete_team_setup_session(thread.id)
                return

            # Pick the first valid image attachment.
            attachment = next(a for a in message.attachments if _is_image_attachment(a))

            # Determine save path — use abspath so it resolves correctly regardless of CWD.
            ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else "png"
            logo_dir = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "team_logos")
            )
            os.makedirs(logo_dir, exist_ok=True)
            filename = f"{team_tag_key.lower()}_{thread.id}.{ext}"
            filepath = os.path.join(logo_dir, filename)

            try:
                await attachment.save(filepath)
                log.info("Team logo saved — path=%s", filepath)
            except Exception as exc:
                log.error("Failed to save team logo: %s", exc, exc_info=True)
                await thread.send(
                    "Could not save the logo image due to a server error. Please send it again."
                )
                return

            # Create the team record.
            team = await db.create_team(
                captain_discord_id=session["captain_discord_id"],
                captain_username=session["captain_username"],
                captain_ign=session["captain_ign"],
                team_name=team_name,
                team_name_key=team_name_key,
                team_tag=team_tag,
                team_tag_key=team_tag_key,
                region=session["region"],
                team_logo_path=filepath,
                thread_id=thread.id,
            )
            if team is None:
                await thread.send(
                    "The team could not be created because the name or tag is already taken. "
                    "Contact an admin to resolve this."
                )
                return

            await db.delete_team_setup_session(thread.id)

            # Send confirmation embed then delete the thread after a short delay.
            await thread.send(embed=_build_final_embed(team))
            await thread.send(
                f"Team **{team['team_name']}** ({team['team_tag']}) has been registered. "
                "This thread will be deleted in 10 seconds."
            )

            log.info(
                "Team created — team_id=%d captain_id=%d region=%s logo=%s",
                team["id"],
                session["captain_discord_id"],
                session["region"],
                filepath,
            )

            await asyncio.sleep(10)
            try:
                await thread.delete()
            except discord.HTTPException as exc:
                log.warning("Could not delete team setup thread %d: %s", thread.id, exc)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Catch-all: log the error and always clean up the DB session so the user
            # is not permanently locked out of creating a team.
            log.error(
                "Unhandled error in _await_logo_upload for thread %d: %s",
                thread.id,
                exc,
                exc_info=True,
            )
            try:
                await thread.send(
                    "An unexpected error occurred during team setup. "
                    "The session has been reset — please use the Create Team button to try again."
                )
            except Exception:
                pass
            try:
                await db.delete_team_setup_session(thread.id)
            except Exception:
                pass


async def setup(bot: commands.Bot) -> None:
    cog = TeamCreationCog(bot)
    await bot.add_cog(cog)
    bot.add_view(TeamCreationView(cog))
    bot.add_view(TeamSetupView(cog))