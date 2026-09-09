"""Persistent store for Admin-managed temporary bots.

Kept outside the catalog DB so normal DB backups never contain bot tokens.
The JSON file is chmod 600, writes are atomic, and a lock file prevents
admin/worker processes from trampling one another's updates.
"""
from __future__ import annotations
import copy
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

STORE_PATH = Path(__file__).with_name("temp_bots.json")
LOCK_PATH = Path(__file__).with_name("temp_bots.lock")
ASSET_DIR = Path(__file__).with_name("assets") / "temp_bots"


def _now():
    return datetime.now().astimezone().isoformat()


def _secure():
    try:
        STORE_PATH.touch(exist_ok=True)
        os.chmod(STORE_PATH, 0o600)
        LOCK_PATH.touch(exist_ok=True)
        os.chmod(LOCK_PATH, 0o600)
    except Exception:
        pass

@contextmanager
def _lock(exclusive=False):
    _secure()
    fh = open(LOCK_PATH, "r+", encoding="utf-8")
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except Exception:
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def _blank():
    return {
        "bots": [],
        "channel": {},
        "message": {
            "button_text": "📢 Join Main Channel",
            "text": "✨ Welcome!\n\nTap below to join our Main Channel.",
            "image_path": "",
        },
        "settings": {"max_failures_before_quarantine": 5},
    }


def _read_unlocked():
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _blank()
        data.setdefault("bots", [])
        data.setdefault("channel", {})
        data.setdefault("message", {})
        data.setdefault("settings", {"max_failures_before_quarantine": 5})
        return data
    except Exception:
        return _blank()


def _write_unlocked(data):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".temp_bots_", suffix=".json", dir=str(STORE_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, STORE_PATH)
        _secure()
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def list_bots():
    with _lock(False):
        return copy.deepcopy(_read_unlocked()["bots"])


def get_bot(bot_id: str):
    with _lock(False):
        for row in _read_unlocked()["bots"]:
            if row.get("id") == str(bot_id):
                return copy.deepcopy(row)
    return None


def add_bot(*, bot_id: str, username: str, first_name: str, token: str, enabled: bool = True):
    with _lock(True):
        data = _read_unlocked()
        for row in data["bots"]:
            if row.get("id") == str(bot_id) or row.get("token") == token:
                return False
        data["bots"].append({
            "id": str(bot_id), "username": username or "", "first_name": first_name or "",
            "token": token, "enabled": bool(enabled), "created_at": _now(),
            "last_started_at": None, "last_seen_at": None, "last_error": None,
            "last_error_at": None, "last_success_at": None, "last_failure_at": None,
            "consecutive_failures": 0, "start_count": 0, "success_count": 0,
            "failure_count": 0, "status": "STARTING", "quarantined": False,
            "quarantine_reason": None,
            "config": {"channel": {}, "message": {}},
        })
        _write_unlocked(data)
        return True


def update_bot(bot_id: str, **fields):
    with _lock(True):
        data = _read_unlocked(); changed = False
        for row in data["bots"]:
            if row.get("id") == str(bot_id):
                for key, value in fields.items():
                    if key != "token": row[key] = value
                changed = True; break
        if changed: _write_unlocked(data)
        return changed


def remove_bot(bot_id: str):
    with _lock(True):
        data = _read_unlocked(); before = len(data["bots"])
        target = next((b for b in data["bots"] if str(b.get("id")) == str(bot_id)), None)
        data["bots"] = [b for b in data["bots"] if b.get("id") != str(bot_id)]
        if len(data["bots"]) == before: return False
        _write_unlocked(data)
    try:
        path = Path(str(((target or {}).get("config") or {}).get("message", {}).get("image_path") or ""))
        if path and path.is_file() and ASSET_DIR in path.parents:
            path.unlink(missing_ok=True)
    except Exception:
        pass
    return True


def set_enabled(bot_id: str, enabled: bool):
    return update_bot(
        bot_id, enabled=bool(enabled), last_error=None, last_error_at=None,
        status=("STARTING" if enabled else "DISABLED"),
        quarantined=False if enabled else False,
        quarantine_reason=None,
        consecutive_failures=0 if enabled else (get_bot(bot_id) or {}).get("consecutive_failures", 0),
    )


def replace_token(bot_id: str, token: str, username: str = "", first_name: str = ""):
    token = str(token or "").strip()
    with _lock(True):
        data = _read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) != str(bot_id) and row.get("token") == token:
                return False, "duplicate"
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                row.update({
                    "token": token, "username": username or row.get("username") or "",
                    "first_name": first_name or row.get("first_name") or "",
                    "enabled": True, "status": "STARTING", "quarantined": False,
                    "quarantine_reason": None, "last_error": None, "last_error_at": None,
                    "consecutive_failures": 0,
                })
                _write_unlocked(data)
                return True, "updated"
        return False, "not_found"


def mark_start(bot_id: str):
    row = get_bot(bot_id) or {}
    count = int(row.get("start_count") or 0) + 1
    return update_bot(bot_id, last_started_at=_now(), start_count=count, status="STARTING")


def mark_success(bot_id: str):
    now = _now()
    return update_bot(
        bot_id, last_seen_at=now, last_success_at=now, last_error=None,
        last_error_at=None, last_failure_at=None, consecutive_failures=0,
        status="HEALTHY", quarantined=False, quarantine_reason=None,
        success_count=int((get_bot(bot_id) or {}).get("success_count") or 0) + 1,
    )


def mark_failure(bot_id: str, error: str, *, hard: bool = False):
    row = get_bot(bot_id) or {}
    now = _now()
    streak = int(row.get("consecutive_failures") or 0) + 1
    total = int(row.get("failure_count") or 0) + 1
    status = "QUARANTINED" if hard else "DEGRADED"
    return update_bot(
        bot_id, last_error=str(error)[:700], last_error_at=now,
        last_failure_at=now, consecutive_failures=streak, failure_count=total,
        status=status, quarantined=bool(hard), quarantine_reason=(str(error)[:300] if hard else row.get("quarantine_reason")),
    )


def recover(bot_id: str):
    return update_bot(bot_id, enabled=True, status="STARTING", quarantined=False,
                      quarantine_reason=None, last_error=None, last_error_at=None,
                      consecutive_failures=0)


def health_snapshot(max_items: int = 100):
    rows = list_bots()[:max_items]
    now = datetime.now().astimezone()
    for row in rows:
        age = None
        raw = row.get("last_seen_at") or row.get("last_started_at")
        if raw:
            try:
                age = max(0, int((now - datetime.fromisoformat(str(raw))).total_seconds()))
            except Exception:
                pass
        row["age_seconds"] = age
    return rows


def token_for(bot_id: str):
    row = get_bot(bot_id); return row.get("token") if row else None


def get_channel(default_link: str = ""):
    with _lock(False):
        channel = _read_unlocked().get("channel") or {}
        return {"name": str(channel.get("name") or "Main Channel"), "link": str(channel.get("link") or default_link or "")}


def set_channel(name: str, link: str):
    with _lock(True):
        data = _read_unlocked(); data["channel"] = {"name": name.strip() or "Main Channel", "link": link.strip()}; _write_unlocked(data)


def get_message():
    with _lock(False):
        message = _read_unlocked().get("message") or {}
        return {
            "button_text": str(message.get("button_text") or "📢 Join Main Channel"),
            "text": str(message.get("text") or "✨ Welcome!\n\nTap below to join our Main Channel."),
            "image_path": str(message.get("image_path") or ""),
        }


def set_message(button_text: str):
    with _lock(True):
        data = _read_unlocked(); current = data.get("message") or {}
        data["message"] = {
            "button_text": button_text.strip() or "📢 Join Main Channel",
            "text": str(current.get("text") or "✨ Welcome!\n\nTap below to join our Main Channel."),
            "image_path": str(current.get("image_path") or ""),
        }; _write_unlocked(data)


def set_start_text(text: str):
    with _lock(True):
        data = _read_unlocked(); current = data.get("message") or {}
        data["message"] = {
            "button_text": str(current.get("button_text") or "📢 Join Main Channel"),
            "text": text.strip() or "✨ Welcome!\n\nTap below to join our Main Channel.",
            "image_path": str(current.get("image_path") or ""),
        }; _write_unlocked(data)


def set_start_image(path: str):
    with _lock(True):
        data = _read_unlocked(); current = data.get("message") or {}
        data["message"] = {
            "button_text": str(current.get("button_text") or "📢 Join Main Channel"),
            "text": str(current.get("text") or "✨ Welcome!\n\nTap below to join our Main Channel."),
            "image_path": str(path or ""),
        }; _write_unlocked(data)


def clear_start_image():
    set_start_image("")


def _bot_config(row):
    cfg = row.get("config") or {}
    cfg.setdefault("channel", {})
    cfg.setdefault("message", {})
    return cfg


def get_bot_config(bot_id: str, *, default_link: str = ""):
    """Return effective per-bot config, falling back to pool defaults per field."""
    with _lock(False):
        data = _read_unlocked()
        row = next((b for b in data["bots"] if str(b.get("id")) == str(bot_id)), None)
        pool_channel = data.get("channel") or {}
        pool_message = data.get("message") or {}
        cfg = _bot_config(row or {})
        bot_channel = cfg.get("channel") or {}
        bot_message = cfg.get("message") or {}
        return {
            "channel": {
                "name": str(bot_channel.get("name") or pool_channel.get("name") or "Main Channel"),
                "link": str(bot_channel.get("link") or pool_channel.get("link") or default_link or ""),
            },
            "message": {
                "button_text": str(bot_message.get("button_text") or pool_message.get("button_text") or "📢 Join Main Channel"),
                "text": str(bot_message.get("text") or pool_message.get("text") or "✨ Welcome!\n\nTap below to join our Main Channel."),
                "image_path": str(bot_message.get("image_path") or pool_message.get("image_path") or ""),
            },
            "overrides": {
                "channel": bool(bot_channel.get("link") or bot_channel.get("name")),
                "button": bool(bot_message.get("button_text")),
                "message": bool(bot_message.get("text")),
                "image": bool(bot_message.get("image_path")),
            },
        }


def set_bot_channel(bot_id: str, name: str, link: str):
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                cfg=_bot_config(row); cfg["channel"]={"name": name.strip() or "Main Channel", "link": link.strip()}
                row["config"]=cfg; _write_unlocked(data); return True
    return False


def clear_bot_channel(bot_id: str):
    return _set_bot_config(bot_id, "channel", {})


def set_bot_message(bot_id: str, *, button_text=None, text=None, image_path=None, clear_image=False):
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                cfg=_bot_config(row); msg=dict(cfg.get("message") or {})
                if button_text is not None: msg["button_text"]=button_text.strip()[:64]
                if text is not None: msg["text"]=text.strip()[:4000]
                if image_path is not None: msg["image_path"]=str(image_path or "")
                if clear_image: msg.pop("image_path", None)
                cfg["message"]=msg; row["config"]=cfg; _write_unlocked(data); return True
    return False


def _set_bot_config(bot_id: str, key: str, value):
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                cfg=_bot_config(row); cfg[key]=value; row["config"]=cfg; _write_unlocked(data); return True
    return False


def reset_bot_config(bot_id: str):
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                old=((row.get("config") or {}).get("message") or {}).get("image_path")
                row["config"]={"channel": {}, "message": {}}
                _write_unlocked(data)
                try:
                    path=Path(str(old or ""))
                    if path.is_file() and ASSET_DIR in path.parents: path.unlink(missing_ok=True)
                except Exception: pass
                return True
    return False



def clear_bot_message_field(bot_id: str, field: str):
    allowed={"button_text", "text", "image_path"}
    if field not in allowed:
        return False
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                cfg=_bot_config(row); msg=dict(cfg.get("message") or {})
                msg.pop(field, None)
                cfg["message"]=msg; row["config"]=cfg; _write_unlocked(data); return True
    return False


def bot_override_summary(bot_id: str, *, default_link: str = ""):
    cfg=get_bot_config(bot_id, default_link=default_link)
    ov=cfg["overrides"]
    labels=[]
    if ov["channel"]: labels.append("channel")
    if ov["button"]: labels.append("button")
    if ov["message"]: labels.append("message")
    if ov["image"]: labels.append("image")
    return labels

def touch_status(bot_id: str, **fields):
    return update_bot(bot_id, **fields)
