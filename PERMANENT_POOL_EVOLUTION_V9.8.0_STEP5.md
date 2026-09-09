# V9.8.0 Step 5 — Permanent Delivery Bot Pool Evolution

- Added resolver selection telemetry (`total_attempts`, `last_selection_at`) without changing bot selection/routing logic.
- Added explicit health snapshot flags for stale heartbeat and active cooldown.
- Added watchdog restart protection for live-but-stale permanent workers (180s heartbeat timeout).
- Watchdog restarts use a short cooldown and preserve quarantine/disabled semantics.
- Added watchdog restart counters for Admin/ops visibility.
- Existing healthy-first / degraded-fallback resolver policy remains unchanged.
- Existing direct t.me links and migration behavior remain unchanged.
