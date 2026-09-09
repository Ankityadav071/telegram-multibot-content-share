# V9.5.6 Direct Delivery / Failover

New catalogue and Mini App watch actions use direct `t.me/<healthy-delivery-bot>?start=...` links.
The VPS `/resolve`/`/watch` endpoints remain compatibility/diagnostic paths only.

When a Delivery Bot becomes unavailable, registered catalogue links are migrated in SQLite to another routable bot and the Catalogue Bot edits the saved Telegram button. If no replacement exists, links enter an `orphaned` state and are retried when a bot becomes healthy.
