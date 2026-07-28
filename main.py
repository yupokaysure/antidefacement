from __future__ import annotations

import logging
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from backup_service import BackupService
from commands import AntiDefacementCommands
from config import DATABASE_URL, DEV_GUILD_ID, DISCORD_TOKEN, GLOBAL_BOT_OWNER_ID
from notifier import Notifier
from protection import ProtectionCog
from restore_service import RestoreService
from storage import PostgresStore


class AntiDefacementBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.moderation = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        self.store = PostgresStore(DATABASE_URL)
        self.notifier = Notifier(self, self.store)
        self.backup_service = BackupService(self, self.store)
        self.restore_service = RestoreService(self.store, self.backup_service)

    async def setup_hook(self) -> None:
        await self.store.connect()
        await self.store.run_migrations()
        await self.store.cleanup_processed_audit_entries()
        await self.add_cog(ProtectionCog(self, self.store, self.notifier))
        await self.add_cog(
            AntiDefacementCommands(
                self,
                self.store,
                self.backup_service,
                self.restore_service,
                self.notifier,
            )
        )

        # Global commands make the same /antidefacement group available in every server.
        await self.tree.sync()
        if DEV_GUILD_ID:
            dev_guild = discord.Object(id=DEV_GUILD_ID)
            self.tree.copy_global_to(guild=dev_guild)
            await self.tree.sync(guild=dev_guild)

    async def close(self) -> None:
        await self.store.close()
        await super().close()

    async def on_ready(self) -> None:
        if self.user is None:
            return
        logging.getLogger(__name__).info(
            "Logged in as %s (%s) in %s guild(s)", self.user, self.user.id, len(self.guilds)
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.store.ensure_guild(guild.id)
        try:
            owner = self.get_user(GLOBAL_BOT_OWNER_ID) or await self.fetch_user(GLOBAL_BOT_OWNER_ID)
            await owner.send(
                f"Anti-Defacement Bot joined **{guild.name}** (`{guild.id}`). "
                "Protection starts inactive until an authorized user runs `/antidefacement activate`."
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        try:
            owner = self.get_user(GLOBAL_BOT_OWNER_ID) or await self.fetch_user(GLOBAL_BOT_OWNER_ID)
            await owner.send(
                f"Anti-Defacement Bot was removed from or lost access to **{guild.name}** (`{guild.id}`)."
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Add it as a Railway environment variable.")
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is missing. Add PostgreSQL to Railway and reference its DATABASE_URL."
        )
    bot = AntiDefacementBot()
    bot.run(DISCORD_TOKEN, log_handler=None)
