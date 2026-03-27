"""
utils/permissions.py
=====================
Helper functions that check whether a member has the event-creator role
or is a server administrator.
"""

import discord
from utils.database import get_guild_config


def is_event_creator(member: discord.Member) -> bool:
    """
    Return True if the member has:
      - Server Administrator permission, OR
      - The designated event-creator role for their guild.
    """
    # Admins always have full access
    if member.guild_permissions.administrator:
        return True

    config = get_guild_config(member.guild.id)
    if not config:
        return False  # Bot hasn't been set up yet

    creator_role_id = config.get("creator_role_id")
    if not creator_role_id:
        return False

    # Check if the member holds the event creator role
    return any(role.id == creator_role_id for role in member.roles)


def check_setup(guild_id: int) -> bool:
    """Return True if the guild has completed /setup (has a config row)."""
    config = get_guild_config(guild_id)
    return config is not None and config.get("creator_role_id") is not None
