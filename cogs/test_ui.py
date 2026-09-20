"""
Discord Components V2 UI Redesign & Simulation Testbed for Vega Queue.
Allows previewing, testing, and interacting with all refreshed bot UIs
using Discord's new Components V2 architecture (LayoutView, Container,
Section, TextDisplay, Separator, ActionRow, etc.) in an isolated test channel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import discord
from discord import app_commands, ui
from discord.ext import commands

log = logging.getLogger("vega.test_ui")

# Environment variable for the test channel ID
TEST_UI_CHANNEL_ENV = "TEST_UI_CHANNEL_ID"


# ---------------------------------------------------------------------------
# Stage 1: Player Registration V2
# ---------------------------------------------------------------------------
class RegistrationModal(ui.Modal, title="Register In-Game ID"):
    ign = ui.TextInput(
        label="In-Game ID & Tag",
        placeholder="e.g. TenZ#NA1 or Kartik#VEGA",
        min_length=3,
        max_length=32,
        required=True,
    )
    region = ui.TextInput(
        label="Preferred Region",
        placeholder="e.g. EU Central, EU West, NA East",
        min_length=2,
        max_length=20,
        required=True,
    )
    tracker_url = ui.TextInput(
        label="Tracker Profile Link (Optional)",
        placeholder="https://tracker.gg/valorant/profile/...",
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"✅ **[SIMULATION] Registration Submitted!**\n"
            f"• **In-Game ID:** `{self.ign.value}`\n"
            f"• **Region:** `{self.region.value}`\n"
            f"• **Tracker:** `{self.tracker_url.value or 'None'}`\n\n"
            f"*Your account is verified and ready for Vega Queue matchmaking.*",
            ephemeral=True,
        )


class RegistrationV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0x5865F2))

        header_text = (
            "# ✦ VEGA ESPORTS — PLAYER REGISTRATION\n"
            "`[ STAGE 1 / 10 ]` `[ STATUS: REGISTRATION OPEN ]` `[ 5v5 COMPETITIVE QUEUE ]`\n\n"
            "Welcome to **Vega Esports Matchmaking**. To participate in 10-man pickup games, "
            "automated scrims, and seasonal MMR divisions, you must link your verified in-game account.\n\n"
            "### 📋 Requirements\n"
            "• Provide your authentic In-Game ID & Tagline\n"
            "• Select your primary matchmaking server region\n"
            "• Join the mandatory match voice lobbies during games\n"
            "• Maintain good sportsmanship and adhere to league rules\n"
        )
        container.add_item(ui.TextDisplay(header_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        btn_register = ui.Button(
            label="Register In-Game ID",
            style=discord.ButtonStyle.primary,
            emoji="📝",
            custom_id="test_ui_register_btn",
        )

        async def reg_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(RegistrationModal())

        btn_register.callback = reg_callback

        btn_rules = ui.Button(
            label="Rules & Conduct",
            style=discord.ButtonStyle.secondary,
            emoji="📜",
            custom_id="test_ui_rules_btn",
        )

        async def rules_callback(interaction: discord.Interaction) -> None:
            rules_msg = (
                "### 📜 Vega Matchmaking Guidelines\n"
                "1. **No Dodging:** Once 10 players are found, check-in is mandatory within 2 minutes.\n"
                "2. **Voice Attendance:** All players must remain in the match VC for callouts.\n"
                "3. **Scoreboard Submission:** Winner or Captain must upload the final scoreboard screenshot.\n"
                "4. **Zero Tolerance:** Cheating, toxicity, or smurfing will result in immediate hardware/league ban."
            )
            await interaction.response.send_message(rules_msg, ephemeral=True)

        btn_rules.callback = rules_callback

        btn_status = ui.Button(
            label="Check My Status",
            style=discord.ButtonStyle.secondary,
            emoji="🔍",
            custom_id="test_ui_status_btn",
        )

        async def status_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                f"👤 **Player Status for {interaction.user.mention}:**\n"
                f"• In-Game ID: `KartikSakhuja#VEGA`\n"
                f"• Division: `Immortal / Radiant (Div 1)`\n"
                f"• Trust Score: `100% (Eligible for Queue)`",
                ephemeral=True,
            )

        btn_status.callback = status_callback

        row.add_item(btn_register)
        row.add_item(btn_rules)
        row.add_item(btn_status)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Stage 2: Matchmaking Queue Lobby V2 (VEGA QUEUE)
# ---------------------------------------------------------------------------
class QueueLobbyV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0xFF4655))

        lobby_text = (
            "# ⚔️ VEGA QUEUE [ 7 / 10 ]\n"
            "`[ STAGE 2 / 10 ]` 🟢 **STATUS: MATCHMAKING ACTIVE** • `AVG MMR: 1,420` • `MAP POOL: ACTIVE 7`\n\n"
            "### 👥 Current Queue Roster\n"
            "`1.` 👑 **KartikSakhuja** — `1,650 ELO` `[IMMORTAL III]` `[EU Central]` • *2m ago*\n"
            "`2.` 🎯 **PhantomSniper** — `1,480 ELO` `[DIAMOND II]` `[EU West]` • *4m ago*\n"
            "`3.` ⚔️ **VandalGod** — `1,510 ELO` `[ASCENDANT I]` `[EU Central]` • *5m ago*\n"
            "`4.` 🛡️ **ShadowStep** — `1,460 ELO` `[DIAMOND III]` `[EU West]` • *6m ago*\n"
            "`5.` ⚡ **AceMachine** — `1,540 ELO` `[ASCENDANT II]` `[EU Central]` • *7m ago*\n"
            "`6.` 💨 **NexusPrime** — `1,320 ELO` `[PLATINUM III]` `[EU Central]` • *8m ago*\n"
            "`7.` 🐍 **ViperMain** — `1,390 ELO` `[DIAMOND I]` `[EU West]` • *9m ago*\n\n"
            "*Need 3 more players to initialize Captain Draft and Voice Check-in.*"
        )
        container.add_item(ui.TextDisplay(lobby_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        btn_join = ui.Button(
            label="Join Queue",
            style=discord.ButtonStyle.success,
            emoji="🎮",
            custom_id="test_ui_join_btn",
        )

        async def join_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                f"✅ **[SIMULATION] Joined Vega Queue!** Your current position: `8 / 10`.",
                ephemeral=True,
            )

        btn_join.callback = join_callback

        btn_leave = ui.Button(
            label="Leave Queue",
            style=discord.ButtonStyle.danger,
            emoji="🚪",
            custom_id="test_ui_leave_btn",
        )

        async def leave_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "🚪 **[SIMULATION] You have left the queue.** No penalty applied.",
                ephemeral=True,
            )

        btn_leave.callback = leave_callback

        btn_refresh = ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            custom_id="test_ui_refresh_btn",
        )

        async def refresh_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "🔄 **[SIMULATION] Queue panel refreshed.** Live ping: `14ms`.",
                ephemeral=True,
            )

        btn_refresh.callback = refresh_callback

        btn_info = ui.Button(
            label="Queue Info",
            style=discord.ButtonStyle.secondary,
            emoji="⚙️",
            custom_id="test_ui_info_btn",
        )

        async def info_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "⚙️ **Vega Queue System:** Matchmaking balances teams by ELO with top 2 players designated as Captains.",
                ephemeral=True,
            )

        btn_info.callback = info_callback

        row.add_item(btn_join)
        row.add_item(btn_leave)
        row.add_item(btn_refresh)
        row.add_item(btn_info)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Stage 3: Match Found & Voice Check-In V2
# ---------------------------------------------------------------------------
class VoiceCheckInV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0xF1C40F))

        deadline_ts = int(time.time()) + 120
        checkin_text = (
            "# 🚨 MATCH #420 FOUND — VOICE CHECK-IN\n"
            f"`[ STAGE 3 / 10 ]` **ALL 10 PLAYERS MUST CONNECT TO VOICE BEFORE <t:{deadline_ts}:R>**\n"
            f"Lobby Voice Channel: 🔊 `Matchmaking VC #1`\n\n"
            "### 📊 Attendance Status `[ 7 / 10 Connected ]`\n"
            "**TEAM 1 (3/5 In Voice):**\n"
            "🟢 `KartikSakhuja (Cap)` • 🟢 `PhantomSniper` • 🟢 `VandalGod` • 🔴 `ShadowStep` • 🔴 `AceMachine`\n\n"
            "**TEAM 2 (4/5 In Voice):**\n"
            "🟢 `ViperMain (Cap)` • 🟢 `NexusPrime` • 🟢 `CyberBlade` • 🟢 `FrostBite` • 🔴 `BlitzKrieg`\n\n"
            "> ⚠️ *Failure to connect to voice before deadline results in a 24h queue suspension.*"
        )
        container.add_item(ui.TextDisplay(checkin_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        btn_ready = ui.Button(
            label="Ready Up & Check In",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="test_ui_ready_btn",
        )

        async def ready_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "✅ **[SIMULATION] Checked in successfully!** Voice connection verified.",
                ephemeral=True,
            )

        btn_ready.callback = ready_callback

        btn_connect = ui.Button(
            label="Voice Channel",
            style=discord.ButtonStyle.secondary,
            emoji="🔊",
            custom_id="test_ui_vc_btn",
        )

        async def connect_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "🔊 **[SIMULATION] Join the designated lobby voice channel in the Discord sidebar.**",
                ephemeral=True,
            )

        btn_connect.callback = connect_callback

        btn_decline = ui.Button(
            label="Decline (Penalty)",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id="test_ui_decline_btn",
        )

        async def decline_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "⚠️ **[SIMULATION] Declining after match creation causes a queue cooldown.**",
                ephemeral=True,
            )

        btn_decline.callback = decline_callback

        row.add_item(btn_ready)
        row.add_item(btn_connect)
        row.add_item(btn_decline)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Stage 4: Captains & Player Draft V2
# ---------------------------------------------------------------------------
class DraftPhaseV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0x9B59B6))

        draft_text = (
            "# 👑 CAPTAIN DRAFT — MATCH #420 [Turn 3 / 7]\n"
            "`[ STAGE 4 / 10 ]` ⚡ **CURRENT PICK:** `KartikSakhuja (Captain A)` — *25s remaining*\n\n"
            "### 🛡️ Team Rosters\n"
            "**TEAM A (2/5):**\n"
            "👑 `KartikSakhuja (Cap)` • `1,650 ELO`\n"
            "⚔️ `VandalGod (Duelist)` • `1,510 ELO`\n"
            "— *Slot 3 (Picking...)*\n"
            "— *Slot 4*\n"
            "— *Slot 5*\n\n"
            "**TEAM B (2/5):**\n"
            "👑 `ViperMain (Cap)` • `1,590 ELO`\n"
            "🛡️ `AceMachine (Sentinel)` • `1,540 ELO`\n"
            "— *Slot 3*\n"
            "— *Slot 4*\n"
            "— *Slot 5*\n\n"
            "### 🎯 Available Players Pool (6 Remaining)\n"
            "• `ShadowStep` (1,460 ELO) `[Controller]`\n"
            "• `PhantomSniper` (1,480 ELO) `[Initiator]`\n"
            "• `NexusPrime` (1,320 ELO) `[Flex]`\n"
            "• `FrostBite` (1,380 ELO) `[Sentinel]`\n"
            "• `BlitzKrieg` (1,410 ELO) `[Duelist]`\n"
            "• `CyberBlade` (1,430 ELO) `[Initiator]`\n"
        )
        container.add_item(ui.TextDisplay(draft_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        select_pick = ui.Select(
            placeholder="Select a player to draft...",
            options=[
                discord.SelectOption(
                    label="PhantomSniper (1,480 ELO)",
                    value="PhantomSniper",
                    description="Role: Initiator • Sova/Fade Main",
                    emoji="🎯",
                ),
                discord.SelectOption(
                    label="ShadowStep (1,460 ELO)",
                    value="ShadowStep",
                    description="Role: Controller • Omen/Astra Main",
                    emoji="🛡️",
                ),
                discord.SelectOption(
                    label="CyberBlade (1,430 ELO)",
                    value="CyberBlade",
                    description="Role: Initiator • Breach/KAYO Main",
                    emoji="⚡",
                ),
                discord.SelectOption(
                    label="BlitzKrieg (1,410 ELO)",
                    value="BlitzKrieg",
                    description="Role: Duelist • Raze Main",
                    emoji="💣",
                ),
                discord.SelectOption(
                    label="FrostBite (1,380 ELO)",
                    value="FrostBite",
                    description="Role: Sentinel • Killjoy Main",
                    emoji="❄️",
                ),
            ],
            custom_id="test_ui_draft_select",
        )

        async def draft_callback(interaction: discord.Interaction) -> None:
            picked = select_pick.values[0] if select_pick.values else "Selected Player"
            await interaction.response.send_message(
                f"👑 **[SIMULATION] Draft Pick Confirmed!** Captain drafted **{picked}** into Team A.",
                ephemeral=True,
            )

        select_pick.callback = draft_callback
        row.add_item(select_pick)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Stage 5: Tactical Map Veto V2
# ---------------------------------------------------------------------------
class MapVetoV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0x3498DB))

        veto_text = (
            "# 🗺️ MAP VETO — MATCH #420\n"
            "`[ STAGE 5 / 10 ]` Captains alternate banning maps until 1 decisive battleground remains.\n\n"
            "⚡ **CURRENT TURN:** 👑 `ViperMain (Team B)` to **BAN** a map *(30s remaining)*\n\n"
            "### ❌ Banned Maps\n"
            "• ~~Ascent~~ *(Banned by Team A)*\n"
            "• ~~Haven~~ *(Banned by Team B)*\n"
            "• ~~Lotus~~ *(Banned by Team A)*\n\n"
            "### 🎯 Remaining Active Map Pool\n"
            "**1. BIND** • **2. SUNSET** • **3. SPLIT**\n"
        )
        container.add_item(ui.TextDisplay(veto_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        btn_ban_bind = ui.Button(
            label="Ban Bind",
            style=discord.ButtonStyle.danger,
            emoji="🚫",
            custom_id="test_ui_ban_bind",
        )
        btn_ban_sunset = ui.Button(
            label="Ban Sunset",
            style=discord.ButtonStyle.danger,
            emoji="🚫",
            custom_id="test_ui_ban_sunset",
        )
        btn_ban_split = ui.Button(
            label="Ban Split",
            style=discord.ButtonStyle.danger,
            emoji="🚫",
            custom_id="test_ui_ban_split",
        )

        async def ban_bind_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "🚫 **[SIMULATION] Map BIND banned by Captain.**", ephemeral=True
            )

        async def ban_sunset_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "🚫 **[SIMULATION] Map SUNSET banned by Captain.**", ephemeral=True
            )

        async def ban_split_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "🚫 **[SIMULATION] Map SPLIT banned by Captain.**", ephemeral=True
            )

        btn_ban_bind.callback = ban_bind_cb
        btn_ban_sunset.callback = ban_sunset_cb
        btn_ban_split.callback = ban_split_cb

        row.add_item(btn_ban_bind)
        row.add_item(btn_ban_sunset)
        row.add_item(btn_ban_split)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Stage 6: Live Match Room & In-Progress Hub V2
# ---------------------------------------------------------------------------
class LiveMatchRoomV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0xE74C3C))

        match_text = (
            "# ⚔️ MATCH #420 — LIVE ON BIND\n"
            "`[ STAGE 6 / 10 ]` 🔴 **STATUS: IN PROGRESS** • `SERVER: EU Central (Frankfurt)`\n\n"
            "### 🛡️ Team Rosters & Comms\n"
            "**TEAM 1 (Average ELO: 1,495)** • 🔊 `Voice: Team 1 VC`\n"
            "👑 `KartikSakhuja` • ⚔️ `VandalGod` • 🎯 `PhantomSniper` • 🛡️ `FrostBite` • 💣 `BlitzKrieg`\n\n"
            "**TEAM 2 (Average ELO: 1,488)** • 🔊 `Voice: Team 2 VC`\n"
            "👑 `ViperMain` • 🛡️ `AceMachine` • 💨 `ShadowStep` • ⚡ `CyberBlade` • ⚔️ `NexusPrime`\n\n"
            "### 📌 Match Instructions\n"
            "• Custom game mode: **Tournament (Standard 5v5)** with Cheats OFF\n"
            "• At the conclusion of the match, take a clear screenshot of the final scoreboard\n"
            "• Click **Submit Scoreboard** below to let AI Vision process your stats and update MMR."
        )
        container.add_item(ui.TextDisplay(match_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        btn_submit = ui.Button(
            label="Submit Scoreboard",
            style=discord.ButtonStyle.primary,
            emoji="📸",
            custom_id="test_ui_submit_score",
        )

        async def submit_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "📸 **[SIMULATION] Upload modal / file prompt opened:** Upload your 1080p+ endgame scoreboard screenshot.",
                ephemeral=True,
            )

        btn_submit.callback = submit_cb

        btn_dispute = ui.Button(
            label="Call Staff / Dispute",
            style=discord.ButtonStyle.secondary,
            emoji="⚠️",
            custom_id="test_ui_dispute",
        )

        async def dispute_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "⚠️ **[SIMULATION] Staff alerted:** A moderator has been pinged to review Match #420.",
                ephemeral=True,
            )

        btn_dispute.callback = dispute_cb

        btn_cancel = ui.Button(
            label="Cancel Match",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id="test_ui_cancel",
        )

        async def cancel_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "✕ **[SIMULATION] Match cancel vote initiated:** Requires 6/10 player votes.",
                ephemeral=True,
            )

        btn_cancel.callback = cancel_cb

        row.add_item(btn_submit)
        row.add_item(btn_dispute)
        row.add_item(btn_cancel)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Stage 7: Scoreboard Submission & OCR AI Verification V2
# ---------------------------------------------------------------------------
class ScoreOCRVerificationV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0x2ECC71))

        ocr_text = (
            "# 🏆 MATCH #420 CONCLUDED — 13 : 9 (Team 1 Victory)\n"
            "`[ STAGE 7 / 10 ]` ✨ **AI VISION OCR ANALYSIS: 98.4% CONFIDENCE (VERIFIED)**\n"
            "`MAP: BIND` • `ROUNDS: 22` • `DURATION: 34m 12s`\n\n"
            "### 🎖️ Performance & ELO Adjustments\n"
            "**TEAM 1 (VICTORS) — AVERAGE GAIN +20 ELO:**\n"
            "⭐ **KartikSakhuja (MVP):** `26/12/7` • `342 ACS` • **`+24 ELO`** `(1,650 -> 1,674)`\n"
            "• **VandalGod:** `21/14/5` • `285 ACS` • **`+21 ELO`** `(1,510 -> 1,531)`\n"
            "• **PhantomSniper:** `18/13/9` • `240 ACS` • **`+19 ELO`** `(1,480 -> 1,499)`\n"
            "• **FrostBite:** `15/15/8` • `210 ACS` • **`+17 ELO`** `(1,380 -> 1,397)`\n"
            "• **BlitzKrieg:** `14/16/6` • `195 ACS` • **`+16 ELO`** `(1,410 -> 1,426)`\n\n"
            "**TEAM 2 (DEFEATED) — AVERAGE LOSS -21 ELO:**\n"
            "• **ViperMain:** `22/16/4` • `290 ACS` • **`-18 ELO`** `(1,590 -> 1,572)`\n"
            "• **AceMachine:** `19/17/5` • `255 ACS` • **`-20 ELO`** `(1,540 -> 1,520)`\n"
            "• **ShadowStep:** `16/18/6` • `220 ACS` • **`-22 ELO`** `(1,460 -> 1,438)`\n"
            "• **CyberBlade:** `14/19/7` • `190 ACS` • **`-23 ELO`** `(1,430 -> 1,407)`\n"
            "• **NexusPrime:** `11/20/4` • `160 ACS` • **`-24 ELO`** `(1,320 -> 1,296)`\n"
        )
        container.add_item(ui.TextDisplay(ocr_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        btn_confirm = ui.Button(
            label="Confirm & Apply MMR",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="test_ui_confirm_ocr",
        )

        async def confirm_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "✅ **[SIMULATION] Match scores committed to database.** Leaderboards and player dossiers updated.",
                ephemeral=True,
            )

        btn_confirm.callback = confirm_cb

        btn_edit = ui.Button(
            label="Edit Scores",
            style=discord.ButtonStyle.secondary,
            emoji="✏️",
            custom_id="test_ui_edit_ocr",
        )

        async def edit_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "✏️ **[SIMULATION] Score editor opened:** Adjust individual round counts or player stats.",
                ephemeral=True,
            )

        btn_edit.callback = edit_cb

        btn_dispute = ui.Button(
            label="Dispute Results",
            style=discord.ButtonStyle.danger,
            emoji="🚩",
            custom_id="test_ui_dispute_ocr",
        )

        async def dispute_ocr_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "🚩 **[SIMULATION] Dispute logged:** Match result placed under administrative review.",
                ephemeral=True,
            )

        btn_dispute.callback = dispute_ocr_cb

        row.add_item(btn_confirm)
        row.add_item(btn_edit)
        row.add_item(btn_dispute)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Stage 8: Competitive Leaderboard V2
# ---------------------------------------------------------------------------
class LeaderboardV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0xFFD700))

        lb_text = (
            "# 🏆 VEGA COMPETITIVE LEADERBOARD — SEASON 2\n"
            "`[ STAGE 8 / 10 ]` `DIVISION 1 (IMMORTAL & RADIANT)` • `TOTAL RANKED PLAYERS: 148`\n\n"
            "### 👑 The Podium\n"
            "🥇 **1st — KartikSakhuja** • `1,674 ELO` `[72.4% WR • 14 Streak]` `[IMMORTAL III]`\n"
            "🥈 **2nd — ViperMain** • `1,572 ELO` `[65.1% WR • 4 Streak]` `[IMMORTAL II]`\n"
            "🥉 **3rd — AceMachine** • `1,540 ELO` `[61.8% WR • 2 Streak]` `[IMMORTAL I]`\n\n"
            "### 🏅 Top Contenders (Ranks 4 - 10)\n"
            "`04.` **VandalGod** — `1,531 ELO` `(63% WR • 48 Matches)`\n"
            "`05.` **PhantomSniper** — `1,499 ELO` `(59% WR • 52 Matches)`\n"
            "`06.` **ShadowStep** — `1,438 ELO` `(56% WR • 41 Matches)`\n"
            "`07.` **BlitzKrieg** — `1,426 ELO` `(54% WR • 39 Matches)`\n"
            "`08.` **CyberBlade** — `1,407 ELO` `(52% WR • 45 Matches)`\n"
            "`09.` **FrostBite** — `1,397 ELO` `(51% WR • 36 Matches)`\n"
            "`10.` **NexusPrime** — `1,296 ELO` `(48% WR • 30 Matches)`\n"
        )
        container.add_item(ui.TextDisplay(lb_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        btn_prev = ui.Button(label="Prev", style=discord.ButtonStyle.secondary, emoji="⬅️")
        btn_page = ui.Button(label="Page 1 / 6", style=discord.ButtonStyle.secondary, disabled=True)
        btn_next = ui.Button(label="Next", style=discord.ButtonStyle.secondary, emoji="➡️")
        btn_filter = ui.Button(label="Filter Division", style=discord.ButtonStyle.primary, emoji="🔍")

        async def filter_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "🔍 **[SIMULATION] Filter Active:** Switching to Division 2 (Diamond / Ascendant).",
                ephemeral=True,
            )

        btn_filter.callback = filter_cb

        row.add_item(btn_prev)
        row.add_item(btn_page)
        row.add_item(btn_next)
        row.add_item(btn_filter)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Stage 9: Player Profile Dossier V2
# ---------------------------------------------------------------------------
class PlayerProfileV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0x00D2FF))

        profile_text = (
            "# 👤 PLAYER DOSSIER — KartikSakhuja#VEGA\n"
            "`[ STAGE 9 / 10 ]` ✨ **CURRENT TIER: IMMORTAL III** • `LEADERBOARD RANK: #1 (DIVISION 1)`\n\n"
            "### 📊 Competitive Metrics\n"
            "• **Current MMR:** `1,674 ELO` *(Peak: 1,710 ELO)*\n"
            "• **Win / Loss Record:** `42W - 16L` **(72.4% Win Rate)**\n"
            "• **Match MVPs:** `18 Awards` *(31.0% Match Dominance Rate)*\n"
            "• **Average Combat Score (ACS):** `284.6`\n"
            "• **Recent Form:** 🟢 `W` • 🟢 `W` • 🟢 `W` • 🔴 `L` • 🟢 `W`\n\n"
            "### 🎮 Agent Specializations\n"
            "• **Jett (Duelist):** `62% Pick Rate • 1.42 K/D`\n"
            "• **Reyna (Duelist):** `24% Pick Rate • 1.35 K/D`\n"
            "• **Omen (Controller):** `14% Pick Rate • 1.18 K/D`\n\n"
            "### 🌐 League Profile\n"
            "• **Server Region:** `EU Central (Frankfurt)`\n"
            "• **Fair Play & Trust Rating:** `99.8% (Elite Standing)`\n"
        )
        container.add_item(ui.TextDisplay(profile_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        btn_edit = ui.Button(label="Edit IGN / Region", style=discord.ButtonStyle.secondary, emoji="✏️")
        btn_history = ui.Button(label="Match History", style=discord.ButtonStyle.secondary, emoji="📜")
        btn_compare = ui.Button(label="Head-to-Head", style=discord.ButtonStyle.primary, emoji="⚔️")

        async def edit_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message("✏️ **[SIMULATION] Edit Profile opened.**", ephemeral=True)

        async def history_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message("📜 **[SIMULATION] Last 5 Matches: W (+24), W (+21), W (+19), L (-18), W (+22)**", ephemeral=True)

        async def compare_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message("⚔️ **[SIMULATION] Head-to-Head Comparison opened.**", ephemeral=True)

        btn_edit.callback = edit_cb
        btn_history.callback = history_cb
        btn_compare.callback = compare_cb

        row.add_item(btn_edit)
        row.add_item(btn_history)
        row.add_item(btn_compare)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Stage 10: Infraction & Matchmaking Ban Notice V2
# ---------------------------------------------------------------------------
class InfractionNoticeV2View(ui.LayoutView):
    def __init__(self, *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)

        container = ui.Container(accent_colour=discord.Colour(0xFF0055))

        expiry_ts = int(time.time()) + 86400
        ban_text = (
            "# ⛔ QUEUE INFRACTION NOTICE\n"
            "`[ STAGE 10 / 10 ]` **AN OFFICIAL LEAGUE PENALTY HAS BEEN RECORDED AGAINST THIS ACCOUNT.**\n\n"
            "### ⚠️ Infraction Breakdown\n"
            "• **Player:** `KartikSakhuja#VEGA`\n"
            "• **Violation:** `Failed Voice Check-in / Match Dodge (#420)`\n"
            "• **Disciplinary Tier:** `Tier 1 (First Warning in 14 Days)`\n"
            "• **Queue Suspension:** `24 Hours Active Penalty`\n"
            f"• **Suspension Lifted:** <t:{expiry_ts}:R> *(<t:{expiry_ts}:F>)*\n"
            "• **Case Reference:** `#INF-8842`\n\n"
            "> ℹ️ *Repeated matchmaking evasion escalates penalties to 7-day bans and season disqualification.*"
        )
        container.add_item(ui.TextDisplay(ban_text))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        btn_appeal = ui.Button(label="Submit Appeal", style=discord.ButtonStyle.danger, emoji="📝")
        btn_rules = ui.Button(label="Disciplinary Code", style=discord.ButtonStyle.secondary, emoji="📜")

        async def appeal_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "📝 **[SIMULATION] Appeal Ticket opened:** Staff will review connection logs for Case #INF-8842.",
                ephemeral=True,
            )

        async def rules_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "📜 **Vega Disciplinary Code:** Check-in dodging, toxicity, or AFK disrupts 9 other players.",
                ephemeral=True,
            )

        btn_appeal.callback = appeal_cb
        btn_rules.callback = rules_cb

        row.add_item(btn_appeal)
        row.add_item(btn_rules)
        container.add_item(row)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Simulator Control Center (Master Hub)
# ---------------------------------------------------------------------------
ALL_STAGES = [
    ("1. Player Registration", RegistrationV2View),
    ("2. Matchmaking Queue Lobby (VEGA QUEUE)", QueueLobbyV2View),
    ("3. Match Found & Voice Check-In", VoiceCheckInV2View),
    ("4. Captains & Player Draft Phase", DraftPhaseV2View),
    ("5. Tactical Map Veto & Ban", MapVetoV2View),
    ("6. Live Match Room (In Progress)", LiveMatchRoomV2View),
    ("7. Scoreboard & AI Vision OCR", ScoreOCRVerificationV2View),
    ("8. Competitive Leaderboard", LeaderboardV2View),
    ("9. Player Profile Dossier", PlayerProfileV2View),
    ("10. Queue Infraction & Ban Notice", InfractionNoticeV2View),
]


class SimulatorControlCenterV2View(ui.LayoutView):
    def __init__(self, cog: "TestUICog", *, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog

        container = ui.Container(accent_colour=discord.Colour(0x5865F2))

        ctrl_text = (
            "# 🎮 VEGA QUEUE — COMPONENTS V2 DESIGN TESTBED\n"
            "This interactive control center allows you to preview, evaluate, and test all "
            "refreshed Discord **Components V2** layouts for Vega Queue.\n\n"
            "### 🚀 Available Actions\n"
            "• **Run Full Simulation Sequence:** Posts all 10 stages sequentially in match order\n"
            "• **Select UI Screen:** Jump directly to any individual UI component card\n"
            "• **Clear Test Channel:** Purges previous simulation messages for a clean canvas\n"
        )
        container.add_item(ui.TextDisplay(ctrl_text))
        container.add_item(ui.Separator())

        # Row 1: Main actions
        row1 = ui.ActionRow()

        btn_run_all = ui.Button(
            label="Run Full Simulation Flow",
            style=discord.ButtonStyle.success,
            emoji="▶️",
            custom_id="test_ui_run_all",
        )

        async def run_all_cb(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await interaction.followup.send("Cannot simulate in this channel type.", ephemeral=True)
                return
            await interaction.followup.send("🚀 **Launching full simulation sequence...** Watch below!", ephemeral=True)
            await self.cog.run_full_simulation(interaction.channel)

        btn_run_all.callback = run_all_cb

        btn_clear = ui.Button(
            label="Clear Test Channel",
            style=discord.ButtonStyle.danger,
            emoji="🧹",
            custom_id="test_ui_clear_chan",
        )

        async def clear_cb(interaction: discord.Interaction) -> None:
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message("Cannot clear this channel type.", ephemeral=True)
                return
            await interaction.response.send_message("🧹 **Purging test messages...**", ephemeral=True)
            try:
                # Purge messages sent by the bot (excluding pinned messages)
                def check(m: discord.Message) -> bool:
                    return m.author == interaction.client.user and not m.pinned

                deleted = await interaction.channel.purge(limit=50, check=check)
                await interaction.followup.send(f"✅ Cleared {len(deleted)} simulation messages.", ephemeral=True)
            except Exception as e:
                log.warning("Could not purge test channel: %s", e)
                await interaction.followup.send(f"Purge note: {e}", ephemeral=True)

        btn_clear.callback = clear_cb

        row1.add_item(btn_run_all)
        row1.add_item(btn_clear)
        container.add_item(row1)

        # Row 2: Direct Stage Selector
        row2 = ui.ActionRow()
        stage_select = ui.Select(
            placeholder="Jump directly to a specific UI screen...",
            options=[
                discord.SelectOption(label=name, value=str(idx), description=f"Preview Stage {idx + 1} V2 UI")
                for idx, (name, _) in enumerate(ALL_STAGES)
            ],
            custom_id="test_ui_stage_select",
        )

        async def stage_select_cb(interaction: discord.Interaction) -> None:
            idx = int(stage_select.values[0])
            name, view_cls = ALL_STAGES[idx]
            await interaction.response.defer(ephemeral=True)
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await interaction.followup.send("Invalid channel.", ephemeral=True)
                return
            await interaction.channel.send(view=view_cls())
            await interaction.followup.send(f"✅ Posted **{name}** below!", ephemeral=True)

        stage_select.callback = stage_select_cb
        row2.add_item(stage_select)
        container.add_item(row2)

        self.add_item(container)


# ---------------------------------------------------------------------------
# Cog Implementation
# ---------------------------------------------------------------------------
class TestUICog(commands.Cog, name="test_ui"):
    """Simulation testbed for previewing refreshed Discord Components V2 UIs."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def get_test_channel(self) -> Optional[discord.TextChannel]:
        raw_id = os.getenv(TEST_UI_CHANNEL_ENV, "").strip()
        if not raw_id:
            return None
        try:
            cid = int(raw_id)
            ch = self.bot.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                return ch
        except ValueError:
            log.warning("Invalid %s in .env: %r", TEST_UI_CHANNEL_ENV, raw_id)
        return None

    async def run_full_simulation(self, channel: discord.TextChannel | discord.Thread) -> None:
        """Posts all 10 stages sequentially in match order with a slight delay."""
        for name, view_cls in ALL_STAGES:
            try:
                await channel.send(view=view_cls())
                await asyncio.sleep(1.0)
            except Exception as e:
                log.error("Error posting simulation stage %s: %s", name, e, exc_info=True)
                await channel.send(f"⚠️ Error rendering `{name}`: {e}")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """If TEST_UI_CHANNEL_ID is configured on bot startup, send the Control Center."""
        target_ch = self.get_test_channel()
        if target_ch:
            log.info("Found %s=%d. Initializing UI testbed...", TEST_UI_CHANNEL_ENV, target_ch.id)
            try:
                await target_ch.send(view=SimulatorControlCenterV2View(self))
                log.info("Successfully posted Simulator Control Center to channel %d", target_ch.id)
            except Exception as e:
                log.warning("Could not auto-post Simulator Control Center: %s", e)

    # -----------------------------------------------------------------------
    # Slash Command: /test_ui
    # -----------------------------------------------------------------------
    ui_group = app_commands.Group(name="test_ui", description="Components V2 UI Simulation Testbed")

    @ui_group.command(name="control_center", description="Spawn the UI Simulator Control Center in this or specified channel")
    @app_commands.describe(channel="Target channel to post the control center (defaults to current)")
    async def control_center_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Please select a text channel.", ephemeral=True)
            return

        await target.send(view=SimulatorControlCenterV2View(self))
        await interaction.response.send_message(
            f"✅ Simulator Control Center spawned in {target.mention}!",
            ephemeral=True,
        )

    @ui_group.command(name="simulate", description="Run the full match simulation sequence showing all 10 refreshed UIs")
    @app_commands.describe(channel="Channel to run the simulation in (defaults to .env or current channel)")
    async def simulate_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        target = channel or self.get_test_channel() or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Please select a valid text channel.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"🚀 **Starting full Components V2 simulation in {target.mention}...**",
            ephemeral=True,
        )

        # Post control center first
        await target.send(view=SimulatorControlCenterV2View(self))
        await asyncio.sleep(1.0)
        # Post all stages
        await self.run_full_simulation(target)

    @ui_group.command(name="show", description="Show a specific Components V2 UI stage directly")
    @app_commands.describe(stage="The UI screen to display")
    @app_commands.choices(
        stage=[
            app_commands.Choice(name=name, value=str(idx))
            for idx, (name, _) in enumerate(ALL_STAGES)
        ]
    )
    async def show_cmd(
        self,
        interaction: discord.Interaction,
        stage: str,
    ) -> None:
        idx = int(stage)
        name, view_cls = ALL_STAGES[idx]
        await interaction.response.send_message(
            view=view_cls(),
        )

    # Prefix command fallback
    @commands.command(name="test_ui", aliases=["simulate_ui"])
    @commands.has_permissions(administrator=True)
    async def prefix_test_ui(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Run the Components V2 simulation flow via prefix command."""
        target = channel or self.get_test_channel() or ctx.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await ctx.send("Please specify a valid text channel.")
            return

        await ctx.send(f"🚀 Starting Components V2 simulation in {target.mention}...")
        await target.send(view=SimulatorControlCenterV2View(self))
        await asyncio.sleep(1.0)
        await self.run_full_simulation(target)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TestUICog(bot))
