from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from config import DATABASE_URL, MIGRATIONS_DIR

log = logging.getLogger(__name__)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_datetime(value: Any, *, default_now: bool = True) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc) if default_now else None


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class PostgresStore:
    """PostgreSQL persistence for all durable bot state.

    Complex Discord snapshots remain JSONB, while identifiers and fields used
    for filtering are stored in ordinary indexed PostgreSQL columns.
    """

    GUILD_FIELDS = {
        "active",
        "protection_owner_id",
        "backup_channel_id",
        "alert_channel_id",
        "backup_schedule",
        "last_health_signature",
    }

    def __init__(self, database_url: str = DATABASE_URL) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    @staticmethod
    async def _init_connection(connection: asyncpg.Connection) -> None:
        # asyncpg normally returns json/jsonb as strings. Register codecs so the
        # rest of the bot can continue using dictionaries and lists directly.
        for type_name in ("json", "jsonb"):
            await connection.set_type_codec(
                type_name,
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
                format="text",
            )

    async def connect(self) -> None:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is missing. Add a Railway PostgreSQL service and "
                "reference its DATABASE_URL in the bot service."
            )

        last_error: Exception | None = None
        for attempt in range(1, 11):
            try:
                self.pool = await asyncpg.create_pool(
                    dsn=self.database_url,
                    min_size=1,
                    max_size=5,
                    command_timeout=30,
                    init=self._init_connection,
                )
                await self.pool.fetchval("SELECT 1")
                return
            except Exception as exc:  # startup may race the Railway database service
                last_error = exc
                log.warning("PostgreSQL connection attempt %s failed: %s", attempt, exc)
                if attempt < 10:
                    await asyncio.sleep(min(attempt * 2, 15))

        raise RuntimeError("Could not connect to PostgreSQL.") from last_error

    async def close(self) -> None:
        if self.pool is None:
            return
        pool = self.pool
        self.pool = None
        try:
            await asyncio.wait_for(pool.close(), timeout=15)
        except TimeoutError:
            pool.terminate()

    def require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("PostgreSQL pool is not connected.")
        return self.pool

    async def run_migrations(self, directory: Path = MIGRATIONS_DIR) -> None:
        pool = self.require_pool()
        directory.mkdir(parents=True, exist_ok=True)
        migration_files = sorted(directory.glob("*.sql"))
        if not migration_files:
            raise RuntimeError(f"No SQL migrations were found in {directory}.")

        async with pool.acquire() as connection:
            # Prevent overlapping Railway deployments from applying the same
            # migration concurrently.
            await connection.execute("SELECT pg_advisory_lock($1)", 845_773_911)
            try:
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        migration_name TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )

                for path in migration_files:
                    applied = await connection.fetchval(
                        "SELECT 1 FROM schema_migrations WHERE migration_name = $1",
                        path.name,
                    )
                    if applied:
                        continue
                    sql = await asyncio.to_thread(path.read_text, encoding="utf-8")
                    async with connection.transaction():
                        await connection.execute(sql)
                        await connection.execute(
                            "INSERT INTO schema_migrations (migration_name) VALUES ($1)",
                            path.name,
                        )
                    log.info("Applied database migration %s", path.name)
            finally:
                await connection.execute("SELECT pg_advisory_unlock($1)", 845_773_911)

    @staticmethod
    def _guild_defaults(guild_id: int) -> dict[str, Any]:
        now = utc_iso()
        return {
            "guild_id": guild_id,
            "active": False,
            "protection_owner_id": None,
            "admins": [],
            "backup_channel_id": None,
            "alert_channel_id": None,
            "backup_schedule": None,
            "created_at": now,
            "updated_at": now,
            "last_health_signature": None,
        }

    async def ensure_guild(self, guild_id: int) -> dict[str, Any]:
        pool = self.require_pool()
        await pool.execute(
            """
            INSERT INTO guild_settings (guild_id)
            VALUES ($1)
            ON CONFLICT (guild_id) DO NOTHING
            """,
            guild_id,
        )
        return await self.get_guild(guild_id, ensure=False)

    async def get_guild(self, guild_id: int, *, ensure: bool = True) -> dict[str, Any]:
        pool = self.require_pool()
        if ensure:
            await pool.execute(
                """
                INSERT INTO guild_settings (guild_id)
                VALUES ($1)
                ON CONFLICT (guild_id) DO NOTHING
                """,
                guild_id,
            )
        row = await pool.fetchrow(
            """
            SELECT guild_id, active, protection_owner_id, backup_channel_id,
                   alert_channel_id, backup_schedule, last_health_signature,
                   created_at, updated_at
            FROM guild_settings
            WHERE guild_id = $1
            """,
            guild_id,
        )
        if row is None:
            return self._guild_defaults(guild_id)
        admins = await pool.fetch(
            "SELECT user_id FROM guild_admins WHERE guild_id = $1 ORDER BY user_id",
            guild_id,
        )
        result = dict(row)
        result["created_at"] = _iso(result["created_at"])
        result["updated_at"] = _iso(result["updated_at"])
        result["admins"] = [int(item["user_id"]) for item in admins]
        return result

    async def update_guild(self, guild_id: int, **changes: Any) -> dict[str, Any]:
        invalid = set(changes) - self.GUILD_FIELDS
        if invalid:
            raise ValueError(f"Unsupported guild setting fields: {sorted(invalid)}")
        await self.ensure_guild(guild_id)
        if not changes:
            return await self.get_guild(guild_id)

        assignments: list[str] = []
        values: list[Any] = [guild_id]
        for index, (field, value) in enumerate(changes.items(), start=2):
            assignments.append(f"{field} = ${index}")
            values.append(value)
        assignments.append("updated_at = NOW()")
        pool = self.require_pool()
        await pool.execute(
            f"UPDATE guild_settings SET {', '.join(assignments)} WHERE guild_id = $1",
            *values,
        )
        return await self.get_guild(guild_id, ensure=False)

    async def add_admin(
        self,
        guild_id: int,
        user_id: int,
        *,
        added_by: int | None = None,
        is_exempt: bool = True,
    ) -> bool:
        await self.ensure_guild(guild_id)
        pool = self.require_pool()
        inserted = await pool.fetchval(
            """
            INSERT INTO guild_admins (guild_id, user_id, is_exempt, added_by)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, user_id) DO NOTHING
            RETURNING user_id
            """,
            guild_id,
            user_id,
            is_exempt,
            added_by,
        )
        return inserted is not None

    async def remove_admin(self, guild_id: int, user_id: int) -> bool:
        pool = self.require_pool()
        removed = await pool.fetchval(
            """
            DELETE FROM guild_admins
            WHERE guild_id = $1 AND user_id = $2
            RETURNING user_id
            """,
            guild_id,
            user_id,
        )
        return removed is not None

    async def list_guilds(self) -> list[dict[str, Any]]:
        pool = self.require_pool()
        rows = await pool.fetch(
            """
            SELECT guild_id, active, protection_owner_id, backup_channel_id,
                   alert_channel_id, backup_schedule, last_health_signature,
                   created_at, updated_at
            FROM guild_settings
            ORDER BY guild_id
            """
        )
        admin_rows = await pool.fetch(
            "SELECT guild_id, user_id FROM guild_admins ORDER BY guild_id, user_id"
        )
        admin_map: dict[int, list[int]] = {}
        for row in admin_rows:
            admin_map.setdefault(int(row["guild_id"]), []).append(int(row["user_id"]))

        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["created_at"] = _iso(item["created_at"])
            item["updated_at"] = _iso(item["updated_at"])
            item["admins"] = admin_map.get(int(item["guild_id"]), [])
            results.append(item)
        return results

    async def create_alarm(self, alarm: dict[str, Any]) -> None:
        pool = self.require_pool()
        item = dict(alarm)
        item.setdefault("updated_at", utc_iso())
        try:
            await pool.execute(
                """
                INSERT INTO alarms (
                    alarm_id, guild_id, offender_id, trigger_type, status,
                    triggered_at, updated_at, data
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                str(item["alarm_id"]),
                int(item["guild_id"]),
                int(item["offender_id"]),
                str(item.get("trigger_type") or "unknown"),
                str(item.get("status") or "unknown"),
                _as_datetime(item.get("triggered_at")),
                _as_datetime(item.get("updated_at")),
                item,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ValueError(f"Alarm ID already exists: {item['alarm_id']}") from exc

    async def get_alarm(self, alarm_id: str) -> dict[str, Any] | None:
        pool = self.require_pool()
        value = await pool.fetchval("SELECT data FROM alarms WHERE alarm_id = $1", alarm_id)
        return dict(value) if isinstance(value, dict) else None

    async def update_alarm(self, alarm_id: str, **changes: Any) -> dict[str, Any] | None:
        pool = self.require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchval(
                    "SELECT data FROM alarms WHERE alarm_id = $1 FOR UPDATE", alarm_id
                )
                if not isinstance(current, dict):
                    return None
                item = dict(current)
                item.update(changes)
                item["updated_at"] = utc_iso()
                await connection.execute(
                    """
                    UPDATE alarms
                    SET guild_id = $2,
                        offender_id = $3,
                        trigger_type = $4,
                        status = $5,
                        triggered_at = $6,
                        updated_at = NOW(),
                        data = $7
                    WHERE alarm_id = $1
                    """,
                    alarm_id,
                    int(item["guild_id"]),
                    int(item["offender_id"]),
                    str(item.get("trigger_type") or "unknown"),
                    str(item.get("status") or "unknown"),
                    _as_datetime(item.get("triggered_at")),
                    item,
                )
                return item

    async def list_alarms(self, guild_id: int, limit: int = 20) -> list[dict[str, Any]]:
        pool = self.require_pool()
        rows = await pool.fetch(
            """
            SELECT data
            FROM alarms
            WHERE guild_id = $1
            ORDER BY triggered_at DESC
            LIMIT $2
            """,
            guild_id,
            limit,
        )
        return [dict(row["data"]) for row in rows]

    async def alarm_id_exists(self, alarm_id: str) -> bool:
        pool = self.require_pool()
        return bool(await pool.fetchval("SELECT EXISTS(SELECT 1 FROM alarms WHERE alarm_id = $1)", alarm_id))

    @staticmethod
    def _backup_from_row(row: asyncpg.Record | None) -> dict[str, Any] | None:
        if row is None or not isinstance(row["data"], dict):
            return None
        item = dict(row["data"])
        item["snapshot"] = row["snapshot"]
        return item

    async def add_backup(self, backup: dict[str, Any]) -> None:
        pool = self.require_pool()
        item = dict(backup)
        snapshot = item.pop("snapshot", None)
        item.setdefault("updated_at", utc_iso())
        try:
            await pool.execute(
                """
                INSERT INTO backups (
                    backup_id, guild_id, backup_type, status, created_at,
                    updated_at, snapshot, data
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                str(item["backup_id"]),
                int(item["guild_id"]),
                str(item.get("backup_type") or "unknown"),
                str(item.get("status") or "unknown"),
                _as_datetime(item.get("created_at")),
                _as_datetime(item.get("updated_at")),
                snapshot,
                item,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ValueError(f"Backup ID already exists: {item['backup_id']}") from exc

    async def update_backup(self, backup_id: str, **changes: Any) -> dict[str, Any] | None:
        pool = self.require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchrow(
                    "SELECT data, snapshot FROM backups WHERE backup_id = $1 FOR UPDATE",
                    backup_id,
                )
                existing = self._backup_from_row(current)
                if existing is None:
                    return None
                existing.update(changes)
                existing["updated_at"] = utc_iso()
                snapshot = existing.pop("snapshot", None)
                await connection.execute(
                    """
                    UPDATE backups
                    SET guild_id = $2,
                        backup_type = $3,
                        status = $4,
                        updated_at = NOW(),
                        snapshot = $5,
                        data = $6
                    WHERE backup_id = $1
                    """,
                    backup_id,
                    int(existing["guild_id"]),
                    str(existing.get("backup_type") or "unknown"),
                    str(existing.get("status") or "unknown"),
                    snapshot,
                    existing,
                )
                existing["snapshot"] = snapshot
                return existing

    async def get_backup(self, backup_id: str) -> dict[str, Any] | None:
        pool = self.require_pool()
        row = await pool.fetchrow(
            "SELECT data, snapshot FROM backups WHERE backup_id = $1", backup_id
        )
        return self._backup_from_row(row)

    async def latest_backup(self, guild_id: int) -> dict[str, Any] | None:
        pool = self.require_pool()
        row = await pool.fetchrow(
            """
            SELECT data, snapshot
            FROM backups
            WHERE guild_id = $1
              AND backup_type IN ('manual', 'scheduled')
              AND status = 'complete'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            guild_id,
        )
        return self._backup_from_row(row)

    async def list_backups(self, guild_id: int, limit: int = 20) -> list[dict[str, Any]]:
        # History views do not need to load potentially large server snapshots.
        pool = self.require_pool()
        rows = await pool.fetch(
            """
            SELECT data
            FROM backups
            WHERE guild_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            guild_id,
            limit,
        )
        return [dict(row["data"]) for row in rows if isinstance(row["data"], dict)]

    async def create_restore_job(self, job: dict[str, Any]) -> None:
        pool = self.require_pool()
        item = dict(job)
        item.setdefault("updated_at", utc_iso())
        await pool.execute(
            """
            INSERT INTO restore_jobs (
                restore_id, guild_id, backup_id, alarm_id, status,
                created_at, updated_at, data
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            str(item["restore_id"]),
            int(item["guild_id"]),
            item.get("backup_id"),
            item.get("alarm_id"),
            str(item.get("status") or "unknown"),
            _as_datetime(item.get("started_at") or item.get("created_at")),
            _as_datetime(item.get("updated_at")),
            item,
        )

    async def update_restore_job(self, restore_id: str, **changes: Any) -> None:
        pool = self.require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchval(
                    "SELECT data FROM restore_jobs WHERE restore_id = $1 FOR UPDATE",
                    restore_id,
                )
                if not isinstance(current, dict):
                    return
                item = dict(current)
                item.update(changes)
                item["updated_at"] = utc_iso()
                await connection.execute(
                    """
                    UPDATE restore_jobs
                    SET status = $2, updated_at = NOW(), data = $3
                    WHERE restore_id = $1
                    """,
                    restore_id,
                    str(item.get("status") or "unknown"),
                    item,
                )

    async def claim_audit_entry(
        self,
        audit_id: int,
        guild_id: int,
        action_type: str,
    ) -> bool:
        """Atomically claim an audit entry so only one handler processes it."""
        pool = self.require_pool()
        claimed = await pool.fetchval(
            """
            INSERT INTO processed_audit_entries (audit_id, guild_id, action_type)
            VALUES ($1, $2, $3)
            ON CONFLICT (audit_id) DO NOTHING
            RETURNING audit_id
            """,
            audit_id,
            guild_id,
            action_type,
        )
        return claimed is not None

    async def cleanup_processed_audit_entries(self, retention_days: int = 30) -> int:
        pool = self.require_pool()
        result = await pool.execute(
            """
            DELETE FROM processed_audit_entries
            WHERE processed_at < NOW() - ($1::INT * INTERVAL '1 day')
            """,
            retention_days,
        )
        try:
            return int(result.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    async def append_configuration_event(self, event: dict[str, Any]) -> None:
        pool = self.require_pool()
        await pool.execute(
            """
            INSERT INTO configuration_events (
                guild_id, actor_id, event_type, event_data, created_at
            )
            VALUES ($1, $2, $3, $4, $5)
            """,
            int(event["guild_id"]),
            int(event["actor_id"]) if event.get("actor_id") is not None else None,
            str(event.get("action") or event.get("event_type") or "unknown"),
            event.get("details") or event.get("event_data") or {},
            _as_datetime(event.get("created_at")),
        )
