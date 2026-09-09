"""Persistent direct-t.me link migration worker.

Catalogue links are stored with their owning Delivery Bot and Telegram
message_id. When that bot becomes unavailable, the link is reassigned to a
healthy pool member and the existing catalogue button is edited in-place.
"""
from __future__ import annotations
import logging
import os
from contextlib import contextmanager
from pathlib import Path
import threading
import db

log = logging.getLogger("delivery_link_migrator")

_MIGRATION_THREAD_LOCK = threading.RLock()
_MIGRATION_LOCK_PATH = Path(os.getenv(
    "VIDEO_VAULT_DELIVERY_MIGRATION_LOCK",
    str(Path(__file__).with_name("delivery_migration.lock")),
)).expanduser()

@contextmanager
def _migration_lock():
    """Serialize link ownership changes across runner/worker/catalog processes.

    SQLite protects individual transactions, but without a process-level lock
    two failure callbacks could both select different replacement bots for the
    same link set. The lock makes migration idempotent and deterministic enough
    under concurrent bot-failure signals.
    """
    with _MIGRATION_THREAD_LOCK:
        fh = None
        try:
            _MIGRATION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            fh = open(_MIGRATION_LOCK_PATH, "a+")
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            yield
        finally:
            if fh is not None:
                try:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass


def _candidate_bots():
    try:
        import permanent_bot_store
        return permanent_bot_store.routable_delivery_bots(180)
    except Exception:
        return []


def migrate_bot_links(bot_id: str, reason: str = "bot unavailable") -> dict:
    with _migration_lock():
        candidates = _candidate_bots()
        result = db.create_delivery_migrations_for_bot(str(bot_id), candidates)
        if result.get("found"):
            log.warning(
                "Delivery link migration bot=%s reason=%s found=%s migrated=%s pending=%s orphaned=%s",
                bot_id, reason, result.get("found"), result.get("migrated"),
                result.get("pending"), result.get("orphaned", 0),
            )
        return result


def retry_orphaned_links(limit: int = 500) -> dict:
    with _migration_lock():
        candidates = _candidate_bots()
        try:
            return db.retry_orphaned_delivery_links(candidates, limit=limit)
        except Exception:
            log.exception("Orphaned link retry failed")
            return {"found": 0, "migrated": 0, "pending": 0}


def audit_unroutable_link_owners() -> dict:
    """Find active link rows whose owning bot was deleted/disabled/quarantined.

    This is a safety-net for upgrades, manual file edits, or an event that was
    missed while the server was restarting. It deliberately does NOT migrate
    healthy/degraded bots, only owners that are no longer valid delivery targets.
    """
    try:
        import permanent_bot_store
        rows = permanent_bot_store.list_bots()
        by_id = {str(r.get("id")): r for r in rows}
        owners = db.get_delivery_link_owner_ids()
        affected = 0
        migrated = 0
        for owner in owners:
            row = by_id.get(str(owner))
            invalid = row is None or (
                not row.get("enabled") or row.get("retired") or
                str(row.get("health") or "") in {"quarantined", "disabled"}
            )
            if not invalid:
                continue
            result = migrate_bot_links(str(owner), reason="periodic owner audit")
            affected += int(result.get("found") or 0)
            migrated += int(result.get("migrated") or 0)
        return {"owners": len(owners), "affected": affected, "migrated": migrated}
    except Exception:
        log.exception("Delivery link owner audit failed")
        return {"owners": 0, "affected": 0, "migrated": 0}


async def apply_pending_catalog_updates(bot, limit: int = 100) -> dict:
    """Apply pending catalogue button edits. Transient Telegram failures stay pending."""
    retry_orphaned_links(limit=max(100, int(limit)))
    rows = db.get_pending_delivery_link_updates(limit)
    done = stale = failed = 0
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden, BadRequest
    for row in rows:
        chat_id, message_id, url = row.get("chat_id"), row.get("message_id"), row.get("delivery_url")
        if chat_id is None or message_id is None or not url:
            db.mark_delivery_link_stale(row["id"]); stale += 1; continue
        label = str(row.get("button_label") or "🔥 Watch Now")
        try:
            await bot.edit_message_reply_markup(
                chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)]]),
            )
            db.mark_delivery_link_active(row["id"]); done += 1
        except RetryAfter as exc:
            failed += 1
            log.warning("Migration rate limited link=%s retry_after=%s", row.get("id"), getattr(exc, "retry_after", "?"))
        except (TimedOut, NetworkError) as exc:
            failed += 1
            log.warning("Transient migration error link=%s: %s", row.get("id"), exc)
        except Forbidden as exc:
            # Catalogue bot lost permission / chat access. Keep the row pending;
            # it may become editable again after permissions are restored.
            failed += 1
            db.update_delivery_link(row["id"], migration_error=str(exc)[:500])
            log.warning("Catalogue edit forbidden link=%s: %s", row.get("id"), exc)
        except BadRequest as exc:
            # Telegram returns HTTP 400 for several *permanent* message-state
            # failures. In particular, deleted catalogue messages produce
            # "Message to edit not found". Retrying these forever only creates
            # API traffic/log spam and can make the migrator look unhealthy.
            text = str(exc).lower()
            if "message is not modified" in text:
                db.mark_delivery_link_active(row["id"]); done += 1
            elif any(t in text for t in (
                "message to edit not found", "message not found",
                "message_id_invalid", "message can't be edited",
                "message can\'t be edited", "chat not found",
                "message identifier is not specified",
            )):
                db.mark_delivery_link_stale(row["id"])
                stale += 1
                log.info("Marked stale catalogue link=%s after Telegram 400: %s", row.get("id"), exc)
            else:
                failed += 1
                db.update_delivery_link(row["id"], migration_error=str(exc)[:500])
                log.warning("Catalogue BadRequest link=%s: %s", row.get("id"), exc)
        except Exception as exc:
            # Keep a defensive text-based classifier for Telegram wrappers or
            # future PTB exception subclasses that expose the same messages.
            text = str(exc).lower()
            if "message is not modified" in text:
                db.mark_delivery_link_active(row["id"]); done += 1
            elif any(t in text for t in (
                "message to edit not found", "message not found",
                "message_id_invalid", "message can't be edited",
                "message can\'t be edited", "chat not found",
            )):
                db.mark_delivery_link_stale(row["id"]); stale += 1
                log.info("Marked stale catalogue link=%s after edit failure: %s", row.get("id"), exc)
            else:
                failed += 1
                db.update_delivery_link(row["id"], migration_error=str(exc)[:500])
                log.warning("Catalogue migration failed link=%s: %s", row.get("id"), exc)
    return {"found": len(rows), "updated": done, "stale": stale, "failed": failed}
