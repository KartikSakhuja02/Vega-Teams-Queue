-- =============================================================================
-- Vega Queue Bot — PostgreSQL Schema
-- =============================================================================
-- Run this once on your Raspberry Pi to initialise the database:
--
--   psql -U <db_user> -d vega_queue -f database/schema.sql
--
-- If the database does not exist yet, create it first:
--   createdb -U <db_user> vega_queue
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------

-- (none required for now; uncomment below if you add UUID support later)
-- CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'region_enum') THEN
        CREATE TYPE region_enum AS ENUM ('India', 'APAC', 'EMEA', 'Americas');
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- bot_config
-- ---------------------------------------------------------------------------
-- Key-value store for persistent bot state (e.g. pinned message IDs).

CREATE TABLE IF NOT EXISTS bot_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);


-- ---------------------------------------------------------------------------
-- players
-- ---------------------------------------------------------------------------
-- One row per registered Discord user.

CREATE TABLE IF NOT EXISTS players (
    id               BIGSERIAL    PRIMARY KEY,
    discord_id       BIGINT       NOT NULL UNIQUE,
    discord_username TEXT         NOT NULL,
    ign              TEXT         NOT NULL,
    region           region_enum  NOT NULL,
    registered_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
    elo              INT          NOT NULL DEFAULT 1000,
    kills            INT          NOT NULL DEFAULT 0,
    deaths           INT          NOT NULL DEFAULT 0,
    assists          INT          NOT NULL DEFAULT 0,
    matches_played   INT          NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_players_discord_id ON players (discord_id);
CREATE INDEX IF NOT EXISTS idx_players_region      ON players (region);
CREATE INDEX IF NOT EXISTS idx_players_registered  ON players (registered_at DESC);
CREATE INDEX IF NOT EXISTS idx_players_elo         ON players (elo DESC);


-- ---------------------------------------------------------------------------
-- teams
-- ---------------------------------------------------------------------------
-- One row per created team.

CREATE TABLE IF NOT EXISTS teams (
    id                    BIGSERIAL    PRIMARY KEY,
    captain_discord_id    BIGINT       NOT NULL UNIQUE,
    captain_username      TEXT         NOT NULL,
    captain_ign           TEXT         NOT NULL,
    team_name             TEXT         NOT NULL,
    team_name_key         TEXT         NOT NULL UNIQUE,
    team_tag              TEXT         NOT NULL,
    team_tag_key          TEXT         NOT NULL UNIQUE,
    region                region_enum  NOT NULL,
    team_logo_path        TEXT,
    thread_id             BIGINT       NOT NULL UNIQUE,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    is_active             BOOLEAN      NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_teams_region ON teams (region);
CREATE INDEX IF NOT EXISTS idx_teams_created ON teams (created_at DESC);


-- ---------------------------------------------------------------------------
-- team_setup_sessions
-- ---------------------------------------------------------------------------
-- Temporary setup state for the private team creation thread.

CREATE TABLE IF NOT EXISTS team_setup_sessions (
    thread_id            BIGINT       PRIMARY KEY,
    captain_discord_id    BIGINT       NOT NULL UNIQUE,
    captain_username      TEXT         NOT NULL,
    captain_ign           TEXT         NOT NULL,
    region                region_enum  NOT NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_team_setup_sessions_captain ON team_setup_sessions (captain_discord_id);


-- ---------------------------------------------------------------------------
-- Migration support for existing installations
-- ---------------------------------------------------------------------------
ALTER TABLE players ADD COLUMN IF NOT EXISTS elo INT NOT NULL DEFAULT 1000;
ALTER TABLE players ADD COLUMN IF NOT EXISTS kills INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS deaths INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS assists INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS matches_played INT NOT NULL DEFAULT 0;

-- Rename logo column from URL to local path (safe to re-run; will no-op if already renamed).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'teams' AND column_name = 'team_logo_url'
    ) THEN
        ALTER TABLE teams RENAME COLUMN team_logo_url TO team_logo_path;
    END IF;
END
$$;

