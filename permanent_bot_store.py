"""Persistent store for Admin-managed permanent delivery bots.

Bot tokens live outside videos.db. Files are chmod 600, writes are atomic, and
an advisory lock prevents the admin bot / runners from racing on updates.
"""
from __future__ import annotations
import copy, json, os, tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_BOTS_DIR = os.environ.get("VIDEO_VAULT_BOTS_DIR", "").strip()
_DEFAULT_STORE = str(Path(_BOTS_DIR).expanduser() / "permanent_bots.json") if _BOTS_DIR else str(Path(__file__).with_name("permanent_bots.json"))
STORE_PATH = Path(os.environ.get("VIDEO_VAULT_PERMANENT_BOTS_FILE", _DEFAULT_STORE)).expanduser()
LOCK_PATH = Path(os.environ.get("VIDEO_VAULT_PERMANENT_BOTS_LOCK", str(STORE_PATH) + ".lock")).expanduser()


def _now():
    return datetime.now().astimezone().isoformat()


def _secure():
    try:
        # Custom VIDEO_VAULT_* paths may point at a directory that does not exist
        # yet. Create it before touching the store/lock so first-run Admin health
        # and Manage screens cannot fail just because the directory is missing.
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORE_PATH.touch(exist_ok=True); os.chmod(STORE_PATH, 0o600)
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.touch(exist_ok=True); os.chmod(LOCK_PATH, 0o600)
    except Exception:
        pass


@contextmanager
def _lock(exclusive=False):
    _secure(); fh = open(LOCK_PATH, "r+", encoding="utf-8")
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
    return {"bots": []}


def _read_unlocked():
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict): return _blank()
        data.setdefault("bots", [])
        return data
    except Exception:
        return _blank()


def _json_default(value):
    """Serialize defensive values so one bad status update cannot crash the store."""
    if isinstance(value, datetime):
        return value.astimezone().isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _write_unlocked(data):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".permanent_bots_", suffix=".json", dir=str(STORE_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=_json_default)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, STORE_PATH); _secure()
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def list_bots():
    with _lock(False):
        data=_normalise_all_unlocked(_read_unlocked())
        return copy.deepcopy(data["bots"])


def get_bot(bot_id: str):
    with _lock(False):
        data=_normalise_all_unlocked(_read_unlocked())
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id): return copy.deepcopy(row)
    return None


def add_bot(*, bot_id: str, username: str, first_name: str = "", token: str, enabled: bool = True, primary: bool = False):
    with _lock(True):
        data = _read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id) or row.get("token") == token:
                return False
        data["bots"].append({
            "id": str(bot_id), "username": username or "", "first_name": first_name or "",
            "token": token, "enabled": bool(enabled), "primary": bool(primary), "retired": False,
            "created_at": _now(), "last_started_at": None, "last_seen_at": None, "last_error": None,
            "health": "starting", "consecutive_failures": 0, "success_count": 0, "failure_count": 0,
            "last_success_at": None, "last_failure_at": None, "cooldown_until": None,
            "total_attempts": 0, "last_selection_at": None,
        })
        _write_unlocked(data); return True


def ensure_primary(token: str, username: str = ""):
    """Register/update the deployment's protected primary Delivery Bot."""
    token=str(token or "").strip()
    if not token: return False
    with _lock(True):
        data=_read_unlocked()
        # Prefer the stable primary id. This prevents duplicate primary rows if
        # the deployment token is rotated in config between restarts.
        target=next((r for r in data["bots"] if str(r.get("id"))=="primary"), None)
        if target is None:
            target=next((r for r in data["bots"] if r.get("token")==token), None)
        if target is not None:
            if any(r is not target and r.get("token")==token for r in data["bots"]):
                return False
            target["id"]="primary"; target["primary"]=True; target["token"]=token
            if username: target["username"]=str(username).lstrip("@")
            target.setdefault("first_name","Delivery Bot"); target["enabled"]=bool(target.get("enabled",True)); target["retired"]=False
            _normalise_row(target); target["health"]="starting"; target["last_error"]=None; target["last_seen_at"]=None; target["ready_at"]=None; target["cooldown_until"]=None
            _write_unlocked(data); return False
        # Never allow another bot to reuse this token.
        if any(r.get("token")==token for r in data["bots"]): return False
        data["bots"].append({
            "id":"primary","username":str(username or "delivery").lstrip("@"),"first_name":"Delivery Bot","token":token,
            "enabled":True,"primary":True,"retired":False,"created_at":_now(),"last_started_at":None,"last_seen_at":None,"last_error":None,
            "health":"starting","consecutive_failures":0,"success_count":0,"failure_count":0,"last_success_at":None,"last_failure_at":None,
            "cooldown_until":None,"total_attempts":0,"last_selection_at":None
        })
        _write_unlocked(data); return True


def update_bot(bot_id: str, **fields):
    with _lock(True):
        data = _read_unlocked(); changed = False
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                for key, value in fields.items():
                    if key != "token": row[key] = value
                changed = True; break
        if changed: _write_unlocked(data)
        return changed



def replace_token(bot_id: str, token: str, username: str = "", first_name: str = ""):
    """Atomically replace a Delivery Bot token after Telegram validation.
    Caller is responsible for calling getMe first; this helper enforces
    uniqueness against every stored bot token before saving."""
    token = str(token or "").strip()
    if not token:
        return False, "empty_token"
    with _lock(True):
        data = _read_unlocked()
        target = None
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                target = row; break
        if not target:
            return False, "not_found"
        for row in data["bots"]:
            if str(row.get("id")) != str(bot_id) and row.get("token") == token:
                return False, "duplicate"
        old_username = str(target.get("username") or "").lstrip("@")
        target["token"] = token
        if username:
            target["username"] = str(username).lstrip("@")
        if first_name:
            target["first_name"] = str(first_name)
        target["last_error"] = None
        target["last_seen_at"] = None
        target["retired"] = False
        target["enabled"] = True
        target["health"] = "starting"
        target["ready_at"] = None
        target["cooldown_until"] = None
        target["delivery_failure_streak"] = 0
        target["token_updated_at"] = _now()
        _write_unlocked(data)
        return True, "updated"

def _trigger_link_migration(bot_id: str, reason: str):
    try:
        from delivery_link_migrator import migrate_bot_links
        return migrate_bot_links(str(bot_id), reason=reason)
    except Exception:
        return None


def delete_bot(bot_id: str):
    """Hard-delete a non-primary Delivery Bot from the persistent pool.

    Direct t.me catalogue links owned by this bot are migrated in the database
    after the bot is removed. Existing already-delivered t.me URLs cannot be
    rewritten by Telegram after the bot itself is deleted.
    """
    with _lock(True):
        data = _read_unlocked()
        kept = []
        removed = None
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                if row.get("primary"):
                    return False, "primary"
                removed = row
            else:
                kept.append(row)
        if removed is None:
            return False, "not_found"
        data["bots"] = kept
        _write_unlocked(data)
    _trigger_link_migration(str(bot_id), "bot deleted from permanent pool")
    return True, "deleted"


def remove_bot(bot_id: str):
    """Soft-retire a bot, then migrate its links after releasing the store lock."""
    retired = False
    with _lock(True):
        data = _read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                if row.get("primary"):
                    return False
                row["enabled"] = False
                row["retired"] = True
                row["health"] = "disabled"
                row["cooldown_until"] = None
                row["last_error"] = None
                _write_unlocked(data)
                retired = True
                break
    if retired:
        # Migration calls back into permanent_bot_store to select candidates;
        # never perform that callback while holding the store file lock.
        _trigger_link_migration(str(bot_id), "bot retired from permanent pool")
    return retired


def set_enabled(bot_id: str, enabled: bool):
    """Enable/disable without destroying bot history."""
    changed = update_bot(bot_id, enabled=bool(enabled), retired=(False if enabled else True), last_error=None, health=("starting" if enabled else "disabled"), cooldown_until=None)
    if changed and not bool(enabled) and str(bot_id) != "primary":
        _trigger_link_migration(str(bot_id), "bot disabled in permanent pool")
    return changed


def _normalise_row(row):
    row.setdefault("health", "starting")
    row.setdefault("consecutive_failures", 0)
    row.setdefault("success_count", 0)
    row.setdefault("failure_count", 0)
    row.setdefault("last_success_at", None)
    row.setdefault("last_failure_at", None)
    row.setdefault("cooldown_until", None)
    row.setdefault("total_attempts", 0)
    row.setdefault("last_selection_at", None)
    row.setdefault("delivery_success_count", 0)
    row.setdefault("delivery_failure_count", 0)
    row.setdefault("delivery_failure_streak", 0)
    row.setdefault("last_delivery_at", None)
    row.setdefault("last_delivery_error", None)
    row.setdefault("ready_at", None)
    row.setdefault("watchdog_restart_count", 0)
    row.setdefault("last_watchdog_restart_at", None)
    return row

def _normalise_all_unlocked(data):
    for row in data.get("bots", []): _normalise_row(row)
    return data

def active_delivery_bots():
    with _lock(False):
        data=_normalise_all_unlocked(_read_unlocked())
        return copy.deepcopy([r for r in data["bots"] if r.get("enabled") and r.get("token") and not r.get("retired")])



def _cooldown_active(row, now=None):
    import time as _time
    now = _time.time() if now is None else now
    cd=row.get("cooldown_until")
    if not cd: return False
    try: return datetime.fromisoformat(str(cd)).timestamp() > now
    except Exception: return False

def _heartbeat_age(row, now=None):
    import time as _time
    now = _time.time() if now is None else now
    seen=row.get("last_seen_at")
    if not seen: return None
    try: return max(0.0, now-datetime.fromisoformat(str(seen)).timestamp())
    except Exception: return None

def _eligible(row, max_age_seconds: int, allow_degraded=False):
    if not row.get("enabled") or row.get("retired") or not row.get("token") or not row.get("username"):
        return False
    health=str(row.get("health") or "starting")
    if health in {"quarantined","disabled","starting"}: return False
    if health != "healthy" and not allow_degraded: return False
    if _cooldown_active(row): return False
    age=_heartbeat_age(row)
    return age is not None and age <= max_age_seconds

def bot_health_snapshot(max_age_seconds: int = 120):
    """Return admin-friendly health rows without changing the persisted store.

    The Admin Bot uses this alongside ``routable_delivery_bots()``.  Keep the
    snapshot derived from the same eligibility rules as the resolver so health
    screens cannot disagree with actual routing state.
    """
    import copy as _copy
    with _lock(False):
        data = _normalise_all_unlocked(_read_unlocked())
        now = None
        rows = []
        for row in data.get("bots", []):
            item = _copy.deepcopy(row)
            item["active"] = bool(item.get("enabled") and not item.get("retired") and item.get("token"))
            item["age_seconds"] = _heartbeat_age(item, now)
            item["healthy"] = _eligible(item, max_age_seconds, False)
            item["routable"] = _eligible(item, max_age_seconds, True)
            # Make stale-heartbeat state explicit for Admin/ops; this does not
            # alter resolver eligibility.
            item["stale_heartbeat"] = bool(item.get("last_seen_at")) and (item.get("age_seconds") is None or item.get("age_seconds", 0) > max_age_seconds)
            item["cooldown_active"] = _cooldown_active(item, now)
            rows.append(item)
        return rows


def healthy_delivery_bots(max_age_seconds: int = 120):
    with _lock(False):
        data=_normalise_all_unlocked(_read_unlocked())
        return copy.deepcopy([r for r in data["bots"] if _eligible(r,max_age_seconds,False)])

def routable_delivery_bots(max_age_seconds: int = 180):
    """Healthy pool first; degraded fallback only when no healthy bot exists."""
    with _lock(False):
        data=_normalise_all_unlocked(_read_unlocked())
        rows=data["bots"]
        healthy=[r for r in rows if _eligible(r,max_age_seconds,False)]
        if healthy: return copy.deepcopy(healthy)
        degraded=[r for r in rows if _eligible(r,max_age_seconds,True)]
        degraded.sort(key=lambda r:(int(r.get("delivery_failure_streak") or 0),int(r.get("consecutive_failures") or 0)))
        return copy.deepcopy(degraded[:3])

def record_selection(bot_id: str):
    """Record resolver selection metrics without affecting routing decisions."""
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                _normalise_row(row)
                row["total_attempts"] = int(row.get("total_attempts") or 0) + 1
                row["last_selection_at"] = _now()
                _write_unlocked(data)
                return True
    return False


def record_watchdog_restart(bot_id: str, reason: str):
    """Record an operational watchdog restart while keeping the bot routable policy unchanged."""
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id")) == str(bot_id):
                _normalise_row(row); now=_now()
                row["watchdog_restart_count"] = int(row.get("watchdog_restart_count") or 0) + 1
                row["last_watchdog_restart_at"] = now
                row["last_error"] = str(reason or "watchdog restart")[:500]
                row["health"] = "degraded" if row.get("enabled") and not row.get("retired") else row.get("health")
                row["cooldown_until"] = datetime.fromtimestamp(__import__('time').time()+15).astimezone().isoformat()
                _write_unlocked(data); return True
    return False


def mark_starting(bot_id: str):
    return touch_status(bot_id, health="starting", last_error=None, cooldown_until=None, ready_at=None)

def mark_ready(bot_id: str, *, username: str = "", first_name: str = ""):
    fields={"health":"healthy","last_error":None,"cooldown_until":None,"ready_at":_now(),"last_seen_at":_now(),"consecutive_failures":0}
    if username: fields["username"]=str(username).lstrip("@")
    if first_name: fields["first_name"]=str(first_name)
    ok = touch_status(bot_id, **fields)
    # A recovered/first-ready bot can automatically rescue links orphaned while
    # the pool had no healthy targets. Best effort: never block worker startup.
    try:
        from delivery_link_migrator import retry_orphaned_links
        retry_orphaned_links()
    except Exception:
        pass
    return ok

def record_delivery_success(bot_id: str):
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id"))==str(bot_id):
                _normalise_row(row); now=_now()
                row["delivery_success_count"]+=1; row["delivery_failure_streak"]=0; row["last_delivery_at"]=now; row["last_delivery_error"]=None; row["last_seen_at"]=now
                if row.get("enabled") and not row.get("retired"):
                    row["health"]="healthy"; row["last_error"]=None; row["cooldown_until"]=None; row["consecutive_failures"]=0
                _write_unlocked(data); return True
    return False

def record_delivery_failure(bot_id: str, error: str, *, cooldown_seconds=45, quarantine_after=5):
    quarantined = False
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id"))==str(bot_id):
                _normalise_row(row); now=_now()
                row["delivery_failure_count"]+=1; row["delivery_failure_streak"]+=1; row["last_delivery_at"]=now; row["last_delivery_error"]=str(error or "delivery failure")[:500]
                row["last_failure_at"]=now; row["last_error"]=row["last_delivery_error"]; row["consecutive_failures"]+=1
                if row["delivery_failure_streak"]>=max(1,int(quarantine_after)):
                    row["health"]="quarantined"; row["cooldown_until"]=None; quarantined=True
                else:
                    row["health"]="degraded"; row["cooldown_until"]=datetime.fromtimestamp(__import__('time').time()+max(5,int(cooldown_seconds))).astimezone().isoformat()
                _write_unlocked(data); break
        else:
            return False
    if quarantined:
        _trigger_link_migration(str(bot_id), f"delivery failure threshold reached: {str(error or '')[:200]}")
    return True

def record_success(bot_id: str):
    return mark_ready(bot_id)

def record_failure(bot_id: str, error: str, *, fatal=False, cooldown_seconds=30):
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id"))==str(bot_id):
                _normalise_row(row); now=_now(); row["failure_count"]+=1; row["total_attempts"]+=1; row["consecutive_failures"]+=1; row["last_failure_at"]=now; row["last_error"]=str(error or "worker failure")[:500]
                row["health"]="quarantined" if fatal else "degraded"
                row["cooldown_until"]=None if fatal else datetime.fromtimestamp(__import__('time').time()+max(5,int(cooldown_seconds))).astimezone().isoformat()
                _write_unlocked(data); break
        else:
            return False
    if fatal:
        _trigger_link_migration(str(bot_id), f"fatal worker failure: {str(error or '')[:220]}")
    return True

def recover_bot(bot_id: str):
    with _lock(True):
        data=_read_unlocked()
        for row in data["bots"]:
            if str(row.get("id"))==str(bot_id):
                _normalise_row(row); row["health"]="starting"; row["last_error"]=None; row["consecutive_failures"]=0; row["cooldown_until"]=None; row["enabled"]=True; row["retired"]=False; row["delivery_failure_streak"]=0
                _write_unlocked(data); return True
    return False

def clear_error(bot_id: str):
    return update_bot(bot_id, last_error=None, health="starting", consecutive_failures=0, cooldown_until=None, delivery_failure_streak=0)

def touch_status(bot_id: str, **fields):
    return update_bot(bot_id, **fields)
