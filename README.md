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
