"""
utils/staff.py
--------------
Centralized staff authorization helper for Vega Queue Bot.
Validates staff privileges across Administrators, Moderators, and Faceit Police
using role IDs configured in .env, Discord permissions, and role names.
"""

from __future__ import annotations

import os
import re
from typing import Optional, Set
import discord
from dotenv import load_dotenv

# Ensure environment variables are loaded
load_dotenv()

# Recognized role names (all lowercased, stripped)
STAFF_ROLE_NAMES: Set[str] = {
    # Face it Police / Faceit Police variations
    "face it police",
    "face it",
    "faceit police",
    "face-it police",
    "face-it-police",
    "face_it_police",
    "faceit-police",
    "faceit_police",
    "faceitpolice",
    "faceit",
    "face it 👮",
    "face it police 👮",
    "👮 face it police",
    "police",
    # Moderator variations
    "moderator",
    "moderators",
    "mod",
    "mods",
    "lead moderator",
    "head moderator",
    "senior moderator",
    "server moderator",
    "chat moderator",
    "queue moderator",
    "staff moderator",
    # Admin & Staff variations
    "admin",
    "administrator",
    "administrators",
    "admins",
    "head admin",
    "senior admin",
    "staff",
    "server staff",
}

ENV_STAFF_KEYS = (
    # Face it Police / Faceit Police
    "FACEIT_POLICE_ROLE_IDS",
    "FACEIT_POLICE_ROLE_ID",
    "FACE_IT_POLICE_ROLE_IDS",
    "FACE_IT_POLICE_ROLE_ID",
    "FACEIT_ROLE_IDS",
    "FACEIT_ROLE_ID",
    "FACE_IT_ROLE_IDS",
    "FACE_IT_ROLE_ID",
    "FACEIT_POLICE_ROLES",
    "FACE_IT_POLICE_ROLES",
    "FACEIT_POLICE",
    "FACE_IT_POLICE",
    "FACEIT",
    # Moderator
    "MODERATOR_ROLE_IDS",
    "MODERATOR_ROLE_ID",
    "MODERATORS_ROLE_IDS",
    "MOD_ROLE_IDS",
    "MOD_ROLE_ID",
    "MOD_ROLES",
    "MODERATOR_ROLES",
    "MODERATOR",
    "MODERATORS",
    "MOD",
    "MODS",
    # Admin / Staff
    "ADMIN_ROLE_IDS",
    "ADMIN_ROLE_ID",
    "ADMINS_ROLE_IDS",
    "ADMIN_ROLES",
    "ADMIN",
    "ADMINS",
    "STAFF_ROLE_IDS",
    "STAFF_ROLE_ID",
    "STAFF_ROLES",
    "STAFF",
    "HELP_ADMIN_ROLE_IDS",
    "TEAM_MOD_ROLE_IDS",
)


def get_staff_role_ids() -> set[int]:
    """
    Extract all staff role IDs from environment variables.
    Handles comma-separated integers, quotes, and Discord role mentions (<@&123456789>).
    """
    role_ids: set[int] = set()
    for key in ENV_STAFF_KEYS:
        val = os.environ.get(key, "").strip()
        if not val:
            continue
        for chunk in val.split(","):
            chunk = chunk.strip().strip("'\"")
            if not chunk:
                continue
            # Extract raw digits (handles role mentions like <@&123456789012345678>)
            digits = re.sub(r"\D", "", chunk)
            if len(digits) >= 15:
                try:
                    role_ids.add(int(digits))
                    continue
                except ValueError:
                    pass
            try:
                role_ids.add(int(chunk))
            except ValueError:
                pass
    return role_ids


def get_custom_staff_role_names() -> set[str]:
    """
    Extract any custom role names specified directly as text strings in .env.
    For example: FACEIT_POLICE_ROLE_IDS=Face it Police
    """
    custom_names: set[str] = set()
    for key in ENV_STAFF_KEYS:
        val = os.environ.get(key, "").strip()
        if not val:
            continue
        for chunk in val.split(","):
            chunk = chunk.strip().strip("'\"")
            if not chunk:
                continue
            digits = re.sub(r"\D", "", chunk)
            if len(digits) >= 15:
                continue
            try:
                int(chunk)
                continue
            except ValueError:
                pass
            custom_names.add(chunk.lower())
    return custom_names


def _matches_staff_role(role_name: str) -> bool:
    """Check if a Discord role name corresponds to a staff role (case/symbol insensitive)."""
    if not role_name:
        return False

    raw_lower = role_name.strip().lower()
    if raw_lower in STAFF_ROLE_NAMES:
        return True

    custom_names = get_custom_staff_role_names()
    if raw_lower in custom_names:
        return True

    # Normalize by keeping only alphanumeric and whitespace characters
    clean = "".join(c.lower() for c in role_name if c.isalnum() or c.isspace()).strip()
    if clean in STAFF_ROLE_NAMES or clean in custom_names:
        return True

    words = set(clean.split())
    squashed = clean.replace(" ", "")

    # Face it Police / Faceit Police checks:
    # 1. "face" and "police" words both present (e.g. "Face it Police", "Face Police")
    if "face" in words and "police" in words:
        return True
    # 2. "faceit" and "police" words both present
    if "faceit" in words and "police" in words:
        return True
    # 3. "faceitpolice" anywhere in squashed name
    if "faceitpolice" in squashed:
        return True
    # 4. "faceit" alone if the server has a role called "Faceit"
    if "faceit" in words or squashed == "faceit":
        return True

    # Moderator checks:
    if any(w in words for w in ("moderator", "moderators", "mod", "mods")):
        return True
    if any(m in squashed for m in ("moderator", "moderators")):
        return True

    # Admin checks:
    if any(w in words for w in ("admin", "administrator", "administrators", "admins")):
        return True
    if any(a in squashed for a in ("administrator", "administrators")):
        return True

    # Generic staff role
    if "staff" in words:
        return True

    return False


def is_staff(
    target: Optional[discord.Member | discord.User | discord.Interaction],
    guild: Optional[discord.Guild] = None,
) -> bool:
    """
    Check if an interaction, member, or user has staff/administrative permissions.
    Returns True if:
      1. Member is Server Owner or has Administrator/Manage Server/Manage Channels perms.
      2. Member has any role ID configured in .env (Moderator, Faceit Police, Admin).
      3. Member has a role matching staff names (Moderator, Face it Police, Admin).
    """
    if target is None:
        return False

    member: Optional[discord.Member | discord.User] = None

    # Handle discord.Interaction input
    if isinstance(target, discord.Interaction):
        guild = target.guild or guild
        user = target.user
        if isinstance(user, discord.Member):
            member = user
        elif guild is not None and user is not None:
            member = guild.get_member(user.id) or user
        else:
            member = user

        # Check interaction permissions directly if available
        int_perms = getattr(target, "permissions", None)
        if int_perms:
            if (
                getattr(int_perms, "administrator", False)
                or getattr(int_perms, "manage_guild", False)
                or getattr(int_perms, "manage_channels", False)
            ):
                return True
    elif isinstance(target, discord.Member):
        member = target
        guild = member.guild or guild
    elif isinstance(target, discord.User):
        if guild is not None:
            member = guild.get_member(target.id) or target
        else:
            member = target
    else:
        member = getattr(target, "user", None) or target

    if member is None:
        return False

    # 1. Server Owner is always staff
    if guild and getattr(guild, "owner_id", None) == getattr(member, "id", None):
        return True

    # 2. Native Discord member permissions
    perms = getattr(member, "guild_permissions", None)
    if perms:
        if (
            getattr(perms, "administrator", False)
            or getattr(perms, "manage_guild", False)
            or getattr(perms, "manage_channels", False)
        ):
            return True

    # 3. Configured role IDs from .env
    staff_ids = get_staff_role_ids()
    roles = getattr(member, "roles", [])
    if any(role.id in staff_ids for role in roles):
        return True

    # 4. Role name matching (Face it Police, Moderator, Admin, Mod, etc.)
    if any(_matches_staff_role(role.name) for role in roles):
        return True

    return False


# Backward-compatible aliases
_is_admin = is_staff
is_admin = is_staff
