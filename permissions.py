from __future__ import annotations

from typing import Any

import discord

from config import GLOBAL_BOT_OWNER_ID
from storage import PostgresStore, utc_iso


async def is_global_owner(user_id: int) -> bool:
    return user_id == GLOBAL_BOT_OWNER_ID


async def is_server_owner(guild: discord.Guild, user_id: int) -> bool:
    return user_id == guild.owner_id


async def is_protection_owner(store: PostgresStore, guild_id: int, user_id: int) -> bool:
    settings = await store.get_guild(guild_id)
    return settings.get("protection_owner_id") == user_id


async def is_configured_admin(store: PostgresStore, guild_id: int, user_id: int) -> bool:
    settings = await store.get_guild(guild_id)
    return user_id in {int(value) for value in settings.get("admins", [])}


async def can_use_commands(store: PostgresStore, guild: discord.Guild, user_id: int) -> bool:
    if user_id == GLOBAL_BOT_OWNER_ID:
        return True
    settings = await store.get_guild(guild.id)
    return user_id == settings.get("protection_owner_id") or user_id in {
        int(value) for value in settings.get("admins", [])
    }


async def can_set_owner(guild: discord.Guild, user_id: int) -> bool:
    return user_id in {GLOBAL_BOT_OWNER_ID, guild.owner_id}


async def can_set_admin(store: PostgresStore, guild: discord.Guild, user_id: int) -> bool:
    if user_id in {GLOBAL_BOT_OWNER_ID, guild.owner_id}:
        return True
    settings = await store.get_guild(guild.id)
    return user_id == settings.get("protection_owner_id")


async def is_exempt_actor(store: PostgresStore, guild: discord.Guild, user_id: int) -> bool:
    """Configured protection operators are exempt, per the requested design."""
    if user_id in {GLOBAL_BOT_OWNER_ID, guild.owner_id}:
        return True
    settings = await store.get_guild(guild.id)
    if user_id == settings.get("protection_owner_id"):
        return True
    return user_id in {int(value) for value in settings.get("admins", [])}


async def record_config_event(
    store: PostgresStore,
    *,
    guild_id: int,
    actor_id: int,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    await store.append_configuration_event(
        {
            "guild_id": guild_id,
            "actor_id": actor_id,
            "action": action,
            "details": details or {},
            "created_at": utc_iso(),
        }
    )
