# Current changes

- Disabled `/antidefacement restore` and `/antidefacement restorealarm`.
- Kept `/antidefacement falsealarm`, including alarm-specific offender role restoration.
- Kept backups, scheduled backups, spreadsheets, alarms, and text recovery logs.
- Removed `Manage Channels` from the required health-check permissions because the bot no longer performs automatic channel restoration.
- Left the existing PostgreSQL restore tables and dormant restore-service module in place for schema compatibility. No SQL migration or database reset is required.

- Removed JSON attachments from alarm and backup Discord messages; structured data remains stored in PostgreSQL.
- Standardized all user-facing branding to “Anti Defacement” without a dash.
