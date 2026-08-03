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

# Optional: set GUILD_ID in .env for instant slash-command sync during development.
# Leave blank (or remove) for global sync.
_GUILD_ID: str = os.environ.get("GUILD_ID", "").strip()
GUILD: discord.Object | None = discord.Object(id=int(_GUILD_ID)) if _GUILD_ID else None

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

        # 3. Sync slash commands.
        if GUILD:
            # Guild sync is instant — useful during development.
            self.tree.copy_global_to(guild=GUILD)
            synced = await self.tree.sync(guild=GUILD)
            log.info("Synced %d slash command(s) to guild %s.", len(synced), GUILD.id)
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

    async def close(self) -> None:
        """Cleanly shut down the database pool before disconnecting."""
        await db.close_db()
        await super().close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

bot = VegaBot()

if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)  # log_handler=None defers to our custom logger
