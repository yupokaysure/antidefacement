from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from backup_service import BackupService
from config import (
    DEFAULT_TIMEZONE,
    GLOBAL_BOT_OWNER_ID,
    MAX_RECENT_ALARMS_SHOWN,
    SCHEDULER_INTERVAL_SECONDS,
    THRESHOLDS,
)
from notifier import Notifier
from permissions import (
    can_set_admin,
    can_set_owner,
    can_use_commands,
    record_config_event,
)
from protection import ProtectionCog
from restore_service import RestoreService
from storage import PostgresStore, utc_iso

log = logging.getLogger(__name__)

WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class DeactivateConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: "AntiDefacementCommands",
        *,
        guild_id: int,
        requested_by: int,
        reason: str,
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.requested_by = requested_by
        self.reason = reason
        self.completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requested_by:
            await interaction.response.send_message(
                "Only the person who requested deactivation can confirm it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm deactivation", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message("This confirmation is no longer valid.", ephemeral=True)
            return
        if not await self.cog._authorized(interaction):
            return
        await self.cog.store.update_guild(guild.id, active=False)
        await record_config_event(
            self.cog.store,
            guild_id=guild.id,
            actor_id=interaction.user.id,
            action="deactivate",
            details={"reason": self.reason},
        )
        content = (
            f"Anti-Defacement protection was **deactivated** in **{guild.name}** "
            f"by <@{interaction.user.id}> (`{interaction.user.id}`).\n"
            f"Reason: {self.reason}"
        )
        await self.cog.notifier.broadcast(guild, content=content)
        self.completed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Protection has been deactivated.", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Deactivation cancelled.", view=self)
        self.stop()


@app_commands.guild_only()
class AntiDefacementCommands(commands.GroupCog, group_name="antidefacement"):
    """All Anti-Defacement slash commands."""

    def __init__(
        self,
        bot: commands.Bot,
        store: PostgresStore,
        backup_service: BackupService,
        restore_service: RestoreService,
        notifier: Notifier,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.store = store
        self.backup_service = backup_service
        self.restore_service = restore_service
        self.notifier = notifier
        self.backup_scheduler.start()

    def cog_unload(self) -> None:
        self.backup_scheduler.cancel()

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is None:
            if not interaction.response.is_done():
                await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return False
        if await can_use_commands(self.store, guild, interaction.user.id):
            return True
        message = (
            "You are not authorized to use Anti-Defacement commands. Discord server "
            "Administrator permission alone does not grant access."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    async def _owner_or_global(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild and await can_set_owner(guild, interaction.user.id):
            return True
        await interaction.response.send_message(
            "Only the actual server owner or the global bot owner can set the protection owner.",
            ephemeral=True,
        )
        return False

    async def _can_manage_admins(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild and await can_set_admin(self.store, guild, interaction.user.id):
            return True
        await interaction.response.send_message(
            "Only the protection owner, actual server owner, or global bot owner can manage admins.",
            ephemeral=True,
        )
        return False

    @app_commands.command(name="setowner", description="Set this server's Anti-Defacement owner.")
    async def setowner(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._owner_or_global(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await self.store.update_guild(guild.id, protection_owner_id=member.id)
        await record_config_event(
            self.store,
            guild_id=guild.id,
            actor_id=interaction.user.id,
            action="set_owner",
            details={"new_owner_id": member.id},
        )
        await interaction.response.send_message(
            f"{member.mention} is now the Anti-Defacement owner for this server.", ephemeral=True
        )
        await self.notifier.broadcast(
            guild,
            content=(
                f"The Anti-Defacement owner for **{guild.name}** was set to "
                f"{member.mention} (`{member.id}`) by <@{interaction.user.id}>."
            ),
        )

    @app_commands.command(name="setadmin", description="Add an exempt Anti-Defacement administrator.")
    async def setadmin(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._can_manage_admins(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        added = await self.store.add_admin(
            guild.id, member.id, added_by=interaction.user.id, is_exempt=True
        )
        await record_config_event(
            self.store,
            guild_id=guild.id,
            actor_id=interaction.user.id,
            action="add_admin",
            details={"admin_id": member.id, "exempt": True},
        )
        await interaction.response.send_message(
            (
                f"{member.mention} was added as an Anti-Defacement administrator and is exempt "
                "from automatic role removal."
                if added
                else f"{member.mention} is already an Anti-Defacement administrator."
            ),
            ephemeral=True,
        )
        if added:
            await self.notifier.broadcast(
                guild,
                content=(
                    f"{member.mention} (`{member.id}`) was added as an exempt Anti-Defacement "
                    f"administrator in **{guild.name}** by <@{interaction.user.id}>."
                ),
            )

    @app_commands.command(name="removeadmin", description="Remove an Anti-Defacement administrator.")
    async def removeadmin(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._can_manage_admins(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        removed = await self.store.remove_admin(guild.id, member.id)
        await record_config_event(
            self.store,
            guild_id=guild.id,
            actor_id=interaction.user.id,
            action="remove_admin",
            details={"admin_id": member.id},
        )
        await interaction.response.send_message(
            f"{member.mention} was removed." if removed else f"{member.mention} was not configured.",
            ephemeral=True,
        )
        if removed:
            await self.notifier.broadcast(
                guild,
                content=(
                    f"{member.mention} (`{member.id}`) was removed as an Anti-Defacement "
                    f"administrator in **{guild.name}** by <@{interaction.user.id}>."
                ),
            )

    @app_commands.command(name="listadmins", description="List configured Anti-Defacement administrators.")
    async def listadmins(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        settings = await self.store.get_guild(guild.id)
        admins = settings.get("admins", [])
        lines = [f"• <@{user_id}> (`{user_id}`) — exempt" for user_id in admins]
        await interaction.response.send_message(
            "**Configured administrators**\n" + ("\n".join(lines) if lines else "None"),
            ephemeral=True,
        )

    @app_commands.command(name="owner", description="Show the configured Anti-Defacement owner.")
    async def owner(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        settings = await self.store.get_guild(guild.id)
        owner_id = settings.get("protection_owner_id")
        await interaction.response.send_message(
            f"Protection owner: <@{owner_id}> (`{owner_id}`)" if owner_id else "No protection owner is set.",
            ephemeral=True,
        )

    @app_commands.command(name="activate", description="Activate persistent Anti-Defacement protection.")
    async def activate(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        protection = self.bot.get_cog("ProtectionCog")
        problems = protection.permission_problems(guild) if isinstance(protection, ProtectionCog) else []
        if problems:
            await interaction.response.send_message(
                "Protection was not activated because setup is unsafe:\n"
                + "\n".join(f"• {problem}" for problem in problems),
                ephemeral=True,
            )
            return
        await self.store.update_guild(guild.id, active=True)
        await record_config_event(
            self.store, guild_id=guild.id, actor_id=interaction.user.id, action="activate"
        )
        await interaction.response.send_message(
            "Anti-Defacement protection is active and will remain active after restarts.", ephemeral=True
        )
        await self.notifier.broadcast(
            guild,
            content=f"Anti-Defacement protection was **activated** in **{guild.name}** by <@{interaction.user.id}>.",
        )

    @app_commands.command(name="deactivate", description="Request persistent deactivation.")
    async def deactivate(self, interaction: discord.Interaction, reason: str) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        view = DeactivateConfirmView(
            self, guild_id=guild.id, requested_by=interaction.user.id, reason=reason
        )
        await interaction.response.send_message(
            "Confirm that you want to disable protection. It will remain disabled through restarts "
            "until `/antidefacement activate` is run.",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="falsealarm", description="Restore roles removed in a specific alarm.")
    async def falsealarm(self, interaction: discord.Interaction, alarm_id: str) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        alarm_id = alarm_id.strip().upper()
        alarm = await self.store.get_alarm(alarm_id)
        if not alarm or int(alarm.get("guild_id", 0)) != guild.id:
            await interaction.response.send_message("Alarm not found for this server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = guild.get_member(int(alarm["offender_id"]))
        if member is None:
            await interaction.followup.send(
                "The offender is no longer in the server, so their roles cannot be restored.", ephemeral=True
            )
            return
        if guild.me is None:
            await interaction.followup.send("Bot member information is unavailable.", ephemeral=True)
            return

        restored: list[str] = []
        failures: list[str] = []
        roles: list[discord.Role] = []
        for data in alarm.get("removed_roles", []):
            role = guild.get_role(int(data["id"]))
            if role is None:
                failures.append(f"Missing role: {data.get('name')} ({data.get('id')})")
            elif role.managed or role >= guild.me.top_role:
                failures.append(f"Unmanageable role: {role.name} ({role.id})")
            elif role not in member.roles:
                roles.append(role)
        if roles:
            try:
                await member.add_roles(
                    *roles,
                    reason=f"False alarm restoration {alarm_id}",
                    atomic=False,
                )
                restored.extend(f"{role.name} ({role.id})" for role in roles)
            except (discord.Forbidden, discord.HTTPException) as exc:
                failures.append(f"Role restoration request failed: {exc}")

        history = list(alarm.get("resolution_history", []))
        history.append(
            {
                "action": "false_alarm",
                "by": interaction.user.id,
                "at": utc_iso(),
                "restored": restored,
                "failures": failures,
            }
        )
        await self.store.update_alarm(
            alarm_id,
            status="false_alarm",
            false_alarm_by=interaction.user.id,
            false_alarm_at=utc_iso(),
            resolution_history=history,
        )
        text = (
            f"Alarm **{alarm_id}** was marked as a false alarm. Restored {len(restored)} role(s)."
        )
        if failures:
            text += "\nFailures:\n" + "\n".join(f"• {item}" for item in failures)
        await interaction.followup.send(text, ephemeral=True)
        await self.notifier.broadcast(
            guild,
            content=(
                f"Alarm **{alarm_id}** was marked as a false alarm by <@{interaction.user.id}>. "
                f"Restored {len(restored)} role(s) to <@{member.id}>."
            ),
        )

    @app_commands.command(name="setbackupchannel", description="Set the channel that receives backups.")
    async def setbackupchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await self.store.update_guild(guild.id, backup_channel_id=channel.id)
        await record_config_event(
            self.store,
            guild_id=guild.id,
            actor_id=interaction.user.id,
            action="set_backup_channel",
            details={"channel_id": channel.id},
        )
        await interaction.response.send_message(
            f"Backups will be delivered to {channel.mention}.", ephemeral=True
        )
        await self.notifier.broadcast(
            guild,
            content=f"The backup channel was changed to {channel.mention} by <@{interaction.user.id}>.",
        )

    @app_commands.command(name="setalertchannel", description="Set the fallback security alert channel.")
    async def setalertchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await self.store.update_guild(guild.id, alert_channel_id=channel.id)
        await record_config_event(
            self.store,
            guild_id=guild.id,
            actor_id=interaction.user.id,
            action="set_alert_channel",
            details={"channel_id": channel.id},
        )
        await interaction.response.send_message(
            f"Security alerts will also be posted to {channel.mention}.", ephemeral=True
        )
        await self.notifier.broadcast(
            guild,
            content=f"The security alert channel was changed to {channel.mention} by <@{interaction.user.id}>.",
        )

    @app_commands.command(name="backup", description="Create a server backup now.")
    async def backup(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.backup_service.create_backup(
                guild, initiated_by=interaction.user.id, backup_type="manual", deliver=True
            )
            message = (
                f"Backup **{result['backup_id']}** completed and was saved to PostgreSQL. "
                f"Delivery status: `{result.get('delivery_status')}`."
            )
            paths: list[str] = []
            try:
                files = []
                if result.get("delivery_status") != "sent":
                    paths = await self.backup_service.export_backup_files(result)
                    files = [discord.File(value) for value in paths]
                await interaction.followup.send(message, files=files, ephemeral=True)
            finally:
                await self.backup_service.cleanup_export_files(paths)
        except Exception as exc:
            await interaction.followup.send(f"Backup failed: {exc}", ephemeral=True)

    @app_commands.command(name="schedulebackup", description="Schedule daily, weekly, or monthly backups.")
    async def schedulebackup(
        self,
        interaction: discord.Interaction,
        frequency: Literal["daily", "weekly", "monthly"],
        hour: app_commands.Range[int, 0, 23] = 3,
        minute: app_commands.Range[int, 0, 59] = 0,
        timezone_name: str = DEFAULT_TIMEZONE,
        weekday: str | None = None,
        day_of_month: int | None = None,
    ) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            await interaction.response.send_message("That timezone name is invalid.", ephemeral=True)
            return
        weekday_number = 0
        if frequency == "weekly":
            weekday_key = (weekday or "monday").strip().lower()
            if weekday_key not in WEEKDAY_NAMES:
                await interaction.response.send_message(
                    "For weekly backups, weekday must be Monday through Sunday.", ephemeral=True
                )
                return
            weekday_number = WEEKDAY_NAMES[weekday_key]
        if day_of_month is not None and not 1 <= day_of_month <= 28:
            await interaction.response.send_message(
                "day_of_month must be between 1 and 28.", ephemeral=True
            )
            return
        schedule = {
            "frequency": frequency,
            "hour": int(hour),
            "minute": int(minute),
            "timezone": timezone_name,
            "weekday": weekday_number,
            "day_of_month": int(day_of_month or 1),
        }
        schedule["next_run_at"] = self.backup_service.compute_next_run(schedule).isoformat()
        await self.store.update_guild(guild.id, backup_schedule=schedule)
        await record_config_event(
            self.store,
            guild_id=guild.id,
            actor_id=interaction.user.id,
            action="schedule_backup",
            details=schedule,
        )
        await interaction.response.send_message(
            f"Backups scheduled `{frequency}`. Next run: `{schedule['next_run_at']}`.", ephemeral=True
        )
        await self.notifier.broadcast(
            guild,
            content=(
                f"The backup schedule in **{guild.name}** was changed by <@{interaction.user.id}>. "
                f"Frequency: `{frequency}`; next run: `{schedule['next_run_at']}`."
            ),
        )

    @app_commands.command(name="cancelschedule", description="Cancel scheduled backups.")
    async def cancelschedule(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await self.store.update_guild(guild.id, backup_schedule=None)
        await record_config_event(
            self.store,
            guild_id=guild.id,
            actor_id=interaction.user.id,
            action="cancel_backup_schedule",
        )
        await interaction.response.send_message("Scheduled backups were cancelled.", ephemeral=True)
        await self.notifier.broadcast(
            guild,
            content=f"Scheduled backups were cancelled in **{guild.name}** by <@{interaction.user.id}>.",
        )

    @app_commands.command(name="restore", description="Preview or execute restoration from a backup.")
    async def restore(
        self,
        interaction: discord.Interaction,
        mode: Literal["preview", "execute"] = "preview",
        backup_id: str | None = None,
    ) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        backup = (
            await self.store.get_backup(backup_id.strip().upper())
            if backup_id
            else await self.store.latest_backup(guild.id)
        )
        if not backup or int(backup.get("guild_id", 0)) != guild.id or backup.get("status") != "complete":
            await interaction.followup.send("No usable backup was found.", ephemeral=True)
            return
        if mode == "preview":
            result = await self.restore_service.preview(guild, backup)
            await interaction.followup.send(
                f"**Restore preview for {backup['backup_id']}**\n"
                f"Roles to recreate: **{result['missing_role_count']}**\n"
                f"Channels to recreate: **{result['missing_channel_count']}**\n"
                f"Member-role assignments to attempt: **{result['member_role_assignments']}**\n"
                f"Backup members no longer present: **{result['members_not_in_server']}**\n\n"
                "Run the same command with `mode:execute` to perform the restoration.",
                ephemeral=True,
            )
            return
        result = await self.restore_service.execute(
            guild, backup, started_by=interaction.user.id
        )
        await interaction.followup.send(self._restore_result_text(result), ephemeral=True)
        await self.notifier.broadcast(
            guild,
            content=(
                f"Restore job **{result['restore_id']}** from backup **{backup['backup_id']}** "
                f"was started by <@{interaction.user.id}> and finished with status `{result['status']}`."
            ),
        )

    @app_commands.command(name="restorealarm", description="Restore channels and roles deleted in an alarm.")
    async def restorealarm(
        self,
        interaction: discord.Interaction,
        alarm_id: str,
        mode: Literal["preview", "execute"] = "preview",
    ) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        alarm_id = alarm_id.strip().upper()
        alarm = await self.store.get_alarm(alarm_id)
        if not alarm or int(alarm.get("guild_id", 0)) != guild.id:
            await interaction.response.send_message("Alarm not found for this server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        roles = []
        channels = []
        member_roles: dict[int, list[int]] = {}
        for event in alarm.get("evidence", []):
            target = event.get("target") or {}
            if event.get("action_type") == "role_delete" and target.get("id"):
                roles.append(target)
                for member_id in target.get("member_ids", []):
                    member_roles.setdefault(int(member_id), []).append(int(target["id"]))
            elif event.get("action_type") == "channel_delete" and target.get("id"):
                channels.append(target)

        snapshot = {
            "schema_version": 1,
            "guild": {
                "id": guild.id,
                "name": guild.name,
                "owner_id": guild.owner_id,
                "backed_up_at": alarm.get("triggered_at"),
            },
            "roles": roles,
            "channels": channels,
            "members": [
                {"id": member_id, "role_ids": role_ids, "role_names": []}
                for member_id, role_ids in member_roles.items()
            ],
        }
        backup_id = f"AR-{secrets.randbelow(10**12):012d}"
        backup = {
            "backup_id": backup_id,
            "guild_id": guild.id,
            "guild_name": guild.name,
            "created_at": utc_iso(),
            "created_by": interaction.user.id,
            "backup_type": "alarm_recovery",
            "source_alarm_id": alarm_id,
            "status": "complete",
            "snapshot": snapshot,
            "delivery_status": "not_applicable",
        }
        await self.store.add_backup(backup)
        if mode == "preview":
            preview = await self.restore_service.preview(guild, backup)
            await interaction.followup.send(
                f"**Alarm restore preview for {alarm_id}**\n"
                f"Roles to recreate: **{preview['missing_role_count']}**\n"
                f"Channels to recreate: **{preview['missing_channel_count']}**\n"
                f"Member-role assignments to attempt: **{preview['member_role_assignments']}**\n\n"
                f"Run `/antidefacement restorealarm alarm_id:{alarm_id} mode:execute` to continue.",
                ephemeral=True,
            )
            return
        result = await self.restore_service.execute(
            guild, backup, started_by=interaction.user.id
        )
        history = list(alarm.get("resolution_history", []))
        history.append(
            {
                "action": "restore_alarm",
                "by": interaction.user.id,
                "at": utc_iso(),
                "restore_id": result["restore_id"],
                "result": result["status"],
            }
        )
        await self.store.update_alarm(alarm_id, resolution_history=history)
        text = self._restore_result_text(result)
        affected_departures = sum(
            1 for event in alarm.get("evidence", []) if event.get("action_type") in {"kick", "ban"}
        )
        if affected_departures:
            text += (
                f"\n\nThis alarm also contains **{affected_departures}** kick/ban event(s). "
                "The bot does not automatically rejoin users or unban accounts; use the recovery log."
            )
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="alarms", description="List recent Anti-Defacement alarms.")
    async def alarms(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, MAX_RECENT_ALARMS_SHOWN] = 10,
    ) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        records = await self.store.list_alarms(guild.id, int(limit))
        if not records:
            await interaction.response.send_message("No alarms have been recorded.", ephemeral=True)
            return
        lines = [
            f"• **{item['alarm_id']}** — <@{item['offender_id']}> — "
            f"`{item['trigger_type']}` — `{item['status']}` — {item['triggered_at']}"
            for item in records
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="alarm", description="Inspect one alarm.")
    async def alarm(self, interaction: discord.Interaction, alarm_id: str) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        alarm = await self.store.get_alarm(alarm_id.strip().upper())
        if not alarm or int(alarm.get("guild_id", 0)) != guild.id:
            await interaction.response.send_message("Alarm not found.", ephemeral=True)
            return

        notification_results = alarm.get("notification_results") or {}
        dm_results = notification_results.get("dms") or {}
        dm_lines = [f"• <@{user_id}> (`{user_id}`): `{result}`" for user_id, result in dm_results.items()]
        notification_text = (
            "\n**Notification delivery**\n"
            + ("\n".join(dm_lines) if dm_lines else "• No DM results were recorded.")
            + f"\n• Alert channel: `{notification_results.get('alert_channel', 'not recorded')}`"
        )
        if notification_results.get("error"):
            notification_text += f"\n• Error: `{notification_results['error']}`"

        await interaction.response.send_message(
            f"**{alarm['alarm_id']}**\n"
            f"Offender: <@{alarm['offender_id']}> (`{alarm['offender_id']}`)\n"
            f"Trigger: `{alarm['trigger_type']}`\n"
            f"Status: `{alarm['status']}`\n"
            f"Events: **{len(alarm.get('evidence', []))}**\n"
            f"Roles removed: **{len(alarm.get('removed_roles', []))}**\n"
            f"Unremovable roles: **{len(alarm.get('unremovable_roles', []))}**\n"
            f"Containment errors: **{len(alarm.get('removal_errors', []))}**"
            + notification_text,
            ephemeral=True,
        )

    @app_commands.command(name="acknowledge", description="Acknowledge an alarm without calling it false.")
    async def acknowledge(self, interaction: discord.Interaction, alarm_id: str, note: str = "") -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        alarm_id = alarm_id.strip().upper()
        alarm = await self.store.get_alarm(alarm_id)
        if not alarm or int(alarm.get("guild_id", 0)) != guild.id:
            await interaction.response.send_message("Alarm not found.", ephemeral=True)
            return
        history = list(alarm.get("resolution_history", []))
        history.append(
            {"action": "acknowledged", "by": interaction.user.id, "at": utc_iso(), "note": note}
        )
        await self.store.update_alarm(
            alarm_id,
            status="acknowledged",
            acknowledged_by=interaction.user.id,
            acknowledged_at=utc_iso(),
            resolution_history=history,
        )
        await interaction.response.send_message(f"Alarm **{alarm_id}** acknowledged.", ephemeral=True)

    @app_commands.command(name="test", description="Test alerts and show delivery results.")
    async def test(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        dm_results, channel_result = await self.notifier.broadcast(
            guild,
            content=(
                f"**TEST ALERT:** Anti-Defacement notifications are working in **{guild.name}**. "
                f"Triggered by <@{interaction.user.id}>. No roles were changed."
            ),
        )
        lines = [
            f"• <@{user_id}> (`{user_id}`): `{result}`"
            for user_id, result in sorted(dm_results.items())
        ]
        await interaction.edit_original_response(
            content=(
                "**Notification test results**\n"
                + ("\n".join(lines) if lines else "• No DM recipients were resolved.")
                + f"\n• Alert channel: `{channel_result}`"
                + "\n\n`dm_forbidden_or_closed` means that recipient must allow DMs "
                  "from server members or unblock the bot."
            )
        )

    @app_commands.command(name="checkpermissions", description="Check role hierarchy and bot permissions.")
    async def checkpermissions(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        protection = self.bot.get_cog("ProtectionCog")
        problems = protection.permission_problems(guild) if isinstance(protection, ProtectionCog) else [
            "Protection service is unavailable."
        ]
        await interaction.response.send_message(
            "All critical permission checks passed."
            if not problems
            else "**Problems found**\n" + "\n".join(f"• {problem}" for problem in problems),
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Show current protection status.")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        settings = await self.store.get_guild(guild.id)
        latest = await self.store.latest_backup(guild.id)
        schedule = settings.get("backup_schedule")
        await interaction.response.send_message(
            f"**Anti-Defacement status**\n"
            f"Protection: **{'ACTIVE' if settings.get('active') else 'INACTIVE'}**\n"
            f"Protection owner: {self._mention(settings.get('protection_owner_id'))}\n"
            f"Configured admins: **{len(settings.get('admins', []))}**\n"
            f"Alert channel: {self._channel_mention(settings.get('alert_channel_id'))}\n"
            f"Backup channel: {self._channel_mention(settings.get('backup_channel_id'))}\n"
            f"Backup schedule: `{schedule or 'None'}`\n"
            f"Latest backup: `{latest['backup_id'] if latest else 'None'}`",
            ephemeral=True,
        )

    @app_commands.command(name="settings", description="Show complete server configuration and fixed thresholds.")
    async def settings(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        settings = await self.store.get_guild(guild.id)
        threshold_lines = [
            f"• `{name}`: {value['amount']} in {value['seconds']} seconds"
            for name, value in THRESHOLDS.items()
        ]
        await interaction.response.send_message(
            f"**Configuration for {guild.name}**\n"
            f"Active: `{settings.get('active')}`\n"
            f"Global owner: <@{GLOBAL_BOT_OWNER_ID}> (`{GLOBAL_BOT_OWNER_ID}`)\n"
            f"Protection owner: {self._mention(settings.get('protection_owner_id'))}\n"
            f"Admins: {', '.join(self._mention(v) for v in settings.get('admins', [])) or 'None'}\n"
            f"Alert channel: {self._channel_mention(settings.get('alert_channel_id'))}\n"
            f"Backup channel: {self._channel_mention(settings.get('backup_channel_id'))}\n\n"
            "**Fixed triggers**\n" + "\n".join(threshold_lines),
            ephemeral=True,
        )

    @app_commands.command(name="backuphistory", description="List recent server backups.")
    async def backuphistory(
        self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10
    ) -> None:
        if not await self._authorized(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        records = await self.store.list_backups(guild.id, int(limit))
        lines = [
            f"• **{item['backup_id']}** — `{item['backup_type']}` — `{item['status']}` — {item['created_at']}"
            for item in records
        ]
        await interaction.response.send_message("\n".join(lines) if lines else "No backups found.", ephemeral=True)

    @tasks.loop(seconds=SCHEDULER_INTERVAL_SECONDS)
    async def backup_scheduler(self) -> None:
        now = datetime.now(timezone.utc)
        for settings in await self.store.list_guilds():
            schedule = settings.get("backup_schedule")
            if not schedule or not schedule.get("next_run_at"):
                continue
            try:
                next_run = datetime.fromisoformat(schedule["next_run_at"])
            except (TypeError, ValueError):
                continue
            if next_run > now:
                continue
            guild = self.bot.get_guild(int(settings["guild_id"]))
            if guild is None:
                continue
            # Move the due date before starting so a slow backup cannot duplicate.
            schedule["next_run_at"] = self.backup_service.compute_next_run(
                schedule, now=now
            ).isoformat()
            await self.store.update_guild(guild.id, backup_schedule=schedule)
            try:
                await self.backup_service.create_backup(
                    guild, initiated_by=None, backup_type="scheduled", deliver=True
                )
            except Exception as exc:
                log.exception("Scheduled backup failed for guild %s", guild.id)
                await self.notifier.broadcast(
                    guild,
                    content=f"Scheduled Anti-Defacement backup failed in **{guild.name}**: `{exc}`",
                )

    @backup_scheduler.before_loop
    async def before_backup_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("Application command failed", exc_info=error)
        message = f"Command failed: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @staticmethod
    def _mention(user_id: int | None) -> str:
        return f"<@{user_id}> (`{user_id}`)" if user_id else "Not set"

    @staticmethod
    def _channel_mention(channel_id: int | None) -> str:
        return f"<#{channel_id}> (`{channel_id}`)" if channel_id else "Not set"

    @staticmethod
    def _restore_result_text(result: dict) -> str:
        text = (
            f"**Restore {result['restore_id']}** finished with status `{result['status']}`.\n"
            f"Roles recreated: **{len(result.get('created_roles', []))}**\n"
            f"Channels recreated: **{len(result.get('created_channels', []))}**\n"
            f"Member-role assignments restored: **{result.get('member_roles_restored', 0)}**"
        )
        errors = result.get("errors", [])
        if errors:
            text += "\n\n**Errors**\n" + "\n".join(f"• {item}" for item in errors[:20])
            if len(errors) > 20:
                text += f"\n• …and {len(errors) - 20} more"
        return text
