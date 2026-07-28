from __future__ import annotations

import asyncio
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

    @staticmethod
    def _close_files(files: list[discord.File]) -> None:
        for file in files:
            try:
                file.close()
            except Exception:
                pass

    async def _send_text_with_retry(
        self,
        destination: discord.abc.Messageable,
        *,
        content: str,
        attempts: int = 3,
    ) -> None:
        last_error: discord.HTTPException | None = None
        for attempt in range(1, attempts + 1):
            try:
                await destination.send(content=content)
                return
            except discord.Forbidden:
                raise
            except discord.HTTPException as exc:
                last_error = exc
                if attempt < attempts:
                    await asyncio.sleep(attempt)
        assert last_error is not None
        raise last_error

    async def _send_attachments(
        self,
        destination: discord.abc.Messageable,
        attachment_paths: tuple[str, ...],
    ) -> str:
        existing_paths = tuple(path for path in attachment_paths if Path(path).is_file())
        missing_paths = tuple(path for path in attachment_paths if not Path(path).is_file())

        if not existing_paths:
            if missing_paths:
                return "attachment_files_missing"
            return "no_attachments"

        files: list[discord.File] = []
        try:
            files = [discord.File(path, filename=Path(path).name) for path in existing_paths]
            await destination.send(content="Recovery log attachments:", files=files)
        except discord.Forbidden:
            return "attachment_forbidden"
        except (discord.HTTPException, OSError) as exc:
            return f"attachment_failed: {type(exc).__name__}: {exc}"
        finally:
            self._close_files(files)

        return "attachments_sent" if not missing_paths else "attachments_sent_some_missing"

    async def send_dms(
        self,
        guild: discord.Guild,
        *,
        content: str,
        attachment_paths: Iterable[str] = (),
    ) -> dict[int, str]:
        results: dict[int, str] = {}
        paths = tuple(attachment_paths)

        for user_id in await self.recipient_ids(guild):
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            except discord.NotFound:
                results[user_id] = "user_not_found"
                log.warning("Notification recipient %s was not found for guild %s", user_id, guild.id)
                continue
            except discord.HTTPException as exc:
                results[user_id] = f"user_fetch_failed: {exc}"
                log.warning(
                    "Could not fetch notification recipient %s for guild %s: %s",
                    user_id,
                    guild.id,
                    exc,
                )
                continue

            try:
                # Send the urgent alarm text first. Attachment trouble must never
                # prevent the containment notification itself from arriving.
                await self._send_text_with_retry(user, content=content)
            except discord.Forbidden:
                results[user_id] = "dm_forbidden_or_closed"
                log.warning(
                    "DM blocked/closed for notification recipient %s in guild %s",
                    user_id,
                    guild.id,
                )
                continue
            except discord.HTTPException as exc:
                results[user_id] = f"dm_failed: {exc}"
                log.warning(
                    "DM delivery failed for notification recipient %s in guild %s: %s",
                    user_id,
                    guild.id,
                    exc,
                )
                continue

            attachment_result = await self._send_attachments(user, paths)
            results[user_id] = f"message_sent; {attachment_result}"
            log.info(
                "Notification result for user %s in guild %s: %s",
                user_id,
                guild.id,
                results[user_id],
            )

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
            await self._send_text_with_retry(channel, content=content)
        except discord.Forbidden:
            log.warning("Alert channel is forbidden in guild %s", guild.id)
            return "message_forbidden"
        except discord.HTTPException as exc:
            log.warning("Alert channel text delivery failed in %s: %s", guild.id, exc)
            return f"message_failed: {exc}"

        attachment_result = await self._send_attachments(channel, tuple(attachment_paths))
        result = f"message_sent; {attachment_result}"
        log.info("Alert channel result for guild %s: %s", guild.id, result)
        return result

    async def broadcast(
        self,
        guild: discord.Guild,
        *,
        content: str,
        attachment_paths: Iterable[str] = (),
    ) -> tuple[dict[int, str], str]:
        # Materialize once because callers sometimes provide generators.
        paths = tuple(attachment_paths)
        dm_results = await self.send_dms(guild, content=content, attachment_paths=paths)
        channel_result = await self.send_alert_channel(
            guild, content=content, attachment_paths=paths
        )
        return dm_results, channel_result
