# V9.7.4 Audit Repair 5 — Steps 8/9 hardening

## Findings repaired
- Cross-process daily quota race: `register_watch()` used a Python read/modify/write sequence. With multiple permanent Delivery Bot worker processes, two simultaneous deliveries could both pass `can_watch()` and then increment the same quota beyond the limit. Replaced with a conditional SQLite UPDATE that performs the date rollover and limit gate atomically.
- Single-delivery active-job guard was released immediately after Telegram accepted the media but before quota registration/bookkeeping. A second permanent Delivery Bot process could therefore race through the pre-check during that window. Guard now remains until all post-send accounting is complete.

## Preserved
- Resolver and random healthy Delivery Bot selection unchanged.
- Failed Telegram delivery still does not consume a watch.
- Premium/ad-member unlimited behavior unchanged.
- Replacement/single/bulk isolation unchanged.
- No destructive DB reset or schema rewrite.
