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
import json
import logging
import math
import os
import re
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

LABEL_TO_METRIC = {
    "mmr": "elo",
    "elo": "elo",
    "wins": "wins",
    "win rate": "winrate",
    "winrate": "winrate",
    "k/d ratio": "kda",
    "k/d": "kda",
    "kda": "kda",
    "mvps": "mvps",
    "mvp": "mvps",
}

# Deterministic custom_ids for persistent interactions
CUSTOM_ID_FIRST = "leaderboard:first"
CUSTOM_ID_PREV = "leaderboard:prev"
CUSTOM_ID_REFRESH = "leaderboard:refresh"
CUSTOM_ID_NEXT = "leaderboard:next"
CUSTOM_ID_LAST = "leaderboard:last"
CUSTOM_ID_METRIC = "leaderboard:metric_select"
CUSTOM_ID_PAGE = "leaderboard:page_select"

CONFIG_KEY_TRACKED_LEADERBOARDS = "tracked_leaderboard_messages"
CONFIG_KEY_PINNED_LEADERBOARD_MSG = "leaderboard_panel_message_id"
CONFIG_KEY_PINNED_LEADERBOARD_CH = "leaderboard_panel_channel_id"


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


def _get_leaderboard_cog(interaction: discord.Interaction) -> Optional[LeaderboardCog]:
    return interaction.client.get_cog("Leaderboard")  # type: ignore


class ClearLeaderboardConfirmView(discord.ui.View):
    """Two-button confirmation so admins can't reset stats by accident."""

    def __init__(self, cog: LeaderboardCog, author_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command invoker can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, reset everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            count = await db.reset_all_player_stats()
            done_embed = discord.Embed(
                title="Leaderboard Cleared",
                description=f"Reset **{count} player(s)** — ELO set to `1000`, all stats zeroed.",
                colour=discord.Colour.green(),
            )
            await interaction.edit_original_response(embed=done_embed, view=None)
            log.info("Admin %s (%d) cleared leaderboard — %d players reset.", interaction.user.name, interaction.user.id, count)
            asyncio.create_task(self.cog.refresh_all_leaderboards())
        except Exception as e:
            log.error("Failed to reset leaderboard: %s", e)
            await interaction.followup.send(f"Failed to reset leaderboard: {e}", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled. No changes were made.", embed=None, view=None)


def build_leaderboard_embed(
    metric: str = "elo",
    region: str = "All",
    page: int = 1,
    total_pages: int = 1,
) -> discord.Embed:
    """Construct embed matching the NeatQueue title and red accent line with footer state."""
    title = METRIC_TITLES.get(metric, "Vega MatchMaking Queue MMR Leaderboard")
    embed = discord.Embed(
        title=title,
        colour=EMBED_COLOUR,
    )
    embed.set_image(url="attachment://leaderboard.png")
    embed.timestamp = discord.utils.utcnow()
    metric_label = METRIC_LABELS.get(metric, metric.upper())
    embed.set_footer(text=f"Page {page}/{total_pages} • Region: {region} • Metric: {metric.upper()} • {metric_label} • Auto-Updated")
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

        url = user.display_avatar.with_format("png").with_size(128).url if user else None
        av_img = await fetch_avatar_image(url)
        avatars[pid] = av_img

    if players:
        await asyncio.gather(*[_fetch_avatar(p) for p in players])

    # Render high-res NeatQueue image
    img_buf = render_leaderboard_image(players=players, avatars=avatars, metric=metric)
    return img_buf, total_count, players


class MetricSelect(discord.ui.Select):
    """Dropdown menu to select ranking metric with persistent custom_id."""

    def __init__(self, current_metric: str = "elo") -> None:
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
            custom_id=CUSTOM_ID_METRIC,
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _get_leaderboard_cog(interaction)
        if cog:
            await cog.handle_view_interaction(interaction, metric=self.values[0])
        else:
            await interaction.response.send_message("Leaderboard system initializing. Please try again shortly.", ephemeral=True)


class PageSelect(discord.ui.Select):
    """Dropdown menu to jump directly to a page with persistent custom_id."""

    def __init__(self, current_page: int = 1, total_pages: int = 1) -> None:
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
            custom_id=CUSTOM_ID_PAGE,
            placeholder=f"Page {current_page}",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _get_leaderboard_cog(interaction)
        if cog:
            try:
                page_val = int(self.values[0])
            except (ValueError, IndexError):
                page_val = 1
            await cog.handle_view_interaction(interaction, page=page_val)
        else:
            await interaction.response.send_message("Leaderboard system initializing. Please try again shortly.", ephemeral=True)


class LeaderboardPaginationView(discord.ui.View):
    """Persistent Interactive NeatQueue UI View with buttons and metric/page dropdown menus."""

    def __init__(
        self,
        bot: Optional[commands.Bot] = None,
        region: str = "All",
        metric: str = "elo",
        current_page: int = 1,
        total_pages: int = 1,
        total_count: int = 0,
        guild: Optional[discord.Guild] = None,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.region = region
        self.metric = metric
        self.current_page = current_page
        self.total_pages = max(1, total_pages)
        self.total_count = total_count
        self.guild = guild

        # Row 0: Navigation Buttons (⏮, ◀, 🔄, ▶, ⏭)
        self.btn_first.disabled = (self.current_page <= 1)
        self.btn_prev.disabled = (self.current_page <= 1)
        self.btn_refresh.disabled = False
        self.btn_next.disabled = (self.current_page >= self.total_pages)
        self.btn_last.disabled = (self.current_page >= self.total_pages)

        # Row 1: Metric Select Menu
        self.add_item(MetricSelect(current_metric=self.metric))

        # Row 2: Page Select Menu
        self.add_item(PageSelect(current_page=self.current_page, total_pages=self.total_pages))

    @discord.ui.button(
        emoji="⏮",
        style=discord.ButtonStyle.secondary,
        custom_id=CUSTOM_ID_FIRST,
        row=0,
    )
    async def btn_first(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = _get_leaderboard_cog(interaction)
        if cog:
            await cog.handle_view_interaction(interaction, action="first")
        else:
            await interaction.response.send_message("Leaderboard system initializing. Please try again shortly.", ephemeral=True)

    @discord.ui.button(
        emoji="◀",
        style=discord.ButtonStyle.secondary,
        custom_id=CUSTOM_ID_PREV,
        row=0,
    )
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = _get_leaderboard_cog(interaction)
        if cog:
            await cog.handle_view_interaction(interaction, action="prev")
        else:
            await interaction.response.send_message("Leaderboard system initializing. Please try again shortly.", ephemeral=True)

    @discord.ui.button(
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id=CUSTOM_ID_REFRESH,
        row=0,
    )
    async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = _get_leaderboard_cog(interaction)
        if cog:
            await cog.handle_view_interaction(interaction, action="refresh")
        else:
            await interaction.response.send_message("Leaderboard system initializing. Please try again shortly.", ephemeral=True)

    @discord.ui.button(
        emoji="▶",
        style=discord.ButtonStyle.secondary,
        custom_id=CUSTOM_ID_NEXT,
        row=0,
    )
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = _get_leaderboard_cog(interaction)
        if cog:
            await cog.handle_view_interaction(interaction, action="next")
        else:
            await interaction.response.send_message("Leaderboard system initializing. Please try again shortly.", ephemeral=True)

    @discord.ui.button(
        emoji="⏭",
        style=discord.ButtonStyle.secondary,
        custom_id=CUSTOM_ID_LAST,
        row=0,
    )
    async def btn_last(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = _get_leaderboard_cog(interaction)
        if cog:
            await cog.handle_view_interaction(interaction, action="last")
        else:
            await interaction.response.send_message("Leaderboard system initializing. Please try again shortly.", ephemeral=True)


class LeaderboardCog(commands.Cog, name="Leaderboard"):
    """Competitive player rankings and persistent matchmaking leaderboard."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._tracked_messages: dict[int, dict] = {}
        self._is_refreshing: bool = False
        self._refresh_pending: bool = False

    async def cog_load(self) -> None:
        # Register persistent view so button and dropdown interactions never expire
        self.bot.add_view(LeaderboardPaginationView(bot=self.bot))
        await self._load_tracked_messages()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._ensure_leaderboard_panel()
        await self.refresh_all_leaderboards()

    async def _load_tracked_messages(self) -> None:
        """Load tracked persistent leaderboard messages from DB."""
        try:
            raw = await db.get_config(CONFIG_KEY_TRACKED_LEADERBOARDS)
            if raw:
                items = json.loads(raw)
                if isinstance(items, list):
                    self._tracked_messages = {
                        int(x["message_id"]): x
                        for x in items
                        if "message_id" in x and "channel_id" in x
                    }
                    log.info("Loaded %d tracked persistent leaderboard message(s).", len(self._tracked_messages))
        except Exception as e:
            log.warning("Failed loading tracked leaderboard messages: %s", e)

    async def _save_tracked_messages(self) -> None:
        """Save tracked persistent leaderboard messages to DB."""
        try:
            items = list(self._tracked_messages.values())
            await db.set_config(CONFIG_KEY_TRACKED_LEADERBOARDS, json.dumps(items))
        except Exception as e:
            log.warning("Failed saving tracked leaderboard messages: %s", e)

    async def track_leaderboard_message(
        self,
        message_id: int,
        channel_id: int,
        region: str,
        metric: str,
        page: int,
        total_pages: int = 1,
    ) -> None:
        """Register a public leaderboard message for real-time auto-updates."""
        # Keep only 1 active tracked leaderboard per channel
        old_ids = [mid for mid, d in self._tracked_messages.items() if d.get("channel_id") == channel_id]
        for old_id in old_ids:
            self._tracked_messages.pop(old_id, None)

        self._tracked_messages[message_id] = {
            "message_id": message_id,
            "channel_id": channel_id,
            "region": region,
            "metric": metric,
            "page": page,
            "total_pages": total_pages,
        }

        # Keep at most 5 channels tracked to minimize load
        if len(self._tracked_messages) > 5:
            oldest_id = next(iter(self._tracked_messages))
            self._tracked_messages.pop(oldest_id, None)

        await self._save_tracked_messages()

    def resolve_message_state(self, message: discord.Message) -> tuple[str, str, int, int]:
        """
        Extract (region, metric, page, total_pages) from tracked state or embed footer/title.
        Defaults to ('All', 'elo', 1, 1).
        """
        if message.id in self._tracked_messages:
            tracked = self._tracked_messages[message.id]
            return (
                tracked.get("region", "All"),
                tracked.get("metric", "elo"),
                tracked.get("page", 1),
                tracked.get("total_pages", 1),
            )

        region = "All"
        metric = "elo"
        page = 1
        total_pages = 1

        if message.embeds:
            embed = message.embeds[0]
            if embed.footer and embed.footer.text:
                text = embed.footer.text
                m_page = re.search(r"Page\s+(\d+)/(\d+)", text)
                if m_page:
                    page = int(m_page.group(1))
                    total_pages = int(m_page.group(2))
                m_reg = re.search(r"Region:\s*([^\s•]+)", text)
                if m_reg:
                    region = m_reg.group(1)
                m_met = re.search(r"Metric:\s*([^\s•]+)", text)
                if m_met:
                    raw_met = m_met.group(1).lower()
                    metric = LABEL_TO_METRIC.get(raw_met, raw_met)
            elif embed.title:
                t = embed.title.lower()
                if "win rate" in t:
                    metric = "winrate"
                elif "win" in t:
                    metric = "wins"
                elif "k/d" in t:
                    metric = "kda"
                elif "mvp" in t:
                    metric = "mvps"
                elif "mmr" in t or "elo" in t:
                    metric = "elo"

        return region, metric, page, total_pages

    async def handle_view_interaction(
        self,
        interaction: discord.Interaction,
        action: Optional[str] = None,
        metric: Optional[str] = None,
        page: Optional[int] = None,
    ) -> None:
        """Handle persistent button clicks and dropdown menu selections."""
        await interaction.response.defer()

        msg = interaction.message
        if not msg:
            return

        curr_region, curr_metric, curr_page, curr_total_pages = self.resolve_message_state(msg)

        new_region = curr_region
        new_metric = metric if metric else curr_metric
        if metric and metric != curr_metric:
            new_page = 1
        elif page is not None:
            new_page = page
        elif action == "first":
            new_page = 1
        elif action == "prev":
            new_page = max(1, curr_page - 1)
        elif action == "next":
            new_page = curr_page + 1
        elif action == "last":
            new_page = max(1, curr_total_pages)
        elif action == "refresh":
            new_page = curr_page
        else:
            new_page = curr_page

        guild = interaction.guild or (self.bot.get_guild(msg.guild.id) if msg.guild else None)
        img_buf, total_count, _ = await prepare_leaderboard_data(
            bot=self.bot,
            guild=guild,
            region=new_region,
            metric=new_metric,
            page=new_page,
        )
        total_pages = max(1, math.ceil(total_count / PAGE_SIZE))
        new_page = min(max(1, new_page), total_pages)

        embed = build_leaderboard_embed(
            metric=new_metric,
            region=new_region,
            page=new_page,
            total_pages=total_pages,
        )
        file = discord.File(img_buf, filename="leaderboard.png")
        new_view = LeaderboardPaginationView(
            bot=self.bot,
            region=new_region,
            metric=new_metric,
            current_page=new_page,
            total_pages=total_pages,
            total_count=total_count,
            guild=guild,
        )

        try:
            await interaction.edit_original_response(
                embed=embed,
                view=new_view,
                attachments=[file],
            )
        except Exception as exc:
            log.warning("Failed to edit leaderboard response: %s", exc)

        # Track public leaderboard message for real-time auto-updates
        is_ephemeral = bool(msg.flags.ephemeral)
        if not is_ephemeral and interaction.channel_id:
            await self.track_leaderboard_message(
                message_id=msg.id,
                channel_id=interaction.channel_id,
                region=new_region,
                metric=new_metric,
                page=new_page,
                total_pages=total_pages,
            )

    async def refresh_all_leaderboards(self) -> None:
        """
        Auto-update all active/tracked leaderboard messages in real-time.
        Called whenever match results or player ELO/stats are updated.
        """
        if self._is_refreshing:
            self._refresh_pending = True
            return

        self._is_refreshing = True
        try:
            if not self._tracked_messages:
                await self._load_tracked_messages()

            if not self._tracked_messages:
                return

            render_cache: dict[tuple[str, str, int], tuple[bytes, int]] = {}
            to_remove: list[int] = []

            for item in list(self._tracked_messages.values()):
                msg_id = item["message_id"]
                channel_id = item["channel_id"]
                region = item.get("region", "All")
                metric = item.get("metric", "elo")
                page = item.get("page", 1)

                channel = self.bot.get_channel(channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except Exception:
                        continue

                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    continue

                cache_key = (region, metric, page)
                if cache_key in render_cache:
                    img_bytes, total_count = render_cache[cache_key]
                else:
                    try:
                        img_buf, total_count, _ = await prepare_leaderboard_data(
                            bot=self.bot,
                            guild=channel.guild,
                            region=region,
                            metric=metric,
                            page=page,
                        )
                        img_bytes = img_buf.getvalue()
                        render_cache[cache_key] = (img_bytes, total_count)
                    except Exception as e:
                        log.error("Failed preparing leaderboard data for auto-update: %s", e)
                        continue

                total_pages = max(1, math.ceil(total_count / PAGE_SIZE))
                page = min(max(1, page), total_pages)
                item["page"] = page
                item["total_pages"] = total_pages

                embed = build_leaderboard_embed(
                    metric=metric,
                    region=region,
                    page=page,
                    total_pages=total_pages,
                )
                file = discord.File(io.BytesIO(img_bytes), filename="leaderboard.png")
                view = LeaderboardPaginationView(
                    bot=self.bot,
                    region=region,
                    metric=metric,
                    current_page=page,
                    total_pages=total_pages,
                    total_count=total_count,
                    guild=channel.guild,
                )

                try:
                    partial = channel.get_partial_message(msg_id)
                    await partial.edit(embed=embed, view=view, attachments=[file])
                    log.info("Auto-updated leaderboard %d in #%s.", msg_id, channel.name)
                except discord.NotFound:
                    log.info("Leaderboard message %d was deleted; pruning.", msg_id)
                    to_remove.append(msg_id)
                except Exception as e:
                    log.warning("Could not auto-update leaderboard message %d: %s", msg_id, e)

            for rid in to_remove:
                self._tracked_messages.pop(rid, None)
            if to_remove:
                await self._save_tracked_messages()

        finally:
            self._is_refreshing = False
            if self._refresh_pending:
                self._refresh_pending = False
                asyncio.create_task(self.refresh_all_leaderboards())

    async def _ensure_leaderboard_panel(self) -> None:
        """Check LEADERBOARD_CHANNEL_ID env var or pinned config on startup and ensure panel exists."""
        channel_id_raw = os.environ.get("LEADERBOARD_CHANNEL_ID") or await db.get_config(CONFIG_KEY_PINNED_LEADERBOARD_CH)
        if not channel_id_raw:
            return

        try:
            channel_id = int(channel_id_raw)
        except ValueError:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        stored_id = await db.get_config(CONFIG_KEY_PINNED_LEADERBOARD_MSG)
        if stored_id:
            try:
                mid = int(stored_id)
                self._tracked_messages[mid] = {
                    "message_id": mid,
                    "channel_id": channel.id,
                    "region": "All",
                    "metric": "elo",
                    "page": 1,
                    "total_pages": 1,
                }
                await self.refresh_all_leaderboards()
                log.info("Leaderboard pinned panel refreshed (ID: %d).", mid)
                return
            except Exception as e:
                log.warning("Could not refresh existing leaderboard panel: %s", e)

        # Post fresh permanent panel if not already existing
        try:
            img_buf, total_count, _ = await prepare_leaderboard_data(
                bot=self.bot,
                guild=channel.guild,
                region="All",
                metric="elo",
                page=1,
            )
            total_pages = max(1, math.ceil(total_count / PAGE_SIZE))
            embed = build_leaderboard_embed(metric="elo", region="All", page=1, total_pages=total_pages)
            file = discord.File(img_buf, filename="leaderboard.png")
            view = LeaderboardPaginationView(
                bot=self.bot,
                region="All",
                metric="elo",
                current_page=1,
                total_pages=total_pages,
                total_count=total_count,
                guild=channel.guild,
            )
            msg = await channel.send(embed=embed, file=file, view=view)
            try:
                await msg.pin()
            except Exception:
                pass
            await self.track_leaderboard_message(
                message_id=msg.id,
                channel_id=channel.id,
                region="All",
                metric="elo",
                page=1,
                total_pages=total_pages,
            )
            await db.set_config(CONFIG_KEY_PINNED_LEADERBOARD_MSG, str(msg.id))
            await db.set_config(CONFIG_KEY_PINNED_LEADERBOARD_CH, str(channel.id))
            log.info("Posted new persistent leaderboard panel (ID: %d).", msg.id)
        except Exception as e:
            log.error("Failed to create persistent leaderboard panel: %s", e)

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

        embed = build_leaderboard_embed(metric=metric_val, region=reg_val, page=1, total_pages=total_pages)
        file = discord.File(img_buf, filename="leaderboard.png")

        view = LeaderboardPaginationView(
            bot=self.bot,
            region=reg_val,
            metric=metric_val,
            current_page=1,
            total_pages=total_pages,
            total_count=total_count,
            guild=interaction.guild,
        )

        msg = await interaction.followup.send(embed=embed, file=file, view=view, ephemeral=hide)

        if not hide and msg and interaction.channel_id:
            await self.track_leaderboard_message(
                message_id=msg.id,
                channel_id=interaction.channel_id,
                region=reg_val,
                metric=metric_val,
                page=1,
                total_pages=total_pages,
            )

    @app_commands.command(
        name="setup-leaderboard",
        description="[Admin] Post a permanent, auto-updating leaderboard panel in this channel.",
    )
    @app_commands.describe(
        region="Default regional matchmaking zone (Default: Global / All Regions).",
        metric="Default sorting criterion (Default: MMR).",
    )
    @app_commands.choices(region=REGION_CHOICES, metric=METRIC_CHOICES)
    async def setup_leaderboard_cmd(
        self,
        interaction: discord.Interaction,
        region: Optional[app_commands.Choice[str]] = None,
        metric: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        """Admin-only: post a dedicated auto-updating leaderboard panel."""
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message(
                "You need Staff or Administrator permissions to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        reg_val = region.value if region else "All"
        metric_val = metric.value if metric else "elo"

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("This command must be run in a text channel.", ephemeral=True)
            return

        img_buf, total_count, _ = await prepare_leaderboard_data(
            bot=self.bot,
            guild=interaction.guild,
            region=reg_val,
            metric=metric_val,
            page=1,
        )
        total_pages = max(1, math.ceil(total_count / PAGE_SIZE))

        embed = build_leaderboard_embed(metric=metric_val, region=reg_val, page=1, total_pages=total_pages)
        file = discord.File(img_buf, filename="leaderboard.png")
        view = LeaderboardPaginationView(
            bot=self.bot,
            region=reg_val,
            metric=metric_val,
            current_page=1,
            total_pages=total_pages,
            total_count=total_count,
            guild=interaction.guild,
        )

        msg = await channel.send(embed=embed, file=file, view=view)
        try:
            await msg.pin()
        except discord.Forbidden:
            pass

        await self.track_leaderboard_message(
            message_id=msg.id,
            channel_id=channel.id,
            region=reg_val,
            metric=metric_val,
            page=1,
            total_pages=total_pages,
        )
        await db.set_config(CONFIG_KEY_PINNED_LEADERBOARD_MSG, str(msg.id))
        await db.set_config(CONFIG_KEY_PINNED_LEADERBOARD_CH, str(channel.id))

        await interaction.followup.send(
            f"✅ Permanent auto-updating leaderboard panel created and pinned in {channel.mention}!",
            ephemeral=True,
        )

    # -------------------------------------------------------------------------
    # /clear-leaderboard & /reset-leaderboard  (admin only)
    # -------------------------------------------------------------------------

    async def _handle_clear_leaderboard(self, interaction: discord.Interaction) -> None:
        """Shared confirmation flow for /clear-leaderboard and /reset-leaderboard."""
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

        view = ClearLeaderboardConfirmView(cog=self, author_id=interaction.user.id)
        await interaction.response.send_message(embed=confirm_embed, view=view, ephemeral=True)

    @app_commands.command(
        name="clear-leaderboard",
        description="[Admin] Reset all player ELO and stats back to default.",
    )
    async def clear_leaderboard_cmd(self, interaction: discord.Interaction) -> None:
        """Admin-only: wipe every active player's ELO and combat stats."""
        await self._handle_clear_leaderboard(interaction)

    @app_commands.command(
        name="reset-leaderboard",
        description="[Admin] Reset all player ELO and stats back to default (alias for clear-leaderboard).",
    )
    async def reset_leaderboard_cmd(self, interaction: discord.Interaction) -> None:
        """Admin-only: alias for clear-leaderboard."""
        await self._handle_clear_leaderboard(interaction)


async def setup(bot: commands.Bot) -> None:
    cog = LeaderboardCog(bot)
    await bot.add_cog(cog)
    bot.add_view(LeaderboardPaginationView(bot=bot))
