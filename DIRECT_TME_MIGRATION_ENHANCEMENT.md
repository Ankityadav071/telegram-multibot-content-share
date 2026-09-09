# V9.5.6 Direct t.me Link Migration

Catalogue messages register the selected permanent Delivery Bot, direct URL, and Telegram message id. On bot failure/disable/retire/delete/quarantine, the registry is migrated to another healthy/routable bot and the existing button is edited. No media is re-uploaded.

If the pool is empty, affected links are marked `orphaned` and automatically retried when the next Delivery Bot becomes healthy.
