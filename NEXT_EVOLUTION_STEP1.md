# Video Vault Next Evolution — Step 1 Architecture & Dependency Audit

Baseline: **V9.7.4 AUDIT-FINAL**
Scope: Telegram Video Vault only. GOD-MAX is explicitly out of scope.

## 1. Architecture map

- `start.py` — VPS supervisor / staggered process launcher.
- `admin_bot.py` — admin control plane, scheduler, health, stats, codes, limits, recovery controls.
- `storage_bot.py` — content intake, single/bulk upload, metadata, covers, scheduling, recovery and replacement.
- `catalog_bot.py` — audience catalogue, persistent menu, search/browse/categories/tags, Mini App entry point.
- `delivery_bot.py` — access gate, quota/redeem/ad unlock, actual video delivery, auto-delete/watch-again.
- `permanent_delivery_runner.py` + `permanent_delivery_worker.py` — persistent Delivery Bot pool.
- `permanent_delivery_redirect_worker.py` + `delivery_link_migrator.py` — orphan/dead-link migration.
- `permanent_bot_store.py` — persistent Delivery Bot registry/health/routing state.
- `temp_bot_*` — temporary Delivery Bot lifecycle.
- `db.py` — shared SQLite state and additive migrations.
- `bgtasks.py` — strong-reference background task lifecycle.
- `botutil.py` / `bot_ux.py` / `boterror.py` — shared helpers and UX/error infrastructure.

## 2. Static audit result

- 22 Python modules compile successfully with `python -m compileall`.
- 49 Admin commands, 11 Catalogue commands, 23 Storage commands and 7 Delivery commands were found; no duplicate command names in the audited source.
- Cross-module imports resolve to the expected local modules.
- Existing resolver routing was deliberately left unchanged.
- Replacement/rebuild mapping remains separated from normal Single/Bulk upload state.
- Database mapping writes use dedicated mapping functions rather than generic metadata editing.
- No destructive database reset was introduced.

## 3. Dependency/state boundaries

### Content lifecycle
`Storage -> SQLite mapping -> Catalogue presentation -> Delivery Bot resolution -> Telegram media copy/send`

### Delivery Bot lifecycle
`Permanent store -> runner/worker -> routable pool -> resolver -> Delivery Bot -> fallback sources`

### Recovery lifecycle
`Storage Recovery job -> persisted item checkpoints -> Backup/Primary copy -> optional 3rd Content source`

### Background lifecycle
`start.py -> resilient polling / runners -> persisted state -> restart recovery`

## 4. Main improvement opportunities for Steps 2–10

1. **Storage:** make health/integrity information more actionable without automatically mutating media; improve operation summaries and repair discoverability.
2. **Catalogue:** reduce navigation friction while preserving the persistent keyboard + inline Back to Menu coexistence; improve search/filter discovery.
3. **Delivery:** improve user-facing failure/retry messaging and observability without changing healthy-bot random routing.
4. **Permanent pool:** clearer degraded/quarantined state and operator diagnostics; preserve old links where intentionally supported.
5. **Admin:** consolidate high-value health signals into a faster control-center view; avoid duplicate command paths.
6. **VPS API / Mini App:** keep API contracts backward-compatible; improve loading/error/empty states and navigation rather than replacing working routes.
7. **Advanced features:** prioritize low-risk discovery features (favorites/history/continue/recommendations/collections) only where they can be added with additive schema changes.
8. **Performance/security:** bounded in-memory caches, SQLite indexes where they materially help, safer logging, input limits and better task cleanup.
9. **Observability:** structured operational events for delivery, replacement, recovery and migration failures.
10. **Release:** compile + static scans + targeted DB tests + regression checklist before packaging.

## 5. Explicit preservation rules

- Do not change the working random healthy Delivery Bot resolver behavior unless a proven bug requires it.
- Do not change catalogue/watch identity when replacing storage media.
- Do not mix Replacement state with Single/Bulk upload state.
- Do not reset or recreate the production database.
- Do not commit secrets, tokens, runtime DB files, bot registries or lock files.
- Do not mix this project with GOD-MAX.

## 6. Step 1 status

**COMPLETE.** The codebase is structurally coherent enough to proceed to feature-level polish. The next implementation work should favor small additive changes over broad refactors.
