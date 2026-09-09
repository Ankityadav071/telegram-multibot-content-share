# Temporary Bot Pool — V9.5.0

Per-bot configuration is now supported without changing the lightweight role of temporary bots.

## Per-bot overrides
Each temporary bot can independently override:
- Join channel name + URL
- Join button label
- `/start` message
- `/start` image

If an override is not set, the bot falls back field-by-field to the pool default. `Use Pool Defaults` resets all overrides for that bot.

## Files
Per-bot images are stored under `assets/temp_bots/<bot_id>.jpg`. Removing a bot or resetting its custom image cleans up that file when it belongs to the temp-bot asset directory.

## Runtime
Workers load the effective bot configuration on every `/start`, so setting changes take effect without restarting the temporary bot worker.
