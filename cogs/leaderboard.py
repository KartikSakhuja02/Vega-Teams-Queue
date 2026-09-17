"""
cogs/leaderboard.py
-------------------
Interactive leaderboard cog — /leaderboard command to view top players,
ELO rankings, win rates, and combat statistics with pagination and regional filtering.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db

log = logging.getLogger(__name__)

EMBED_COLOUR = discord.Colour.from_str("#5B4FCF")
PAGE_SIZE = 10

REGION_CHOICES = [
    app_commands.Choice(name="Global / All Regions", value="All"),
    app_commands.Choice(name="India", value="India"),
    app_commands.Choice(name="APAC", value="APAC"),
    app_commands.Choice(name="EMEA", value="EMEA"),
    app_commands.Choice(name="Americas", value="Americas"),
]

METRIC_CHOICES = [
    app_commands.Choice(name="Rating (ELO)", value="elo"),
    app_commands.Choice(name="Total Wins", value="wins"),
    app_commands.Choice(name="K/D Ratio", value="kda"),
    app_commands.Choice(name="MVP Count", value="mvps"),
]

METRIC_LABELS = {
    "elo": "ELO Rating",
    "wins": "Total Wins",
    "kda": "K/D Ratio",
    "mvps": "MVP Count",
}


def build_leaderboard_embed(
    players: list[dict],
    page: int,
    total_pages: int,
    total_count: int,
    region: str,
    metric: str,
    caller_rank: Optional[dict] = None,
) -> discord.Embed:
    """Construct a clean, rich embed displaying a single leaderboard page."""
    region_label = "Global (All Regions)" if region in ("All", None) else region
    metric_label = METRIC_LABELS.get(metric, "ELO Rating")

    embed = discord.Embed(
        title="🏆 Vega Scrims — Competitive Leaderboard",
        description=(
            f"**Region:** `{region_label}` • **Sorted by:** `{metric_label}`\n"
            f"**Total Registered Players:** `{total_count}`\n"
            "────────────────────────────────────────"
        ),
        colour=EMBED_COLOUR,
    )

    if not players:
        embed.add_field(
            name="No Players Found",
            value="*No active players found for this region/metric filter.*",
            inline=False,
        )
    else:
        lines: list[str] = []
        for p in players:
            rank = p.get("rank_num", 0)
            if rank == 1:
                medal = "🥇"
            elif rank == 2:
                medal = "🥈"
            elif rank == 3:
                medal = "🥉"
            else:
                medal = f"`#{rank}`"

            ign = p.get("ign") or p.get("discord_username") or "Player"
            pid = p.get("discord_id")
            elo = p.get("elo", 1000)
            wins = p.get("wins", 0)
            matches = p.get("matches_played", 0)
            losses = max(0, matches - wins)
            win_pct = round((wins / max(1, matches)) * 100, 1)
            kills = p.get("kills", 0)
            deaths = p.get("deaths", 0)
            assists = p.get("assists", 0)
            kd = round(kills / max(1, deaths), 2)
            mvps = p.get("mvp_count", 0)

            # Player headline
            line_top = f"{medal} **{ign}** (<@{pid}>)"

            # Stats pill row
            pill_parts = [f"⭐ `{elo} ELO`", f"`{wins}W - {losses}L` `({win_pct}%)`", f"`{kd} K/D` `({kills}/{deaths}/{assists})`"]
            if mvps > 0:
                pill_parts.append(f"👑 `{mvps} MVP`")

            line_bot = "└ " + " • ".join(pill_parts)
            lines.append(f"{line_top}\n{line_bot}")

        embed.add_field(name="Rankings", value="\n\n".join(lines)[:4000], inline=False)

    # Footer with caller's personal rank
    footer_parts = [f"Page {page}/{total_pages}"]
    if caller_rank:
        c_rank = caller_rank.get("rank_num")
        c_elo = caller_rank.get("elo", 1000)
        footer_parts.insert(0, f"Your Standing: #{c_rank} ({c_elo} ELO)")
    else:
        footer_parts.insert(0, "Use /register to join the leaderboard")

    embed.set_footer(text=" • ".join(footer_parts))
    return embed


class LeaderboardPaginationView(discord.ui.View):
    """Interactive button pagination for leaderboard pages."""

    def __init__(
        self,
        author_id: int,
        region: str,
        metric: str,
        current_page: int,
        total_pages: int,
        total_count: int,
    ) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.region = region
        self.metric = metric
        self.current_page = current_page
        self.total_pages = max(1, total_pages)
        self.total_count = total_count

        self._update_buttons()

    def _update_buttons(self) -> None:
        self.clear_items()

        # Prev button
        prev_btn = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.primary,
            disabled=(self.current_page <= 1),
            row=0,
        )
        prev_btn.callback = self._on_prev
        self.add_item(prev_btn)

        # Page indicator (disabled)
        ind_btn = discord.ui.Button(
            label=f"Page {self.current_page} / {self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            row=0,
        )
        self.add_item(ind_btn)

        # Next button
        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.primary,
            disabled=(self.current_page >= self.total_pages),
            row=0,
        )
        next_btn.callback = self._on_next
        self.add_item(next_btn)

    async def _render_page(self, interaction: discord.Interaction) -> None:
        offset = (self.current_page - 1) * PAGE_SIZE
        players, total_count = await db.get_solo_leaderboard(
            region=self.region,
            metric=self.metric,
            limit=PAGE_SIZE,
            offset=offset,
        )
        caller_rank = await db.get_player_leaderboard_rank(
            discord_id=self.author_id,
            region=self.region,
            metric=self.metric,
        )

        self.total_count = total_count
        self.total_pages = max(1, math.ceil(total_count / PAGE_SIZE))
        self._update_buttons()

        embed = build_leaderboard_embed(
            players=players,
            page=self.current_page,
            total_pages=self.total_pages,
            total_count=self.total_count,
            region=self.region,
            metric=self.metric,
            caller_rank=caller_rank,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command caller can use pagination buttons.", ephemeral=True)
            return
        if self.current_page > 1:
            self.current_page -= 1
            await self._render_page(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command caller can use pagination buttons.", ephemeral=True)
            return
        if self.current_page < self.total_pages:
            self.current_page += 1
            await self._render_page(interaction)


class LeaderboardCog(commands.Cog, name="Leaderboard"):
    """Competitive player rankings and matchmaking leaderboard."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="leaderboard",
        description="View the competitive leaderboard rankings, ELO ratings, and player stats.",
    )
    @app_commands.describe(
        region="Filter leaderboard by regional matchmaking zone.",
        metric="Sorting criterion for player ranking.",
        hide="Whether to hide the leaderboard from other members (ephemeral).",
    )
    @app_commands.choices(region=REGION_CHOICES, metric=METRIC_CHOICES)
    async def leaderboard_cmd(
        self,
        interaction: discord.Interaction,
        region: Optional[app_commands.Choice[str]] = None,
        metric: Optional[app_commands.Choice[str]] = None,
        hide: bool = False,
    ) -> None:
        """Fetch and display competitive leaderboard rankings."""
        await interaction.response.defer(ephemeral=hide)

        reg_val = region.value if region else "All"
        metric_val = metric.value if metric else "elo"

        players, total_count = await db.get_solo_leaderboard(
            region=reg_val,
            metric=metric_val,
            limit=PAGE_SIZE,
            offset=0,
        )

        caller_rank = await db.get_player_leaderboard_rank(
            discord_id=interaction.user.id,
            region=reg_val,
            metric=metric_val,
        )

        total_pages = max(1, math.ceil(total_count / PAGE_SIZE))

        embed = build_leaderboard_embed(
            players=players,
            page=1,
            total_pages=total_pages,
            total_count=total_count,
            region=reg_val,
            metric=metric_val,
            caller_rank=caller_rank,
        )

        view = LeaderboardPaginationView(
            author_id=interaction.user.id,
            region=reg_val,
            metric=metric_val,
            current_page=1,
            total_pages=total_pages,
            total_count=total_count,
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=hide)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LeaderboardCog(bot))
