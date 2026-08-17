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
    """Fetch a player record by Discord snowflake ID (active or inactive)."""
    row = await get_pool().fetchrow(
        "SELECT * FROM players WHERE discord_id = $1",
        discord_id,
    )
    return dict(row) if row else None


async def deactivate_player(discord_id: int) -> Optional[dict]:
    """
    Soft-delete a player by setting is_active = FALSE.
    Stats and history are preserved. Returns the updated row, or None.
    """
    row = await get_pool().fetchrow(
        """
        UPDATE players
        SET is_active = FALSE
        WHERE discord_id = $1
        RETURNING *
        """,
        discord_id,
    )
    return dict(row) if row else None


async def reactivate_player(discord_id: int, new_username: str) -> Optional[dict]:
    """
    Re-activate an inactive player, refreshing their Discord username.
    All existing stats, IGN and region are kept intact.
    Returns the updated row, or None.
    """
    row = await get_pool().fetchrow(
        """
        UPDATE players
        SET is_active = TRUE, discord_username = $2
        WHERE discord_id = $1
        RETURNING *
        """,
        discord_id,
        new_username,
    )
    return dict(row) if row else None


async def reset_and_reactivate_player(
    discord_id: int,
    new_username: str,
    new_ign: str,
    new_region: str,
) -> Optional[dict]:
    """
    Re-activate an inactive player with a completely fresh profile.
    All previous stats are wiped to 0 and ELO reset to 1000.
    Returns the updated row, or None.
    """
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE players
            SET is_active        = TRUE,
                discord_username = $2,
                ign              = $3,
                region           = $4::region_enum,
                elo              = 1000,
                kills            = 0,
                deaths           = 0,
                assists          = 0,
                matches_played   = 0,
                wins             = 0,
                mvp_count        = 0,
                registered_at    = NOW()
            WHERE discord_id = $1
            RETURNING *
            """,
            discord_id,
            new_username,
            new_ign,
            new_region,
        )
        return dict(row) if row else None
    except Exception:
        return None


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
                wins,
                mvp_count,
                ROW_NUMBER() OVER (PARTITION BY region ORDER BY elo DESC) as regional_rank
            FROM players
            WHERE is_active = TRUE
        )
        SELECT * FROM ranked_players WHERE discord_id = $1
        """,
        discord_id,
    )
    return dict(row) if row else None


async def update_player_ign(discord_id: int, new_ign: str) -> Optional[dict]:
    """Update a player's in-game name. Returns the updated row or None."""
    row = await get_pool().fetchrow(
        """
        UPDATE players
        SET ign = $1
        WHERE discord_id = $2
        RETURNING *
        """,
        new_ign,
        discord_id,
    )
    return dict(row) if row else None


async def update_player_region(discord_id: int, new_region: str) -> Optional[dict]:
    """Update a player's region. Returns the updated row or None."""
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE players
            SET region = $1::region_enum
            WHERE discord_id = $2
            RETURNING *
            """,
            new_region,
            discord_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


async def set_player_status(
    discord_id: int,
    new_status: str,
    penalty_ends_at=None,
) -> Optional[dict]:
    """
    Update a player's status field.

    new_status      — one of 'IDLE', 'IN_QUEUE', 'IN_MATCH', 'PENALTY_COOLDOWN'
    penalty_ends_at — datetime (UTC) when the penalty expires; only meaningful
                      when new_status == 'PENALTY_COOLDOWN'. Pass None otherwise.

    Returns the updated row or None.
    """
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE players
            SET status          = $1::player_status_enum,
                status_since    = NOW(),
                penalty_ends_at = $2
            WHERE discord_id = $3
            RETURNING *
            """,
            new_status,
            penalty_ends_at,
            discord_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


async def toggle_player_dms(discord_id: int) -> Optional[dict]:
    """
    Flip the dms_enabled flag for a player.
    Returns the updated row (with the new value) or None on failure.
    """
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE players
            SET dms_enabled = NOT dms_enabled
            WHERE discord_id = $1
            RETURNING *
            """,
            discord_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


async def set_player_dms(discord_id: int, enabled: bool) -> Optional[dict]:
    """Explicitly set dms_enabled. Returns updated row or None."""
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE players
            SET dms_enabled = $1
            WHERE discord_id = $2
            RETURNING *
            """,
            enabled,
            discord_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


async def update_team_region(team_id: int, new_region: str) -> Optional[dict]:
    """Update the region of a team row. Returns updated row or None."""
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE teams
            SET region = $1::region_enum
            WHERE id = $2
            RETURNING *
            """,
            new_region,
            team_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


async def bulk_update_team_members_region(team_id: int, new_region: str) -> int:
    """
    Update the region of every player who is a member of *team_id*.
    Returns the number of rows updated.
    """
    try:
        result = await get_pool().execute(
            """
            UPDATE players
            SET region = $1::region_enum
            WHERE discord_id IN (
                SELECT discord_id FROM team_members WHERE team_id = $2
            )
            """,
            new_region,
            team_id,
        )
        # result is e.g. "UPDATE 5"
        return int(result.split()[-1])
    except Exception:
        return 0


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


async def get_team_role_counts(team_id: int) -> dict[str, int]:
    """
    Get the count of active members per role for a given team.
    Returns e.g. {'Player': 3, 'Manager': 1, 'Coach': 0, 'Substitute': 1}
    """
    rows = await get_pool().fetch(
        """
        SELECT role, COUNT(*)::INT as count
        FROM team_members
        WHERE team_id = $1
        GROUP BY role
        """,
        team_id,
    )
    counts = {"Player": 0, "Manager": 0, "Coach": 0, "Substitute": 0}
    for r in rows:
        counts[r["role"]] = r["count"]
    return counts


async def get_team_pending_invite_counts(team_id: int) -> dict[str, int]:
    """
    Get the count of active, unexpired pending invites per role for a given team.
    """
    rows = await get_pool().fetch(
        """
        SELECT role, COUNT(*)::INT as count
        FROM team_invites
        WHERE team_id = $1 AND is_active = TRUE AND expires_at > NOW()
        GROUP BY role
        """,
        team_id,
    )
    counts = {"Player": 0, "Manager": 0, "Coach": 0, "Substitute": 0}
    for r in rows:
        counts[r["role"]] = r["count"]
    return counts


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


async def clear_team_members(team_id: int) -> list[dict]:
    """
    Remove all players from a team and return their records.
    Used during reactivation so old members can be notified to ask for reinvites.
    """
    rows = await get_pool().fetch(
        """
        DELETE FROM team_members
        WHERE team_id = $1
        RETURNING *
        """,
        team_id,
    )
    return [dict(r) for r in rows]


async def remove_team_member(team_id: int, discord_id: int) -> bool:
    """
    Remove a specific player from a team. Returns True if a row was deleted.
    """
    status = await get_pool().execute(
        """
        DELETE FROM team_members
        WHERE team_id = $1 AND discord_id = $2
        """,
        team_id,
        discord_id,
    )
    # status is usually something like "DELETE 1" or "DELETE 0"
    return status.endswith(" 1")


async def update_team_member_role(team_id: int, discord_id: int, new_role: str) -> Optional[dict]:
    """
    Update an existing team member's role (Player, Manager, Coach, Substitute).
    Returns the updated row or None.
    """
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE team_members
            SET role = $1::team_role_enum
            WHERE team_id = $2 AND discord_id = $3
            RETURNING *
            """,
            new_role,
            team_id,
            discord_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


# =============================================================================
# Team Invite helpers
# =============================================================================

async def create_team_invite(
    team_id: int,
    inviter_discord_id: int,
    target_discord_id: int,
    role: str,
    dm_message_id: Optional[int] = None,
) -> Optional[dict]:
    """
    Create a new pending team invite.
    First deactivates any existing pending invites from this team to this target.
    """
    try:
        await get_pool().execute(
            """
            UPDATE team_invites
            SET is_active = FALSE
            WHERE team_id = $1 AND target_discord_id = $2 AND is_active = TRUE
            """,
            team_id,
            target_discord_id,
        )
        row = await get_pool().fetchrow(
            """
            INSERT INTO team_invites (team_id, inviter_discord_id, target_discord_id, role, dm_message_id, is_active)
            VALUES ($1, $2, $3, $4::team_role_enum, $5, TRUE)
            RETURNING *
            """,
            team_id,
            inviter_discord_id,
            target_discord_id,
            role,
            dm_message_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


async def set_invite_dm_message_id(invite_id: int, dm_message_id: int) -> bool:
    """Save the DM message ID for an invite so it can be edited/cancelled later."""
    try:
        res = await get_pool().execute(
            """
            UPDATE team_invites
            SET dm_message_id = $1
            WHERE id = $2
            """,
            dm_message_id,
            invite_id,
        )
        return res.endswith(" 1")
    except Exception:
        return False


async def get_pending_invite_by_id(invite_id: int) -> Optional[dict]:
    """Fetch an active, unexpired invite by primary ID."""
    row = await get_pool().fetchrow(
        """
        SELECT * FROM team_invites
        WHERE id = $1 AND is_active = TRUE AND expires_at > NOW()
        """,
        invite_id,
    )
    return dict(row) if row else None


async def get_pending_invite_for_target(team_id: int, target_discord_id: int) -> Optional[dict]:
    """Check if there is an active, unexpired invite from a team to a target player."""
    row = await get_pool().fetchrow(
        """
        SELECT * FROM team_invites
        WHERE team_id = $1 AND target_discord_id = $2 AND is_active = TRUE AND expires_at > NOW()
        """,
        team_id,
        target_discord_id,
    )
    return dict(row) if row else None


async def get_pending_invites_for_team(team_id: int) -> list[dict]:
    """Fetch all active, unexpired invites sent by a team, joined with player info."""
    rows = await get_pool().fetch(
        """
        SELECT ti.*, p.ign, p.discord_username
        FROM team_invites ti
        LEFT JOIN players p ON ti.target_discord_id = p.discord_id
        WHERE ti.team_id = $1 AND ti.is_active = TRUE AND ti.expires_at > NOW()
        ORDER BY ti.created_at DESC
        """,
        team_id,
    )
    return [dict(r) for r in rows]


async def cancel_team_invite(team_id: int, target_discord_id: int) -> Optional[dict]:
    """Cancel an active invite for a specific target player. Returns the cancelled row."""
    row = await get_pool().fetchrow(
        """
        UPDATE team_invites
        SET is_active = FALSE
        WHERE team_id = $1 AND target_discord_id = $2 AND is_active = TRUE AND expires_at > NOW()
        RETURNING *
        """,
        team_id,
        target_discord_id,
    )
    return dict(row) if row else None


async def cancel_all_team_invites(team_id: int) -> list[dict]:
    """Cancel all active invites for a team. Returns the list of cancelled rows."""
    rows = await get_pool().fetch(
        """
        UPDATE team_invites
        SET is_active = FALSE
        WHERE team_id = $1 AND is_active = TRUE AND expires_at > NOW()
        RETURNING *
        """,
        team_id,
    )
    return [dict(r) for r in rows]


async def complete_team_invite(invite_id: int) -> bool:
    """Mark an invite as completed/inactive (accepted or declined)."""
    try:
        res = await get_pool().execute(
            """
            UPDATE team_invites
            SET is_active = FALSE
            WHERE id = $1
            """,
            invite_id,
        )
        return res.endswith(" 1")
    except Exception:
        return False


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


async def update_team_tag(team_id: int, new_tag: str) -> Optional[dict]:
    """
    Update a team's tag.
    new_tag     — the display tag (e.g. 'VGA')
    team_tag_key — normalised lowercase used for uniqueness checks.

    Returns the updated row on success, None on unique-key conflict or error.
    """
    new_tag_key = new_tag.strip().lower()
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE teams
            SET team_tag     = $1,
                team_tag_key = $2
            WHERE id = $3
            RETURNING *
            """,
            new_tag.strip(),
            new_tag_key,
            team_id,
        )
        return dict(row) if row else None
    except asyncpg.UniqueViolationError:
        return None


async def update_team_name(team_id: int, new_name: str) -> Optional[dict]:
    """
    Update a team's display name.
    new_name     — the display name (stored as-is, e.g. 'Vega Assassins')
    team_name_key — normalised lowercase+stripped used for uniqueness checks.

    Returns the updated row on success, None on unique-key conflict or error.
    """
    new_name_key = new_name.strip().lower()
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE teams
            SET team_name     = $1,
                team_name_key = $2
            WHERE id = $3
            RETURNING *
            """,
            new_name.strip(),
            new_name_key,
            team_id,
        )
        return dict(row) if row else None
    except asyncpg.UniqueViolationError:
        return None


async def update_team_logo(team_id: int, new_logo_path: str) -> Optional[dict]:
    """Update the saved logo file path for a team. Returns updated row or None."""
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE teams
            SET team_logo_path = $1
            WHERE id = $2
            RETURNING *
            """,
            new_logo_path,
            team_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


async def transfer_team_captain(
    team_id: int,
    old_captain_id: int,
    new_captain_id: int,
    new_captain_username: str,
    new_captain_ign: str,
    old_captain_new_role: str = "Player",
) -> Optional[dict]:
    """
    Atomically transfer ownership of a team:
    1. Remove the new captain from team_members.
    2. Update the teams row with the new captain details.
    3. Add the old captain into team_members with old_captain_new_role.
    Returns the updated team row on success, None on error.
    """
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. Remove new captain from team_members
                await conn.execute(
                    """
                    DELETE FROM team_members
                    WHERE team_id = $1 AND discord_id = $2
                    """,
                    team_id,
                    new_captain_id,
                )
                # 2. Update teams row
                updated_team = await conn.fetchrow(
                    """
                    UPDATE teams
                    SET captain_discord_id = $1,
                        captain_username   = $2,
                        captain_ign        = $3
                    WHERE id = $4 AND is_active = TRUE
                    RETURNING *
                    """,
                    new_captain_id,
                    new_captain_username,
                    new_captain_ign,
                    team_id,
                )
                if not updated_team:
                    return None

                # 3. Add old captain into team_members
                await conn.execute(
                    """
                    INSERT INTO team_members (team_id, discord_id, role)
                    VALUES ($1, $2, $3::team_role_enum)
                    """,
                    team_id,
                    old_captain_id,
                    old_captain_new_role,
                )

                return dict(updated_team)
    except Exception:
        return None


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
