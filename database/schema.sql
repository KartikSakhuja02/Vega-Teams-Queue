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
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_players_discord_id ON players (discord_id);
CREATE INDEX IF NOT EXISTS idx_players_region      ON players (region);
CREATE INDEX IF NOT EXISTS idx_players_registered  ON players (registered_at DESC);
