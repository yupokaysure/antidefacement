from __future__ import annotations

import os
import tempfile
from pathlib import Path


GLOBAL_BOT_OWNER_ID = 99631342559432704

BASE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = BASE_DIR / "migrations"
TEMP_DIR = Path(
    os.getenv(
        "TEMP_DIR",
        str(Path(tempfile.gettempdir()) / "anti_defacement_bot"),
    )
)
BACKUP_DIR = TEMP_DIR / "backups"
ALARM_DIR = TEMP_DIR / "alarms"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0") or 0)

# The limits agreed on during planning.
THRESHOLDS: dict[str, dict[str, int]] = {
    "channel_delete": {"amount": 3, "seconds": 15},
    "role_delete": {"amount": 3, "seconds": 15},
    "ban": {"amount": 3, "seconds": 20},
    "kick": {"amount": 5, "seconds": 20},
    "combined": {"amount": 5, "seconds": 20},
}

MAX_RECENT_ALARMS_SHOWN = 20
SCHEDULER_INTERVAL_SECONDS = 60
HEALTH_CHECK_INTERVAL_SECONDS = 300
PENDING_SNAPSHOT_TTL_SECONDS = 60

DEFAULT_TIMEZONE = "America/New_York"
