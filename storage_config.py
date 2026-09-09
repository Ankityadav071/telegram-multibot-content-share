"""Persistent runtime storage-channel configuration.

Keeps the two Telegram storage channel IDs in the shared SQLite settings table so
an admin can migrate storage without editing environment/config files. All bot
processes read the current values on demand, so a recovered channel becomes
active without a restart. Catalogue/Delivery/Admin use these runtime pointers as
the shared integration point after recovery.
"""
import config
import db

PRIMARY_KEY = "storage_primary_channel_id"
BACKUP_KEY = "storage_backup_channel_id"
RECOVERY_KEY = "storage_recovery_channel_id"


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def primary() -> int:
    return _to_int(db.get_setting(PRIMARY_KEY, getattr(config, "PRIMARY_CHANNEL_ID", 0)))


def backup() -> int:
    return _to_int(db.get_setting(BACKUP_KEY, getattr(config, "BACKUP_CHANNEL_ID", 0)))


def set_primary(chat_id: int) -> int:
    cid = _to_int(chat_id)
    if not cid:
        raise ValueError("invalid primary channel id")
    db.set_setting(PRIMARY_KEY, str(cid))
    return cid


def set_backup(chat_id: int) -> int:
    cid = _to_int(chat_id)
    if not cid:
        raise ValueError("invalid backup channel id")
    db.set_setting(BACKUP_KEY, str(cid))
    return cid


def recovery() -> int:
    return _to_int(db.get_setting(RECOVERY_KEY, 0))


def set_recovery(chat_id: int) -> int:
    cid = _to_int(chat_id)
    if not cid:
        raise ValueError("invalid recovery channel id")
    db.set_setting(RECOVERY_KEY, str(cid))
    return cid


def clear_recovery():
    db.set_setting(RECOVERY_KEY, "")


def ensure_seeded():
    """Seed runtime settings from the current config exactly once."""
    if db.get_setting(PRIMARY_KEY) is None and getattr(config, "PRIMARY_CHANNEL_ID", 0):
        db.set_setting(PRIMARY_KEY, str(int(config.PRIMARY_CHANNEL_ID)))
    if db.get_setting(BACKUP_KEY) is None and getattr(config, "BACKUP_CHANNEL_ID", 0):
        db.set_setting(BACKUP_KEY, str(int(config.BACKUP_CHANNEL_ID)))


def channels() -> dict:
    ensure_seeded()
    return {"primary": primary(), "backup": backup(), "recovery": recovery()}
