# Video Vault V9.6.9 — Specific Video Delivery Repair

Adds an admin recovery tool for individual videos that fail delivery.

## Storage Bot
- `/repair <video_number>` or `/repair <video_id>`
- Storage menu: **🧰 Repair Specific Video**
- Tests the stored Primary / Backup / 3rd Content message references using a short probe copy to the admin chat, then deletes the probe message.
- Rebuilds only missing/broken copies from the first reachable source.
- Updates `primary_msg_id`, `backup_msg_id`, and `recovery_msg_id` in the existing catalog row.
- Does not delete the source media or reset the database.

## Delivery Bot
- A failed delivery now shows **🔄 Retry Delivery** for the same video.
- Retry re-enters the normal delivery path: Primary → Backup → 3rd Content.

This is a targeted repair tool; it does not guarantee success when Telegram itself rejects the media, the content is gone from all configured sources, or the bot lacks required channel permissions.
