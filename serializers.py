from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

import discord


def iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_user(user: discord.abc.User | discord.Member | None) -> dict[str, Any]:
    if user is None:
        return {"id": None, "username": "Unknown", "display_name": "Unknown"}
    return {
        "id": user.id,
        "username": str(user),
        "name": getattr(user, "name", str(user)),
        "display_name": getattr(user, "display_name", getattr(user, "name", str(user))),
        "bot": bool(getattr(user, "bot", False)),
        "created_at": iso_or_none(getattr(user, "created_at", None)),
    }


def serialize_member(member: discord.Member) -> dict[str, Any]:
    data = serialize_user(member)
    data.update(
        {
            "nickname": member.nick,
            "joined_at": iso_or_none(member.joined_at),
            "pending": member.pending,
            "role_ids": [role.id for role in member.roles if not role.is_default()],
            "role_names": [role.name for role in member.roles if not role.is_default()],
        }
    )
    return data


def serialize_role(role: discord.Role) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "position": role.position,
        "color": role.color.value,
        "permissions": role.permissions.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "managed": role.managed,
        "is_default": role.is_default(),
        "unicode_emoji": getattr(role, "unicode_emoji", None),
        "member_ids": [member.id for member in role.members],
        "created_at": iso_or_none(role.created_at),
    }


def serialize_overwrites(channel: discord.abc.GuildChannel) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        output.append(
            {
                "target_id": target.id,
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "target_name": getattr(target, "name", str(target)),
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return output


def channel_kind(channel: discord.abc.GuildChannel) -> str:
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    if isinstance(channel, discord.ForumChannel):
        return "forum"
    if isinstance(channel, discord.StageChannel):
        return "stage"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    if isinstance(channel, discord.TextChannel):
        return "news" if channel.is_news() else "text"
    return str(channel.type)


def serialize_channel(channel: discord.abc.GuildChannel) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": channel.id,
        "name": channel.name,
        "kind": channel_kind(channel),
        "position": channel.position,
        "category_id": getattr(channel, "category_id", None),
        "category_name": getattr(getattr(channel, "category", None), "name", None),
        "overwrites": serialize_overwrites(channel),
        "created_at": iso_or_none(channel.created_at),
    }

    for name in (
        "topic",
        "nsfw",
        "slowmode_delay",
        "default_auto_archive_duration",
        "default_thread_slowmode_delay",
        "bitrate",
        "user_limit",
    ):
        if hasattr(channel, name):
            value = getattr(channel, name)
            if isinstance(value, Enum):
                value = value.value
            data[name] = value

    rtc_region = getattr(channel, "rtc_region", None)
    data["rtc_region"] = str(rtc_region) if rtc_region else None
    video_quality = getattr(channel, "video_quality_mode", None)
    data["video_quality_mode"] = getattr(video_quality, "value", None)

    if isinstance(channel, discord.ForumChannel):
        data["default_sort_order"] = getattr(channel.default_sort_order, "value", None)
        data["default_layout"] = getattr(channel.default_layout, "value", None)
        data["available_tags"] = [
            {
                "id": tag.id,
                "name": tag.name,
                "moderated": tag.moderated,
                "emoji": str(tag.emoji) if tag.emoji else None,
            }
            for tag in channel.available_tags
        ]
    return data


def audit_diff_to_dict(diff: discord.AuditLogDiff) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in vars(diff).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            output[key] = value
        elif isinstance(value, discord.Permissions):
            output[key] = value.value
        elif isinstance(value, discord.Colour):
            output[key] = value.value
        elif isinstance(value, discord.abc.Snowflake):
            output[key] = {"id": value.id, "name": getattr(value, "name", str(value))}
        else:
            output[key] = str(value)
    return output
