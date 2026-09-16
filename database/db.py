"""
database/db.py
Async PostgreSQL connection pool and CRUD helpers for the Vega Queue Bot.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg


log = logging.getLogger(__name__)

# Module-level pool — initialised once at startup, reused across the bot's lifetime.
_pool: Optional[asyncpg.Pool] = None


# =============================================================================
# Pool lifecycle
# =============================================================================

async def init_db() -> None:
    """Create the async connection pool and auto-apply schema on first boot."""
    global _pool
    dsn = os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    log.info("Database connection pool created.")
    await _apply_schema()


async def _apply_schema() -> None:
    """
    Read database/schema.sql and execute it against the connected database.
    All statements use IF NOT EXISTS / ADD COLUMN IF NOT EXISTS guards so this
    is completely safe to re-run on every startup — it is a no-op when the
    schema is already up to date.
    """
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    if not os.path.exists(schema_path):
        log.warning("schema.sql not found at %s — skipping auto-migration.", schema_path)
        return

    with open(schema_path, "r", encoding="utf-8") as fh:
        sql = fh.read()

    async with _pool.acquire() as conn:
        try:
            await conn.execute(sql)
            log.info("Database schema applied successfully (auto-migration complete).")
        except Exception as exc:
            # Log the error but don't crash — tables may already exist from a prior run.
            log.warning("Schema auto-migration warning (usually safe to ignore): %s", exc)

        try:
            await conn.execute(
                """
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
                """
            )
        except Exception as e:
            log.warning("Could not ensure matchmaking_verifications table: %s", e)



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

async def get_player_by_ign(ign: str, active_only: bool = True) -> Optional[dict]:
    """
    Fetch a player record by case-insensitive in-game name (trimmed).
    If active_only is True, matches only is_active = TRUE rows.
    """
    clean_ign = (ign or "").strip()
    if not clean_ign:
        return None
    if active_only:
        row = await get_pool().fetchrow(
            """
            SELECT * FROM players
            WHERE LOWER(TRIM(ign)) = LOWER(TRIM($1))
              AND is_active = TRUE
            LIMIT 1
            """,
            clean_ign,
        )
    else:
        row = await get_pool().fetchrow(
            """
            SELECT * FROM players
            WHERE LOWER(TRIM(ign)) = LOWER(TRIM($1))
            LIMIT 1
            """,
            clean_ign,
        )
    return dict(row) if row else None


async def register_player(
    discord_id: int,
    discord_username: str,
    ign: str,
    region: str,
) -> Optional[dict]:
    """
    Insert a new player row.

    Returns a dict of the inserted row on success.
    Returns None if the player is already registered or the IGN is already taken by an active player.
    """
    clean_ign = (ign or "").strip()
    pool = get_pool()

    # Reject if an active player already holds this IGN
    existing_ign = await pool.fetchrow(
        """
        SELECT discord_id FROM players
        WHERE LOWER(TRIM(ign)) = LOWER(TRIM($1))
          AND is_active = TRUE
        LIMIT 1
        """,
        clean_ign,
    )
    if existing_ign:
        log.warning(
            "Registration rejected: IGN '%s' is already registered by discord_id %d.",
            clean_ign, existing_ign["discord_id"]
        )
        return None

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO players (discord_id, discord_username, ign, region)
            VALUES ($1, $2, $3, $4::region_enum)
            RETURNING *
            """,
            discord_id,
            discord_username,
            clean_ign,
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
    discord_username: str,
    new_ign: str,
    new_region: str,
) -> Optional[dict]:
    """
    Re-activate an inactive player with a completely fresh profile.
    All previous stats are wiped to 0 and ELO reset to 1000.
    Checks that new_ign is not taken by another active player.
    Returns the updated row, or None.
    """
    clean_ign = (new_ign or "").strip()
    pool = get_pool()
    existing_ign = await pool.fetchrow(
        """
        SELECT discord_id FROM players
        WHERE LOWER(TRIM(ign)) = LOWER(TRIM($1))
          AND discord_id != $2
          AND is_active = TRUE
        LIMIT 1
        """,
        clean_ign,
        discord_id,
    )
    if existing_ign:
        log.warning(
            "Reset/reactivate rejected: IGN '%s' already taken by discord_id %d.",
            clean_ign, existing_ign["discord_id"]
        )
        return None

    try:
        row = await pool.fetchrow(
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
            discord_username,
            clean_ign,
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
    """Update a player's in-game name ensuring no other active player shares it. Returns the updated row or None."""
    clean_ign = (new_ign or "").strip()
    pool = get_pool()
    existing_ign = await pool.fetchrow(
        """
        SELECT discord_id FROM players
        WHERE LOWER(TRIM(ign)) = LOWER(TRIM($1))
          AND discord_id != $2
          AND is_active = TRUE
        LIMIT 1
        """,
        clean_ign,
        discord_id,
    )
    if existing_ign:
        log.warning(
            "Update IGN rejected: '%s' already taken by discord_id %d.",
            clean_ign, existing_ign["discord_id"]
        )
        return None

    row = await pool.fetchrow(
        """
        UPDATE players
        SET ign = $1
        WHERE discord_id = $2
        RETURNING *
        """,
        clean_ign,
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


# Alias for compatibility across cogs
admin_update_player_region = update_player_region


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


async def ban_player(
    discord_id: int,
    reason: str,
    banned_by: int,
    duration_hours: Optional[int] = None,
) -> Optional[dict]:
    """
    Ban a player, storing the reason, admin ID, and optional expiration timestamp.
    Also resets their status to IDLE and clears any existing penalty timestamp.
    """
    try:
        if duration_hours is not None and duration_hours > 0:
            row = await get_pool().fetchrow(
                """
                UPDATE players
                SET is_banned       = TRUE,
                    banned_at       = NOW(),
                    banned_until    = NOW() + ($1 || ' hours')::INTERVAL,
                    ban_reason      = $2,
                    banned_by       = $3,
                    status          = 'IDLE'::player_status_enum,
                    status_since    = NOW(),
                    penalty_ends_at = NULL
                WHERE discord_id = $4
                RETURNING *
                """,
                str(duration_hours),
                reason,
                banned_by,
                discord_id,
            )
        else:
            row = await get_pool().fetchrow(
                """
                UPDATE players
                SET is_banned       = TRUE,
                    banned_at       = NOW(),
                    banned_until    = NULL,
                    ban_reason      = $1,
                    banned_by       = $2,
                    status          = 'IDLE'::player_status_enum,
                    status_since    = NOW(),
                    penalty_ends_at = NULL
                WHERE discord_id = $3
                RETURNING *
                """,
                reason,
                banned_by,
                discord_id,
            )
        return dict(row) if row else None
    except Exception as e:
        log.error("Error banning player %d: %s", discord_id, e)
        return None


async def unban_player(discord_id: int) -> Optional[dict]:
    """
    Unban a player, clearing the ban status, reason, timestamps, and cooldown penalties.
    """
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE players
            SET is_banned       = FALSE,
                banned_at       = NULL,
                banned_until    = NULL,
                ban_reason      = NULL,
                banned_by       = NULL,
                status          = 'IDLE'::player_status_enum,
                status_since    = NOW(),
                penalty_ends_at = NULL
            WHERE discord_id = $1
            RETURNING *
            """,
            discord_id,
        )
        return dict(row) if row else None
    except Exception as e:
        log.error("Error unbanning player %d: %s", discord_id, e)
        return None


async def get_player_ban_status(discord_id: int) -> tuple[bool, Optional[str], Optional[datetime], Optional[int]]:
    """
    Check if a player is banned. Automatically expires temporary bans whose banned_until has passed.
    Returns (is_banned, ban_reason, banned_until, banned_by).
    """
    try:
        row = await get_pool().fetchrow(
            "SELECT is_banned, banned_at, banned_until, ban_reason, banned_by FROM players WHERE discord_id = $1",
            discord_id,
        )
        if not row or not row["is_banned"]:
            return False, None, None, None

        banned_until = row["banned_until"]
        if banned_until:
            if banned_until.tzinfo is None:
                banned_until = banned_until.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= banned_until:
                # Ban expired — auto unban
                await unban_player(discord_id)
                return False, None, None, None

        return True, row["ban_reason"], banned_until, row["banned_by"]
    except Exception:
        return False, None, None, None



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

                # 4. Update captain in team_queue if team is queued
                await conn.execute(
                    "UPDATE team_queue SET captain_discord_id = $1 WHERE team_id = $2",
                    new_captain_id,
                    team_id,
                )

                return dict(updated_team)
    except Exception:
        return None


async def deactivate_team(captain_discord_id: int) -> None:
    """Soft-delete a team — marks is_active=FALSE, keeps all data, and removes from queue."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE teams SET is_active = FALSE WHERE captain_discord_id = $1",
                captain_discord_id,
            )
            await conn.execute(
                "DELETE FROM team_queue WHERE captain_discord_id = $1",
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


# =============================================================================
# Team Queue helpers
# =============================================================================

async def add_team_to_queue(
    team_id: int,
    queue_type: str,
    region: str,
    captain_discord_id: int,
) -> bool:
    """
    Add or update a team in the team queue for a specific queue_type ('REGIONAL' or 'GLOBAL').
    A team can be in both regional and global queues at the same time.
    Returns True if successfully queued.
    """
    query = """
        INSERT INTO team_queue (team_id, queue_type, region, captain_discord_id, joined_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (team_id, queue_type) DO UPDATE
            SET region = EXCLUDED.region,
                captain_discord_id = EXCLUDED.captain_discord_id,
                joined_at = NOW()
    """
    await get_pool().execute(query, team_id, queue_type, region, captain_discord_id)
    return True


async def remove_team_from_queue(team_id: int, queue_type: Optional[str] = None) -> bool:
    """
    Remove a team from the queue.
    If queue_type is provided, removes from that specific queue ('REGIONAL' or 'GLOBAL').
    If queue_type is None, removes from all queues.
    Returns True if any row was removed, False if not found.
    """
    if queue_type:
        res = await get_pool().execute(
            "DELETE FROM team_queue WHERE team_id = $1 AND queue_type = $2",
            team_id,
            queue_type,
        )
    else:
        res = await get_pool().execute(
            "DELETE FROM team_queue WHERE team_id = $1",
            team_id,
        )
    return not res.endswith(" 0")


async def get_team_queue(queue_type: Optional[str] = None) -> list[dict]:
    """
    Fetch all teams currently in the queue, joined with team details.
    Optionally filter by queue_type ('REGIONAL' or 'GLOBAL').
    Ordered by joined_at ASC (first in, first out).
    """
    if queue_type:
        query = """
            SELECT
                tq.id as queue_id,
                tq.team_id,
                tq.queue_type,
                tq.region,
                tq.captain_discord_id,
                tq.joined_at,
                t.team_name,
                t.team_tag,
                t.captain_ign,
                t.captain_username
            FROM team_queue tq
            JOIN teams t ON tq.team_id = t.id
            WHERE tq.queue_type = $1 AND t.is_active = TRUE
            ORDER BY tq.joined_at ASC
        """
        rows = await get_pool().fetch(query, queue_type)
    else:
        query = """
            SELECT
                tq.id as queue_id,
                tq.team_id,
                tq.queue_type,
                tq.region,
                tq.captain_discord_id,
                tq.joined_at,
                t.team_name,
                t.team_tag,
                t.captain_ign,
                t.captain_username
            FROM team_queue tq
            JOIN teams t ON tq.team_id = t.id
            WHERE t.is_active = TRUE
            ORDER BY tq.joined_at ASC
        """
        rows = await get_pool().fetch(query)
    return [dict(r) for r in rows]


async def get_queued_team(team_id: int, queue_type: Optional[str] = None) -> Optional[dict]:
    """Check if a specific team is currently in a queue (optionally filtered by queue_type)."""
    if queue_type:
        row = await get_pool().fetchrow(
            """
            SELECT
                tq.*,
                t.team_name,
                t.team_tag,
                t.captain_ign
            FROM team_queue tq
            JOIN teams t ON tq.team_id = t.id
            WHERE tq.team_id = $1 AND tq.queue_type = $2
            """,
            team_id,
            queue_type,
        )
    else:
        row = await get_pool().fetchrow(
            """
            SELECT
                tq.*,
                t.team_name,
                t.team_tag,
                t.captain_ign
            FROM team_queue tq
            JOIN teams t ON tq.team_id = t.id
            WHERE tq.team_id = $1
            LIMIT 1
            """,
            team_id,
        )
    return dict(row) if row else None


async def get_team_queues(team_id: int) -> list[dict]:
    """Fetch all active queue entries for a specific team (can return both REGIONAL and GLOBAL)."""
    rows = await get_pool().fetch(
        """
        SELECT
            tq.*,
            t.team_name,
            t.team_tag,
            t.captain_ign
        FROM team_queue tq
        JOIN teams t ON tq.team_id = t.id
        WHERE tq.team_id = $1
        ORDER BY tq.joined_at ASC
        """,
        team_id,
    )
    return [dict(r) for r in rows]


# =============================================================================
# Admin Management Helpers
# =============================================================================

async def search_teams(query: str, limit: int = 25) -> list[dict]:
    """
    Search teams for autocomplete by name, tag, or numeric ID.
    Returns matching team records up to limit.
    """
    cleaned = query.strip()
    if not cleaned:
        rows = await get_pool().fetch(
            "SELECT id, team_name, team_tag, region, is_active FROM teams ORDER BY is_active DESC, team_name ASC LIMIT $1",
            limit,
        )
    else:
        like_term = f"%{cleaned}%"
        rows = await get_pool().fetch(
            """
            SELECT id, team_name, team_tag, region, is_active
            FROM teams
            WHERE team_name ILIKE $1 OR team_tag ILIKE $1 OR id::TEXT = $2
            ORDER BY is_active DESC, team_name ASC
            LIMIT $3
            """,
            like_term,
            cleaned,
            limit,
        )
    return [dict(r) for r in rows]


async def get_team_by_identifier(identifier: str) -> Optional[dict]:
    """
    Resolve a team by numeric ID, team name, or team tag (case-insensitive).
    """
    cleaned = identifier.strip()
    if not cleaned:
        return None

    if cleaned.isdigit():
        row = await get_pool().fetchrow("SELECT * FROM teams WHERE id = $1", int(cleaned))
        if row:
            return dict(row)

    row = await get_pool().fetchrow(
        """
        SELECT * FROM teams
        WHERE LOWER(team_name) = LOWER($1)
           OR LOWER(team_tag) = LOWER($1)
           OR team_name_key = LOWER($1)
           OR team_tag_key = LOWER($1)
        LIMIT 1
        """,
        cleaned,
    )
    return dict(row) if row else None


async def admin_update_player_ign(discord_id: int, new_ign: str) -> Optional[dict]:
    """
    Update a player's IGN and update teams.captain_ign if they are a captain.
    """
    clean_ign = (new_ign or "").strip()
    pool = get_pool()
    existing_ign = await pool.fetchrow(
        """
        SELECT discord_id FROM players
        WHERE LOWER(TRIM(ign)) = LOWER(TRIM($1))
          AND discord_id != $2
          AND is_active = TRUE
        LIMIT 1
        """,
        clean_ign,
        discord_id,
    )
    if existing_ign:
        log.warning(
            "Admin update IGN rejected: '%s' already taken by discord_id %d.",
            clean_ign, existing_ign["discord_id"]
        )
        return None

    async with pool.acquire() as conn:
        async with conn.transaction():
            player_row = await conn.fetchrow(
                """
                UPDATE players
                SET ign = $1
                WHERE discord_id = $2
                RETURNING *
                """,
                clean_ign,
                discord_id,
            )
            if not player_row:
                return None

            # Synchronize captain_ign if player is captain of an active team
            await conn.execute(
                """
                UPDATE teams
                SET captain_ign = $1
                WHERE captain_discord_id = $2
                """,
                clean_ign,
                discord_id,
            )
            return dict(player_row)


async def admin_update_player_elo(discord_id: int, new_elo: int) -> Optional[dict]:
    """
    Directly update a player's ELO rating.
    """
    row = await get_pool().fetchrow(
        """
        UPDATE players
        SET elo = $1
        WHERE discord_id = $2
        RETURNING *
        """,
        new_elo,
        discord_id,
    )
    return dict(row) if row else None


async def admin_reset_player_stats(discord_id: int, reset_elo: bool = False) -> Optional[dict]:
    """
    Reset combat statistics for a player, optionally resetting ELO back to 1000.
    """
    if reset_elo:
        query = """
            UPDATE players
            SET kills = 0,
                deaths = 0,
                assists = 0,
                matches_played = 0,
                wins = 0,
                mvp_count = 0,
                elo = 1000
            WHERE discord_id = $1
            RETURNING *
        """
    else:
        query = """
            UPDATE players
            SET kills = 0,
                deaths = 0,
                assists = 0,
                matches_played = 0,
                wins = 0,
                mvp_count = 0
            WHERE discord_id = $1
            RETURNING *
        """
    row = await get_pool().fetchrow(query, discord_id)
    return dict(row) if row else None


async def admin_reset_player_status(discord_id: int) -> Optional[dict]:
    """
    Clear active queue/match status or cooldown penalty for a player.
    """
    row = await get_pool().fetchrow(
        """
        UPDATE players
        SET status = 'IDLE'::player_status_enum,
            status_since = NOW(),
            penalty_ends_at = NULL
        WHERE discord_id = $1
        RETURNING *
        """,
        discord_id,
    )
    return dict(row) if row else None


async def admin_delete_player(discord_id: int) -> tuple[bool, str]:
    """
    Delete a player record from the system.
    Safely checks if the player is captain of an active team first.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Check if active team captain
            active_team = await conn.fetchrow(
                "SELECT team_name FROM teams WHERE captain_discord_id = $1 AND is_active = TRUE",
                discord_id,
            )
            if active_team:
                return False, f"Player is captain of active team '{active_team['team_name']}'. Transfer captaincy or disband team first."

            # Delete from team_members and invites
            await conn.execute("DELETE FROM team_members WHERE discord_id = $1", discord_id)
            await conn.execute("DELETE FROM team_invites WHERE target_discord_id = $1 OR inviter_discord_id = $1", discord_id)
            # Delete from players
            res = await conn.execute("DELETE FROM players WHERE discord_id = $1", discord_id)
            if res.endswith(" 0"):
                return False, "Player was not found in the database."
            return True, "Player record successfully deleted."


async def admin_force_add_team_member(team_id: int, discord_id: int, role: str) -> tuple[bool, str]:
    """
    Force-add a player to a team roster.
    If the player is currently in another team, they are removed from that team first.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Ensure target team exists
            team = await conn.fetchrow("SELECT id, team_name FROM teams WHERE id = $1 AND is_active = TRUE", team_id)
            if not team:
                return False, "Target team not found or is inactive."

            # Ensure player is registered
            player = await conn.fetchrow("SELECT discord_id, ign FROM players WHERE discord_id = $1", discord_id)
            if not player:
                return False, "Player is not registered in the system."

            # Check if player is already captain of this or another team
            captain_team = await conn.fetchrow("SELECT team_name FROM teams WHERE captain_discord_id = $1 AND is_active = TRUE", discord_id)
            if captain_team:
                return False, f"Player is already the captain of team '{captain_team['team_name']}'."

            # Remove from any existing team roster
            await conn.execute("DELETE FROM team_members WHERE discord_id = $1", discord_id)

            # Insert into team_members
            await conn.execute(
                """
                INSERT INTO team_members (team_id, discord_id, role, joined_at)
                VALUES ($1, $2, $3::team_role_enum, NOW())
                """,
                team_id,
                discord_id,
                role,
            )
            return True, f"Added **{player['ign']}** to **{team['team_name']}** as **{role}**."


async def admin_hard_delete_team(team_id: int) -> bool:
    """
    Permanently delete a team and cascade deletes to members, invites, and queue.
    """
    res = await get_pool().execute("DELETE FROM teams WHERE id = $1", team_id)
    return not res.endswith(" 0")


# =============================================================================
# Scrim Match Helpers
# =============================================================================

async def create_scrim_match(
    team1_id: int,
    team2_id: int,
    channel_id: int,
    match_type: str,
    region: str,
) -> Optional[dict]:
    """Create a new scrim match entry."""
    row = await get_pool().fetchrow(
        """
        INSERT INTO scrim_matches (team1_id, team2_id, channel_id, match_type, region, status, created_at)
        VALUES ($1, $2, $3, $4, $5::region_enum, 'NEGOTIATING', NOW())
        RETURNING *
        """,
        team1_id,
        team2_id,
        channel_id,
        match_type,
        region,
    )
    return dict(row) if row else None


async def update_scrim_match_panel(match_id: int, panel_message_id: int) -> bool:
    """Save the panel message ID for the scrim negotiation embed."""
    res = await get_pool().execute(
        "UPDATE scrim_matches SET panel_message_id = $1 WHERE id = $2",
        panel_message_id,
        match_id,
    )
    return not res.endswith(" 0")


async def get_scrim_match_by_channel(channel_id: int) -> Optional[dict]:
    """Fetch scrim match details by Discord channel ID, joined with both teams."""
    row = await get_pool().fetchrow(
        """
        SELECT
            sm.*,
            t1.team_name as team1_name,
            t1.team_tag as team1_tag,
            t1.captain_discord_id as team1_captain_id,
            t1.captain_ign as team1_captain_ign,
            t2.team_name as team2_name,
            t2.team_tag as team2_tag,
            t2.captain_discord_id as team2_captain_id,
            t2.captain_ign as team2_captain_ign
        FROM scrim_matches sm
        JOIN teams t1 ON sm.team1_id = t1.id
        JOIN teams t2 ON sm.team2_id = t2.id
        WHERE sm.channel_id = $1
        """,
        channel_id,
    )
    return dict(row) if row else None


async def get_scrim_match_by_id(match_id: int) -> Optional[dict]:
    """Fetch scrim match details by primary ID, joined with both teams."""
    row = await get_pool().fetchrow(
        """
        SELECT
            sm.*,
            t1.team_name as team1_name,
            t1.team_tag as team1_tag,
            t1.captain_discord_id as team1_captain_id,
            t1.captain_ign as team1_captain_ign,
            t2.team_name as team2_name,
            t2.team_tag as team2_tag,
            t2.captain_discord_id as team2_captain_id,
            t2.captain_ign as team2_captain_ign
        FROM scrim_matches sm
        JOIN teams t1 ON sm.team1_id = t1.id
        JOIN teams t2 ON sm.team2_id = t2.id
        WHERE sm.id = $1
        """,
        match_id,
    )
    return dict(row) if row else None


async def propose_scrim_time(
    match_id: int,
    proposed_time: str,
    team_id: int,
    user_id: int,
) -> Optional[dict]:
    """Set a newly proposed scrim time."""
    row = await get_pool().fetchrow(
        """
        UPDATE scrim_matches
        SET proposed_time = $1,
            proposed_by_team_id = $2,
            proposed_by_user_id = $3,
            confirmed_by_user_id = NULL,
            confirmed_at = NULL,
            status = 'NEGOTIATING'
        WHERE id = $4
        RETURNING *
        """,
        proposed_time,
        team_id,
        user_id,
        match_id,
    )
    return dict(row) if row else None


async def accept_scrim_time(match_id: int, user_id: int) -> Optional[dict]:
    """Accept the proposed scrim time and confirm the match."""
    row = await get_pool().fetchrow(
        """
        UPDATE scrim_matches
        SET status = 'CONFIRMED',
            confirmed_by_user_id = $1,
            confirmed_at = NOW()
        WHERE id = $2 AND proposed_time IS NOT NULL
        RETURNING *
        """,
        user_id,
        match_id,
    )
    return dict(row) if row else None


async def cancel_scrim_match(match_id: int) -> Optional[dict]:
    """Cancel a scrim match."""
    row = await get_pool().fetchrow(
        """
        UPDATE scrim_matches
        SET status = 'CANCELLED'
        WHERE id = $1
        RETURNING *
        """,
        match_id,
    )
    return dict(row) if row else None


# =============================================================================
# Solo Queue Helpers (Server B)
# =============================================================================

async def add_player_to_solo_queue(discord_id: int) -> bool:
    """Add a player to the 10-man solo queue."""
    query = """
        INSERT INTO solo_queue (discord_id, joined_at)
        VALUES ($1, NOW())
        ON CONFLICT (discord_id) DO UPDATE
            SET joined_at = NOW()
    """
    await get_pool().execute(query, discord_id)
    return True


async def remove_player_from_solo_queue(discord_id: int) -> bool:
    """Remove a player from the 10-man solo queue."""
    res = await get_pool().execute(
        "DELETE FROM solo_queue WHERE discord_id = $1",
        discord_id,
    )
    return not res.endswith(" 0")


async def get_solo_queue() -> list[dict]:
    """Fetch all players currently waiting in the 10-man solo queue, joined with player stats."""
    query = """
        SELECT
            sq.id as queue_id,
            sq.discord_id,
            sq.joined_at,
            p.ign,
            p.discord_username,
            p.elo,
            p.region,
            p.wins,
            p.matches_played
        FROM solo_queue sq
        JOIN players p ON sq.discord_id = p.discord_id
        WHERE p.is_active = TRUE AND p.is_banned = FALSE
        ORDER BY sq.joined_at ASC
    """
    rows = await get_pool().fetch(query)
    return [dict(r) for r in rows]


async def clear_solo_queue(discord_ids: Optional[list[int]] = None) -> None:
    """Clear specific players or all players from the solo queue."""
    if discord_ids:
        await get_pool().execute(
            "DELETE FROM solo_queue WHERE discord_id = ANY($1::BIGINT[])",
            discord_ids,
        )
    else:
        await get_pool().execute("DELETE FROM solo_queue")


# =============================================================================
# Solo Match Helpers (Server B)
# =============================================================================

async def create_solo_match(
    channel_id: int,
    captain1_id: int,
    captain2_id: int,
    available_player_ids: list[int],
    available_maps: list[str],
) -> Optional[dict]:
    """Create a new 10-man solo match record."""
    row = await get_pool().fetchrow(
        """
        INSERT INTO solo_matches (
            channel_id,
            status,
            captain1_id,
            captain2_id,
            team1_player_ids,
            team2_player_ids,
            available_player_ids,
            current_turn_captain_id,
            draft_step,
            available_maps,
            created_at
        )
        VALUES ($1, 'DRAFTING', $2, $3, ARRAY[$2]::BIGINT[], ARRAY[$3]::BIGINT[], $4::BIGINT[], $2, 1, $5::TEXT[], NOW())
        RETURNING *
        """,
        channel_id,
        captain1_id,
        captain2_id,
        available_player_ids,
        available_maps,
    )
    return dict(row) if row else None


async def update_solo_match_panel(match_id: int, panel_message_id: int) -> bool:
    """Save the panel message ID for the solo match embed."""
    res = await get_pool().execute(
        "UPDATE solo_matches SET panel_message_id = $1 WHERE id = $2",
        panel_message_id,
        match_id,
    )
    return not res.endswith(" 0")


async def get_solo_match_by_channel(channel_id: int) -> Optional[dict]:
    """Fetch solo match details by Discord channel ID."""
    row = await get_pool().fetchrow(
        "SELECT * FROM solo_matches WHERE channel_id = $1",
        channel_id,
    )
    return dict(row) if row else None


async def get_solo_match_by_id(match_id: int) -> Optional[dict]:
    """Fetch solo match details by match ID."""
    row = await get_pool().fetchrow(
        "SELECT * FROM solo_matches WHERE id = $1",
        match_id,
    )
    return dict(row) if row else None


async def update_solo_match_draft(
    match_id: int,
    team1_player_ids: list[int],
    team2_player_ids: list[int],
    available_player_ids: list[int],
    current_turn_captain_id: Optional[int],
    draft_step: int,
    status: str,
) -> Optional[dict]:
    """Update teams, remaining pool, draft turn, and status during player draft."""
    row = await get_pool().fetchrow(
        """
        UPDATE solo_matches
        SET team1_player_ids = $1::BIGINT[],
            team2_player_ids = $2::BIGINT[],
            available_player_ids = $3::BIGINT[],
            current_turn_captain_id = $4,
            draft_step = $5,
            status = $6
        WHERE id = $7
        RETURNING *
        """,
        team1_player_ids,
        team2_player_ids,
        available_player_ids,
        current_turn_captain_id,
        draft_step,
        status,
        match_id,
    )
    return dict(row) if row else None


async def update_solo_match_map_veto(
    match_id: int,
    available_maps: list[str],
    banned_maps: list[str],
    selected_map: Optional[str],
    current_turn_captain_id: Optional[int],
    status: str,
) -> Optional[dict]:
    """Update map veto state and final selected map."""
    row = await get_pool().fetchrow(
        """
        UPDATE solo_matches
        SET available_maps = $1::TEXT[],
            banned_maps = $2::TEXT[],
            selected_map = $3,
            current_turn_captain_id = $4,
            status = $5
        WHERE id = $6
        RETURNING *
        """,
        available_maps,
        banned_maps,
        selected_map,
        current_turn_captain_id,
        status,
        match_id,
    )
    return dict(row) if row else None


async def update_solo_match_voices(
    match_id: int,
    voice_team1_id: int,
    voice_team2_id: int,
) -> bool:
    """Save the created voice channel IDs for the match."""
    res = await get_pool().execute(
        "UPDATE solo_matches SET voice_team1_id = $1, voice_team2_id = $2 WHERE id = $3",
        voice_team1_id,
        voice_team2_id,
        match_id,
    )
    return not res.endswith(" 0")


async def cancel_solo_match(match_id: int) -> Optional[dict]:
    """Cancel a solo match."""
    row = await get_pool().fetchrow(
        """
        UPDATE solo_matches
        SET status = 'CANCELLED',
            completed_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        match_id,
    )
    return dict(row) if row else None


# =============================================================================
# Matchmaking Verification Helpers (Server B)
# =============================================================================

async def save_matchmaking_verification(
    orig_message_id: int,
    reply_message_id: int,
    channel_id: int,
    guild_id: int,
    player_id: int,
    player_name: str,
    ign: str,
    region: Optional[str] = None,
    status: str = "PENDING_REGION",
) -> dict:
    """Insert or update a persistent verification session."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO matchmaking_verifications (
            orig_message_id, reply_message_id, channel_id, guild_id,
            player_id, player_name, ign, region, status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (orig_message_id) DO UPDATE SET
            reply_message_id = EXCLUDED.reply_message_id,
            player_id        = EXCLUDED.player_id,
            player_name      = EXCLUDED.player_name,
            ign              = EXCLUDED.ign,
            region           = EXCLUDED.region,
            status           = EXCLUDED.status
        RETURNING *
        """,
        orig_message_id,
        reply_message_id,
        channel_id,
        guild_id,
        player_id,
        player_name,
        ign,
        region,
        status,
    )
    return dict(row)


async def get_matchmaking_verification(message_id: int) -> Optional[dict]:
    """Lookup pending verification by either screenshot message ID or bot reply message ID."""
    row = await get_pool().fetchrow(
        """
        SELECT * FROM matchmaking_verifications
        WHERE orig_message_id = $1 OR reply_message_id = $1
        LIMIT 1
        """,
        message_id,
    )
    return dict(row) if row else None


async def update_matchmaking_verification_ign(
    orig_message_id: int,
    ign: str,
    status: str = "PENDING_REGION",
) -> Optional[dict]:
    """Update the detected or edited IGN for a verification session."""
    row = await get_pool().fetchrow(
        """
        UPDATE matchmaking_verifications
        SET ign = $2, status = $3
        WHERE orig_message_id = $1
        RETURNING *
        """,
        orig_message_id,
        ign,
        status,
    )
    return dict(row) if row else None


async def update_matchmaking_verification_region(
    orig_message_id: int,
    region: str,
    status: str = "PENDING_APPROVAL",
) -> Optional[dict]:
    """Update the chosen region and transition status to pending moderator approval."""
    row = await get_pool().fetchrow(
        """
        UPDATE matchmaking_verifications
        SET region = $2, status = $3
        WHERE orig_message_id = $1
        RETURNING *
        """,
        orig_message_id,
        region,
        status,
    )
    return dict(row) if row else None


async def approve_matchmaking_verification_atomic(message_id: int) -> Optional[dict]:
    """
    Atomically transition verification from PENDING_APPROVAL to APPROVED.
    Guarantees only one moderator action succeeds across concurrent reactions.
    """
    row = await get_pool().fetchrow(
        """
        UPDATE matchmaking_verifications
        SET status = 'APPROVED'
        WHERE (orig_message_id = $1 OR reply_message_id = $1)
          AND status = 'PENDING_APPROVAL'
        RETURNING *
        """,
        message_id,
    )
    return dict(row) if row else None


async def delete_matchmaking_verification(orig_message_id: int) -> None:
    """Delete a verification session."""
    await get_pool().execute(
        "DELETE FROM matchmaking_verifications WHERE orig_message_id = $1",
        orig_message_id,
    )



