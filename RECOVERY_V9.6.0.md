# Storage Recovery V9.6.0

- Added persistent runtime Primary/Backup storage channel settings in SQLite.
- Added Storage Recovery UI to the Storage Bot.
- Admin can enter a new private channel numeric ID; no hard-coded channel link is required.
- Verifies destination channel and bot administrator access before migration.
- Migrates all videos with existing backup message references into the new channel.
- Preserves stable video IDs and catalogue metadata; updates only Primary message references.
- Activates the recovered channel as Primary only after the migration pass starts successfully.
- Existing Backup channel remains configured as Backup.
- Updated Storage, Delivery, Catalog and Permanent Delivery code paths to read runtime channel settings so future operations follow the recovered Primary automatically.
- No media backup outside Telegram was added; this covers the two-channel / single-channel-failure recovery case discussed.
