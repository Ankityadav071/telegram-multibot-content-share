# Storage Recovery V9.6.5

Final recovery hardening:

- Recovery/standby channel ID is persisted in SQLite, so it survives bot restarts.
- A saved standby can be verified or started without re-entering its channel ID.
- Source Backup is verified before a migration begins.
- Recovery copies the media message and, when available, the Storage Bot's stored cover file ID into the destination.
- Per-item destination media/cover message IDs are checkpointed before live catalog cutover.
- Existing live `primary_msg_id` / `cover_msg_id` values are not changed while recovery is paused or stopped.
- On full success, mappings are atomically applied, destination becomes Primary, surviving source remains Backup, and the standby pointer is cleared.
- Missing Catalogue/Delivery admin access blocks final cutover rather than leaving a half-integrated Primary.
- Pause, Stop, Resume, Cancel and crash-safe checkpoints are preserved.
