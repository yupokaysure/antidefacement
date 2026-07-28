from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

import discord

from backup_service import BackupService
from storage import PostgresStore, utc_iso

log = logging.getLogger(__name__)


class RestoreService:
    def __init__(self, store: PostgresStore, backup_service: BackupService) -> None:
        self.store = store
        self.backup_service = backup_service

    async def preview(self, guild: discord.Guild, backup: dict[str, Any]) -> dict[str, Any]:
        snapshot = await self.backup_service.load_snapshot(backup)
        current_role_ids = {role.id for role in guild.roles}
        current_channel_ids = {channel.id for channel in guild.channels}
        missing_roles = [
            role
            for role in snapshot["roles"]
            if not role.get("is_default") and not role.get("managed") and int(role["id"]) not in current_role_ids
        ]
        missing_channels = [
            channel for channel in snapshot["channels"] if int(channel["id"]) not in current_channel_ids
        ]

        member_assignment_count = 0
        missing_members = 0
        for member_data in snapshot["members"]:
            member = guild.get_member(int(member_data["id"]))
            if member is None:
                missing_members += 1
                continue
            current = {role.id for role in member.roles}
            member_assignment_count += sum(
                1 for role_id in member_data.get("role_ids", []) if int(role_id) not in current
            )

        return {
            "backup_id": backup["backup_id"],
            "missing_role_count": len(missing_roles),
            "missing_channel_count": len(missing_channels),
            "member_role_assignments": member_assignment_count,
            "members_not_in_server": missing_members,
            "missing_roles": missing_roles,
            "missing_channels": missing_channels,
        }

    async def execute(
        self,
        guild: discord.Guild,
        backup: dict[str, Any],
        *,
        started_by: int,
    ) -> dict[str, Any]:
        snapshot = await self.backup_service.load_snapshot(backup)
        restore_id = f"RS-{datetime.now(timezone.utc):%Y%m%d}-{secrets.randbelow(10**10):010d}"
        result: dict[str, Any] = {
            "restore_id": restore_id,
            "guild_id": guild.id,
            "backup_id": backup["backup_id"],
            "status": "running",
            "started_by": started_by,
            "started_at": utc_iso(),
            "role_id_map": {},
            "channel_id_map": {},
            "created_roles": [],
            "created_channels": [],
            "member_roles_restored": 0,
            "errors": [],
        }
        await self.store.create_restore_job(result)

        try:
            await self._restore_roles(guild, snapshot, result)
            await self._restore_channels(guild, snapshot, result)
            await self._restore_member_roles(guild, snapshot, result)
            result["status"] = "complete_with_errors" if result["errors"] else "complete"
        except Exception as exc:
            log.exception("Restore %s failed", restore_id)
            result["status"] = "failed"
            result["errors"].append(f"Fatal restore error: {exc}")
        finally:
            result["completed_at"] = utc_iso()
            try:
                await self.store.update_restore_job(
                    restore_id,
                    **{key: value for key, value in result.items() if key != "restore_id"},
                )
            except Exception as exc:
                # The Discord-side restore may already have completed. Record the
                # persistence problem in the returned result instead of raising a
                # misleading command failure after the work went through.
                log.exception("Could not persist final state for restore %s", restore_id)
                result["database_persistence_error"] = f"{type(exc).__name__}: {exc}"

        return result

    async def _restore_roles(
        self, guild: discord.Guild, snapshot: dict[str, Any], result: dict[str, Any]
    ) -> None:
        current = {role.id: role for role in guild.roles}
        to_position: dict[discord.Role, int] = {}

        for data in sorted(snapshot["roles"], key=lambda item: int(item.get("position", 0))):
            old_id = int(data["id"])
            if data.get("is_default"):
                result["role_id_map"][str(old_id)] = guild.default_role.id
                continue
            if old_id in current:
                result["role_id_map"][str(old_id)] = old_id
                continue
            if data.get("managed"):
                result["errors"].append(f"Managed role not recreated: {data.get('name')} ({old_id})")
                continue
            try:
                new_role = await guild.create_role(
                    name=data.get("name") or "Restored Role",
                    permissions=discord.Permissions(int(data.get("permissions", 0))),
                    colour=discord.Colour(int(data.get("color", 0))),
                    hoist=bool(data.get("hoist", False)),
                    mentionable=bool(data.get("mentionable", False)),
                    reason=f"Anti-Defacement restore {result['restore_id']}",
                )
                result["role_id_map"][str(old_id)] = new_role.id
                result["created_roles"].append({"old_id": old_id, "new_id": new_role.id, "name": new_role.name})
                to_position[new_role] = int(data.get("position", 1))
            except (discord.Forbidden, discord.HTTPException) as exc:
                result["errors"].append(f"Role {data.get('name')} could not be recreated: {exc}")

        if to_position:
            try:
                bot_top = guild.me.top_role.position if guild.me else 1
                safe_positions = {
                    role: max(1, min(position, bot_top - 1)) for role, position in to_position.items()
                }
                await guild.edit_role_positions(
                    positions=safe_positions,
                    reason=f"Anti-Defacement restore {result['restore_id']}",
                )
            except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
                result["errors"].append(f"Could not fully restore role ordering: {exc}")

    def _build_overwrites(
        self,
        guild: discord.Guild,
        data: dict[str, Any],
        role_id_map: dict[str, int],
    ) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
        for item in data.get("overwrites", []):
            target = None
            old_target_id = int(item.get("target_id", 0))
            if item.get("target_type") == "role":
                mapped_id = int(role_id_map.get(str(old_target_id), old_target_id))
                target = guild.get_role(mapped_id)
            else:
                target = guild.get_member(old_target_id)
            if target is None:
                continue
            allow = discord.Permissions(int(item.get("allow", 0)))
            deny = discord.Permissions(int(item.get("deny", 0)))
            overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)
        return overwrites

    async def _restore_channels(
        self, guild: discord.Guild, snapshot: dict[str, Any], result: dict[str, Any]
    ) -> None:
        existing = {channel.id: channel for channel in guild.channels}
        role_map = {str(key): int(value) for key, value in result["role_id_map"].items()}
        categories = [item for item in snapshot["channels"] if item.get("kind") == "category"]
        others = [item for item in snapshot["channels"] if item.get("kind") != "category"]

        for data in sorted(categories, key=lambda item: int(item.get("position", 0))):
            old_id = int(data["id"])
            if old_id in existing:
                result["channel_id_map"][str(old_id)] = old_id
                continue
            try:
                channel = await guild.create_category(
                    data.get("name") or "restored-category",
                    position=int(data.get("position", 0)),
                    overwrites=self._build_overwrites(guild, data, role_map),
                    reason=f"Anti-Defacement restore {result['restore_id']}",
                )
                result["channel_id_map"][str(old_id)] = channel.id
                result["created_channels"].append({"old_id": old_id, "new_id": channel.id, "name": channel.name})
            except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
                result["errors"].append(f"Category {data.get('name')} could not be recreated: {exc}")

        for data in sorted(others, key=lambda item: int(item.get("position", 0))):
            old_id = int(data["id"])
            if old_id in existing:
                result["channel_id_map"][str(old_id)] = old_id
                continue

            category = None
            category_id = data.get("category_id")
            if category_id:
                mapped_category_id = int(result["channel_id_map"].get(str(category_id), category_id))
                found = guild.get_channel(mapped_category_id)
                if isinstance(found, discord.CategoryChannel):
                    category = found

            overwrites = self._build_overwrites(guild, data, role_map)
            common = {
                "category": category,
                "position": int(data.get("position", 0)),
                "overwrites": overwrites,
                "reason": f"Anti-Defacement restore {result['restore_id']}",
            }
            try:
                kind = data.get("kind")
                name = data.get("name") or "restored-channel"
                if kind in {"text", "news"}:
                    channel = await guild.create_text_channel(
                        name,
                        news=kind == "news",
                        topic=data.get("topic"),
                        slowmode_delay=int(data.get("slowmode_delay", 0) or 0),
                        nsfw=bool(data.get("nsfw", False)),
                        **common,
                    )
                elif kind == "forum":
                    channel = await guild.create_forum(
                        name,
                        topic=data.get("topic"),
                        slowmode_delay=int(data.get("slowmode_delay", 0) or 0),
                        nsfw=bool(data.get("nsfw", False)),
                        **common,
                    )
                elif kind == "voice":
                    channel = await guild.create_voice_channel(
                        name,
                        bitrate=int(data.get("bitrate") or 64000),
                        user_limit=int(data.get("user_limit") or 0),
                        nsfw=bool(data.get("nsfw", False)),
                        **common,
                    )
                elif kind == "stage":
                    channel = await guild.create_stage_channel(
                        name,
                        bitrate=int(data.get("bitrate") or 64000),
                        user_limit=int(data.get("user_limit") or 0),
                        nsfw=bool(data.get("nsfw", False)),
                        **common,
                    )
                else:
                    result["errors"].append(f"Unsupported channel type {kind}: {name}")
                    continue
                result["channel_id_map"][str(old_id)] = channel.id
                result["created_channels"].append({"old_id": old_id, "new_id": channel.id, "name": channel.name})
            except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as exc:
                result["errors"].append(f"Channel {data.get('name')} could not be recreated: {exc}")

    async def _restore_member_roles(
        self, guild: discord.Guild, snapshot: dict[str, Any], result: dict[str, Any]
    ) -> None:
        role_map = {str(key): int(value) for key, value in result["role_id_map"].items()}
        bot_member = guild.me
        if bot_member is None:
            result["errors"].append("Bot member object unavailable; member roles were not restored.")
            return

        for member_data in snapshot["members"]:
            member = guild.get_member(int(member_data["id"]))
            if member is None or member.id == guild.owner_id:
                continue
            current_ids = {role.id for role in member.roles}
            roles: list[discord.Role] = []
            for old_id in member_data.get("role_ids", []):
                mapped_id = int(role_map.get(str(old_id), old_id))
                role = guild.get_role(mapped_id)
                if role and role.id not in current_ids and not role.managed and role < bot_member.top_role:
                    roles.append(role)
            if not roles:
                continue
            try:
                await member.add_roles(
                    *roles,
                    reason=f"Anti-Defacement restore {result['restore_id']}",
                    atomic=False,
                )
                result["member_roles_restored"] += len(roles)
            except (discord.Forbidden, discord.HTTPException) as exc:
                result["errors"].append(f"Could not restore roles to {member.id}: {exc}")
