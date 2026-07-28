from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import discord

from config import GLOBAL_BOT_OWNER_ID
from storage import PostgresStore

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: discord.Client, store: PostgresStore) -> None:
        self.bot = bot
        self.store = store

    async def recipient_ids(self, guild: discord.Guild) -> set[int]:
        settings = await self.store.get_guild(guild.id)
        recipients = {GLOBAL_BOT_OWNER_ID, guild.owner_id}
        owner_id = settings.get("protection_owner_id")
        if owner_id:
            recipients.add(int(owner_id))
        recipients.update(int(value) for value in settings.get("admins", []))
        return recipients

    async def send_dms(
        self,
        guild: discord.Guild,
        *,
        content: str,
        attachment_paths: Iterable[str] = (),
    ) -> dict[int, str]:
        results: dict[int, str] = {}
        for user_id in await self.recipient_ids(guild):
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                files = [discord.File(path, filename=Path(path).name) for path in attachment_paths]
                await user.send(content=content, files=files)
                results[user_id] = "sent"
            except discord.Forbidden:
                results[user_id] = "dm_closed"
            except (discord.NotFound, discord.HTTPException, OSError) as exc:
                results[user_id] = f"failed: {exc}"
        return results

    async def send_alert_channel(
        self,
        guild: discord.Guild,
        *,
        content: str,
        attachment_paths: Iterable[str] = (),
    ) -> str:
        settings = await self.store.get_guild(guild.id)
        channel_id = settings.get("alert_channel_id") or settings.get("backup_channel_id")
        if not channel_id:
            return "not_configured"
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.abc.Messageable):
            return "channel_missing"
        try:
            files = [discord.File(path, filename=Path(path).name) for path in attachment_paths]
            await channel.send(content=content, files=files)
            return "sent"
        except (discord.Forbidden, discord.HTTPException, OSError) as exc:
            log.warning("Alert channel delivery failed in %s: %s", guild.id, exc)
            return f"failed: {exc}"

    async def broadcast(
        self,
        guild: discord.Guild,
        *,
        content: str,
        attachment_paths: Iterable[str] = (),
    ) -> tuple[dict[int, str], str]:
        dm_results = await self.send_dms(
            guild, content=content, attachment_paths=attachment_paths
        )
        channel_result = await self.send_alert_channel(
            guild, content=content, attachment_paths=attachment_paths
        )
        return dm_results, channel_result
