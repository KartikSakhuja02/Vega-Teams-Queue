"""
Vega Queue Bot — main.py
Entry point for the Discord bot.
"""

import os
import logging

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()  # Reads variables from .env into os.environ
TOKEN: str = os.environ["DISCORD_BOT_TOKEN"]

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
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()

class VegaBot(commands.Bot):
    """Custom Bot subclass — keeps setup logic isolated and testable."""

    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        """Called once after login, before connecting to the gateway."""
        # Sync slash commands globally (can take up to an hour to propagate).
        # For instant testing, pass a guild object instead:
        #   await self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))
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


bot = VegaBot()

# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping(interaction: discord.Interaction) -> None:
    """Responds with Pong! and the current WebSocket latency."""
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(
        f"🏓 **Pong!** Latency: `{latency_ms} ms`"
    )

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)  # log_handler=None uses our custom logger
