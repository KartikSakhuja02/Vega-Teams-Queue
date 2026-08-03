"""
cogs/edit_profile.py
--------------------
/edit-profile command.

Flow:
  1. User runs /edit-profile  →  ephemeral panel with buttons for each editable field.
  2. User clicks a button      →  modal dialog opens with the current value pre-filled.
  3. User submits the modal    →  confirmation view (Confirm / Cancel buttons).
  4. User clicks Confirm       →  DB updated, panel closes, success message sent,
                                   change logged to the log channel.
  5. User clicks Cancel        →  nothing changes.

Editable fields:
  • In-Game Name (IGN)
  • Region  (India | APAC | EMEA | Americas)
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from cogs.bot_logger import send_log, COL_SUCCESS, COL_WARNING

log = logging.getLogger(__name__)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")

VALID_REGIONS = ("India", "APAC", "EMEA", "Americas")


# =============================================================================
# Modals
# =============================================================================

class EditIGNModal(discord.ui.Modal, title="Edit In-Game Name"):
    new_ign: discord.ui.TextInput = discord.ui.TextInput(
        label="New In-Game Name",
        placeholder="Enter your new IGN…",
        min_length=2,
        max_length=32,
        required=True,
    )

    def __init__(self, current_ign: str) -> None:
        super().__init__()
        self.new_ign.default = current_ign

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        # Hand off to confirmation view
        view = ConfirmChangeView(
            field="IGN",
            old_value=self.new_ign.default or "",
            new_value=self.new_ign.value.strip(),
        )
        embed = _confirm_embed("IGN", self.new_ign.default or "—", self.new_ign.value.strip())
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()


class EditRegionModal(discord.ui.Modal, title="Edit Region"):
    new_region: discord.ui.TextInput = discord.ui.TextInput(
        label="New Region",
        placeholder="India | APAC | EMEA | Americas",
        min_length=2,
        max_length=16,
        required=True,
    )

    def __init__(self, current_region: str) -> None:
        super().__init__()
        self.new_region.default = current_region

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.new_region.value.strip()
        # Validate region
        if value not in VALID_REGIONS:
            await interaction.response.send_message(
                f"Invalid region `{value}`. Choose from: {', '.join(VALID_REGIONS)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        view = ConfirmChangeView(
            field="Region",
            old_value=self.new_region.default or "",
            new_value=value,
        )
        embed = _confirm_embed("Region", self.new_region.default or "—", value)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()


# =============================================================================
# Confirmation view
# =============================================================================

def _confirm_embed(field: str, old: str, new: str) -> discord.Embed:
    embed = discord.Embed(
        title="Confirm Change",
        description=f"Are you sure you want to update your **{field}**?",
        colour=EMBED_COLOUR,
    )
    embed.add_field(name="Current", value=f"`{old}`", inline=True)
    embed.add_field(name="New",     value=f"`{new}`", inline=True)
    return embed


class ConfirmChangeView(discord.ui.View):
    message: discord.Message | None = None

    def __init__(self, field: str, old_value: str, new_value: str) -> None:
        super().__init__(timeout=120)
        self.field     = field
        self.old_value = old_value
        self.new_value = new_value

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable_all(interaction)

        user = interaction.user

        # Apply the change
        updated = None
        if self.field == "IGN":
            updated = await db.update_player_ign(user.id, self.new_value)
        elif self.field == "Region":
            updated = await db.update_player_region(user.id, self.new_value)

        if not updated:
            await interaction.followup.send(
                "An error occurred while updating your profile. Please try again.",
                ephemeral=True,
            )
            return

        # Success message
        embed = discord.Embed(
            title="Profile Updated",
            description=f"Your **{self.field}** has been updated successfully.",
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Old", value=f"`{self.old_value}`", inline=True)
        embed.add_field(name="New", value=f"`{self.new_value}`", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Log to Discord log channel
        await send_log(
            interaction.client,
            title="Profile Edited",
            description=f"{user.mention} updated their **{self.field}**",
            colour=COL_SUCCESS,
            fields=[
                ("User",  f"{user} ({user.id})", True),
                ("Field", self.field,             True),
                ("Old",   self.old_value,         True),
                ("New",   self.new_value,          True),
            ],
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable_all(interaction)
        await interaction.followup.send("Change cancelled. Nothing was updated.", ephemeral=True)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                for item in self.children:
                    item.disabled = True  # type: ignore[union-attr]
                await self.message.edit(content="*Confirmation timed out.*", view=self)
            except Exception:
                pass


# =============================================================================
# Field select panel
# =============================================================================

class EditFieldView(discord.ui.View):
    """Main panel shown to the user with one button per editable field."""

    def __init__(self, profile: dict) -> None:
        super().__init__(timeout=120)
        self.profile = profile

    @discord.ui.button(label="In-Game Name (IGN)", style=discord.ButtonStyle.primary, emoji="🎮", row=0)
    async def edit_ign(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = EditIGNModal(current_ign=self.profile.get("ign", ""))
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Region", style=discord.ButtonStyle.primary, emoji="🌍", row=0)
    async def edit_region(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = EditRegionModal(current_region=self.profile.get("region", ""))
        await interaction.response.send_modal(modal)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


# =============================================================================
# Cog
# =============================================================================

class EditProfileCog(commands.Cog, name="EditProfile"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="edit-profile",
        description="Edit your registered profile details (IGN, Region).",
    )
    async def edit_profile(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        profile = await db.get_player(interaction.user.id)
        if not profile:
            await interaction.followup.send(
                "You are not registered. Use `/register` first.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Edit Profile",
            description=(
                "Select a field below to edit it.\n\n"
                f"**IGN:** `{profile['ign']}`\n"
                f"**Region:** `{profile['region']}`\n"
            ),
            colour=EMBED_COLOUR,
        )
        embed.set_footer(text="This panel expires in 2 minutes.")

        view = EditFieldView(profile=profile)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EditProfileCog(bot))
