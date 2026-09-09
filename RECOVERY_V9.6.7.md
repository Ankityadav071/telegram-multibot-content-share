# V9.6.7 — 3rd Content Source + Schedule/Catalog Fixes

- Recovery channel is now an optional permanent 3rd content source and is never silently promoted over the hardcore Primary/Backup pair.
- Full migration scans every catalog item and attempts Backup first, then Primary per video, repairing stale Backup references where Primary still has the media.
- Existing successful recovery checkpoints are linked into `videos.recovery_msg_id` without recopying.
- New single and bulk saves mirror media to the configured 3rd channel when available and persist its message ID.
- Delivery fallback is per-video: Primary → Backup → 3rd Content.
- Permanent Delivery preflight also checks the runtime 3rd content channel.
- Recovery progress shows Pending separately from Failed; failed items remain retryable with Resume.
- Recovery audit includes Catalogue, hardcore Delivery, and every active Permanent Delivery Bot.
- Admin Video Management shows scheduled collection times and collection-level Reschedule / Publish Now / Cancel controls.
- Catalogue audience lists hide future scheduled content. Top/Random keep a 5+ page size with navigation, and result screens remove the reply keyboard to stop accidental keyboard popups.
