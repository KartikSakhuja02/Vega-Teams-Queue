# Vega Queue Bot — Handoff Guide

This README is intended for other AI agents, developers, and maintainers. It summarises the current state of the bot, the codebase structure, all slash commands, and all database helpers so future work can be picked up without re-reading every file.

---

## Project Overview

Vega Queue is a Discord bot for managing player registration, player profiles, team creation, and support tickets for Vega Scrims. The bot uses Python with discord.py, PostgreSQL via asyncpg, and a modular cog-based architecture.

Current feature set:

- Persistent onboarding and command-info embeds that auto-refresh on startup
- Button-driven player registration with region selection
- Player profile lookup with regional ranking (ELO, K/D/A, matches played)
- Private help-ticket channels with close buttons
- Private team setup threads with modal validation, local logo storage, and full disband / resume flow

---

## File Structure

```text
Vega-Queue/
├── cogs/
│   ├── __init__.py
│   ├── commands_info.py      # Posts/updates the pinned commands overview embed
│   ├── help_ticket.py        # /help — private support ticket channels
│   ├── profile.py            # /profile — player stats and regional ranking
│   ├── registration.py       # /register — player registration flow
│   └── team_creation.py      # /create_team, /disband — team setup and management
├── database/
│   ├── __init__.py
│   ├── db.py                 # All async DB helpers (asyncpg)
│   └── schema.sql            # Full table definitions (apply once to PostgreSQL)
├── team_logos/               # Auto-created at runtime — stores uploaded team logos
├── main.py                   # Bot entry point, pool init, cog loader, command sync
├── README.md
├── requirements.txt
└── .env.example
```

---

## Environment Variables

Configured via a `.env` file (see `.env.example`):

| Variable | Purpose |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token from the Discord Developer Portal |
| `DATABASE_URL` | asyncpg-compatible PostgreSQL connection string |
| `REGISTRATION_CHANNEL_ID` | Channel where /register and the onboarding embed live |
| `COMMANDS_CHANNEL_ID` | Channel where the bot commands overview embed is posted |
| `TEAM_PANEL_CHANNEL_ID` | Channel where the Create Team panel and private threads are created |
| `HELP_ADMIN_ROLE_IDS` | Comma-separated role IDs added to help-ticket channels |
| `TEAM_MOD_ROLE_IDS` | Comma-separated role IDs added to team setup threads |
| `GUILD_ID` | (Optional) Guild ID for instant command sync during development |

> **Required Discord intents (set in Developer Portal → Bot → Privileged Gateway Intents):**
> - Server Members Intent
> - Message Content Intent

---

## Slash Commands

### Player

| Command | Description |
|---|---|
| `/register ign:<ign> region:<region>` | Register a player profile. Region is locked at registration. Can only be used in the registration channel. |
| `/profile` | View your own ELO, K/D/A, matches played, and regional ranking. Response is ephemeral. |
| `/profile player:<@user>` | View another registered player's profile. Shows error if user is not in the database. |
| `/team-profile` | View the profile, region, and roster of your own team. Response is ephemeral. |
| `/team-profile player:<@user>` | View the team profile and roster for another player's team. |

### Support

| Command | Description |
|---|---|
| `/help` | Opens a private help-ticket channel visible to the user and configured admin roles. Includes a close button that deletes the channel. |

### Teams

| Command | Description |
|---|---|
| `/create_team` | Opens a private team setup thread. Captain fills in team name and tag via a modal, then uploads the logo image directly in the thread. Region is locked to the captain's registered region. If the captain previously disbanded a team, they are offered to resume the old team or start fresh. |
| `/disband` | Disbands the current team. Only Captains or Managers can do this. Data is soft-deleted (`is_active = FALSE`). All team members receive a DM confirming the disband. Next time they use `/create_team` they can choose to resume or start fresh. |
| `/invite player:<@user>` | Invites a registered player to your active team. Only Captains or Managers can use this. You select the role (Player, Manager, or Coach) and the player receives an interactive DM to Accept or Decline. Upon acceptance, the bot assigns the Discord role to the player. |
| `/kick player:<@user>` | Kicks a player from your team. Only Captains or Managers can use this. The kicked player is notified via DM, and their Discord role is automatically removed. |
| `/leave` | Leaves your current team. Captains cannot use this command; they must use `/disband` instead. The team leadership (Captain + Managers) will receive a DM notification that you have left, and your Discord role is automatically removed. |

### Team Matchmaking & Scrims

| Command | Description |
|---|---|
| `/post_team_queue` | Refreshes or posts the persistent Team Matchmaking panel in the configured queue channel (Admin only). |
| `/matchmake_teams` | Evaluates active queues and immediately triggers matchmaking and private scrim channel creation for queued pairs (Admin only). |
| `/close_scrim` | Closes and deletes the current scrim match channel (Staff or Captains after cancellation). |

- **Multi-Queue Participation**: Teams can queue in their Regional Queue, the Global Queue, or **both simultaneously**.
- **Automated Matchmaking**: As soon as 2 teams are queued (either in the same region under Regional or in Global), both teams are automatically dequeued from all queues and a private text channel (`#scrim-tag1-vs-tag2`) is generated.
- **Private Scrim Channel**:
  - Accessible only to Team 1 members, Team 2 members, and Server Staff (`TEAM_MOD_ROLE_IDS`).
  - Created under `SCRIM_CATEGORY_ID` (or the queue channel's category).
- **Persistent Negotiation UI**:
  - **Propose Match Time**: Opens a modal for either team captain or roster player to propose a match schedule.
  - **Accept Time**: Opposing team accepts the proposed schedule, transitioning the match status to `CONFIRMED`. Self-acceptance is prevented.
  - **Cancel Match**: Either captain or staff member can cancel the match negotiation.
  - Strictly **zero emojis** in all panel titles, descriptions, status messages, and buttons.

### 10-Man Solo Player Queue (Server B)

| Command | Description |
|---|---|
| `/post_solo_queue` | Refreshes or posts the persistent 10-man Solo Queue panel in Server B (Admin only). |
| `/submit-result` | Submits match scoreboard screenshot for OCR analysis, updates stats/ELO, and posts to results channel. |
| `/leaderboard` | View competitive player rankings, ELO ratings, and combat stats with pagination and region filters. |
| `/solo_config scoring` | Switch ELO scoring template between `DEFAULT` (+25/-20) and `PERFORMANCE` (stats-scaled). |
| `/solo_config results_channel` | Set dedicated channel where match result scoreboards are posted. |
| `/cancel_solo_match` | Cancels the active 10-man match lobby and returns players to `IDLE` status (Staff only). |

- **Queue Lobby**:
  - Displays live player counter `[ X / 10 Players Waiting ]` with player IGN, ELO, and joined time.
  - Interactive buttons: **`Join Queue`** and **`Leave Queue`** (strictly zero emojis).
- **Automated 10-Man Match Creation**:
  - Upon 10 players joining, all 10 players are dequeued and set to `IN_MATCH`.
  - A private match channel (`#match-lobby-xxx`) is created with access for the 10 players, bot, and staff.
- **Captain Selection Templates**:
  - `HIGHEST_ELO` (Default): Top 2 ranked ELO players become Captain 1 and Captain 2.
  - `RANDOM`: 2 random players are selected as captains.
  - `FIRST_JOINED`: The first 2 players to queue up become captains.
  - `HIGHEST_WINRATE`: Top 2 players with the best win percentage become captains.
- **Player Draft Templates**:
  - `SNAKE` (Default `1-2-2-2-1`): Turn-based dropdown select where captains draft players.
  - `ALTERNATING` (`1-1-1-1-1-1-1-1`): Captains alternate picking 1 player each.
- **Map Veto Phase**:
  - Dynamic buttons for the map pool (`Ascent`, `Bind`, `Haven`, `Split`, `Sunset`, `Lotus`, `Abyss`).
  - Captains take turns clicking a button to **BAN** maps until 1 decisive map remains.
- **Match Result & Scoreboard OCR**:
  - Any player in the match lobby can upload the match end-screen screenshot using `/submit-result`.
  - Concurrency locking prevents double submissions.
  - Ollama vision extracts K/D/A, ACS, first bloods, plants, defuses, and MVP badges.
  - ELO templates: `DEFAULT` (flat +25/-20) or `PERFORMANCE` (combat scaling with carry loss protection).
  - Detailed scoreboard is automatically posted to the dedicated results channel and players return to `IDLE`.

---

## Team Setup Flow

### New Team
1. Captain uses `/create_team` or clicks the **Create Team** button on the panel.
2. Bot creates a private thread and adds the captain + all mod-role members.
3. Captain clicks **Enter Team Details** → fills in team name and tag via modal.
4. Captain sends the team logo image directly in the thread (no prompt needed).
5. Bot saves the image to `team_logos/<tag>_<thread_id>.<ext>` on the server.
6. Team record is inserted into the database. Thread is deleted after 10 seconds.

### Disband + Resume
1. Captain runs `/disband` → confirmation buttons → team marked `is_active = FALSE`.
2. Next time captain uses `/create_team`:
   - **Continue with Old Team** → private thread opens, shows old team details.
     - If logo file still exists on disk: shown with **Keep This Logo / Upload New Logo** buttons.
     - If logo file is missing: bot waits for a new upload.
   - **Start Fresh** → normal setup flow (modal → logo upload) but the old DB record is updated rather than inserting a new row (avoids UNIQUE constraint on `captain_discord_id`).

---

## Database

### Connection

Managed via asyncpg connection pool. Pool is created in `main.py` during `on_ready` and injected into `database/db.py` via `set_pool()`.

### Tables

#### `players`
Stores registered Discord users.

| Column | Type | Notes |
|---|---|---|
| `discord_id` | BIGINT PK | Discord user snowflake |
| `discord_username` | TEXT | Username at registration time |
| `ign` | TEXT UNIQUE | In-game name |
| `region` | region_enum | Locked at registration |
| `elo` | INTEGER | Defaults to 1000 |
| `kills` | INTEGER | Lifetime kills |
| `deaths` | INTEGER | Lifetime deaths |
| `assists` | INTEGER | Lifetime assists |
| `matches_played` | INTEGER | Total matches |
| `registered_at` | TIMESTAMPTZ | Auto-set |
| `is_active` | BOOLEAN | TRUE by default |

#### `teams`
Stores team records. One row per captain (`captain_discord_id` UNIQUE).

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `captain_discord_id` | BIGINT UNIQUE | |
| `captain_username` | TEXT | |
| `captain_ign` | TEXT | |
| `team_name` | TEXT UNIQUE | Display name |
| `team_name_key` | TEXT UNIQUE | Casefolded for conflict checks |
| `team_tag` | TEXT UNIQUE | Uppercase alphanumeric |
| `team_tag_key` | TEXT UNIQUE | Same as team_tag (uppercase) |
| `region` | region_enum | |
| `team_logo_path` | TEXT | Absolute path on the Raspberry Pi |
| `thread_id` | BIGINT | Private setup thread snowflake |
| `is_active` | BOOLEAN | FALSE = disbanded, TRUE = active |
| `created_at` | TIMESTAMPTZ | |

#### `team_members`
Stores the active roster for teams. 

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `team_id` | BIGINT FK | References teams(id) |
| `discord_id` | BIGINT UNIQUE | One team per player |
| `role` | team_role_enum | 'Player', 'Manager', or 'Coach' |
| `joined_at` | TIMESTAMPTZ | Auto-set |

#### `team_setup_sessions`
Temporary state while a team setup thread is in progress. Deleted when the team is finalized or the session times out.

| Column | Type | Notes |
|---|---|---|
| `thread_id` | BIGINT PK | |
| `captain_discord_id` | BIGINT UNIQUE | |
| `captain_username` | TEXT | |
| `captain_ign` | TEXT | |
| `region` | region_enum | |
| `created_at` | TIMESTAMPTZ | |

#### `team_queue`
Stores active teams in the matchmaking queue (Regional or Global).

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `team_id` | BIGINT FK | References teams(id) ON DELETE CASCADE |
| `queue_type` | TEXT | 'REGIONAL' or 'GLOBAL' (UNIQUE with team_id) |
| `region` | region_enum | Matchmaking region |
| `captain_discord_id` | BIGINT | Captain snowflake |
| `joined_at` | TIMESTAMPTZ | Queue entry timestamp |

#### `bot_config`
Key-value store for persistent message IDs (so embeds are edited instead of re-posted).

| Column | Type |
|---|---|
| `key` | TEXT PK |
| `value` | TEXT |

---

## Database Helper Functions (`database/db.py`)

### Pool management
```
set_pool(pool)           — inject the asyncpg pool
get_pool()               — retrieve the pool (raises if not set)
```

### Players
```
get_player(discord_id)               — fetch player by Discord ID
create_player(discord_id, username, ign, region)
                                     — insert new player record
get_regional_ranking(discord_id, region)
                                     — returns dict with elo, rank, total_in_region, kda, matches
```

### Teams
```
get_team_by_captain(captain_discord_id)   — active team only
get_team_by_name_key(name_key)            — active team by normalised name
get_team_by_tag_key(tag_key)              — active team by normalised tag
get_team_by_thread_id(thread_id)          — active team by thread ID
get_inactive_team_by_captain(captain_discord_id)
                                          — most recently disbanded team
create_team(captain_discord_id, captain_username, captain_ign,
            team_name, team_name_key, team_tag, team_tag_key,
            region, team_logo_path, thread_id)
                                          — INSERT new team record
deactivate_team(captain_discord_id)       — sets is_active=FALSE (disband)
reactivate_team(captain_discord_id, thread_id, team_logo_path=None)
                                          — sets is_active=TRUE, optionally updates logo
reactivate_team_fresh(captain_discord_id, team_name, team_name_key,
                      team_tag, team_tag_key, region, team_logo_path, thread_id)
                                          — UPDATE entire row with new details (fresh restart)

### Team Members
```
add_team_member(team_id, discord_id, role)
                                          — insert a player into a team
remove_team_member(team_id, discord_id)   — remove a specific player from a team
get_team_members(team_id)                 — fetch all members in a team
get_player_team_membership(discord_id)    — fetch active membership info for a player
clear_team_members(team_id)               — wipe members on reactivation (returns old members)
```

### Team Setup Sessions
```
create_team_setup_session(thread_id, captain_discord_id, captain_username,
                          captain_ign, region)
get_team_setup_session_by_thread_id(thread_id)
get_team_setup_session_by_captain(captain_discord_id)
delete_team_setup_session(thread_id)
```

### Team Queue
```
add_team_to_queue(team_id, queue_type, region, captain_discord_id)
                                          — enter or update team in queue (supports dual queues)
remove_team_from_queue(team_id, queue_type=None)
                                          — leave a specific queue or all queues
get_team_queue(queue_type=None)           — fetch all queued teams (or filtered by type)
get_queued_team(team_id, queue_type=None) — check if a team is in a queue
get_team_queues(team_id)                  — fetch all active queue rows for a team
```

### Admin Management
```
search_teams(query, limit=25)             — autocomplete search teams by ID, name, or tag
get_team_by_identifier(identifier)        — resolve team by ID, name, or tag
admin_update_player_ign(discord_id, ign)  — update player IGN & captain IGN
admin_update_player_elo(discord_id, elo)  — update player ELO rating
admin_reset_player_stats(discord_id)      — reset player combat statistics
admin_reset_player_status(discord_id)     — reset player status to IDLE and clear cooldowns
admin_delete_player(discord_id)           — safely unregister/delete player record
admin_force_add_team_member(team_id, discord_id, role)
                                          — force-assign player to team roster
admin_hard_delete_team(team_id)           — permanently delete team and cascaded rows
```

### Bot Config
```
get_config(key)          — returns value string or None
set_config(key, value)   — upsert key-value pair
```

---

## Applying the Database Schema

```bash
psql -U "VEGA-QUEUES" -d Vega_Queue_System_New -f database/schema.sql
```

The schema uses a custom `region_enum` PostgreSQL type. Run the full schema file once on a fresh database. On updates, apply only the new `ALTER TABLE` / `CREATE TABLE IF NOT EXISTS` statements manually.

---

## Running the Bot

```bash
python -m venv venv
source venv/bin/activate        # On Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

---

## Design Conventions

Keep these consistent when making any future changes:

- **No emojis** in any bot message or embed
- **Ephemeral-only** responses for all slash commands
- **Brand colour** `#5B4FCF` for all embeds
- **Soft-delete** data rather than hard-deleting (use `is_active = FALSE`)
- **Persistent message IDs** stored in `bot_config` so embeds are edited on restart
- **Modular cog structure** — one file per feature domain
- **message_content intent** must be enabled in the Developer Portal for `wait_for('message')` to receive attachments
