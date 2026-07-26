"""
database/db.py
Async PostgreSQL connection pool and CRUD helpers for the Vega Queue Bot.
"""

import os
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# Module-level pool — initialised once at startup, reused across the bot's lifetime.
_pool: Optional[asyncpg.Pool] = None


# =============================================================================
# Pool lifecycle
# =============================================================================

async def init_db() -> None:
    """Create the async connection pool.  Must be called once before any query."""
    global _pool
    dsn = os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    log.info("Database connection pool created.")


async def close_db() -> None:
    """Gracefully close all connections in the pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        log.info("Database connection pool closed.")


def get_pool() -> asyncpg.Pool:
    """Return the active pool, raising if init_db() has not been called."""
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_db() first.")
    return _pool


# =============================================================================
# bot_config helpers
# =============================================================================

async def get_config(key: str) -> Optional[str]:
    """Fetch a single config value by key.  Returns None if not found."""
    row = await get_pool().fetchrow(
        "SELECT value FROM bot_config WHERE key = $1",
        key,
    )
    return row["value"] if row else None


async def set_config(key: str, value: str) -> None:
    """Insert or overwrite a config value."""
    await get_pool().execute(
        """
        INSERT INTO bot_config (key, value)
        VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        key,
        value,
    )


# =============================================================================
# Player helpers
# =============================================================================

async def register_player(
    discord_id: int,
    discord_username: str,
    ign: str,
    region: str,
) -> Optional[dict]:
    """
    Insert a new player row.

    Returns a dict of the inserted row on success.
    Returns None if the player is already registered (UNIQUE violation).
    """
    try:
        row = await get_pool().fetchrow(
            """
            INSERT INTO players (discord_id, discord_username, ign, region)
            VALUES ($1, $2, $3, $4::region_enum)
            RETURNING *
            """,
            discord_id,
            discord_username,
            ign,
            region,
        )
        return dict(row) if row else None
    except asyncpg.UniqueViolationError:
        return None


async def get_player(discord_id: int) -> Optional[dict]:
    """Fetch a player record by Discord snowflake ID."""
    row = await get_pool().fetchrow(
        "SELECT * FROM players WHERE discord_id = $1",
        discord_id,
    )
    return dict(row) if row else None


async def get_all_players(region: Optional[str] = None) -> list:
    """
    Fetch all active players, optionally filtered by region.
    Returns a list of dicts ordered by registration date ascending.
    """
    if region:
        rows = await get_pool().fetch(
            "SELECT * FROM players WHERE is_active = TRUE AND region = $1::region_enum ORDER BY registered_at ASC",
            region,
        )
    else:
        rows = await get_pool().fetch(
            "SELECT * FROM players WHERE is_active = TRUE ORDER BY registered_at ASC",
        )
    return [dict(r) for r in rows]


async def get_player_profile(discord_id: int) -> Optional[dict]:
    """
    Fetch a player profile including calculated regional ranking.
    Regional ranking partitions by the player's region and ranks by ELO descending.
    """
    row = await get_pool().fetchrow(
        """
        WITH ranked_players AS (
            SELECT 
                id,
                discord_id,
                discord_username,
                ign,
                region,
                registered_at,
                is_active,
                elo,
                kills,
                deaths,
                assists,
                matches_played,
                ROW_NUMBER() OVER (PARTITION BY region ORDER BY elo DESC) as regional_rank
            FROM players
            WHERE is_active = TRUE
        )
        SELECT * FROM ranked_players WHERE discord_id = $1
        """,
        discord_id,
    )
    return dict(row) if row else None


# =============================================================================
# Team Member helpers
# =============================================================================

async def add_team_member(team_id: int, discord_id: int, role: str) -> Optional[dict]:
    """
    Add a player to a team with a specific role.
    Returns the inserted row on success, None if the player is already in a team.
    """
    try:
        row = await get_pool().fetchrow(
            """
            INSERT INTO team_members (team_id, discord_id, role)
            VALUES ($1, $2, $3::team_role_enum)
            RETURNING *
            """,
            team_id,
            discord_id,
            role,
        )
        return dict(row) if row else None
    except asyncpg.UniqueViolationError:
        return None


async def get_team_members(team_id: int) -> list[dict]:
    """Fetch all members of a team, ordered by role and joined date."""
    rows = await get_pool().fetch(
        """
        SELECT tm.*, p.ign, p.discord_username 
        FROM team_members tm
        JOIN players p ON tm.discord_id = p.discord_id
        WHERE tm.team_id = $1
        ORDER BY tm.role ASC, tm.joined_at ASC
        """,
        team_id,
    )
    return [dict(r) for r in rows]


async def get_player_team_membership(discord_id: int) -> Optional[dict]:
    """
    Check if a player is currently in any active team.
    Returns a dict with team and member details if found.
    """
    row = await get_pool().fetchrow(
        """
        SELECT tm.*, t.team_name, t.team_tag, t.is_active 
        FROM team_members tm
        JOIN teams t ON tm.team_id = t.id
        WHERE tm.discord_id = $1 AND t.is_active = TRUE
        """,
        discord_id,
    )
    return dict(row) if row else None


# =============================================================================
# Team helpers
# =============================================================================

async def get_team_by_id(team_id: int) -> Optional[dict]:
    """Fetch a team by its primary key ID."""
    row = await get_pool().fetchrow(
        "SELECT * FROM teams WHERE id = $1",
        team_id,
    )
    return dict(row) if row else None


async def get_team_by_captain(captain_discord_id: int) -> Optional[dict]:
    """Fetch a team by the captain's Discord ID."""
    row = await get_pool().fetchrow(
        "SELECT * FROM teams WHERE captain_discord_id = $1 AND is_active = TRUE",
        captain_discord_id,
    )
    return dict(row) if row else None


async def get_team_by_name_key(team_name_key: str) -> Optional[dict]:
    """Fetch a team by its normalized name key."""
    row = await get_pool().fetchrow(
        "SELECT * FROM teams WHERE team_name_key = $1 AND is_active = TRUE",
        team_name_key,
    )
    return dict(row) if row else None


async def get_team_by_tag_key(team_tag_key: str) -> Optional[dict]:
    """Fetch a team by its normalized tag key."""
    row = await get_pool().fetchrow(
        "SELECT * FROM teams WHERE team_tag_key = $1 AND is_active = TRUE",
        team_tag_key,
    )
    return dict(row) if row else None


async def get_team_by_thread_id(thread_id: int) -> Optional[dict]:
    """Fetch a team by its private setup thread ID."""
    row = await get_pool().fetchrow(
        "SELECT * FROM teams WHERE thread_id = $1 AND is_active = TRUE",
        thread_id,
    )
    return dict(row) if row else None


async def deactivate_team(captain_discord_id: int) -> None:
    """Soft-delete a team — marks is_active=FALSE, keeps all data."""
    await get_pool().execute(
        "UPDATE teams SET is_active = FALSE WHERE captain_discord_id = $1",
        captain_discord_id,
    )


async def get_inactive_team_by_captain(captain_discord_id: int) -> Optional[dict]:
    """Fetch the most recently disbanded team for a captain."""
    row = await get_pool().fetchrow(
        """
        SELECT * FROM teams
        WHERE captain_discord_id = $1 AND is_active = FALSE
        ORDER BY created_at DESC
        LIMIT 1
        """,
        captain_discord_id,
    )
    return dict(row) if row else None


async def reactivate_team(
    captain_discord_id: int,
    thread_id: int,
    team_logo_path: Optional[str] = None,
) -> Optional[dict]:
    """Reactivate a disbanded team, keeping existing details.  Optionally updates the logo path."""
    if team_logo_path is not None:
        row = await get_pool().fetchrow(
            """
            UPDATE teams
            SET is_active = TRUE, thread_id = $2, team_logo_path = $3
            WHERE captain_discord_id = $1 AND is_active = FALSE
            RETURNING *
            """,
            captain_discord_id,
            thread_id,
            team_logo_path,
        )
    else:
        row = await get_pool().fetchrow(
            """
            UPDATE teams
            SET is_active = TRUE, thread_id = $2
            WHERE captain_discord_id = $1 AND is_active = FALSE
            RETURNING *
            """,
            captain_discord_id,
            thread_id,
        )
    return dict(row) if row else None


async def reactivate_team_fresh(
    captain_discord_id: int,
    team_name: str,
    team_name_key: str,
    team_tag: str,
    team_tag_key: str,
    region: str,
    team_logo_path: Optional[str],
    thread_id: int,
) -> Optional[dict]:
    """Reactivate a disbanded team with completely new details (fresh start)."""
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE teams
            SET
                is_active     = TRUE,
                team_name     = $2,
                team_name_key = $3,
                team_tag      = $4,
                team_tag_key  = $5,
                region        = $6::region_enum,
                team_logo_path= $7,
                thread_id     = $8,
                created_at    = NOW()
            WHERE captain_discord_id = $1 AND is_active = FALSE
            RETURNING *
            """,
            captain_discord_id,
            team_name,
            team_name_key,
            team_tag,
            team_tag_key,
            region,
            team_logo_path,
            thread_id,
        )
        return dict(row) if row else None
    except asyncpg.UniqueViolationError:
        return None


async def create_team(
    captain_discord_id: int,
    captain_username: str,
    captain_ign: str,
    team_name: str,
    team_name_key: str,
    team_tag: str,
    team_tag_key: str,
    region: str,
    team_logo_path: Optional[str],
    thread_id: int,
) -> Optional[dict]:
    """Insert a new team row and return the created record."""
    try:
        row = await get_pool().fetchrow(
            """
            INSERT INTO teams (
                captain_discord_id,
                captain_username,
                captain_ign,
                team_name,
                team_name_key,
                team_tag,
                team_tag_key,
                region,
                team_logo_path,
                thread_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::region_enum, $9, $10)
            RETURNING *
            """,
            captain_discord_id,
            captain_username,
            captain_ign,
            team_name,
            team_name_key,
            team_tag,
            team_tag_key,
            region,
            team_logo_path,
            thread_id,
        )
        return dict(row) if row else None
    except asyncpg.UniqueViolationError:
        return None


async def create_team_setup_session(
    thread_id: int,
    captain_discord_id: int,
    captain_username: str,
    captain_ign: str,
    region: str,
) -> Optional[dict]:
    """Insert a new team setup session."""
    try:
        row = await get_pool().fetchrow(
            """
            INSERT INTO team_setup_sessions (
                thread_id,
                captain_discord_id,
                captain_username,
                captain_ign,
                region
            )
            VALUES ($1, $2, $3, $4, $5::region_enum)
            RETURNING *
            """,
            thread_id,
            captain_discord_id,
            captain_username,
            captain_ign,
            region,
        )
        return dict(row) if row else None
    except asyncpg.UniqueViolationError:
        return None


async def get_team_setup_session_by_thread_id(thread_id: int) -> Optional[dict]:
    """Fetch a team setup session by the thread ID."""
    row = await get_pool().fetchrow(
        "SELECT * FROM team_setup_sessions WHERE thread_id = $1",
        thread_id,
    )
    return dict(row) if row else None


async def get_team_setup_session_by_captain(captain_discord_id: int) -> Optional[dict]:
    """Fetch a team setup session by captain Discord ID."""
    row = await get_pool().fetchrow(
        "SELECT * FROM team_setup_sessions WHERE captain_discord_id = $1",
        captain_discord_id,
    )
    return dict(row) if row else None


async def delete_team_setup_session(thread_id: int) -> None:
    """Delete a setup session once the team has been created."""
    await get_pool().execute(
        "DELETE FROM team_setup_sessions WHERE thread_id = $1",
        thread_id,
    )
