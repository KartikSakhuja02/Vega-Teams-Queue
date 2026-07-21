# Vega Queue Bot — Repository Guide & Architecture

This document provides a comprehensive overview of the **Vega Scrims Queue Bot** project structure, database schema, cogs, and execution flow to assist other developer tools, AIs, and developers.

---

## 1. Directory Structure

```
Vega-Queue/
├── cogs/
│   ├── __init__.py          # Marks cogs directory as a Python package
│   ├── commands_info.py     # Pinned commands overview embed & updates
│   ├── help_ticket.py       # /help private ticket creation, admin DMs, close button
│   ├── profile.py           # /profile slash command (stats & dynamic rank)
│   └── registration.py      # /register slash command, onboarding DMs & guides
├── database/
│   ├── __init__.py          # Marks database directory as a Python package
│   ├── db.py                # Database pool connection and asyncpg SQL query helper methods
│   └── schema.sql           # PostgreSQL tables creation & upgrade script
├── .env.example             # Configuration template for system environment variables
├── .gitignore               # Excludes secrets (.env) and Python environment files
├── main.py                  # Entry point of the Discord Bot (login, setup_hook, cog loader)
└── requirements.txt         # Project dependencies (discord.py, asyncpg, python-dotenv)
```

---

## 2. Component Reference

### `main.py`
The entry point of the application.
- Uses `discord.ext.commands.Bot` subclass `VegaBot`.
- **`setup_hook`**:
  1. Initialises the database pool using `database.db.init_db()`.
  2. Loads the feature cogs dynamically (`cogs.registration`, `cogs.profile`, `cogs.commands_info`).
  3. Handles slash command synchronization. Supports guild-specific instant synchronization (dev mode) if `GUILD_ID` is set, or global sync.
- **Graceful Shutdown**: Calls `db.close_db()` in `close()` to release the connection pool before disconnecting from the Discord gateway.

### `database/`
Handles persistent storage. The database is a PostgreSQL instance (typically hosted on a Raspberry Pi).
- **`schema.sql`**: Initialises tables and adds indexes. Includes:
  - `region_enum`: Custom ENUM containing `'India'`, `'APAC'`, `'EMEA'`, `'Americas'`.
  - `players` table: Stores registration details, ELO (default `1000`), matches played, overall K/D/A stats.
  - `bot_config` table: Simple key-value store used to remember the pinned Discord message IDs so they can be modified/updated on bot restarts.
  - Upgrade script lines (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) for backward compatibility.
- **`db.py`**:
  - Uses `asyncpg.create_pool` to maintain an active pool of connections (pool size 2 to 10).
  - Contains CRUD helpers: `register_player()`, `get_player()`, `get_player_profile()`, `get_all_players()`, and `get_config()`/`set_config()`.

### `cogs/`
Encapsulates individual modular bot features.
- **`registration.py`**:
  - Posts and pins a persistent registration guide embed in `REGISTRATION_CHANNEL_ID`. If the bot restarts, it fetches and edits the existing message to avoid spamming.
  - `/register ign:<ign> region:<region>`: Channel-restricted command to register. Ephemerally replies on success.
  - Button flow: The registration panel includes a button that opens a modal for IGN and region, then runs the same registration logic.
  - Dispatches a welcome confirmation DM to the player in the background using `asyncio.create_task` to prevent blocking slash command execution times.
- **`profile.py`**:
  - `/profile [player]`: Ephemeral command showing registered details, ELO rating, matches played, overall K/D/A breakdown, calculated K/D ratio, and dynamic regional ranking.
  - Defaulting: Defaults to the calling user if no player parameter is specified.
- **`help_ticket.py`**:
  - `/help`: Creates a private ticket channel for the user and configured admins.
  - Sends a minimal support embed with a close button inside the ticket channel.
  - DMs each configured admin with the ticket channel mention so they can join immediately.
- **`commands_info.py`**:
  - Posts and pins a persistent commands directory list embed in `COMMANDS_CHANNEL_ID`. Updates the embed dynamically on start-up.

---

## 3. Database Schema Diagram & Design

```
                     +----------------------------+
                     |         players            |
                     +----------------------------+
                     | id               (PK)      |
                     | discord_id       (Unique)  |
                     | discord_username           |
                     | ign                        |
                     | region           (ENUM)    |
                     | registered_at              |
                     | is_active        (Bool)    |
                     | elo              (Default) |
                     | kills            (Default) |
                     | deaths           (Default) |
                     | assists          (Default) |
                     | matches_played   (Default) |
                     +----------------------------+
                                   |
                     +----------------------------+
                     |        bot_config          |
                     +----------------------------+
                     | key              (PK)      |
                     | value                      |
                     +----------------------------+
```

### Dynamic Regional Ranking Query
The bot does not store player ranks directly. Instead, regional rankings are calculated on-demand via a SQL window function in `db.py`:
```sql
WITH ranked_players AS (
    SELECT 
        id, discord_id, discord_username, ign, region, registered_at, is_active,
        elo, kills, deaths, assists, matches_played,
        ROW_NUMBER() OVER (PARTITION BY region ORDER BY elo DESC) as regional_rank
    FROM players
    WHERE is_active = TRUE
)
SELECT * FROM ranked_players WHERE discord_id = $1
```
This isolates calculations by region, ordering by highest ELO first.

---

## 4. Timezone Reference
All registration timestamps are formatted relative to the player's target region:
- **India**: UTC+5:30 (`IST`)
- **APAC**: UTC+8:00 (`SGT`)
- **EMEA**: UTC+1:00 (`CET`)
- **Americas**: UTC-5:00 (`EST`)

---

## 5. UI and Design Philosophy
To present a clean, aesthetic, and premium look, the following styling guidelines must be followed:
1. **No Emojis**: Avoid using emojis in command outputs, error responses, embeds, and status messages to keep the interface highly professional.
2. **Cohesive Colors**: Embed messages must use deep indigo (`#5B4FCF`) to align with the brand color palette.
3. **Ephemeral Responses**: Command responses must default to `ephemeral=True` to minimize server clutter.
4. **Persistent Messages**: Onboarding channel messages (registration instructions, command list) must be kept clean, pinned, and edited in-place using cached database message IDs across restarts.
5. **Private Support Tickets**: The `/help` command creates a private ticket channel for the user and configured admins, with a close button that removes the channel when the issue is resolved.
6. **Registration Button**: The registration panel includes a button that opens a modal, but `/register` remains available for users who prefer the slash command.
