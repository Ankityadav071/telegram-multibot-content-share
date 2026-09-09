# Storage Recovery V9.6.1

- Polished Storage Recovery UI with clearer Primary/Backup hierarchy and compact action layout.
- Added recovery progress bar, processed count, copied/failed counters, and clearer completion summary.
- Reworked Add Recovery Channel instructions into a simple 3-step flow; no channel link is required.
- Kept runtime channel IDs persisted in SQLite; no manual config edits are required after migration.
- Important resilience fix: Storage Bot startup no longer blocks when one or both configured storage channels are inaccessible, so the recovery UI remains reachable during a channel-loss incident.
- Recovery remains scoped to the two-channel / single-channel-failure model: the surviving Backup is the recovery source.
