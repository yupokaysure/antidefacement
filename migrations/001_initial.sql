CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    protection_owner_id BIGINT,
    backup_channel_id BIGINT,
    alert_channel_id BIGINT,
    backup_schedule JSONB,
    last_health_signature TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS guild_admins (
    guild_id BIGINT NOT NULL REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    is_exempt BOOLEAN NOT NULL DEFAULT TRUE,
    added_by BIGINT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS alarms (
    alarm_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    offender_id BIGINT NOT NULL,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS alarms_guild_triggered_idx
    ON alarms (guild_id, triggered_at DESC);
CREATE INDEX IF NOT EXISTS alarms_offender_triggered_idx
    ON alarms (guild_id, offender_id, triggered_at DESC);
CREATE INDEX IF NOT EXISTS alarms_status_idx
    ON alarms (status);

CREATE TABLE IF NOT EXISTS backups (
    backup_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    backup_type TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    snapshot JSONB,
    data JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS backups_guild_created_idx
    ON backups (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS backups_status_idx
    ON backups (status);

CREATE TABLE IF NOT EXISTS restore_jobs (
    restore_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    backup_id TEXT,
    alarm_id TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS restore_jobs_guild_created_idx
    ON restore_jobs (guild_id, created_at DESC);

CREATE TABLE IF NOT EXISTS processed_audit_entries (
    audit_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    action_type TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS processed_audit_entries_time_idx
    ON processed_audit_entries (processed_at);

CREATE TABLE IF NOT EXISTS configuration_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    actor_id BIGINT,
    event_type TEXT NOT NULL,
    event_data JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS configuration_events_guild_created_idx
    ON configuration_events (guild_id, created_at DESC);
