# Vega Queue Bot — Handoff Guide

This README is intended for other AI agents, developers, and maintainers. It summarizes the current state of the bot, the major workflow changes, and the project structure so future work can be understood quickly.

---

## Project Overview

Vega Queue is a Discord bot for managing player registration, player profiles, team creation, and support tickets for Vega Scrims. The bot uses Python with discord.py, PostgreSQL via asyncpg, and a modular cog-based architecture.

The project has recently been expanded from a simple registration bot into a fuller community management system with:

- persistent onboarding and command-info embeds
- a button-driven registration experience
- player profile lookup with regional ranking
- private help-ticket channels
- private team setup threads with validation and persistence

---

## Current File Structure

```text
Vega-Queue/
├── cogs/
│   ├── __init__.py
│   ├── commands_info.py
│   ├── help_ticket.py
│   ├── profile.py
│   ├── registration.py
│   └── team_creation.py
├── database/
│   ├── __init__.py
│   ├── db.py
│   └── schema.sql
├── main.py
├── README.md
├── requirements.txt
└── .env.example (if present in your environment)
```

---

## Main Components

### main.py
The entry point for the bot.

What it does:
- creates the custom Discord bot class
- initializes the database pool during startup
- loads all feature cogs
- syncs slash commands globally or to a specific guild if configured
- closes the database pool cleanly on shutdown

### cogs/registration.py
Handles player registration.

Recent behavior:
- posts a pinned registration guide embed in the configured registration channel
- supports two registration entry points:
  - slash command: /register
  - button flow: region dropdown → IGN modal → registration
- validates channel restrictions
- sends a welcome DM after successful registration
- stores registration state and persistent message IDs in the database

### cogs/profile.py
Handles player profile lookup.

Recent behavior:
- slash command: /profile
- shows ELO, region, matches played, K/D/A, K/D ratio, and dynamic regional rank
- defaults to the calling user if no target is provided
- replies ephemerally to keep channels clean

### cogs/commands_info.py
Handles the pinned commands overview embed.

Recent behavior:
- posts or updates a persistent embed showing bot commands
- keeps the message pinned and refreshes it on startup

### cogs/help_ticket.py
Handles support tickets.

Recent behavior:
- slash command: /help
- creates a private text channel for the user and configured admin roles
- adds a close button to the ticket channel
- notifies admins through direct messages
- deletes the channel when the ticket is closed

### cogs/team_creation.py
Handles private team setup.

Recent behavior:
- slash command: /create_team
- opens a private team setup thread for the captain and mod team
- locks the team region to the captain’s registered region
- collects team name, tag, and logo URL via a modal
- stores team records and temporary setup sessions in the database
- prevents duplicate team names or tags

---

## Database Design

The bot uses PostgreSQL with asyncpg and a connection pool created at startup.

### Tables

- players
  - stores registered Discord users, IGN, region, ELO, K/D/A stats, and match count
- teams
  - stores finalized teams, captain details, team name/tag, region, logo URL, and the private thread ID
- team_setup_sessions
  - stores temporary state while a private team setup thread is in progress
- bot_config
  - stores persistent message IDs so embeds can be updated instead of recreated

### Important database helpers

The database layer in [database/db.py](database/db.py) includes helpers for:
- registering players
- retrieving player profiles
- calculating regional ranking using SQL window functions
- creating and validating team setup sessions
- creating team records
- storing and reading config values

---

## Environment Variables

The bot expects runtime configuration through environment variables, such as:

- DISCORD_BOT_TOKEN
- DATABASE_URL
- REGISTRATION_CHANNEL_ID
- COMMANDS_CHANNEL_ID
- TEAM_PANEL_CHANNEL_ID
- HELP_ADMIN_ROLE_IDS
- TEAM_MOD_ROLE_IDS
- GUILD_ID

For local development, create a .env file with the values required by the bot.

---

## Slash Commands

Current slash commands include:

- /register
  - register a player profile
- /profile
  - view a player’s stats and ranking
- /help
  - open a private help ticket
- /create_team
  - start the private team setup flow

---

## Design Notes for Future AI Work

When making changes, keep these conventions in mind:

- keep the interface professional and emoji-free
- use the shared brand color #5B4FCF for embeds
- prefer ephemeral responses for command outputs
- reuse existing persistent message IDs instead of spamming new embeds
- preserve the modular cog structure
- keep database changes compatible with the schema in [database/schema.sql](database/schema.sql)

---

## Setup and Run

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

If you are updating the database schema, apply the SQL in [database/schema.sql](database/schema.sql) to your PostgreSQL instance.

