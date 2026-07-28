from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands, tasks

from config import (
    ALARM_DIR,
    HEALTH_CHECK_INTERVAL_SECONDS,
    PENDING_SNAPSHOT_TTL_SECONDS,
    THRESHOLDS,
)
from notifier import Notifier
from permissions import is_exempt_actor
from serializers import (
    audit_diff_to_dict,
    serialize_channel,
    serialize_member,
    serialize_role,
    serialize_user,
)
from storage import PostgresStore, utc_iso

log = logging.getLogger(__name__)

ACTION_MAP = {
    discord.AuditLogAction.channel_delete: "channel_delete",
    discord.AuditLogAction.role_delete: "role_delete",
    discord.AuditLogAction.ban: "ban",
    discord.AuditLogAction.kick: "kick",
}


class ProtectionCog(commands.Cog):
    def __init__(self, bot: commands.Bot, store: PostgresStore, notifier: Notifier) -> None:
        self.bot = bot
        self.store = store
        self.notifier = notifier
        self.history: dict[tuple[int, int, str], deque[tuple[float, dict[str, Any]]]] = defaultdict(deque)
        self.actor_locks: dict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        self.pending_snapshots: dict[tuple[int, str, int], tuple[float, dict[str, Any]]] = {}
        self.health_checks.start()

    def cog_unload(self) -> None:
        self.health_checks.cancel()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        self.pending_snapshots[(channel.guild.id, "channel_delete", channel.id)] = (
            time.monotonic(),
            serialize_channel(channel),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        self.pending_snapshots[(role.guild.id, "role_delete", role.id)] = (
            time.monotonic(),
            serialize_role(role),
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member) -> None:
        self.pending_snapshots[(guild.id, "ban", user.id)] = (
            time.monotonic(),
            serialize_user(user),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        self.pending_snapshots[(member.guild.id, "kick", member.id)] = (
            time.monotonic(),
            serialize_member(member),
        )

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        action_name = ACTION_MAP.get(entry.action)
        if action_name is None:
            return
        guild = entry.guild
        settings = await self.store.get_guild(guild.id)
        if not settings.get("active", False):
            return
        claimed = await self.store.claim_audit_entry(entry.id, guild.id, action_name)
        if not claimed:
            return

        actor = entry.user
        actor_id = getattr(actor, "id", None)
        if actor_id is None or (self.bot.user and actor_id == self.bot.user.id):
            return
        if await is_exempt_actor(self.store, guild, actor_id):
            return

        target_id = getattr(entry.target, "id", None)
        snapshot = self._take_pending_snapshot(guild.id, action_name, target_id)
        if snapshot is None:
            # Gateway delete/member events and the audit-log event can arrive in either order.
            await asyncio.sleep(0.75)
            snapshot = self._take_pending_snapshot(guild.id, action_name, target_id)
        if snapshot is None:
            snapshot = self._fallback_snapshot(entry, action_name)

        event = {
            "audit_log_id": entry.id,
            "action_type": action_name,
            "actor": serialize_user(actor),
            "actor_id": actor_id,
            "target_id": target_id,
            "target": snapshot,
            "reason": entry.reason,
            "created_at": entry.created_at.isoformat(),
        }
        await self._register_event(guild, actor_id, event)

    def _take_pending_snapshot(
        self, guild_id: int, action_name: str, target_id: int | None
    ) -> dict[str, Any] | None:
        if target_id is None:
            return None
        item = self.pending_snapshots.pop((guild_id, action_name, target_id), None)
        if not item:
            return None
        timestamp, snapshot = item
        if time.monotonic() - timestamp > PENDING_SNAPSHOT_TTL_SECONDS:
            return None
        return snapshot

    def _fallback_snapshot(
        self, entry: discord.AuditLogEntry, action_name: str
    ) -> dict[str, Any]:
        target = entry.target
        if action_name in {"ban", "kick"}:
            return serialize_user(target if isinstance(target, discord.abc.User) else None)
        data = {
            "id": getattr(target, "id", None),
            "name": getattr(target, "name", None),
            "audit_before": audit_diff_to_dict(entry.before),
        }
        return data

    async def _register_event(
        self, guild: discord.Guild, actor_id: int, event: dict[str, Any]
    ) -> None:
        lock = self.actor_locks[(guild.id, actor_id)]
        async with lock:
            now = time.monotonic()
            action_name = event["action_type"]
            action_key = (guild.id, actor_id, action_name)
            combined_key = (guild.id, actor_id, "combined")
            self.history[action_key].append((now, event))
            self.history[combined_key].append((now, event))

            self._prune(action_key, THRESHOLDS[action_name]["seconds"], now)
            self._prune(combined_key, THRESHOLDS["combined"]["seconds"], now)

            action_count = len(self.history[action_key])
            combined_count = len(self.history[combined_key])
            action_triggered = action_count >= THRESHOLDS[action_name]["amount"]
            combined_triggered = combined_count >= THRESHOLDS["combined"]["amount"]
            if not action_triggered and not combined_triggered:
                return

            trigger_type = action_name if action_triggered else "combined"
            evidence = [item[1] for item in self.history[combined_key]]
            self._clear_actor_history(guild.id, actor_id)
            await self._contain_and_alarm(guild, actor_id, trigger_type, evidence)

    def _prune(self, key: tuple[int, int, str], window: int, now: float) -> None:
        history = self.history[key]
        cutoff = now - window
        while history and history[0][0] < cutoff:
            history.popleft()

    def _clear_actor_history(self, guild_id: int, actor_id: int) -> None:
        for key in list(self.history):
            if key[0] == guild_id and key[1] == actor_id:
                del self.history[key]

    async def generate_alarm_id(self) -> str:
        while True:
            candidate = f"AD-{secrets.randbelow(10**12):012d}"
            if not await self.store.alarm_id_exists(candidate):
                return candidate

    async def _contain_and_alarm(
        self,
        guild: discord.Guild,
        actor_id: int,
        trigger_type: str,
        evidence: list[dict[str, Any]],
    ) -> None:
        alarm_id = await self.generate_alarm_id()
        member = guild.get_member(actor_id)
        if member is None:
            try:
                member = await guild.fetch_member(actor_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None

        role_snapshot = [serialize_role(role) for role in member.roles if not role.is_default()] if member else []
        alarm = {
            "alarm_id": alarm_id,
            "guild_id": guild.id,
            "guild_name": guild.name,
            "offender_id": actor_id,
            "offender_username": str(member) if member else evidence[-1]["actor"].get("username", "Unknown"),
            "trigger_type": trigger_type,
            "triggered_at": utc_iso(),
            "status": "containing",
            "evidence": evidence,
            "offender_role_snapshot": role_snapshot,
            "removed_roles": [],
            "unremovable_roles": [],
            "removal_errors": [],
            "notification_results": {},
            "resolution_history": [],
        }
        # The PostgreSQL primary key is the final uniqueness guarantee. If an
        # extremely rare random collision occurs, generate a new alarm ID.
        while True:
            try:
                await self.store.create_alarm(alarm)
                break
            except ValueError:
                alarm_id = await self.generate_alarm_id()
                alarm["alarm_id"] = alarm_id

        if member is None:
            alarm["removal_errors"].append("Offender is no longer present in the server.")
        elif member.id == guild.owner_id:
            alarm["removal_errors"].append("Discord does not allow bots to modify the server owner.")
        elif guild.me is None:
            alarm["removal_errors"].append("Bot member object was unavailable.")
        else:
            removable = [
                role
                for role in member.roles
                if not role.is_default() and not role.managed and role < guild.me.top_role
            ]
            unremovable = [
                role
                for role in member.roles
                if not role.is_default() and role not in removable
            ]
            alarm["unremovable_roles"] = [serialize_role(role) for role in unremovable]

            if removable:
                try:
                    await member.remove_roles(
                        *removable,
                        reason=f"Anti-Defacement containment alarm {alarm_id}",
                        atomic=False,
                    )
                    alarm["removed_roles"] = [serialize_role(role) for role in removable]
                except (discord.Forbidden, discord.HTTPException) as bulk_error:
                    alarm["removal_errors"].append(f"Bulk role removal failed: {bulk_error}")
                    for role in removable:
                        try:
                            await member.remove_roles(
                                role,
                                reason=f"Anti-Defacement containment alarm {alarm_id}",
                            )
                            alarm["removed_roles"].append(serialize_role(role))
                        except (discord.Forbidden, discord.HTTPException) as exc:
                            alarm["removal_errors"].append(
                                f"Could not remove {role.name} ({role.id}): {exc}"
                            )

        alarm["status"] = "contained" if alarm["removed_roles"] or not role_snapshot else "containment_failed"
        # ``alarm`` contains its own alarm_id. Passing the full mapping together
        # with the positional alarm_id used to raise ``got multiple values for
        # argument 'alarm_id'`` after containment. That stopped the event handler
        # before notifications were sent. Persist a key-safe copy and, even if
        # PostgreSQL is temporarily unavailable, continue to send the urgent alert.
        try:
            await self.store.update_alarm(
                alarm_id,
                **{key: value for key, value in alarm.items() if key != "alarm_id"},
            )
        except Exception as exc:
            alarm["persistence_error"] = (
                f"Initial post-containment alarm update failed: {type(exc).__name__}: {exc}"
            )
            log.exception("Could not persist post-containment state for alarm %s", alarm_id)

        log_paths: list[str] = []
        notification_error: str | None = None
        try:
            try:
                log_paths = await self._write_alarm_logs(alarm)
            except Exception as exc:
                # Recovery-log generation must never suppress the urgent alert.
                notification_error = f"Recovery log generation failed: {type(exc).__name__}: {exc}"
                log.exception("Could not generate recovery logs for alarm %s", alarm_id)

            content = self._alarm_message(alarm)
            try:
                dm_results, channel_result = await self.notifier.broadcast(
                    guild,
                    content=content,
                    attachment_paths=log_paths,
                )
            except Exception as exc:
                # Preserve the alarm and expose an explicit failure instead of
                # letting an event-listener exception disappear into the logs.
                notification_error = (
                    f"Notification broadcast failed: {type(exc).__name__}: {exc}"
                )
                log.exception("Notification broadcast failed for alarm %s", alarm_id)
                dm_results = {}
                channel_result = "broadcast_exception"

            alarm["notification_results"] = {
                "dms": {str(key): value for key, value in dm_results.items()},
                "alert_channel": channel_result,
                "error": notification_error,
            }
            log.info(
                "Alarm %s notification results: dms=%s alert_channel=%s error=%s",
                alarm_id,
                alarm["notification_results"]["dms"],
                channel_result,
                notification_error,
            )
            try:
                await self.store.update_alarm(
                    alarm_id,
                    notification_results=alarm["notification_results"],
                    persistence_error=alarm.get("persistence_error"),
                )
            except Exception:
                # Notification delivery has already been attempted. A database
                # write failure here must not turn a successful containment into
                # an unhandled listener exception.
                log.exception("Could not persist notification results for alarm %s", alarm_id)
        finally:
            for value in log_paths:
                try:
                    await asyncio.to_thread(Path(value).unlink, missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _alarm_message(alarm: dict[str, Any]) -> str:
        return (
            "The Anti Defacement Bot has triggered and removed all removable roles from "
            f"<@{alarm['offender_id']}> `{alarm['offender_id']}` "
            f"`{alarm['offender_username']}`. This is alarm ID **{alarm['alarm_id']}**.\n\n"
            "If this is a false alarm, please run the command "
            f"`/antidefacement falsealarm alarm_id:{alarm['alarm_id']}`.\n\n"
            "Please see the attached recovery log for all users, channels, or roles that were "
            "banned, kicked, or deleted. The bot did not kick or ban anyone."
        )

    async def _write_alarm_logs(self, alarm: dict[str, Any]) -> list[str]:
        guild_dir = ALARM_DIR / str(alarm["guild_id"])
        guild_dir.mkdir(parents=True, exist_ok=True)
        json_path = guild_dir / f"{alarm['alarm_id']}.json"
        text_path = guild_dir / f"{alarm['alarm_id']}-recovery-log.txt"

        await asyncio.to_thread(
            json_path.write_text,
            json.dumps(alarm, indent=2, ensure_ascii=False),
            "utf-8",
        )
        lines = [
            "ANTI-DEFACEMENT RECOVERY LOG",
            f"Alarm ID: {alarm['alarm_id']}",
            f"Server: {alarm['guild_name']} ({alarm['guild_id']})",
            f"Offender: {alarm['offender_username']} ({alarm['offender_id']})",
            f"Trigger: {alarm['trigger_type']}",
            f"Triggered: {alarm['triggered_at']}",
            "",
            "REMOVED OFFENDER ROLES",
        ]
        if alarm["removed_roles"]:
            lines.extend(
                f"- {role['name']} ({role['id']})" for role in alarm["removed_roles"]
            )
        else:
            lines.append("- None")
        lines.extend(["", "UNREMOVABLE OFFENDER ROLES"])
        if alarm["unremovable_roles"]:
            lines.extend(
                f"- {role['name']} ({role['id']})" for role in alarm["unremovable_roles"]
            )
        else:
            lines.append("- None")
        lines.extend(["", "DESTRUCTIVE EVENTS"])
        for event in alarm["evidence"]:
            target = event.get("target", {})
            lines.append(
                f"- {event['created_at']} | {event['action_type']} | "
                f"target={target.get('name') or target.get('username') or event.get('target_id')} "
                f"({event.get('target_id')}) | audit_log_id={event['audit_log_id']}"
            )
            lines.append(f"  Snapshot: {json.dumps(target, ensure_ascii=False)}")
        if alarm["removal_errors"]:
            lines.extend(["", "CONTAINMENT ERRORS"])
            lines.extend(f"- {value}" for value in alarm["removal_errors"])
        await asyncio.to_thread(text_path.write_text, "\n".join(lines), "utf-8")
        return [str(text_path), str(json_path)]

    @tasks.loop(seconds=HEALTH_CHECK_INTERVAL_SECONDS)
    async def health_checks(self) -> None:
        cutoff = time.monotonic() - PENDING_SNAPSHOT_TTL_SECONDS
        for key, (created, _) in list(self.pending_snapshots.items()):
            if created < cutoff:
                self.pending_snapshots.pop(key, None)

        for guild in self.bot.guilds:
            settings = await self.store.get_guild(guild.id)
            if not settings.get("active", False):
                continue
            problems = self.permission_problems(guild)
            for field, label in (("alert_channel_id", "alert"), ("backup_channel_id", "backup")):
                channel_id = settings.get(field)
                if channel_id and guild.get_channel(int(channel_id)) is None:
                    problems.append(f"Configured {label} channel no longer exists: {channel_id}")
            signature = "|".join(problems)
            if signature == (settings.get("last_health_signature") or ""):
                continue
            await self.store.update_guild(guild.id, last_health_signature=signature)
            if not problems:
                continue
            content = (
                f"Anti-Defacement health warning for **{guild.name}** (`{guild.id}`):\n"
                + "\n".join(f"- {problem}" for problem in problems)
            )
            await self.notifier.broadcast(guild, content=content)

    @health_checks.before_loop
    async def before_health_checks(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def permission_problems(guild: discord.Guild) -> list[str]:
        bot_member = guild.me
        if bot_member is None:
            return ["Bot member object is unavailable."]
        perms = bot_member.guild_permissions
        problems: list[str] = []
        if not perms.view_audit_log:
            problems.append("Missing View Audit Log; destructive actions cannot be attributed.")
        if not perms.manage_roles:
            problems.append("Missing Manage Roles; offenders cannot be contained.")
        everyone = guild.default_role.permissions
        dangerous = []
        for attr, label in (
            ("administrator", "Administrator"),
            ("manage_roles", "Manage Roles"),
            ("manage_channels", "Manage Channels"),
            ("ban_members", "Ban Members"),
            ("kick_members", "Kick Members"),
        ):
            if getattr(everyone, attr, False):
                dangerous.append(label)
        if dangerous:
            problems.append("@everyone has dangerous permissions: " + ", ".join(dangerous))
        dangerous_above = [
            role.name
            for role in guild.roles
            if role >= bot_member.top_role
            and role != bot_member.top_role
            and (
                role.permissions.administrator
                or role.permissions.manage_roles
                or role.permissions.manage_channels
                or role.permissions.ban_members
                or role.permissions.kick_members
            )
        ]
        if dangerous_above:
            problems.append(
                "Dangerous roles above the bot cannot be removed: " + ", ".join(dangerous_above[:20])
            )
        return problems
