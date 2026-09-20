"""
Vega Queue Bot — main.py
Entry point for the Discord bot.
"""

import os
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import db

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()  # Reads variables from .env into os.environ
TOKEN: str = os.environ["DISCORD_BOT_TOKEN"]

# Optional: set GUILD_IDS (or GUILD_ID) in .env for instant slash-command sync.
# Can be comma-separated: GUILD_IDS=1111111111111111,2222222222222222
# Leave blank (or remove) for global sync.
_GUILD_IDS_RAW: str = os.environ.get("GUILD_IDS", "").strip() or os.environ.get("GUILD_ID", "").strip()
GUILD_IDS: list[int] = [int(g.strip()) for g in _GUILD_IDS_RAW.split(",") if g.strip().isdigit()]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # Required to read message.attachments in wait_for('message')


class VegaBot(commands.Bot):
    """Custom Bot subclass — keeps setup logic isolated and testable."""

    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        """
        Called once after login, before connecting to the gateway.
        Initialises the database pool and loads all cogs.
        """
        # 1. Connect to PostgreSQL.
        await db.init_db()

        # 2. Load feature cogs.
        await self.load_extension("cogs.bot_logger")   # must be first — logs all events
        log.info("Loaded cog: cogs.bot_logger")
        await self.load_extension("cogs.registration")
        log.info("Loaded cog: cogs.registration")
        await self.load_extension("cogs.profile")
        log.info("Loaded cog: cogs.profile")
        await self.load_extension("cogs.edit_profile")
        log.info("Loaded cog: cogs.edit_profile")
        await self.load_extension("cogs.commands_info")
        log.info("Loaded cog: cogs.commands_info")
        await self.load_extension("cogs.team_creation")
        log.info("Loaded cog: cogs.team_creation")
        await self.load_extension("cogs.team_management")
        log.info("Loaded cog: cogs.team_management")
        await self.load_extension("cogs.help_ticket")
        log.info("Loaded cog: cogs.help_ticket")
        await self.load_extension("cogs.player_status")
        log.info("Loaded cog: cogs.player_status")
        await self.load_extension("cogs.toggle_dms")
        log.info("Loaded cog: cogs.toggle_dms")
        await self.load_extension("cogs.admin")
        log.info("Loaded cog: cogs.admin")
        # cogs.vision disabled — general AI image description not needed
        await self.load_extension("cogs.team_queue")
        log.info("Loaded cog: cogs.team_queue")
        await self.load_extension("cogs.solo_queue")
        log.info("Loaded cog: cogs.solo_queue")
        await self.load_extension("cogs.verification")
        log.info("Loaded cog: cogs.verification")
        await self.load_extension("cogs.leaderboard")
        log.info("Loaded cog: cogs.leaderboard")
        try:
            await self.load_extension("cogs.test_ui")
            log.info("Loaded cog: cogs.test_ui")
        except Exception as e:
            log.warning("Could not load cogs.test_ui: %s", e)


        # 3. Sync slash commands.
        if GUILD_IDS:
            for g_id in GUILD_IDS:
                guild_obj = discord.Object(id=g_id)
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                log.info("Synced %d slash command(s) instantly to guild %d.", len(synced), g_id)
        else:
            # Global sync — can take up to an hour to propagate to all servers.
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s) globally.", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="the queue",
            )
        )

        # Sync slash commands to all connected guilds for instant permission updates (0 seconds)
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Instantly synced %d slash command(s) to guild '%s' (%d).", len(synced), guild.name, guild.id)
            except Exception as e:
                log.warning("Could not sync commands to guild %d: %s", guild.id, e)

    async def close(self) -> None:
        """Cleanly shut down the database pool before disconnecting."""
        await db.close_db()
        await super().close()


# ---------------------------------------------------------------------------
# Entry point & Management Commands
# ---------------------------------------------------------------------------

bot = VegaBot()


@bot.command(name="sync")
async def sync_prefix_cmd(ctx: commands.Context) -> None:
    """Instantly sync slash commands to this guild and clear Discord's client-side permission cache."""
    if not ctx.guild:
        return
    from utils.staff import is_staff
    if not (ctx.author.guild_permissions.administrator or is_staff(ctx.author) or ctx.guild.owner_id == ctx.author.id):
        await ctx.reply("You need Staff or Administrator permissions to sync commands.")
        return
    msg = await ctx.reply("🔄 Syncing slash commands and clearing Discord's cached permissions for this server...")
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await msg.edit(
            content=f"✅ Successfully synced **{len(synced)}** slash command(s) to **{ctx.guild.name}**!\n"
            "Discord's cached permission lock is now cleared. You can run `/clear_solo_queue` without 'Missing Permissions'."
        )
    except Exception as e:
        await msg.edit(content=f"❌ Failed to sync commands: {e}")


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)  # log_handler=None defers to our custom logger
