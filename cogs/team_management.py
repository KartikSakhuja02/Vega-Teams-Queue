"""
cogs/team_management.py
Team management cog — /invite command and interactive DM flows for joining teams.
"""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from cogs.bot_logger import send_log, COL_SUCCESS, COL_DANGER

log = logging.getLogger(__name__)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")


async def _notify_team_leadership(bot: commands.Bot, team: dict, message: str) -> None:
    """Notify the captain and all managers of a team."""
    notified_ids = set()
    
    async def _send(user_id: int):
        if user_id in notified_ids:
            return
        notified_ids.add(user_id)
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            await user.send(message)
        except Exception:
            pass
            
    # Notify captain
    await _send(team['captain_discord_id'])
    
    # Notify managers
    members = await db.get_team_members(team['id'])
    for member in members:
        if member['role'] == "Manager":
            await _send(member['discord_id'])


class InviteResponseView(discord.ui.View):
    """View sent to the invited player in DMs."""
    
    def __init__(self, bot: commands.Bot, team: dict, inviter: discord.User, target: discord.User, role: str) -> None:
        super().__init__(timeout=86400) # 24 hours timeout
        self.bot = bot
        self.team = team
        self.inviter = inviter
        self.target = target
        self.role = role
        self._done = False

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._done:
            return
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This invite is not for you.", ephemeral=True)
            return

        self._done = True
        self.stop()
        
        await interaction.response.defer()

        # Check if they joined another team in the meantime
        existing = await db.get_player_team_membership(self.target.id)
        if existing:
            await interaction.followup.send(f"You cannot accept this invite because you are already in team **{existing['team_name']}**.")
            return

        # Check if team is still active
        current_team = await db.get_team_by_captain(self.team['captain_discord_id'])
        if not current_team or current_team['id'] != self.team['id']:
            await interaction.followup.send("This team has been disbanded or is no longer active.")
            return

        # Add to database
        member = await db.add_team_member(self.team['id'], self.target.id, self.role)
        if not member:
            await interaction.followup.send("An error occurred while adding you to the team. You might already be in a team.")
            return

        # Attempt to assign Discord role if it exists by name
        assigned_role_msg = ""
        try:
            if hasattr(self.bot, 'guilds') and len(self.bot.guilds) > 0:
                # We assume the bot is mostly in one main guild for this scrim server
                guild = self.bot.guilds[0]
                member_obj = guild.get_member(self.target.id)
                if not member_obj:
                    member_obj = await guild.fetch_member(self.target.id)
                
                if member_obj:
                    # Look for a role with the exact name "Player", "Manager", or "Coach"
                    discord_role = discord.utils.get(guild.roles, name=self.role)
                    if discord_role:
                        await member_obj.add_roles(discord_role, reason=f"Joined team {self.team['team_name']} as {self.role}")
                        assigned_role_msg = f" You have also been given the **{self.role}** role in the server."
        except Exception as e:
            log.warning(f"Could not assign Discord role {self.role} to user {self.target.id}: {e}")

        # Update the original DM message
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(content=f"You accepted the invite to join **{self.team['team_name']}** as a **{self.role}**!{assigned_role_msg}", view=self, embed=None)

        # Notify leadership
        await _notify_team_leadership(
            self.bot, 
            self.team, 
            f"**{self.target.display_name}** has accepted the invite to join **{self.team['team_name']}** as a **{self.role}**."
        )

        # Log to Discord log channel
        await send_log(
            self.bot,
            title="Invite Accepted",
            description=f"**{self.target}** accepted an invite to join **{self.team['team_name']}**",
            colour=COL_SUCCESS,
            fields=[
                ("Player",  f"{self.target} ({self.target.id})", True),
                ("Team",    self.team['team_name'],               True),
                ("Role",    self.role,                            True),
                ("Invited by", str(self.inviter),                 True),
            ],
        )


    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._done:
            return
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This invite is not for you.", ephemeral=True)
            return

        self._done = True
        self.stop()
        
        await interaction.response.defer()

        # Update the original DM message
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(content=f"You declined the invite to join **{self.team['team_name']}**.", view=self, embed=None)

        # Notify leadership
        await _notify_team_leadership(
            self.bot, 
            self.team, 
            f"**{self.target.display_name}** has declined the invite to join **{self.team['team_name']}**."
        )

        # Log to Discord log channel
        await send_log(
            self.bot,
            title="Invite Declined",
            description=f"**{self.target}** declined an invite to join **{self.team['team_name']}**",
            colour=COL_DANGER,
            fields=[
                ("Player",    f"{self.target} ({self.target.id})", True),
                ("Team",      self.team['team_name'],               True),
                ("Invited by", str(self.inviter),                   True),
            ],
        )


class RoleSelectView(discord.ui.View):
    """View sent to the captain/manager to select the role for the invitee."""
    
    def __init__(self, cog: "TeamManagementCog", team: dict, target: discord.User) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.team = team
        self.target = target
        self._done = False

    async def _send_invite(self, interaction: discord.Interaction, role: str) -> None:
        if self._done:
            return
        self._done = True
        self.stop()
        
        await interaction.response.defer(ephemeral=True)
        
        embed = discord.Embed(
            title="Team Invite",
            description=f"You have been invited by **{interaction.user.display_name}** to join the team **{self.team['team_name']}** ({self.team['team_tag']}) as a **{role}**.",
            colour=EMBED_COLOUR
        )
        embed.set_footer(text="Accept or Decline below.")
        
        view = InviteResponseView(self.cog.bot, self.team, interaction.user, self.target, role)
        
        try:
            await self.target.send(embed=embed, view=view)
            await interaction.followup.send(f"Invite sent to {self.target.mention} as a **{role}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(f"Could not send a DM to {self.target.mention}. They may have DMs disabled.", ephemeral=True)


    @discord.ui.button(label="Player", style=discord.ButtonStyle.primary)
    async def player_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._send_invite(interaction, "Player")

    @discord.ui.button(label="Manager", style=discord.ButtonStyle.secondary)
    async def manager_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._send_invite(interaction, "Manager")

    @discord.ui.button(label="Coach", style=discord.ButtonStyle.secondary)
    async def coach_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._send_invite(interaction, "Coach")


REGION_OPTIONS = [
    discord.SelectOption(label="India",    value="India",    emoji="🇮🇳"),
    discord.SelectOption(label="APAC",     value="APAC",     emoji="🌏"),
    discord.SelectOption(label="EMEA",     value="EMEA",     emoji="🌍"),
    discord.SelectOption(label="Americas", value="Americas", emoji="🌎"),
]


# ---------------------------------------------------------------------------
# /team_change_region  views
# ---------------------------------------------------------------------------

class _TeamRegionConfirmView(discord.ui.View):
    """Confirm/Cancel before changing every team member's region."""

    def __init__(self, bot: commands.Bot, team: dict, new_region: str) -> None:
        super().__init__(timeout=60)
        self.bot        = bot
        self.team       = team
        self.new_region = new_region

    async def _disable(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)

        old_region = self.team["region"]

        # 1. Update team row
        updated_team = await db.update_team_region(self.team["id"], self.new_region)
        if not updated_team:
            await interaction.followup.send("Failed to update team region. Please try again.", ephemeral=True)
            return

        # 2. Bulk-update all member rows
        count = await db.bulk_update_team_members_region(self.team["id"], self.new_region)

        await interaction.followup.send(
            f"✅ Team region updated to **{self.new_region}**.\n"
            f"**{count}** team member(s) had their individual region updated too.",
            ephemeral=True,
        )

        # 3. DM every member
        members = await db.get_team_members(self.team["id"])
        for member in members:
            try:
                user = self.bot.get_user(member["discord_id"]) or await self.bot.fetch_user(member["discord_id"])
                await user.send(
                    f"Your team **{self.team['team_name']}** has moved to a new region: "
                    f"**{self.new_region}** (was `{old_region}`).\n"
                    f"Your individual player region has also been updated to **{self.new_region}**."
                )
            except Exception:
                pass

        # 4. Also DM/notify captain if they're not in team_members
        try:
            captain = self.bot.get_user(self.team["captain_discord_id"]) or \
                      await self.bot.fetch_user(self.team["captain_discord_id"])
            await captain.send(
                f"Your team **{self.team['team_name']}** has moved to a new region: "
                f"**{self.new_region}** (was `{old_region}`).\n"
                f"Your individual player region has also been updated to **{self.new_region}**."
            )
        except Exception:
            pass

        await send_log(
            self.bot,
            title="Team Region Changed",
            description=f"**{self.team['team_name']}** region changed by {interaction.user.mention}",
            colour=COL_SUCCESS,
            fields=[
                ("Team",        self.team["team_name"],                        True),
                ("Old Region",  f"`{old_region}`",                             True),
                ("New Region",  f"`{self.new_region}`",                        True),
                ("Members updated", str(count),                                True),
                ("Captain",     f"{interaction.user} ({interaction.user.id})", True),
            ],
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)
        await interaction.followup.send("Cancelled. Region was not changed.", ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


class _TeamRegionSelectView(discord.ui.View):
    """Dropdown to pick new team region."""

    def __init__(self, bot: commands.Bot, team: dict) -> None:
        super().__init__(timeout=60)
        self.bot  = bot
        self.team = team

    @discord.ui.select(
        cls=discord.ui.Select,
        placeholder="Select new region…",
        options=REGION_OPTIONS,
        min_values=1,
        max_values=1,
    )
    async def region_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        chosen = select.values[0]
        if chosen == self.team["region"]:
            await interaction.response.send_message(
                f"Your team is already in **{chosen}**. No change needed.",
                ephemeral=True,
            )
            return

        # Disable dropdown then show confirmation
        select.disabled = True
        await interaction.response.edit_message(view=self)

        members = await db.get_team_members(self.team["id"])
        member_count = len(members) + 1  # +1 for captain

        confirm_embed = discord.Embed(
            title="Confirm Region Change",
            description=(
                f"Are you sure you want to move **{self.team['team_name']}** to **{chosen}**?\n\n"
                f"⚠️ This will update the individual region of **{member_count} player(s)** in the team."
            ),
            colour=EMBED_COLOUR,
        )
        confirm_embed.add_field(name="Current Region", value=f"`{self.team['region']}`", inline=True)
        confirm_embed.add_field(name="New Region",     value=f"`{chosen}`",              inline=True)

        view = _TeamRegionConfirmView(bot=self.bot, team=self.team, new_region=chosen)
        await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# /player_change_region  views
# ---------------------------------------------------------------------------

class _PlayerRegionConfirmView(discord.ui.View):
    """Confirm/Cancel before changing a single player's region."""

    def __init__(self, bot: commands.Bot, target: discord.User, old_region: str, new_region: str) -> None:
        super().__init__(timeout=60)
        self.bot        = bot
        self.target     = target
        self.old_region = old_region
        self.new_region = new_region

    async def _disable(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)

        updated = await db.update_player_region(self.target.id, self.new_region)
        if not updated:
            await interaction.followup.send("Failed to update region. Please try again.", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ {self.target.mention}'s region updated from **{self.old_region}** → **{self.new_region}**.",
            ephemeral=True,
        )

        # DM the affected player
        try:
            await self.target.send(
                f"Your individual region has been updated to **{self.new_region}** "
                f"(was `{self.old_region}`) by a team manager/captain."
            )
        except Exception:
            pass

        await send_log(
            self.bot,
            title="Player Region Changed",
            description=f"{self.target.mention}'s region changed by {interaction.user.mention}",
            colour=COL_SUCCESS,
            fields=[
                ("Player",      f"{self.target} ({self.target.id})",           True),
                ("Old Region",  f"`{self.old_region}`",                        True),
                ("New Region",  f"`{self.new_region}`",                        True),
                ("Changed by",  f"{interaction.user} ({interaction.user.id})", True),
            ],
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)
        await interaction.followup.send("Cancelled. Region was not changed.", ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


class _PlayerRegionSelectView(discord.ui.View):
    """Dropdown to pick a player's new region."""

    def __init__(self, bot: commands.Bot, target: discord.User, current_region: str) -> None:
        super().__init__(timeout=60)
        self.bot            = bot
        self.target         = target
        self.current_region = current_region

    @discord.ui.select(
        cls=discord.ui.Select,
        placeholder="Select new region…",
        options=REGION_OPTIONS,
        min_values=1,
        max_values=1,
    )
    async def region_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        chosen = select.values[0]
        if chosen == self.current_region:
            await interaction.response.send_message(
                f"{self.target.mention} is already in **{chosen}**. No change needed.",
                ephemeral=True,
            )
            return

        select.disabled = True
        await interaction.response.edit_message(view=self)

        confirm_embed = discord.Embed(
            title="Confirm Region Change",
            description=f"Change {self.target.mention}'s region?",
            colour=EMBED_COLOUR,
        )
        confirm_embed.add_field(name="Player",         value=str(self.target),         inline=True)
        confirm_embed.add_field(name="Current Region", value=f"`{self.current_region}`", inline=True)
        confirm_embed.add_field(name="New Region",     value=f"`{chosen}`",             inline=True)

        view = _PlayerRegionConfirmView(
            bot=self.bot,
            target=self.target,
            old_region=self.current_region,
            new_region=chosen,
        )
        await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# /team_rename  views
# ---------------------------------------------------------------------------

class _TeamRenameConfirmView(discord.ui.View):
    """Confirmation view for /team_rename."""

    def __init__(self, bot: commands.Bot, team: dict, new_name: str) -> None:
        super().__init__(timeout=60)
        self.bot      = bot
        self.team     = team
        self.new_name = new_name

    async def _disable(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)

        old_name = self.team["team_name"]
        updated = await db.update_team_name(self.team["id"], self.new_name)
        if not updated:
            await interaction.followup.send(
                f"Could not rename — **{self.new_name}** is already taken by another team. "
                f"Please choose a different name.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Team renamed from **{old_name}** → **{self.new_name}**.",
            ephemeral=True,
        )

        # DM all team members about the rename
        members = await db.get_team_members(self.team["id"])
        for member in members:
            try:
                user = self.bot.get_user(member["discord_id"]) or \
                       await self.bot.fetch_user(member["discord_id"])
                await user.send(
                    f"Your team has been renamed from **{old_name}** to **{self.new_name}**."
                )
            except Exception:
                pass

        # Also notify captain
        try:
            captain = self.bot.get_user(self.team["captain_discord_id"]) or \
                      await self.bot.fetch_user(self.team["captain_discord_id"])
            await captain.send(
                f"Your team has been renamed from **{old_name}** to **{self.new_name}**."
            )
        except Exception:
            pass

        await send_log(
            self.bot,
            title="Team Renamed",
            description=f"**{old_name}** renamed to **{self.new_name}** by {interaction.user.mention}",
            colour=COL_SUCCESS,
            fields=[
                ("Old Name", old_name,                                          True),
                ("New Name", self.new_name,                                     True),
                ("Changed by", f"{interaction.user} ({interaction.user.id})",  True),
            ],
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)
        await interaction.followup.send("Cancelled. Team name was not changed.", ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


class _TagChangeConfirmView(discord.ui.View):
    """Confirmation view for /change_team_tag."""

    def __init__(self, bot: commands.Bot, team: dict, new_tag: str) -> None:
        super().__init__(timeout=60)
        self.bot     = bot
        self.team    = team
        self.new_tag = new_tag

    async def _disable(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)

        old_tag = self.team["team_tag"]
        updated = await db.update_team_tag(self.team["id"], self.new_tag)
        if not updated:
            await interaction.followup.send(
                f"Could not update the tag — **{self.new_tag}** may already be taken by another team.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Team tag updated from **{old_tag}** → **{self.new_tag}**.",
            ephemeral=True,
        )

        # Notify all team members
        members = await db.get_team_members(self.team["id"])
        for member in members:
            try:
                user = self.bot.get_user(member["discord_id"]) or await self.bot.fetch_user(member["discord_id"])
                await user.send(
                    f"Your team **{self.team['team_name']}** has a new tag: **{self.new_tag}** (was `{old_tag}`)."
                )
            except Exception:
                pass

        await send_log(
            self.bot,
            title="Team Tag Changed",
            description=f"**{self.team['team_name']}** tag changed by captain {interaction.user.mention}",
            colour=COL_SUCCESS,
            fields=[
                ("Team",    self.team["team_name"],                         True),
                ("Old Tag", f"`{old_tag}`",                                 True),
                ("New Tag", f"`{self.new_tag}`",                            True),
                ("Captain", f"{interaction.user} ({interaction.user.id})",  True),
            ],
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._disable(interaction)
        await interaction.followup.send("Cancelled. Tag was not changed.", ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


class TeamManagementCog(commands.Cog, name="TeamManagement"):
    """Handles team management commands like inviting players."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="invite",
        description="Invite a player to your team.",
    )
    @app_commands.describe(
        player="The registered player you want to invite."
    )
    async def invite(
        self,
        interaction: discord.Interaction,
        player: discord.User,
    ) -> None:
        """Invite a player to the team. Only captains and managers can use this."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        # 1. Check if caller is captain or manager
        caller_team = await db.get_team_by_captain(interaction.user.id)
        
        # If not captain, check if they are a manager in team_members
        if not caller_team:
            membership = await db.get_player_team_membership(interaction.user.id)
            if membership and membership["role"] == "Manager":
                # To get the full team dict, we'd need get_team_by_thread_id or a generic get_team_by_id
                # Wait, we don't have get_team_by_id. Let's add it or modify get_player_team_membership
                # Luckily get_player_team_membership returns team_name, team_tag, is_active.
                # But InviteResponseView expects the full team dict. 
                # Let's write a get_team_by_id in db.py to be safe.
                pass
            
            if not membership or membership["role"] != "Manager":
                await interaction.response.send_message("You must be the Captain or a Manager of a team to invite players.", ephemeral=True)
                return
        
        # We need a robust way to get the team dict. Let's use db.get_team_by_id. 
        # I'll update db.py to include this next.
        
        # 2. Check if target is registered
        target_profile = await db.get_player(player.id)
        if not target_profile:
            await interaction.response.send_message(
                f"{player.mention} is not registered in the database yet.",
                ephemeral=True,
            )
            return
            
        # 3. Check if target is already in a team
        target_team = await db.get_team_by_captain(player.id)
        if target_team:
            await interaction.response.send_message(
                f"{player.mention} is already the captain of **{target_team['team_name']}**.",
                ephemeral=True,
            )
            return
            
        target_membership = await db.get_player_team_membership(player.id)
        if target_membership:
            await interaction.response.send_message(
                f"{player.mention} is already in the team **{target_membership['team_name']}**.",
                ephemeral=True,
            )
            return
            
        # We'll resolve the caller team object fully before displaying the view
        team_id = caller_team["id"] if caller_team else membership["team_id"]
        # I need to fetch the team directly by ID
        # For now I will mock this with a query logic that I will add to db.py:
        full_team = await db.get_team_by_id(team_id)
        
        if not full_team or not full_team["is_active"]:
            await interaction.response.send_message("Your team is not active.", ephemeral=True)
            return

        # 4. Ask for role
        view = RoleSelectView(self, full_team, player)
        await interaction.response.send_message(
            f"What role should {player.mention} have in **{full_team['team_name']}**?",
            view=view,
            ephemeral=True
        )

    @app_commands.command(
        name="kick",
        description="Kick a player from your team.",
    )
    @app_commands.describe(
        player="The registered player you want to kick."
    )
    async def kick(
        self,
        interaction: discord.Interaction,
        player: discord.User,
    ) -> None:
        """Kick a player from the team. Only captains and managers can use this."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        # 1. Check if caller is captain or manager
        caller_team = await db.get_team_by_captain(interaction.user.id)
        
        # If not captain, check if they are a manager in team_members
        if not caller_team:
            membership = await db.get_player_team_membership(interaction.user.id)
            if membership and membership["role"] == "Manager":
                pass
            if not membership or membership["role"] != "Manager":
                await interaction.response.send_message("You must be the Captain or a Manager of a team to kick players.", ephemeral=True)
                return
                
        team_id = caller_team["id"] if caller_team else membership["team_id"]
        full_team = await db.get_team_by_id(team_id)
        
        if not full_team or not full_team["is_active"]:
            await interaction.response.send_message("Your team is not active.", ephemeral=True)
            return
            
        # 2. Prevent kicking the captain
        if player.id == full_team["captain_discord_id"]:
            await interaction.response.send_message("You cannot kick the captain of the team.", ephemeral=True)
            return
            
        # 3. Check if target is in the team
        target_membership = await db.get_player_team_membership(player.id)
        if not target_membership or target_membership["team_id"] != full_team["id"]:
            await interaction.response.send_message(
                f"{player.mention} is not in your team.",
                ephemeral=True,
            )
            return
            
        # 4. Remove them from the team
        await interaction.response.defer(ephemeral=True)
        removed = await db.remove_team_member(full_team["id"], player.id)
        
        if not removed:
            await interaction.followup.send("Could not remove the player. They might have already left.")
            return
            
        # 5. Remove their discord role if they have it
        try:
            if hasattr(self.bot, 'guilds') and len(self.bot.guilds) > 0:
                guild = self.bot.guilds[0]
                member_obj = guild.get_member(player.id) or await guild.fetch_member(player.id)
                if member_obj:
                    discord_role = discord.utils.get(guild.roles, name=target_membership['role'])
                    if discord_role:
                        await member_obj.remove_roles(discord_role, reason=f"Kicked from team {full_team['team_name']}")
        except Exception as e:
            log.warning(f"Could not remove Discord role {target_membership['role']} from user {player.id}: {e}")
            
        # 6. Notify the kicked player
        try:
            await player.send(f"You have been kicked from the team **{full_team['team_name']}** by **{interaction.user.display_name}**.")
        except discord.Forbidden:
            pass
            
        # 7. Notify leadership (Captain + Managers)
        await _notify_team_leadership(
            self.bot,
            full_team,
            f"**{player.display_name}** has been kicked from the team by **{interaction.user.display_name}**."
        )
        
        await interaction.followup.send(f"Successfully kicked {player.mention} from the team.")


    @app_commands.command(
        name="leave",
        description="Leave your current team.",
    )
    async def leave(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Leave your current team. Captains cannot use this; they must use /disband."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        membership = await db.get_player_team_membership(interaction.user.id)
        if not membership:
            # Check if they are a captain
            captain_team = await db.get_team_by_captain(interaction.user.id)
            if captain_team:
                await interaction.followup.send("You are the Captain of a team. You cannot leave the team; use `/disband` instead.")
            else:
                await interaction.followup.send("You are not currently in an active team.")
            return

        full_team = await db.get_team_by_id(membership["team_id"])
        if not full_team:
            await interaction.followup.send("Could not retrieve your team data.")
            return

        removed = await db.remove_team_member(full_team["id"], interaction.user.id)
        if not removed:
            await interaction.followup.send("Could not remove you from the team. You might have already left.")
            return

        # Remove their discord role if they have it
        try:
            if hasattr(self.bot, 'guilds') and len(self.bot.guilds) > 0:
                guild = self.bot.guilds[0]
                member_obj = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
                if member_obj:
                    discord_role = discord.utils.get(guild.roles, name=membership['role'])
                    if discord_role:
                        await member_obj.remove_roles(discord_role, reason=f"Left team {full_team['team_name']}")
        except Exception as e:
            log.warning(f"Could not remove Discord role {membership['role']} from user {interaction.user.id}: {e}")

        # Notify leadership (Captain + Managers)
        await _notify_team_leadership(
            self.bot,
            full_team,
            f"**{interaction.user.display_name}** has voluntarily left the team."
        )

        await interaction.followup.send(f"You have successfully left **{full_team['team_name']}**.")


    @app_commands.command(
        name="change_team_tag",
        description="Change your team's tag. Captain only.",
    )
    @app_commands.describe(new_tag="New team tag (2–6 uppercase characters, e.g. VGA).")
    async def change_team_tag(
        self,
        interaction: discord.Interaction,
        new_tag: str,
    ) -> None:
        """Let the captain change their team tag."""
        await interaction.response.defer(ephemeral=True)

        # 1. Must be a captain of an active team
        team = await db.get_team_by_captain(interaction.user.id)
        if not team:
            await interaction.followup.send(
                "You are not the Captain of an active team.",
                ephemeral=True,
            )
            return

        # 2. Validate tag format: 2–6 alphanumeric chars
        tag = new_tag.strip()
        if not (2 <= len(tag) <= 6) or not tag.isalnum():
            await interaction.followup.send(
                "Invalid tag. Tags must be **2–6 alphanumeric characters** (letters and numbers only, no spaces or symbols).",
                ephemeral=True,
            )
            return

        # 3. Check for same tag
        if tag.lower() == team["team_tag_key"]:
            await interaction.followup.send(
                f"Your team tag is already **{team['team_tag']}**. No change needed.",
                ephemeral=True,
            )
            return

        # 4. Check uniqueness
        existing = await db.get_team_by_tag_key(tag.lower())
        if existing:
            await interaction.followup.send(
                f"The tag **{tag}** is already taken by another team. Please choose a different one.",
                ephemeral=True,
            )
            return

        # 5. Confirmation view
        view = _TagChangeConfirmView(
            bot=self.bot,
            team=team,
            new_tag=tag,
        )
        embed = discord.Embed(
            title="Confirm Tag Change",
            description=f"Are you sure you want to change your team tag?",
            colour=EMBED_COLOUR,
        )
        embed.add_field(name="Current Tag", value=f"`{team['team_tag']}`", inline=True)
        embed.add_field(name="New Tag",     value=f"`{tag}`",              inline=True)
        embed.add_field(name="Team",        value=team["team_name"],        inline=False)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /team_rename ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="team_rename",
        description="Change the team's display name. Unique across the database. Captains/Managers only.",
    )
    @app_commands.describe(new_name="The new team name (2–50 characters).")
    async def team_rename(
        self,
        interaction: discord.Interaction,
        new_name: str,
    ) -> None:
        """Captain or Manager: rename the team."""
        await interaction.response.defer(ephemeral=True)

        # 1. Auth: captain or manager
        caller_team       = await db.get_team_by_captain(interaction.user.id)
        caller_membership = await db.get_player_team_membership(interaction.user.id)

        if not caller_team and (
            not caller_membership or caller_membership.get("role") not in ("Manager",)
        ):
            await interaction.followup.send(
                "You must be the Captain or a Manager of a team to use this command.",
                ephemeral=True,
            )
            return

        team_id   = caller_team["id"] if caller_team else caller_membership["team_id"]
        full_team = await db.get_team_by_id(team_id)
        if not full_team or not full_team["is_active"]:
            await interaction.followup.send("Your team is not active.", ephemeral=True)
            return

        # 2. Validate length (2–50 chars, non-empty after strip)
        name = new_name.strip()
        if not (2 <= len(name) <= 50):
            await interaction.followup.send(
                "Team names must be **2–50 characters** long.",
                ephemeral=True,
            )
            return

        # 3. Same name check
        if name.lower() == full_team["team_name_key"]:
            await interaction.followup.send(
                f"Your team is already named **{full_team['team_name']}**. No change needed.",
                ephemeral=True,
            )
            return

        # 4. Uniqueness check
        existing = await db.get_team_by_name_key(name.lower())
        if existing:
            await interaction.followup.send(
                f"The name **{name}** is already taken by another team. Please choose a different name.",
                ephemeral=True,
            )
            return

        # 5. Confirmation view
        view = _TeamRenameConfirmView(bot=self.bot, team=full_team, new_name=name)
        embed = discord.Embed(
            title="Confirm Team Rename",
            description="Are you sure you want to rename the team?",
            colour=EMBED_COLOUR,
        )
        embed.add_field(name="Current Name", value=full_team["team_name"], inline=True)
        embed.add_field(name="New Name",     value=name,                   inline=True)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /team_change_region ──────────────────────────────────────────────────

    @app_commands.command(
        name="team_change_region",
        description="Change your team's region. Changes all team members' individual regions too. Captain only.",
    )
    async def team_change_region(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Captain-only: change the entire team's region."""
        await interaction.response.defer(ephemeral=True)

        team = await db.get_team_by_captain(interaction.user.id)
        if not team:
            await interaction.followup.send(
                "You are not the Captain of an active team.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Change Team Region",
            description=(
                f"Select the new region for **{team['team_name']}**.\n\n"
                "⚠️ **This will also update the individual region of every team member.**"
            ),
            colour=EMBED_COLOUR,
        )
        embed.add_field(name="Current Region", value=f"`{team['region']}`", inline=False)
        view = _TeamRegionSelectView(bot=self.bot, team=team)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /player_change_region ────────────────────────────────────────────────

    @app_commands.command(
        name="player_change_region",
        description="Change your own region.",
    )
    async def player_change_region(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Any registered player can change their own region."""
        await interaction.response.defer(ephemeral=True)

        # Just needs to be registered
        player_record = await db.get_player(interaction.user.id)
        if not player_record or not player_record["is_active"]:
            await interaction.followup.send(
                "You are not registered. Use `/register` first.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Change Your Region",
            description="Select your new region from the dropdown below.",
            colour=EMBED_COLOUR,
        )
        embed.add_field(name="Current Region", value=f"`{player_record['region']}`", inline=False)
        view = _PlayerRegionSelectView(
            bot=self.bot,
            target=interaction.user,
            current_region=player_record["region"],
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamManagementCog(bot))
