# Anti Defacement Discord Bot

A multi-server Discord bot that detects rapid destructive moderation actions, removes all roles it can manage from the actor, creates a unique alarm, preserves recovery evidence, and notifies trusted operators. It never kicks or bans anyone.

## Fixed triggers

- 3 channel deletions in 15 seconds
- 3 role deletions in 15 seconds
- 3 bans in 20 seconds
- 5 kicks in 20 seconds
- 5 combined destructive actions in 20 seconds

Each actor has independent rolling counters. One alarm does not disable or pause protection. Multiple simultaneous offenders are processed independently.

## Durable PostgreSQL storage

PostgreSQL is authoritative for:

- Per-server activation state
- Protection owners and administrators
- Alert and backup channel settings
- Backup schedules
- Unique alarms and offender role snapshots
- Audit-log entry claims
- Configuration-change history
- Full server backup snapshots
- Restoration jobs and results

The bot automatically applies numbered SQL migrations from `migrations/` when it starts. Excel backup files and text alarm logs are temporary Discord exports generated from PostgreSQL data and deleted after they are sent. A Railway persistent volume is not required for the bot's critical state.

## Project files

- `main.py` — bot startup, PostgreSQL connection, migrations, and slash-command sync
- `storage.py` — asynchronous PostgreSQL repository using `asyncpg`
- `migrations/001_initial.sql` — initial database schema
- `database_check.py` — verifies the connection, applies migrations, and lists tables
- `protection.py` — audit-log detection, counters, containment, alarms, and health checks
- `commands.py` — `/antidefacement` commands and backup scheduler
- `backup_service.py` — PostgreSQL snapshots and temporary Excel exports
- `restore_service.py` — retained as dormant code for possible future recovery tooling; not loaded by the bot
- `serializers.py` — Discord object snapshots
- `notifier.py` — DM and alert-channel delivery
- `permissions.py` — database-backed command authorization

## Discord Developer Portal setup

1. Create a Discord application and bot.
2. In **Bot → Privileged Gateway Intents**, enable **Server Members Intent**.
3. Reset/copy the bot token and store it only as Railway's `DISCORD_TOKEN` variable.
4. Install the bot with the `bot` and `applications.commands` scopes.
5. Give the bot:
   - View Audit Log
   - Manage Roles
   - Manage Channels
   - View Channels
   - Send Messages
   - Attach Files
   - Read Message History
6. Put the bot role above moderator or administrator roles whenever possible. Protection can still be activated when higher roles exist, but Discord will prevent the bot from removing roles at or above its own highest role.

The bot intentionally does not need Kick Members or Ban Members. It observes those actions through the audit log but never performs them.

## Railway deployment

### 1. Create the bot service

1. Put this folder in a GitHub repository.
2. Create a Railway project.
3. Choose **Deploy from GitHub repo** and select the repository.

### 2. Add PostgreSQL

1. On the Railway project canvas, click **New**.
2. Select **Database → PostgreSQL**.
3. Keep the PostgreSQL service in the same Railway project/environment as the bot.

### 3. Set bot-service variables

Open the bot service, go to **Variables**, and add:

```text
DISCORD_TOKEN=your_real_discord_bot_token
DATABASE_URL=${{Postgres.DATABASE_URL}}
DEV_GUILD_ID=your_test_server_id
```

If your database service is not named `Postgres`, replace `Postgres` with its exact Railway service name. `DEV_GUILD_ID` is optional; omit it or set it to `0` after development.

Do not create a second manually copied database password when a Railway reference variable can be used.

### 4. Deploy

Railway reads `railway.json`, uses Railpack, runs a pre-deploy database check/migration, and then starts the service with:

```text
python main.py
```

Before startup, Railway runs `python database_check.py`, which verifies PostgreSQL and applies migrations. The bot then:

1. Connects to `DATABASE_URL`
2. Creates a small async PostgreSQL connection pool
3. Re-checks migrations safely
4. Registers the protection and command cogs
5. Syncs slash commands
6. Connects to Discord


### 4a. Keep one bot replica

Keep the bot service at **one replica**. The database prevents duplicate audit-entry processing, but the 15–20 second rolling action counters are intentionally held in the bot process for immediate response. Multiple independent replicas could split those counters.

### 5. Verify PostgreSQL before Discord testing

From a local terminal with the Railway CLI linked to the bot service, you can run:

```bash
railway run python database_check.py
```

Or temporarily change the Railway start command to `python database_check.py`, deploy once, inspect the logs, and change it back to `python main.py`.

A successful check lists tables including:

```text
guild_settings
guild_admins
alarms
backups
restore_jobs
processed_audit_entries
configuration_events
schema_migrations
```

## Local setup

1. Install Python 3.12 or newer.
2. Create a PostgreSQL database.
3. Copy `.env.example` to `.env`.
4. Set `DISCORD_TOKEN`, `DATABASE_URL`, and optionally `DEV_GUILD_ID`.
5. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

6. Verify the database:

```bash
python database_check.py
```

7. Start the bot:

```bash
python main.py
```

## Initial setup in each Discord server

1. The actual server owner or global bot owner runs `/antidefacement setowner`.
2. The protection owner, actual server owner, or global bot owner runs `/antidefacement setadmin` as needed.
3. Set `/antidefacement setalertchannel` and `/antidefacement setbackupchannel`.
4. Run `/antidefacement checkpermissions`.
5. Run `/antidefacement backup`.
6. Run `/antidefacement activate`.
7. Run `/antidefacement test` and confirm the expected DMs and channel alert arrive.

Discord server Administrator permission by itself does not authorize bot commands. Runtime authorization uses only the global bot owner, configured protection owner, and configured Anti Defacement administrators. The actual Discord server owner is specifically allowed to set the protection owner and manage the administrator list.

## Commands

- `/antidefacement setowner`
- `/antidefacement setadmin`
- `/antidefacement removeadmin`
- `/antidefacement listadmins`
- `/antidefacement owner`
- `/antidefacement activate`
- `/antidefacement deactivate`
- `/antidefacement falsealarm`
- `/antidefacement acknowledge`
- `/antidefacement alarms`
- `/antidefacement alarm`
- `/antidefacement test`
- `/antidefacement checkpermissions`
- `/antidefacement status`
- `/antidefacement settings`
- `/antidefacement setalertchannel`
- `/antidefacement setbackupchannel`
- `/antidefacement backup`
- `/antidefacement schedulebackup`
- `/antidefacement cancelschedule`
- `/antidefacement backuphistory`

## Restoration limits

- Recreated channels and roles receive new Discord IDs.
- The bot maps old IDs to new IDs while restoring overwrites and member-role assignments.
- Deleted message history cannot be recreated.
- Kicked users cannot be forced back into a server.
- The bot records bans but does not automatically unban accounts.
- Managed integration roles and roles above the bot cannot normally be removed or recreated.
- The actual server owner cannot be modified by a bot.
- PostgreSQL is the authoritative structured backup and recovery-log source; Discord receives human-readable Excel and text exports only.

## Security behavior

Configured protection administrators and the configured protection owner are exempt from automatic role removal, as requested. This means a compromised exempt account cannot be contained by this bot. Configuration changes are recorded in PostgreSQL and major changes are broadcast to trusted operators.

## Adding future schema changes

Never edit a migration that has already run in production. Add a new numbered file, for example:

```text
migrations/002_add_backup_retention.sql
```

The bot will apply it once at the next startup and record it in `schema_migrations`.

## Notification troubleshooting

Run `/antidefacement test` after configuring the owner/admin list. The command now reports a delivery result for every DM recipient and the alert channel.

- `message_sent; no_attachments` means the test DM arrived.
- `dm_forbidden_or_closed` means the user must allow direct messages from server members or unblock the bot.
- `user_not_found` means the configured Discord user ID is not available to the bot.
- `dm_failed: ...` indicates a Discord API failure; inspect the Railway deployment logs for the full recipient ID and error.

Alarm notifications send the urgent text first and recovery attachments in a second message. An attachment failure can no longer suppress the primary alarm notification. Configure `/antidefacement setalertchannel` as a server-side fallback because Discord users can disable DMs.

## Higher-role behavior

The bot no longer blocks activation or sends health warnings merely because some dangerous roles are above its role. Discord role hierarchy still applies: the bot can remove only roles below its highest role. A user whose higher role retains destructive permissions may therefore not be fully contained.
