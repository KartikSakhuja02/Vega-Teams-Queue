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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamManagementCog(bot))
