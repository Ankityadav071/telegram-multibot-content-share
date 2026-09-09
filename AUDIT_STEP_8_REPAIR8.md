# Video Vault V9.7.4 — Audit Step 8 / Repair 8

## Scope
Background jobs, scheduler, startup/restart recovery, auto-delete, permanent delivery runner, and cross-process lifecycle.

## Findings
- `bgtasks.spawn()` is used for fire-and-forget asyncio tasks; no direct `asyncio.create_task()` callers remain outside the helper.
- Catalog and Delivery scheduled deletes are persisted in SQLite and resumed at startup. Malformed persisted timestamps are safely removed rather than crashing startup.
- Storage recovery uses persisted per-job/per-item checkpoints and rejects a second live worker for the same recovery job.
- Permanent Delivery Runner has a single-instance lock, worker reconciliation, token-change detection, disabled/retired worker shutdown, crash backoff, and periodic link-owner audit.
- Main supervisor has a single-instance lock, staggered startup, memory-pressure delay, restart backoff, and graceful shutdown.
- Admin scheduler jobs are individually exception-isolated and persist their own fire/run markers.

## Repair
The Admin scheduler previously waited a full 60 seconds before its first persisted-state pass after startup. It now performs one immediate pass, then continues at 60-second intervals. All jobs remain persisted/idempotent, so this does not create duplicate scheduled operations.

## Regression checks
- Python compile: PASS
- Duplicate top-level function scan: PASS
- Resolver/random Delivery Bot selection: unchanged
- Storage replacement/mapping logic: unchanged
- No destructive DB reset or schema migration introduced
