"""
utils/ui_renderers.py
Modern, compact, Discord-native Components V2 UI renderers for VEGA Esports Matchmaking.
Produces FACEIT-style information-dense screens with minimal clutter and maximum clarity.
"""

import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import discord
from discord.ui import View, Button, Select

# Visual Accent Colors
VEGA_PURPLE = discord.Colour.from_str("#8B5CF6")
VEGA_RED = discord.Colour.from_str("#FF4655")
VEGA_GREEN = discord.Colour.from_str("#10B981")
VEGA_DARK = discord.Colour.from_str("#1E1E2E")


def _get_progress_bar(current: int, total: int = 10, length: int = 10) -> str:
    """Generate a clean visual progress bar."""
    filled = int(round((current / total) * length))
    filled = max(0, min(length, filled))
    return "🟩" * filled + "⬛" * (length - filled)


def _get_dot_indicators(connected: int, total: int = 10) -> str:
    """Generate visual dot indicators for voice check-in (● connected, ○ missing)."""
    connected = max(0, min(total, connected))
    return " ".join(["●" if i < connected else "○" for i in range(total)])


# =============================================================================
# 1. QUEUE UI RENDERER
# =============================================================================

class QueueView(View):
    """Interactive view for the Queue UI."""
    def __init__(self, is_in_queue: bool = False):
        super().__init__(timeout=None)
        
        if not is_in_queue:
            join_btn = Button(
                label="JOIN QUEUE",
                style=discord.ButtonStyle.success,
                custom_id="test_solo_queue_join",
            )
            self.add_item(join_btn)
        else:
            leave_btn = Button(
                label="LEAVE QUEUE",
                style=discord.ButtonStyle.danger,
                custom_id="test_solo_queue_leave",
            )
            self.add_item(leave_btn)


def render_queue_ui(
    queued_players: List[Dict[str, Any]],
    is_paused: bool = False,
    pause_until: Optional[float] = None,
    user_is_in_queue: bool = False,
) -> Tuple[discord.Embed, View]:
    """
    Render compact, FACEIT-style Queue UI.
    Shows 7/10 indicator, progress bar, avg ELO, and visible player list.
    """
    count = len(queued_players)
    total = 10
    
    # Calculate Average ELO
    elos = [p.get("elo", 1000) for p in queued_players]
    avg_elo = round(sum(elos) / len(elos)) if elos else 1000
    
    progress = _get_progress_bar(count, total, length=10)

    if is_paused:
        title = "VEGA QUEUE • PAUSED"
        colour = VEGA_RED
        if pause_until:
            status_desc = f"**Status:** Paused — Reopens <t:{int(pause_until)}:R>"
        else:
            status_desc = "**Status:** Paused indefinitely by staff"
    else:
        title = "VEGA QUEUE"
        colour = VEGA_PURPLE
        status_desc = f"**{count} / {total}**   `{progress}`   **Avg ELO:** `{avg_elo}`"

    lines = [status_desc, ""]

    if queued_players:
        lines.append("**Queued Players**")
        for idx, p in enumerate(queued_players, 1):
            pid = p.get("discord_id")
            ign = p.get("ign") or p.get("discord_username") or f"Player{idx}"
            elo = p.get("elo", 1000)
            user_part = f"<@{pid}>" if pid else f"**{ign}**"
            lines.append(f"`{idx}.` {user_part} — `{elo} ELO`")
    else:
        lines.append("*Queue is currently empty. Click below to join.*")

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        colour=colour,
    )
    embed.set_footer(text="VEGA Esports • Competitive Matchmaking")

    view = QueueView(is_in_queue=user_is_in_queue)
    return embed, view


# =============================================================================
# 2. VOICE CHECK-IN UI RENDERER
# =============================================================================

class CheckInView(View):
    """Interactive view for Voice Check-in UI."""
    def __init__(self, lobby_vc_id: Optional[int] = None):
        super().__init__(timeout=None)
        if lobby_vc_id:
            vc_button = Button(
                label="JOIN VOICE",
                style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/@me/{lobby_vc_id}",
            )
            self.add_item(vc_button)
        else:
            vc_button = Button(
                label="JOIN VOICE",
                style=discord.ButtonStyle.primary,
                custom_id="test_join_voice",
            )
            self.add_item(vc_button)


def render_checkin_ui(
    match_id: int,
    connected_count: int,
    total_players: int = 10,
    lobby_vc_id: Optional[int] = None,
    deadline_ts: Optional[int] = None,
) -> Tuple[discord.Embed, View]:
    """
    Render visual Voice Check-in UI with dot indicators (● ● ● ○ ○).
    """
    dots = _get_dot_indicators(connected_count, total_players)
    is_ready = (connected_count >= total_players)
    
    colour = VEGA_GREEN if is_ready else VEGA_RED
    status_header = "10 / 10 READY" if is_ready else f"{connected_count} / {total_players} CONNECTED"
    
    desc_lines = [
        f"**{status_header}**",
        f"`{dots}`",
        "",
    ]
    if lobby_vc_id:
        desc_lines.append(f"**Lobby VC:** <#{lobby_vc_id}>")
    if deadline_ts and not is_ready:
        desc_lines.append(f"**Check-in Deadline:** <t:{deadline_ts}:R>")

    embed = discord.Embed(
        title=f"QUEUE #{match_id} — VOICE CHECK-IN",
        description="\n".join(desc_lines),
        colour=colour,
    )
    embed.set_footer(text="VEGA Esports • Voice Verification")

    view = CheckInView(lobby_vc_id=lobby_vc_id)
    return embed, view


# =============================================================================
# 3. DRAFT UI RENDERER
# =============================================================================

class DraftSelect(Select):
    """Dropdown for picking players in Draft."""
    def __init__(self, available_players: List[Dict[str, Any]]):
        options = []
        for p in available_players[:25]:
            pid = p.get("discord_id")
            ign = p.get("ign") or p.get("discord_username") or str(pid)
            elo = p.get("elo", 1000)
            options.append(
                discord.SelectOption(
                    label=f"{ign}",
                    value=str(pid),
                    description=f"Rating: {elo} ELO",
                )
            )
        super().__init__(
            placeholder="SELECT PLAYER...",
            min_values=1,
            max_values=1,
            options=options if options else [discord.SelectOption(label="None", value="0")],
            custom_id="test_draft_select",
        )


class DraftView(View):
    """View containing player pick dropdown."""
    def __init__(self, available_players: List[Dict[str, Any]]):
        super().__init__(timeout=None)
        if available_players:
            self.add_item(DraftSelect(available_players))


def render_draft_ui(
    match_id: int,
    step: int,
    total_steps: int,
    picker_name: str,
    team1_players: List[Dict[str, Any]],
    team2_players: List[Dict[str, Any]],
    available_players: List[Dict[str, Any]],
    captain1_id: int = 0,
    captain2_id: int = 0,
) -> Tuple[discord.Embed, View]:
    """
    Render clean two-column Team A vs Team B Draft UI with slots and captain indicators.
    """
    embed = discord.Embed(
        title=f"QUEUE #{match_id} — DRAFT · {step} / {total_steps}",
        description=f"**PICK · {picker_name.upper()}**",
        colour=VEGA_PURPLE,
    )

    # Format Team A Column
    t1_lines = []
    for i in range(5):
        if i < len(team1_players):
            p = team1_players[i]
            is_cap = (p.get("discord_id") == captain1_id or i == 0)
            prefix = "👑 " if is_cap else "● "
            ign = p.get("ign") or p.get("discord_username") or "Player"
            elo = p.get("elo", 1000)
            t1_lines.append(f"{prefix}**{ign}** `{elo}`")
        else:
            t1_lines.append("○ *Empty Slot*")
            
    # Format Team B Column
    t2_lines = []
    for i in range(5):
        if i < len(team2_players):
            p = team2_players[i]
            is_cap = (p.get("discord_id") == captain2_id or i == 0)
            prefix = "👑 " if is_cap else "● "
            ign = p.get("ign") or p.get("discord_username") or "Player"
            elo = p.get("elo", 1000)
            t2_lines.append(f"{prefix}**{ign}** `{elo}`")
        else:
            t2_lines.append("○ *Empty Slot*")

    embed.add_field(name="TEAM A", value="\n".join(t1_lines), inline=True)
    embed.add_field(name="TEAM B", value="\n".join(t2_lines), inline=True)

    if available_players:
        avail_text = ", ".join(f"`{p.get('ign', 'Player')}` ({p.get('elo', 1000)})" for p in available_players)
        embed.add_field(name="Remaining Pool", value=avail_text, inline=False)

    embed.set_footer(text="VEGA Esports • Captain Draft")
    view = DraftView(available_players)
    return embed, view


# =============================================================================
# 4. MAP VOTING UI RENDERER
# =============================================================================

class MapVoteView(View):
    """View with map choice buttons."""
    def __init__(self, map_options: List[str], user_voted_map: Optional[str] = None):
        super().__init__(timeout=None)
        for m in map_options:
            is_selected = (m == user_voted_map)
            btn = Button(
                label=f"✓ {m}" if is_selected else m,
                style=discord.ButtonStyle.success if is_selected else discord.ButtonStyle.secondary,
                custom_id=f"test_map_vote_{m.lower()}",
            )
            self.add_item(btn)


def render_map_vote_ui(
    match_id: int,
    map_options: List[str],
    votes_by_map: Dict[str, int],
    end_time_ts: int,
    user_voted_map: Optional[str] = None,
) -> Tuple[discord.Embed, View]:
    """
    Render clean Map Voting UI with live tally and compact timer.
    """
    lines = [
        f"**Voting Ends:** <t:{end_time_ts}:R>",
        "",
        "```",
    ]
    for m in map_options:
        tally = votes_by_map.get(m, 0)
        selected_mark = "  [YOUR VOTE]" if m == user_voted_map else ""
        lines.append(f"{m:<12} {tally:>2}{selected_mark}")
    lines.append("```")

    embed = discord.Embed(
        title=f"QUEUE #{match_id} — MAP VOTE",
        description="\n".join(lines),
        colour=VEGA_PURPLE,
    )
    embed.set_footer(text="VEGA Esports • Map Veto")
    view = MapVoteView(map_options, user_voted_map=user_voted_map)
    return embed, view


# =============================================================================
# 5. MATCH READY UI RENDERER
# =============================================================================

class MatchReadyView(View):
    """View with Submit Result button."""
    def __init__(self):
        super().__init__(timeout=None)
        submit_btn = Button(
            label="SUBMIT RESULT",
            style=discord.ButtonStyle.danger,
            custom_id="test_submit_result",
        )
        self.add_item(submit_btn)


def render_match_ready_ui(
    match_id: int,
    selected_map: str,
    team1_players: List[Dict[str, Any]],
    team2_players: List[Dict[str, Any]],
    team1_avg_elo: int,
    team2_avg_elo: int,
) -> Tuple[discord.Embed, View]:
    """
    Render Match Ready presentation with map banner and Team A vs Team B rosters.
    """
    t1_lines = []
    for p in team1_players:
        pid = p.get("discord_id")
        ign = p.get("ign") or p.get("discord_username") or f"<@{pid}>"
        t1_lines.append(f"<@{pid}>" if pid else f"**{ign}**")

    t2_lines = []
    for p in team2_players:
        pid = p.get("discord_id")
        ign = p.get("ign") or p.get("discord_username") or f"<@{pid}>"
        t2_lines.append(f"<@{pid}>" if pid else f"**{ign}**")

    embed = discord.Embed(
        title=f"QUEUE #{match_id} — MATCH READY",
        description=f"📍 **MAP: {selected_map.upper()}**",
        colour=VEGA_RED,
        timestamp=datetime.now(timezone.utc),
    )
    
    embed.add_field(
        name=f"TEAM A (`{team1_avg_elo} ELO`)",
        value="\n".join(t1_lines) if t1_lines else "—",
        inline=True,
    )
    embed.add_field(
        name=f"TEAM B (`{team2_avg_elo} ELO`)",
        value="\n".join(t2_lines) if t2_lines else "—",
        inline=True,
    )
    
    embed.set_footer(text="VEGA Esports • Match In Progress")
    
    # Attach map thumbnail if available
    clean_map = selected_map.strip().lower()
    map_filename = f"{clean_map}.png"
    maps_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "maps")
    map_path = os.path.join(maps_dir, map_filename)
    if os.path.exists(map_path):
        embed.set_thumbnail(url=f"attachment://{map_filename}")

    view = MatchReadyView()
    return embed, view
