"""
cogs/test_ui.py
Test & Simulation cog to preview redesigned Discord Components V2 UIs in a dedicated channel.
Allows testing all 5 matchmaking UI states without modifying production queue code.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.ui_renderers import (
    render_queue_ui,
    render_checkin_ui,
    render_draft_ui,
    render_map_vote_ui,
    render_match_ready_ui,
)

log = logging.getLogger(__name__)


# Mock Data for Simulation
MOCK_PLAYERS = [
    {"discord_id": 1001, "ign": "Kringzee", "discord_username": "Kringzee", "elo": 1076},
    {"discord_id": 1002, "ign": "KiRMADA", "discord_username": "KiRMADA", "elo": 1120},
    {"discord_id": 1003, "ign": "WhoWhos", "discord_username": "WhoWhos", "elo": 968},
    {"discord_id": 1004, "ign": "-Maxx", "discord_username": "-Maxx", "elo": 1180},
    {"discord_id": 1005, "ign": "D4Cofcdd", "discord_username": "D4Cofcdd", "elo": 1016},
    {"discord_id": 1006, "ign": "SpncrtheGoat", "discord_username": "SpncrtheGoat", "elo": 1000},
    {"discord_id": 1007, "ign": "CROSS", "discord_username": "CROSS", "elo": 972},
    {"discord_id": 1008, "ign": "SuicyOvO", "discord_username": "SuicyOvO", "elo": 1040},
    {"discord_id": 1009, "ign": "ReY", "discord_username": "ReY", "elo": 1050},
    {"discord_id": 1010, "ign": "GaKu!!!", "discord_username": "GaKu!!!", "elo": 990},
]


class TestUICog(commands.Cog, name="TestUI"):
    """Cog for previewing and testing redesigned matchmaking UIs in a test channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _get_target_channel(self, interaction: discord.Interaction) -> discord.abc.Messageable:
        """Helper to get designated test UI channel from environment or current channel."""
        env_channel_id = os.environ.get("TEST_UI_CHANNEL_ID")
        if env_channel_id and env_channel_id.strip().isdigit() and int(env_channel_id) > 0:
            channel = self.bot.get_channel(int(env_channel_id))
            if channel:
                return channel
        return interaction.channel

    @app_commands.command(
        name="test_ui_preview",
        description="Simulate and post redesigned FACEIT-style UIs to the test UI channel.",
    )
    @app_commands.describe(
        screen="Select which UI screen state to simulate (or 'ALL' for complete flow).",
    )
    @app_commands.choices(
        screen=[
            app_commands.Choice(name="All Screens (Full Flow)", value="all"),
            app_commands.Choice(name="1. Queue UI (7/10)", value="queue"),
            app_commands.Choice(name="2. Voice Check-In UI (8/10)", value="checkin"),
            app_commands.Choice(name="3. Draft UI (Pick Step)", value="draft"),
            app_commands.Choice(name="4. Map Vote UI", value="map_vote"),
            app_commands.Choice(name="5. Match Ready UI", value="match_ready"),
        ]
    )
    async def test_ui_preview(
        self,
        interaction: discord.Interaction,
        screen: str = "all",
    ) -> None:
        """Command handler for previewing redesigned UIs."""
        await interaction.response.defer(ephemeral=True)

        target_channel = self._get_target_channel(interaction)
        target_name = getattr(target_channel, "mention", "current channel")
        
        now_ts = int(datetime.now(timezone.utc).timestamp())
        deadline_ts = now_ts + 300 # 5 minutes

        sent_count = 0

        # 1. Queue UI
        if screen in ("all", "queue"):
            embed, view = render_queue_ui(
                queued_players=MOCK_PLAYERS[:7],
                is_paused=False,
                user_is_in_queue=False,
            )
            await target_channel.send(embed=embed, view=view)
            sent_count += 1

        # 2. Voice Check-In UI
        if screen in ("all", "checkin"):
            embed, view = render_checkin_ui(
                match_id=80,
                connected_count=8,
                total_players=10,
                lobby_vc_id=None,
                deadline_ts=deadline_ts,
            )
            await target_channel.send(embed=embed, view=view)
            sent_count += 1

        # 3. Draft UI
        if screen in ("all", "draft"):
            embed, view = render_draft_ui(
                match_id=80,
                step=1,
                total_steps=7,
                picker_name="Kringzee",
                team1_players=[MOCK_PLAYERS[0]], # Kringzee captain
                team2_players=[MOCK_PLAYERS[1]], # KiRMADA captain
                available_players=MOCK_PLAYERS[2:],
                captain1_id=MOCK_PLAYERS[0]["discord_id"],
                captain2_id=MOCK_PLAYERS[1]["discord_id"],
            )
            await target_channel.send(embed=embed, view=view)
            sent_count += 1

        # 4. Map Voting UI
        if screen in ("all", "map_vote"):
            embed, view = render_map_vote_ui(
                match_id=80,
                map_options=["ABYSS", "ASCENT", "ICEBOX", "LOTUS"],
                votes_by_map={"ABYSS": 1, "ASCENT": 6, "ICEBOX": 2, "LOTUS": 1},
                end_time_ts=now_ts + 60,
                user_voted_map="ASCENT",
            )
            await target_channel.send(embed=embed, view=view)
            sent_count += 1

        # 5. Match Ready UI
        if screen in ("all", "match_ready"):
            t1 = [MOCK_PLAYERS[0], MOCK_PLAYERS[2], MOCK_PLAYERS[4], MOCK_PLAYERS[6], MOCK_PLAYERS[7]]
            t2 = [MOCK_PLAYERS[1], MOCK_PLAYERS[3], MOCK_PLAYERS[5], MOCK_PLAYERS[8], MOCK_PLAYERS[9]]
            
            t1_avg = round(sum(p["elo"] for p in t1) / len(t1))
            t2_avg = round(sum(p["elo"] for p in t2) / len(t2))

            embed, view = render_match_ready_ui(
                match_id=80,
                selected_map="ASCENT",
                team1_players=t1,
                team2_players=t2,
                team1_avg_elo=t1_avg,
                team2_avg_elo=t2_avg,
            )
            
            # Check for Ascent thumbnail
            maps_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "maps")
            ascent_path = os.path.join(maps_dir, "ascent.png")
            if os.path.exists(ascent_path):
                file = discord.File(ascent_path, filename="ascent.png")
                await target_channel.send(embed=embed, view=view, file=file)
            else:
                await target_channel.send(embed=embed, view=view)
            sent_count += 1

        await interaction.followup.send(
            f"✅ Posted {sent_count} redesigned UI preview card(s) to {target_name}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TestUICog(bot))
