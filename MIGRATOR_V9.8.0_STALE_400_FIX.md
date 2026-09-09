# V9.8.0 — Delivery Link Migrator stale-message fix

## Fix
Telegram HTTP 400 errors such as `Message to edit not found` are permanent for a catalogue message that has already been deleted or is no longer editable. The migrator now catches `telegram.error.BadRequest` explicitly and marks these link rows `stale` instead of retrying them on every migration cycle.

## Result
- Deleted/missing catalogue messages stop generating repeated migration attempts.
- Log spam is reduced from warning-level retries to a single informational stale transition.
- Genuine rate limits, network timeouts, and temporary network errors remain retryable.
- Resolver routing and Delivery Bot selection are unchanged.
