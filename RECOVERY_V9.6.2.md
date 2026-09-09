# Storage Recovery V9.6.2

- Added persistent, per-video recovery checkpoints in SQLite.
- Added Pause, Stop, Resume and Cancel controls.
- Pause/Stop are cooperative: the current Telegram copy is allowed to finish, then the job stops at the next safe checkpoint.
- Resume retries only pending/failed items and never recopies an item whose checkpoint is already marked copied.
- Cancel preserves already-copied media and the catalogue; it only abandons the active recovery session.
- Recovery state survives Storage Bot restarts. A job left in `running` state after a process interruption is automatically converted to `paused` so it can be resumed.
- The recovered channel is promoted to Primary only after all catalogued backup items have been copied successfully.
