"""
cogs/leaderboard.py
-------------------
Interactive leaderboard cog — /leaderboard command replicating the NeatQueue UI:
renders high-definition visual leaderboard cards with player avatars, medals,
rank movement indicators, pagination buttons, metric/page dropdown menus, and website link.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from database import db
from utils.generate_leaderboard import (
    render_leaderboard_image,
    fetch_avatar_image,
)

log = logging.getLogger(__name__)

# NeatQueue / Vega Embed Colour (Vibrant Red)
EMBED_COLOUR = discord.Colour(0xE74C3C)
PAGE_SIZE = 10

REGION_CHOICES = [
    app_commands.Choice(name="Global / All Regions", value="All"),
    app_commands.Choice(name="India", value="India"),
    app_commands.Choice(name="APAC", value="APAC"),
    app_commands.Choice(name="EMEA", value="EMEA"),
    app_commands.Choice(name="Americas", value="Americas"),
]

METRIC_CHOICES = [
    app_commands.Choice(name="Rating (MMR / ELO)", value="elo"),
    app_commands.Choice(name="Total Wins", value="wins"),
    app_commands.Choice(name="Win Rate (%)", value="winrate"),
    app_commands.Choice(name="K/D Ratio", value="kda"),
    app_commands.Choice(name="MVP Count", value="mvps"),
]

METRIC_TITLES = {
    "elo": "Vega MatchMaking Queue MMR Leaderboard",
    "wins": "Vega MatchMaking Queue Wins Leaderboard",
    "winrate": "Vega MatchMaking Queue Win Rate Leaderboard",
    "kda": "Vega MatchMaking Queue K/D Leaderboard",
    "mvps": "Vega MatchMaking Queue MVP Leaderboard",
}

METRIC_LABELS = {
    "elo": "MMR",
    "wins": "Wins",
    "winrate": "Win Rate",
    "kda": "K/D Ratio",
    "mvps": "MVPs",
}


def _is_admin(member: discord.Member) -> bool:
    """Check administrator/manage_guild perms or configured admin role IDs."""
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    raw = os.environ.get("HELP_ADMIN_ROLE_IDS", "")
    admin_ids: list[int] = []
    for chunk in raw.split(","):
        try:
            admin_ids.append(int(chunk.strip()))
        except ValueError:
            pass
    return any(role.id in admin_ids for role in member.roles)


class ClearLeaderboardConfirmView(discord.ui.View):
    """Two-button confirmation so admins can't reset stats by accident."""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command invoker can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, reset everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled. No changes were made.", embed=None, view=None)


def build_leaderboard_embed(metric: str) -> discord.Embed:
    """Construct embed matching the NeatQueue title and red accent line."""
    title = METRIC_TITLES.get(metric, "Vega MatchMaking Queue MMR Leaderboard")
    embed = discord.Embed(
        title=title,
        colour=EMBED_COLOUR,
    )
    embed.set_image(url="attachment://leaderboard.png")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def prepare_leaderboard_data(
    bot: commands.Bot,
    guild: Optional[discord.Guild],
    region: str,
    metric: str,
    page: int,
) -> tuple[io.BytesIO, int, list[dict]]:
    """
    Fetches the requested page of players, loads avatars and rank movements,
    and returns (image_bytes_io, total_count, players).
    """
    offset = (page - 1) * PAGE_SIZE
    players, total_count = await db.get_solo_leaderboard(
        region=region,
        metric=metric,
        limit=PAGE_SIZE,
        offset=offset,
    )

    # Fetch last match outcomes to determine up/down rank movement triangles
    pids = [p["discord_id"] for p in players if p.get("discord_id")]
    outcomes = await db.get_players_last_match_outcomes(pids)
    for p in players:
        p["rank_delta"] = outcomes.get(p.get("discord_id"), 0)

    # Fetch avatars asynchronously in parallel
    avatars: dict[int, Image.Image] = {}

    async def _fetch_avatar(p: dict) -> None:
        pid = p.get("discord_id")
        if not pid:
            return
        member = guild.get_member(pid) if guild else None
        user = member or bot.get_user(pid)
        if not user:
            try:
                user = await bot.fetch_user(pid)
            except Exception:
                user = None

        url = user.display_avatar.with_format("png").with_size(64).url if user else None
        av_img = await fetch_avatar_image(url)
        avatars[pid] = av_img

    if players:
        await asyncio.gather(*[_fetch_avatar(p) for p in players])

    # Render high-res NeatQueue image
    img_buf = render_leaderboard_image(players=players, avatars=avatars, metric=metric)
    return img_buf, total_count, players


class MetricSelect(discord.ui.Select):
    """Dropdown menu to select ranking metric."""

    def __init__(self, current_metric: str) -> None:
        options = [
            discord.SelectOption(
                label="MMR",
                value="elo",
                description="Rank by MMR / ELO rating",
                default=(current_metric == "elo"),
            ),
            discord.SelectOption(
                label="Wins",
                value="wins",
                description="Rank by total match wins",
                default=(current_metric == "wins"),
            ),
            discord.SelectOption(
                label="Win Rate",
                value="winrate",
                description="Rank by match win percentage",
                default=(current_metric == "winrate"),
            ),
            discord.SelectOption(
                label="K/D Ratio",
                value="kda",
                description="Rank by kill/death ratio",
                default=(current_metric == "kda"),
            ),
            discord.SelectOption(
                label="MVPs",
                value="mvps",
                description="Rank by MVP awards",
                default=(current_metric == "mvps"),
            ),
        ]
        placeholder = METRIC_LABELS.get(current_metric, "MMR")
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: LeaderboardPaginationView = self.view  # type: ignore
        if interaction.user.id != view.author_id:
            await interaction.response.send_message(
                "Only the user who ran `/leaderboard` can use these controls. You can run `/leaderboard` yourself!",
                ephemeral=True,
            )
            return

        chosen_metric = self.values[0]
        if chosen_metric != view.metric:
            view.metric = chosen_metric
            view.current_page = 1
            await view.refresh_and_edit(interaction)


class PageSelect(discord.ui.Select):
    """Dropdown menu to jump directly to a page."""

    def __init__(self, current_page: int, total_pages: int) -> None:
        max_pages = min(max(1, total_pages), 25)
        options = [
            discord.SelectOption(
                label=f"Page {p}",
                value=str(p),
                default=(p == current_page),
            )
            for p in range(1, max_pages + 1)
        ]
        super().__init__(
            placeholder=f"Page {current_page}",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: LeaderboardPaginationView = self.view  # type: ignore
        if interaction.user.id != view.author_id:
            await interaction.response.send_message(
                "Only the user who ran `/leaderboard` can use these controls. You can run `/leaderboard` yourself!",
                ephemeral=True,
            )
            return

        chosen_page = int(self.values[0])
        if chosen_page != view.current_page:
            view.current_page = chosen_page
            await view.refresh_and_edit(interaction)


class LeaderboardPaginationView(discord.ui.View):
    """Interactive NeatQueue UI View with buttons, dropdowns, and website link."""

    def __init__(
        self,
        bot: commands.Bot,
        author_id: int,
        region: str,
        metric: str,
        current_page: int,
        total_pages: int,
        total_count: int,
        guild: Optional[discord.Guild] = None,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.author_id = author_id
        self.region = region
        self.metric = metric
        self.current_page = current_page
        self.total_pages = max(1, total_pages)
        self.total_count = total_count
        self.guild = guild

        self._build_components()

    def _build_components(self) -> None:
        self.clear_items()

        # Row 0: Navigation Buttons (⏮, ◀, 🔄, ▶, ⏭)
        first_btn = discord.ui.Button(
            emoji="⏮",
            style=discord.ButtonStyle.secondary,
            disabled=(self.current_page <= 1),
            row=0,
        )
        first_btn.callback = self._on_first
        self.add_item(first_btn)

        prev_btn = discord.ui.Button(
            emoji="◀",
            style=discord.ButtonStyle.secondary,
            disabled=(self.current_page <= 1),
            row=0,
        )
        prev_btn.callback = self._on_prev
        self.add_item(prev_btn)

        refresh_btn = discord.ui.Button(
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            disabled=False,
            row=0,
        )
        refresh_btn.callback = self._on_refresh
        self.add_item(refresh_btn)

        next_btn = discord.ui.Button(
            emoji="▶",
            style=discord.ButtonStyle.secondary,
            disabled=(self.current_page >= self.total_pages),
            row=0,
        )
        next_btn.callback = self._on_next
        self.add_item(next_btn)

        last_btn = discord.ui.Button(
            emoji="⏭",
            style=discord.ButtonStyle.secondary,
            disabled=(self.current_page >= self.total_pages),
            row=0,
        )
        last_btn.callback = self._on_last
        self.add_item(last_btn)

        # Row 1: Metric Select Menu
        self.add_item(MetricSelect(current_metric=self.metric))

        # Row 2: Page Select Menu
        self.add_item(PageSelect(current_page=self.current_page, total_pages=self.total_pages))

        # Row 3: Website Leaderboard Link Button
        website_url = os.environ.get("LEADERBOARD_URL", "https://discord.com")
        web_btn = discord.ui.Button(
            label="Website Leaderboard",
            emoji="📊",
            style=discord.ButtonStyle.link,
            url=website_url,
            row=3,
        )
        self.add_item(web_btn)

    async def refresh_and_edit(self, interaction: discord.Interaction) -> None:
        """Fetch fresh data, update components, and edit interaction response."""
        await interaction.response.defer()

        img_buf, total_count, _ = await prepare_leaderboard_data(
            bot=self.bot,
            guild=self.guild or interaction.guild,
            region=self.region,
            metric=self.metric,
            page=self.current_page,
        )
        self.total_count = total_count
        self.total_pages = max(1, math.ceil(total_count / PAGE_SIZE))
        if self.current_page > self.total_pages:
            self.current_page = self.total_pages

        self._build_components()

        embed = build_leaderboard_embed(metric=self.metric)
        file = discord.File(img_buf, filename="leaderboard.png")

        try:
            await interaction.edit_original_response(
                embed=embed,
                view=self,
                attachments=[file],
            )
        except Exception as exc:
            log.warning("Failed to edit leaderboard response: %s", exc)

    async def _on_first(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command caller can use pagination.", ephemeral=True)
            return
        if self.current_page > 1:
            self.current_page = 1
            await self.refresh_and_edit(interaction)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command caller can use pagination.", ephemeral=True)
            return
        if self.current_page > 1:
            self.current_page -= 1
            await self.refresh_and_edit(interaction)

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command caller can use pagination.", ephemeral=True)
            return
        await self.refresh_and_edit(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command caller can use pagination.", ephemeral=True)
            return
        if self.current_page < self.total_pages:
            self.current_page += 1
            await self.refresh_and_edit(interaction)

    async def _on_last(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command caller can use pagination.", ephemeral=True)
            return
        if self.current_page < self.total_pages:
            self.current_page = self.total_pages
            await self.refresh_and_edit(interaction)


class LeaderboardCog(commands.Cog, name="Leaderboard"):
    """Competitive player rankings and matchmaking leaderboard."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="leaderboard",
        description="View the competitive matchmaking leaderboard, MMR ratings, and player stats.",
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

        img_buf, total_count, _ = await prepare_leaderboard_data(
            bot=self.bot,
            guild=interaction.guild,
            region=reg_val,
            metric=metric_val,
            page=1,
        )

        total_pages = max(1, math.ceil(total_count / PAGE_SIZE))

        embed = build_leaderboard_embed(metric=metric_val)
        file = discord.File(img_buf, filename="leaderboard.png")

        view = LeaderboardPaginationView(
            bot=self.bot,
            author_id=interaction.user.id,
            region=reg_val,
            metric=metric_val,
            current_page=1,
            total_pages=total_pages,
            total_count=total_count,
            guild=interaction.guild,
        )

        await interaction.followup.send(embed=embed, file=file, view=view, ephemeral=hide)

    # -------------------------------------------------------------------------
    # /clear-leaderboard  (admin only)
    # -------------------------------------------------------------------------

    @app_commands.command(
        name="clear-leaderboard",
        description="[Admin] Reset all player ELO and stats back to default.",
    )
    async def clear_leaderboard_cmd(self, interaction: discord.Interaction) -> None:
        """Admin-only: wipe every active player's ELO and combat stats."""
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message(
                "You need Staff or Administrator permissions to use this command.",
                ephemeral=True,
            )
            return

        confirm_embed = discord.Embed(
            title="Reset Leaderboard",
            description=(
                "This will reset **all active players** to:\n"
                "- ELO → `1000`\n"
                "- Matches played → `0`\n"
                "- Wins, Kills, Deaths, Assists, MVPs → `0`\n\n"
                "**This cannot be undone.** Are you sure?"
            ),
            colour=discord.Colour.red(),
        )

        view = ClearLeaderboardConfirmView(author_id=interaction.user.id)
        await interaction.response.send_message(embed=confirm_embed, view=view, ephemeral=True)

        await view.wait()

        if not view.confirmed:
            return  # cancel button already edited the message

        count = await db.reset_all_player_stats()

        done_embed = discord.Embed(
            title="Leaderboard Cleared",
            description=f"Reset **{count} player(s)** — ELO set to `1000`, all stats zeroed.",
            colour=discord.Colour.green(),
        )
        await interaction.edit_original_response(embed=done_embed, view=None)
        log.info("Admin %s (%d) cleared leaderboard — %d players reset.", interaction.user.name, interaction.user.id, count)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LeaderboardCog(bot))
