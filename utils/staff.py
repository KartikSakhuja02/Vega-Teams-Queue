"""
utils/staff.py
--------------
Centralized staff authorization helper for Vega Queue Bot.
Validates staff privileges across Administrators, Moderators, and Faceit Police
using role IDs configured in .env, Discord permissions, and role names.
"""

from __future__ import annotations

import os
from typing import Optional, Set
import discord

STAFF_ROLE_NAMES: Set[str] = {
    "moderator",
    "mod",
    "mods",
    "faceit police",
    "faceit-police",
    "faceitpolice",
    "admin",
    "administrator",
}


def get_staff_role_ids() -> set[int]:
    """
    Extract all staff role IDs from environment variables:
      - MODERATOR_ROLE_IDS / MODERATOR_ROLE_ID / MOD_ROLE_IDS / MOD_ROLE_ID
      - FACEIT_POLICE_ROLE_IDS / FACEIT_POLICE_ROLE_ID
      - ADMIN_ROLE_IDS / ADMIN_ROLE_ID / HELP_ADMIN_ROLE_IDS / TEAM_MOD_ROLE_IDS
    Supports single integers or comma-separated lists.
    """
    keys = (
        "MODERATOR_ROLE_IDS",
        "MODERATOR_ROLE_ID",
        "MOD_ROLE_IDS",
        "MOD_ROLE_ID",
        "FACEIT_POLICE_ROLE_IDS",
        "FACEIT_POLICE_ROLE_ID",
        "ADMIN_ROLE_IDS",
        "ADMIN_ROLE_ID",
        "HELP_ADMIN_ROLE_IDS",
        "TEAM_MOD_ROLE_IDS",
    )
    role_ids: set[int] = set()
    for key in keys:
        val = os.environ.get(key, "").strip()
        if not val:
            continue
        for chunk in val.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                role_ids.add(int(chunk))
            except ValueError:
                pass
    return role_ids


def is_staff(member: Optional[discord.Member | discord.User]) -> bool:
    """
    Check if a guild member has staff/administrative permissions.
    Returns True if:
      1. Member has Discord Administrator, Manage Server, or Manage Channels permissions.
      2. Member has any role ID configured in .env (Moderator, Faceit Police, Admin).
      3. Member has a role matching common staff names (Moderator, Faceit Police, Admin).
    """
    if not member or not isinstance(member, discord.Member):
        return False

    # 1. Native guild permissions
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild or perms.manage_channels:
        return True

    # 2. Configured role IDs from .env
    staff_ids = get_staff_role_ids()
    if any(role.id in staff_ids for role in member.roles):
        return True

    # 3. Role name fallback (case-insensitive)
    return any(role.name.strip().lower() in STAFF_ROLE_NAMES for role in member.roles)


# Backward-compatible alias
_is_admin = is_staff
is_admin = is_staff
