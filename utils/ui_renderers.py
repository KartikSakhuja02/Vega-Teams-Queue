"""
utils/ui_renderers.py
Modern, compact, ultra-minimalist Discord UI renderers for VEGA Esports Matchmaking.
Designed for high readability, clean formatting, and modern aesthetic choices.
"""

import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import discord
from discord.ui import View, Button, Select

# Theme Palette Definitions
THEMES = {
    "NEON": {
        "primary": discord.Colour.from_str("#7C3AED"),     # Deep Violet
        "secondary": discord.Colour.from_str("#06B6D4"),   # Cyan
        "danger": discord.Colour.from_str("#EF4444"),      # Crimson
        "success": discord.Colour.from_str("#10B981"),     # Emerald
        "name": "Minimalist Neon Violet & Cyan",
    },
    "DARK": {
        "primary": discord.Colour.from_str("#27272A"),     # Charcoal Obsidian
        "secondary": discord.Colour.from_str("#E4E4E7"),   # Platinum
        "danger": discord.Colour.from_str("#DC2626"),      # Dark Red
        "success": discord.Colour.from_str("#16A34A"),     # Green
        "name": "Minimalist Dark Slate & Monochrome",
    },
    "VALORANT": {
        "primary": discord.Colour.from_str("#FF4655"),     # Valorant Red
        "secondary": discord.Colour.from_str("#0F172A"),   # Midnight Slate
        "danger": discord.Colour.from_str("#991B1B"),      # Deep Red
        "success": discord.Colour.from_str("#059669"),     # Emerald
        "name": "Minimalist Esports Crimson",
    },
}


def _get_progress_bar(current: int, total: int = 10, length: int = 10) -> str:
    """Generate a clean visual block progress bar."""
    filled = int(round((current / total) * length))
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)


def _get_dot_indicators(connected: int, total: int = 10) -> str:
    """Generate clean dot indicators for voice check-in."""
    connected = max(0, min(total, connected))
    return " ".join(["🟢" if i < connected else "🔴" for i in range(total)])


# =============================================================================
# 1. QUEUE UI RENDERER
# =============================================================================

class QueueView(View):
    """Interactive view for the Queue UI."""
    def __init__(self, is_in_queue: bool = False):
        super().__init__(timeout=None)
        
        if not is_in_queue:
            self.add_item(Button(
                label="⚔️ JOIN QUEUE",
                style=discord.ButtonStyle.success,
                custom_id="test_solo_queue_join",
            ))
        else:
            self.add_item(Button(
                label="🚪 LEAVE QUEUE",
                style=discord.ButtonStyle.danger,
                custom_id="test_solo_queue_leave",
            ))
        self.add_item(Button(
            label="🔄 REFRESH",
            style=discord.ButtonStyle.secondary,
            custom_id="test_solo_queue_refresh",
        ))


def render_queue_ui(
    queued_players: List[Dict[str, Any]],
    is_paused: bool = False,
    pause_until: Optional[float] = None,
    user_is_in_queue: bool = False,
    theme_key: str = "NEON",
) -> Tuple[discord.Embed, View]:
    """
    Render compact, ultra-minimalist Queue UI.
    Shows 7/10 indicator, progress bar, avg ELO, and visible player list.
    """
    theme = THEMES.get(theme_key.upper(), THEMES["NEON"])
    count = len(queued_players)
    total = 10
    
    elos = [p.get("elo", 1000) for p in queued_players]
    avg_elo = round(sum(elos) / len(elos)) if elos else 1000
    progress = _get_progress_bar(count, total, length=10)

    if is_paused:
        title = "⚡ VEGA QUEUE — PAUSED"
        colour = theme["danger"]
        status_desc = f"**STATUS:** Paused by Staff"
        if pause_until:
            status_desc += f" • Reopens <t:{int(pause_until)}:R>"
    else:
        title = "⚡ VEGA COMPETITIVE QUEUE"
        colour = theme["primary"]
        status_desc = f"`[ {progress} ]` **{count} / {total} Players** • Avg `{avg_elo} ELO`"

    lines = [status_desc, ""]

    if queued_players:
        lines.append("**PLAYERS IN QUEUE**")
        for idx, p in enumerate(queued_players, 1):
            pid = p.get("discord_id")
            ign = p.get("ign") or p.get("discord_username") or f"Player{idx}"
            elo = p.get("elo", 1000)
            user_part = f"<@{pid}>" if pid else f"**{ign}**"
            lines.append(f"`▸ {idx:02d}` │ {user_part} — `{elo} ELO`")
    else:
        lines.append("*Queue is currently empty. Click Join Queue below.*")

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        colour=colour,
    )
    embed.set_footer(text=f"VEGA ESPORTS • {theme['name'].upper()}")

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
            self.add_item(Button(
                label="🎙️ JOIN LOBBY VC",
                style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/@me/{lobby_vc_id}",
            ))
        else:
            self.add_item(Button(
                label="🎙️ JOIN LOBBY VC",
                style=discord.ButtonStyle.primary,
                custom_id="test_join_voice",
            ))


def render_checkin_ui(
    match_id: int,
    connected_count: int,
    total_players: int = 10,
    lobby_vc_id: Optional[int] = None,
    deadline_ts: Optional[int] = None,
    theme_key: str = "NEON",
) -> Tuple[discord.Embed, View]:
    """
    Render visual Voice Check-in UI with status indicators.
    """
    theme = THEMES.get(theme_key.upper(), THEMES["NEON"])
    dots = _get_dot_indicators(connected_count, total_players)
    is_ready = (connected_count >= total_players)
    
    colour = theme["success"] if is_ready else theme["danger"]
    status_header = "10 / 10 READY — COMMENCING DRAFT" if is_ready else f"{connected_count} / {total_players} PLAYERS IN VOICE"
    
    desc_lines = [
        f"**{status_header}**",
        f"{dots}",
        "",
    ]
    if lobby_vc_id:
        desc_lines.append(f"**Lobby VC:** <#{lobby_vc_id}>")
    if deadline_ts and not is_ready:
        desc_lines.append(f"⏰ **Check-in Deadline:** <t:{deadline_ts}:R> (<t:{deadline_ts}:T>)")

    embed = discord.Embed(
        title=f"🔊 QUEUE #{match_id} — VOICE CHECK-IN",
        description="\n".join(desc_lines),
        colour=colour,
    )
    embed.set_footer(text="VEGA ESPORTS • Connect to lobby VC to confirm check-in")

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
            uname = p.get("discord_username") or p.get("username")
            elo = p.get("elo", 1000)
            label = f"{ign} (@{uname})" if uname else f"{ign} ({pid})"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(pid),
                    description=f"IGN: {ign} | ID: {pid} | ELO: {elo}"[:100],
                )
            )
        super().__init__(
            placeholder="SELECT PLAYER TO PICK...",
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
    theme_key: str = "NEON",
) -> Tuple[discord.Embed, View]:
    """
    Render clean two-column Team A vs Team B Draft UI with slots, Discord ID + IGN, and captain indicators.
    """
    theme = THEMES.get(theme_key.upper(), THEMES["NEON"])
    embed = discord.Embed(
        title=f"⚔️ QUEUE #{match_id} — CAPTAIN DRAFT [{step}/{total_steps}]",
        description=f"🎯 **CURRENT TURN:** **{picker_name}**",
        colour=theme["primary"],
    )

    t1_lines = []
    for i, p in enumerate(team1_players):
        is_cap = (p.get("discord_id") == captain1_id or i == 0)
        prefix = "👑 " if is_cap else "▫️ "
        ign = p.get("ign") or p.get("discord_username") or "Player"
        uname = p.get("discord_username") or p.get("username")
        d_id = p.get("discord_id")
        user_ref = f"<@{d_id}>" if d_id else (f"@{uname}" if uname else f"@{ign}")
        elo = p.get("elo", 1000)
        t1_lines.append(f"{prefix}{user_ref} (**{ign}**) `({elo})`")

    t2_lines = []
    for i, p in enumerate(team2_players):
        is_cap = (p.get("discord_id") == captain2_id or i == 0)
        prefix = "👑 " if is_cap else "▫️ "
        ign = p.get("ign") or p.get("discord_username") or "Player"
        uname = p.get("discord_username") or p.get("username")
        d_id = p.get("discord_id")
        user_ref = f"<@{d_id}>" if d_id else (f"@{uname}" if uname else f"@{ign}")
        elo = p.get("elo", 1000)
        t2_lines.append(f"{prefix}{user_ref} (**{ign}**) `({elo})`")

    embed.add_field(name=f"─── TEAM A [{len(team1_players)}/5] ───", value="\n".join(t1_lines), inline=True)
    embed.add_field(name=f"─── TEAM B [{len(team2_players)}/5] ───", value="\n".join(t2_lines), inline=True)

    if available_players:
        avail_items = []
        for p in available_players:
            ign = p.get("ign") or p.get("discord_username") or "Player"
            uname = p.get("discord_username") or p.get("username")
            d_id = p.get("discord_id")
            user_ref = f"<@{d_id}>" if d_id else (f"@{uname}" if uname else f"@{ign}")
            elo = p.get("elo", 1000)
            avail_items.append(f"{user_ref} (**{ign}**) `({elo})`")
        embed.add_field(name="─── AVAILABLE PLAYERS ───", value=" • ".join(avail_items), inline=False)

    embed.set_footer(text="VEGA ESPORTS • Select player from dropdown below")
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
                label=m,
                style=discord.ButtonStyle.primary if is_selected else discord.ButtonStyle.secondary,
                custom_id=f"test_map_vote_{m.lower()}",
            )
            self.add_item(btn)


def render_map_vote_ui(
    match_id: int,
    map_options: List[str],
    votes_by_map: Dict[str, int],
    end_time_ts: int,
    user_voted_map: Optional[str] = None,
    theme_key: str = "NEON",
) -> Tuple[discord.Embed, View]:
    """
    Render clean Map Voting UI with live tally, user vote indicator, and compact timer.
    """
    theme = THEMES.get(theme_key.upper(), THEMES["NEON"])
    lines = [
        f"⏳ **Voting Deadline:** <t:{end_time_ts}:R>",
    ]
    if user_voted_map:
        lines.append(f"🎯 **Your Vote:** **{user_voted_map}**")
    lines.append("")

    for m in map_options:
        tally = votes_by_map.get(m, 0)
        selected_mark = "  ◀ YOUR VOTE" if user_voted_map and m == user_voted_map else ""
        bar = "█" * tally + "░" * (10 - tally)
        lines.append(f"`{m:<10}` `{bar}` **{tally} votes**{selected_mark}")

    embed = discord.Embed(
        title=f"🗺️ QUEUE #{match_id} — MAP VOTE",
        description="\n".join(lines),
        colour=theme["secondary"],
    )
    embed.set_footer(text="VEGA ESPORTS • Click a map button below to vote")
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
            label="🏆 SUBMIT RESULT",
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
    theme_key: str = "NEON",
) -> Tuple[discord.Embed, View]:
    """
    Render Match Ready presentation with map banner and Team A vs Team B rosters.
    """
    theme = THEMES.get(theme_key.upper(), THEMES["NEON"])
    t1_lines = []
    for p in team1_players:
        pid = p.get("discord_id")
        ign = p.get("ign") or p.get("discord_username") or f"Player"
        elo = p.get("elo", 1000)
        t1_lines.append(f"<@{pid}>" if pid else f"**{ign}** (`{elo}`)")

    t2_lines = []
    for p in team2_players:
        pid = p.get("discord_id")
        ign = p.get("ign") or p.get("discord_username") or f"Player"
        elo = p.get("elo", 1000)
        t2_lines.append(f"<@{pid}>" if pid else f"**{ign}** (`{elo}`)")

    embed = discord.Embed(
        title=f"🏆 QUEUE #{match_id} — MATCH READY",
        description=f"📍 **SELECTED MAP:** `{selected_map.upper()}`\n> Use `/submit-result` when the match concludes.",
        colour=theme["primary"],
        timestamp=datetime.now(timezone.utc),
    )
    
    embed.add_field(
        name=f"─── TEAM A (`{team1_avg_elo} ELO`) ───",
        value="\n".join(t1_lines) if t1_lines else "—",
        inline=True,
    )
    embed.add_field(
        name=f"─── TEAM B (`{team2_avg_elo} ELO`) ───",
        value="\n".join(t2_lines) if t2_lines else "—",
        inline=True,
    )
    
    embed.set_footer(text="VEGA ESPORTS • Match In Progress")
    
    clean_map = selected_map.strip().lower()
    map_filename = f"{clean_map}.png"
    maps_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "maps")
    map_path = os.path.join(maps_dir, map_filename)
    if os.path.exists(map_path):
        embed.set_thumbnail(url=f"attachment://{map_filename}")

    view = MatchReadyView()
    return embed, view


# =============================================================================
# 6. MATCH RESULT UI RENDERER
# =============================================================================

def render_match_result_ui(
    match_id: int,
    winning_team: int,
    team1_score: int,
    team2_score: int,
    selected_map: str,
    mvp_name: str,
    theme_key: str = "NEON",
) -> discord.Embed:
    """
    Render clean Match Result Scorecard embed.
    """
    theme = THEMES.get(theme_key.upper(), THEMES["NEON"])
    winner_str = "TEAM A VICTORY" if winning_team == 1 else ("TEAM B VICTORY" if winning_team == 2 else "DRAW")
    score_str = f"**TEAM A** `{team1_score}`  —  `{team2_score}` **TEAM B**"

    embed = discord.Embed(
        title=f"🏆 MATCH #{match_id} RESULTS — {winner_str}",
        description=f"📍 **MAP:** `{selected_map.upper()}`\n\n{score_str}\n\n🌟 **MVP:** `{mvp_name}` (+25 ELO)",
        colour=theme["success"] if winning_team != 0 else theme["secondary"],
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="VEGA ESPORTS • Official Match Record")
    return embed

