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
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'team_role_enum') THEN
        CREATE TYPE team_role_enum AS ENUM ('Player', 'Manager', 'Coach', 'Substitute');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'player_status_enum') THEN
        CREATE TYPE player_status_enum AS ENUM ('IDLE', 'IN_QUEUE', 'IN_MATCH', 'PENALTY_COOLDOWN');
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
    id               BIGSERIAL          PRIMARY KEY,
    discord_id       BIGINT             NOT NULL UNIQUE,
    discord_username TEXT               NOT NULL,
    ign              TEXT               NOT NULL,
    region           region_enum        NOT NULL,
    registered_at    TIMESTAMPTZ        NOT NULL DEFAULT NOW(),
    is_active        BOOLEAN            NOT NULL DEFAULT TRUE,
    status           player_status_enum NOT NULL DEFAULT 'IDLE',
    status_since     TIMESTAMPTZ,
    penalty_ends_at  TIMESTAMPTZ,
    dms_enabled      BOOLEAN            NOT NULL DEFAULT TRUE,
    is_banned        BOOLEAN            NOT NULL DEFAULT FALSE,
    banned_at        TIMESTAMPTZ,
    banned_until     TIMESTAMPTZ,
    ban_reason       TEXT,
    banned_by        BIGINT,
    elo              INT          NOT NULL DEFAULT 1000,
    kills            INT          NOT NULL DEFAULT 0,
    deaths           INT          NOT NULL DEFAULT 0,
    assists          INT          NOT NULL DEFAULT 0,
    matches_played   INT          NOT NULL DEFAULT 0,
    wins             INT          NOT NULL DEFAULT 0,
    mvp_count        INT          NOT NULL DEFAULT 0
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
-- team_members
-- ---------------------------------------------------------------------------
-- One row per player in a team.

CREATE TABLE IF NOT EXISTS team_members (
    id                    BIGSERIAL    PRIMARY KEY,
    team_id               BIGINT       NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    discord_id            BIGINT       NOT NULL UNIQUE,
    role                  team_role_enum NOT NULL,
    joined_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_team_members_team_id ON team_members (team_id);


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
-- team_invites
-- ---------------------------------------------------------------------------
-- One row per pending or historical team invite.

CREATE TABLE IF NOT EXISTS team_invites (
    id                   BIGSERIAL       PRIMARY KEY,
    team_id              BIGINT          NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    inviter_discord_id   BIGINT          NOT NULL,
    target_discord_id    BIGINT          NOT NULL,
    role                 team_role_enum  NOT NULL,
    created_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    expires_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
    dm_message_id        BIGINT,
    is_active            BOOLEAN         NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_team_invites_team_id ON team_invites (team_id);
CREATE INDEX IF NOT EXISTS idx_team_invites_target  ON team_invites (target_discord_id);


-- ---------------------------------------------------------------------------
-- Migration support for existing installations
-- ---------------------------------------------------------------------------
ALTER TABLE players ADD COLUMN IF NOT EXISTS elo INT NOT NULL DEFAULT 1000;
ALTER TABLE players ADD COLUMN IF NOT EXISTS kills INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS deaths INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS assists INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS matches_played INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS wins INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS mvp_count INT NOT NULL DEFAULT 0;

-- Add player_status_enum if missing (safe to re-run).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'player_status_enum') THEN
        CREATE TYPE player_status_enum AS ENUM ('IDLE', 'IN_QUEUE', 'IN_MATCH', 'PENALTY_COOLDOWN');
    END IF;
END
$$;

ALTER TABLE players ADD COLUMN IF NOT EXISTS status          player_status_enum NOT NULL DEFAULT 'IDLE';
ALTER TABLE players ADD COLUMN IF NOT EXISTS status_since    TIMESTAMPTZ;
ALTER TABLE players ADD COLUMN IF NOT EXISTS penalty_ends_at TIMESTAMPTZ;
ALTER TABLE players ADD COLUMN IF NOT EXISTS dms_enabled     BOOLEAN NOT NULL DEFAULT TRUE;

-- Create team_invites table for existing databases
CREATE TABLE IF NOT EXISTS team_invites (
    id                   BIGSERIAL       PRIMARY KEY,
    team_id              BIGINT          NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    inviter_discord_id   BIGINT          NOT NULL,
    target_discord_id    BIGINT          NOT NULL,
    role                 team_role_enum  NOT NULL,
    created_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    expires_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
    dm_message_id        BIGINT,
    is_active            BOOLEAN         NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_team_invites_team_id ON team_invites (team_id);
CREATE INDEX IF NOT EXISTS idx_team_invites_target  ON team_invites (target_discord_id);

-- Ensure Substitute exists in team_role_enum
ALTER TYPE team_role_enum ADD VALUE IF NOT EXISTS 'Substitute';

-- Ban columns migration (safe to re-run)
ALTER TABLE players ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE players ADD COLUMN IF NOT EXISTS banned_at TIMESTAMPTZ;
ALTER TABLE players ADD COLUMN IF NOT EXISTS banned_until TIMESTAMPTZ;
ALTER TABLE players ADD COLUMN IF NOT EXISTS ban_reason TEXT;
ALTER TABLE players ADD COLUMN IF NOT EXISTS banned_by BIGINT;
CREATE INDEX IF NOT EXISTS idx_players_is_banned ON players (is_banned);

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

-- ---------------------------------------------------------------------------
-- team_queue
-- ---------------------------------------------------------------------------
-- Stores teams actively waiting in the regional or global queue.

CREATE TABLE IF NOT EXISTS team_queue (
    id                   BIGSERIAL    PRIMARY KEY,
    team_id              BIGINT       NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    queue_type           TEXT         NOT NULL, -- 'REGIONAL' or 'GLOBAL'
    region               region_enum  NOT NULL,
    captain_discord_id   BIGINT       NOT NULL,
    joined_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_team_queue_team_type UNIQUE (team_id, queue_type)
);

CREATE INDEX IF NOT EXISTS idx_team_queue_type_region ON team_queue (queue_type, region);
CREATE INDEX IF NOT EXISTS idx_team_queue_joined ON team_queue (joined_at ASC);

-- Migration for existing databases: allow team in both regional and global queues
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_team_queue_team'
    ) THEN
        ALTER TABLE team_queue DROP CONSTRAINT uq_team_queue_team;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_team_queue_team_type'
    ) THEN
        ALTER TABLE team_queue ADD CONSTRAINT uq_team_queue_team_type UNIQUE (team_id, queue_type);
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- scrim_matches
-- ---------------------------------------------------------------------------
-- Stores matched scrims, private negotiation channels, and agreed timings.

CREATE TABLE IF NOT EXISTS scrim_matches (
    id                   BIGSERIAL    PRIMARY KEY,
    team1_id             BIGINT       NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    team2_id             BIGINT       NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    channel_id           BIGINT       NOT NULL UNIQUE,
    panel_message_id     BIGINT,
    match_type           TEXT         NOT NULL, -- 'REGIONAL' or 'GLOBAL'
    region               region_enum  NOT NULL,
    status               TEXT         NOT NULL DEFAULT 'NEGOTIATING', -- 'NEGOTIATING', 'CONFIRMED', 'CANCELLED', 'COMPLETED'
    proposed_time        TEXT,
    proposed_by_team_id  BIGINT       REFERENCES teams (id) ON DELETE SET NULL,
    proposed_by_user_id  BIGINT,
    confirmed_by_user_id BIGINT,
    confirmed_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scrim_matches_channel ON scrim_matches (channel_id);
CREATE INDEX IF NOT EXISTS idx_scrim_matches_teams ON scrim_matches (team1_id, team2_id);
CREATE INDEX IF NOT EXISTS idx_scrim_matches_status ON scrim_matches (status);


-- ---------------------------------------------------------------------------
-- solo_queue (Server B: 10-Man Solo Player Queue)
-- ---------------------------------------------------------------------------
-- Stores individual players waiting for a 10-man PUG match.

CREATE TABLE IF NOT EXISTS solo_queue (
    id          BIGSERIAL    PRIMARY KEY,
    discord_id  BIGINT       NOT NULL UNIQUE REFERENCES players (discord_id) ON DELETE CASCADE,
    joined_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_solo_queue_joined ON solo_queue (joined_at ASC);


-- ---------------------------------------------------------------------------
-- solo_matches (Server B: 10-Man PUG Matches)
-- ---------------------------------------------------------------------------
-- Stores 10-man lobby matches, player drafting, and map veto state.

CREATE TABLE IF NOT EXISTS solo_matches (
    id                      BIGSERIAL    PRIMARY KEY,
    channel_id              BIGINT       NOT NULL UNIQUE,
    panel_message_id        BIGINT,
    status                  TEXT         NOT NULL DEFAULT 'DRAFTING', -- 'DRAFTING', 'MAP_VETO', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'
    captain1_id             BIGINT       NOT NULL,
    captain2_id             BIGINT       NOT NULL,
    team1_player_ids        BIGINT[]     NOT NULL DEFAULT '{}',
    team2_player_ids        BIGINT[]     NOT NULL DEFAULT '{}',
    available_player_ids    BIGINT[]     NOT NULL DEFAULT '{}',
    current_turn_captain_id BIGINT,
    draft_step              INT          NOT NULL DEFAULT 1,
    selected_map            TEXT,
    available_maps          TEXT[]       NOT NULL DEFAULT '{}',
    banned_maps             TEXT[]       NOT NULL DEFAULT '{}',
    voice_team1_id          BIGINT,
    voice_team2_id          BIGINT,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_solo_matches_channel ON solo_matches (channel_id);
CREATE INDEX IF NOT EXISTS idx_solo_matches_status ON solo_matches (status);


-- ---------------------------------------------------------------------------
-- matchmaking_verifications (Server B: Verification Screenshots)
-- ---------------------------------------------------------------------------
-- Persistent state tracking for screenshot OCR, region selection, and staff approval.

CREATE TABLE IF NOT EXISTS matchmaking_verifications (
    orig_message_id   BIGINT PRIMARY KEY,
    reply_message_id  BIGINT,
    channel_id        BIGINT NOT NULL,
    guild_id          BIGINT NOT NULL,
    player_id         BIGINT NOT NULL,
    player_name       TEXT NOT NULL,
    ign               TEXT NOT NULL,
    region            TEXT,
    status            TEXT NOT NULL DEFAULT 'PENDING_REGION',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mv_reply_msg ON matchmaking_verifications (reply_message_id);
CREATE INDEX IF NOT EXISTS idx_mv_player ON matchmaking_verifications (player_id);
CREATE INDEX IF NOT EXISTS idx_mv_status ON matchmaking_verifications (status);
