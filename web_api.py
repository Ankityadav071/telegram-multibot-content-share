
"""
Video Vault API
Serves live catalog data from the SAME SQLite DB used by the Telegram bots.

Run inside the Pterodactyl container. Expose this HTTP port through an HTTPS
reverse proxy/tunnel before putting the URL into the Netlify Mini App.
"""
import json, os, hashlib, threading, urllib.request, urllib.parse, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None
    ImageOps = None

import config
import db
try:
    import permanent_bot_store
except Exception:
    try:
        import sys
        candidate=os.getenv("VIDEO_VAULT_BOTS_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bots")))
        if candidate not in sys.path: sys.path.insert(0, candidate)
        import permanent_bot_store
    except Exception:
        permanent_bot_store = None


HOST = os.getenv("VIDEO_VAULT_API_HOST", "0.0.0.0")
# Public VPS API port
# Prefer the dedicated Video Vault variable. Default to the Pterodactyl
# allocation used by the Hyderabad VPS so the API works even when the
# generic Pterodactyl PORT variable is absent or points elsewhere.
PORT = int(os.getenv("VIDEO_VAULT_API_PORT", "5043"))
PUBLIC_API = os.getenv("VIDEO_VAULT_API_PUBLIC_URL", "").rstrip("/")
COVER_CACHE_DIR = os.getenv("VIDEO_VAULT_COVER_CACHE", os.path.join(os.path.dirname(__file__), ".cover_cache"))
os.makedirs(COVER_CACHE_DIR, exist_ok=True)

# Per-cover locks prevent duplicate downloads of the SAME cover without
# serialising unrelated covers. This makes the first catalogue render much faster.
_COVER_LOCKS = {}
_COVER_LOCKS_GUARD = threading.Lock()
_FILE_PATH_CACHE = {}
_FILE_PATH_CACHE_TTL = 6 * 60 * 60
_CACHE_SWEEP_INTERVAL = 600
_last_cache_sweep = 0.0
_WATCH_LIMIT = 30
_WATCH_WINDOW = 60
_COVER_LIMIT = 120
_COVER_WINDOW = 60
_RATE = {}
_RATE_GUARD = threading.Lock()

def _sweep_caches(now=None):
    global _last_cache_sweep
    now = now or time.time()
    if now - _last_cache_sweep < _CACHE_SWEEP_INTERVAL:
        return
    _last_cache_sweep = now
    with _COVER_LOCKS_GUARD:
        # Locks are never removed while held; idle locks are cheap to evict.
        for k, lock in list(_COVER_LOCKS.items()):
            if not lock.locked():
                _COVER_LOCKS.pop(k, None)
    for k, hit in list(_FILE_PATH_CACHE.items()):
        if hit[0] <= now:
            _FILE_PATH_CACHE.pop(k, None)

def _cover_lock(key):
    _sweep_caches()
    with _COVER_LOCKS_GUARD:
        lock = _COVER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _COVER_LOCKS[key] = lock
        return lock

def _cached_file_path(file_id, token):
    _sweep_caches()
    now=time.time()
    hit=_FILE_PATH_CACHE.get(file_id)
    if hit and hit[0] > now:
        return hit[1]
    api=f"https://api.telegram.org/bot{token}/getFile?file_id={urllib.parse.quote(file_id)}"
    with urllib.request.urlopen(api,timeout=8) as r:
        data=json.loads(r.read())
    fp=data.get("result",{}).get("file_path") if data.get("ok") else None
    if not fp:
        raise FileNotFoundError("Telegram file_path unavailable")
    _FILE_PATH_CACHE[file_id]=(now+_FILE_PATH_CACHE_TTL,fp)
    return fp



def _watch_resolver_base():
    return (os.getenv("VIDEO_VAULT_WATCH_RESOLVER_URL", "").rstrip("/")
            or getattr(config, "VIDEO_VAULT_WATCH_RESOLVER_URL", "").rstrip("/")
            or PUBLIC_API
            or "https://us1.visihost.in:5059").rstrip("/")


def _watch_param(*, video_id=None, batch_id=None):
    return f"b_{batch_id}" if batch_id else f"v_{video_id}" if video_id else ""


def stable_watch_url(*, video_id=None, batch_id=None):
    param = _watch_param(video_id=video_id, batch_id=batch_id)
    if not param:
        return ""
    return f"{_watch_resolver_base()}/watch?start={urllib.parse.quote(param)}"


def direct_delivery_url(*, video_id=None, batch_id=None, log=True):
    """Diagnostic direct bot URL; public Watch links never use this helper."""
    param=_watch_param(video_id=video_id,batch_id=batch_id)
    if not param: return ""
    bots=_active_delivery_bots()
    if not bots: return ""
    import random
    bot=random.choice(bots)
    if log:
        try: db.log_analytics("resolver_direct", user_id=None, value=1, detail=str(bot.get("id") or bot.get("username") or ""))
        except Exception: pass
    return f"https://t.me/{str(bot['username']).lstrip('@')}?start={urllib.parse.quote(param)}"


def _active_delivery_bots():
    # Once the permanent pool store is loaded, an empty/unhealthy pool is a
    # real outage condition — never silently fall back to the primary config
    # bot, because that could route clicks to a banned/decommissioned bot.
    if permanent_bot_store is not None:
        try:
            rows=permanent_bot_store.routable_delivery_bots(180)
            return [r for r in rows if r.get("username")]
        except Exception as exc:
            print("[VideoVault API] permanent bot store error", repr(exc), flush=True)
            return []
    username=str(getattr(config,"DELIVERY_BOT_USERNAME","") or "").lstrip("@")
    return [{"id":"legacy","username":username}] if username else []


def _resolve_delivery_target(start_param):
    import random
    start_param = str(start_param or "").strip()
    if not (start_param.startswith("v_") or start_param.startswith("b_")):
        return None
    ident=start_param[2:]
    try:
        if start_param.startswith("v_"):
            row=db.get_video(ident)
            if not row or not _safe_visible(row): return None
        else:
            with db.get_conn() as conn:
                params = visibility_params() + [ident]
                row=conn.execute(f"SELECT 1 FROM videos WHERE {visible_clause()} AND batch_id=? LIMIT 1",params).fetchone()
            if not row: return None
    except Exception:
        return None
    bots = _active_delivery_bots()
    bots = [b for b in bots if b.get("username")]
    if not bots:
        return None
    bot = random.choice(bots)
    try:
        db.log_analytics("resolver", user_id=None, value=1, detail=str(bot.get("id") or bot.get("username") or ""))
    except Exception:
        pass
    return f"https://t.me/{str(bot['username']).lstrip('@')}?start={urllib.parse.quote(start_param)}"

_VIDEO_COLUMNS = None

def _video_columns():
    global _VIDEO_COLUMNS
    if _VIDEO_COLUMNS is not None:
        return _VIDEO_COLUMNS
    try:
        with db.get_conn() as conn:
            rows = conn.execute("PRAGMA table_info(videos)").fetchall()
            _VIDEO_COLUMNS = {str(r[1]) for r in rows}
    except Exception as exc:
        print("[VideoVault API] schema probe failed", repr(exc), flush=True)
        _VIDEO_COLUMNS = set()
    return _VIDEO_COLUMNS

def visible_clause():
    # Be compatible with older videos.db files that predate publish_at.
    # Calendar only needs upload_date, while catalogue previously failed
    # outright when this newer column was absent.
    cols = _video_columns()
    if "publish_at" not in cols:
        return "1=1"
    return "(publish_at IS NULL OR publish_at = '' OR publish_at <= ?)"

def visibility_params():
    return [now_iso()] if "publish_at" in _video_columns() else []

def now_iso():
    return datetime.now(config.TIMEZONE).isoformat()

def _safe_visible(row):
    """Visibility is fail-closed per row, not per request.

    One malformed legacy publish_at value must never turn the entire public
    catalogue endpoint into HTTP 500.
    """
    try:
        return bool(row) and bool(db.is_visible(dict(row)))
    except Exception as exc:
        print("[VideoVault API] visibility parse error", repr(exc), flush=True)
        return False


def _direct_delivery_target(video_id=None, batch_id=None):
    """Pick one healthy permanent Delivery Bot and return a direct t.me URL.

    Mini App cards should open Telegram directly; the VPS resolver remains
    available as a compatibility endpoint but is not used for user-facing
    Mini App watch URLs.
    """
    import random
    from urllib.parse import quote
    param = (f"b_{batch_id}" if batch_id else f"v_{video_id}" if video_id else "")
    if not param:
        return None
    bots = [b for b in _active_delivery_bots() if b.get("username")]
    if not bots:
        return None
    bot = random.choice(bots)
    username = str(bot.get("username") or "").lstrip("@")
    if not username:
        return None
    return {
        "bot_id": str(bot.get("id") or ""),
        "username": username,
        "url": f"https://t.me/{username}?start={quote(param)}",
    }


def video_public(v):
    if not _safe_visible(v):
        return None
    vid = v.get("id")
    bid = v.get("batch_id")
    cover_id = v.get("cover_file_id")
    cover_url = (f"{PUBLIC_API}/api/cover?id={urllib.parse.quote(str(vid))}" if PUBLIC_API else f"/api/cover?id={urllib.parse.quote(str(vid))}") if cover_id else ""
    # Mini App uses a direct Telegram Delivery Bot URL. Catalogue messages
    # separately register their exact bot ownership so dead-bot links can be
    # migrated automatically.
    delivery_target = _direct_delivery_target(batch_id=bid) if bid else _direct_delivery_target(video_id=vid)
    delivery_url = delivery_target["url"] if delivery_target else ""
    return {
        "id": vid,
        "n": v.get("video_number"),
        "number": v.get("video_number"),
        "title": v.get("title") or "Untitled video",
        "description": v.get("description") or "",
        "tags": v.get("tags") or "",
        "date": v.get("upload_date") or "",
        "upload_date": v.get("upload_date") or "",
        "created_at": v.get("created_at") or "",
        "views": int(v.get("view_count") or 0),
        "duration": v.get("duration_seconds"),
        "premium": (v.get("access_tier") or "free") not in ("free", ""),
        "media_type": v.get("media_type") or "video",
        "batch_id": bid or "",
        "collection_id": bid or "",
        "collection_count": int(v.get("batch_count") or 1),
        "collection": bool(bid),
        "collection_title": v.get("batch_title") or v.get("title") or "Untitled collection",
        "thumbnail": cover_url,
        "cover_url": cover_url,
        # Direct t.me Delivery Bot link for the Mini App.
        "watch_url": delivery_url,
        "delivery_url": delivery_url,
        "delivery_bot_id": (delivery_target or {}).get("bot_id", ""),
        "delivery_username": (delivery_target or {}).get("username", ""),
    }

def batch_public(bid, rows, batch_row=None):
    b=dict(batch_row) if batch_row else {}
    items=[x for r in rows if (x := video_public(dict(r))) is not None]
    return {
        "id": bid,
        "title": b.get("title") or (items[0]["title"] if items else "Untitled collection"),
        "cover_url": (items[0].get("cover_url") if items else ""),
        "count": len(items),
        "views": sum(int(x.get("views") or 0) for x in items),
        "items": items,
    }

def _paged_from_rows(rows, page, limit):
    grouped=[]; seen={}
    for row in rows:
        v=dict(row)
        bid=v.get("batch_id")
        if not bid:
            v["batch_count"]=1; grouped.append(v); continue
        if bid in seen:
            seen[bid]["batch_count"] += 1
            seen[bid]["view_count"] = int(seen[bid].get("view_count") or 0) + int(v.get("view_count") or 0)
            continue
        v["batch_count"]=1; seen[bid]=v; grouped.append(v)
    total=len(grouped); offset=(page-1)*limit; page_rows=grouped[offset:offset+limit]
    public=[]
    for v in page_rows:
        item=video_public(v)
        if item is not None:
            public.append(item)
    return {"videos":public,"total":total,"page":page,"pages":max(1,(total+limit-1)//limit)}

def list_videos(mode="today", page=1, limit=4, q="", date_value=None):
    # Fetch visible video rows first, then collapse bulk rows into ONE
    # collection card. This keeps pagination correct even when a collection
    # contains many videos.
    page=max(1,int(page)); limit=max(1,min(100,int(limit)));
    params=[]
    where=[visible_clause()]
    params.extend(visibility_params())
    target_day = date_value or db.today_str()
    if mode=="popular":
        # Global Popular = every visible video, including older uploads + all of today's uploads.
        order="view_count DESC, created_at DESC"
    elif mode in ("premium", "premium_today", "premium_popular"):
        where.append("COALESCE(access_tier,'free') NOT IN ('free','')")
        if mode == "premium_today":
            where.append("(upload_date=? OR substr(created_at,1,10)=? OR (publish_at IS NOT NULL AND substr(publish_at,1,10)=?) )")
            params += [target_day, target_day, target_day]
            order="created_at DESC"
        elif mode == "premium_popular":
            order="view_count DESC, created_at DESC"
        else:
            order="created_at DESC"
    elif mode=="date":
        where.append("(upload_date=? OR substr(created_at,1,10)=? OR (publish_at IS NOT NULL AND substr(publish_at,1,10)=?))")
        params += [target_day, target_day, target_day]
        order="created_at ASC"
    else:
        # Today must not depend solely on upload_date. Older rows can have a
        # correct created_at/publish_at local date while upload_date was left
        # stale or imported from another timezone.
        where.append("(upload_date=? OR substr(created_at,1,10)=? OR (publish_at IS NOT NULL AND substr(publish_at,1,10)=?) )")
        params += [target_day, target_day, target_day]
        order="created_at DESC"
    if q:
        # Advanced search is handled by a dedicated query path below.
        # Keep the legacy SQL path for simple one-term catalogue pagination.
        if ("\"" in q) or any(op+":" in q.lower() for op in ("cat","tag","date")) or len(q.split()) > 1:
            adv = db.advanced_search(q, limit=200, visible_only=True)
            return _paged_from_rows(adv, page, limit)
        like=f"%{q.lower()}%"
        if q.isdigit():
            where.append("(video_number=? OR LOWER(title) LIKE ? OR LOWER(tags) LIKE ? OR LOWER(description) LIKE ?)")
            params += [int(q),like,like,like]
        else:
            where.append("(LOWER(title) LIKE ? OR LOWER(tags) LIKE ? OR LOWER(description) LIKE ?)")
            params += [like,like,like]
    w=" AND ".join(where)
    # Keep all batch lookups on the same live connection.  The previous build
    # closed the SQLite connection before resolving batch metadata, which made
    # /api/videos return HTTP 500 as soon as a bulk collection existed.
    with db.get_conn() as conn:
        rows=conn.execute(f"SELECT * FROM videos WHERE {w} ORDER BY {order}",params).fetchall()
        # SQL handles normal rows; Python is the final authority for malformed
        # legacy schedule values. This prevents video_public() from returning
        # null entries that can break the Mini App renderer.
        rows=[r for r in rows if _safe_visible(r)]

        # Collapse rows with the same batch_id. Single videos remain unchanged.
        grouped=[]
        seen={}
        for row in rows:
            v=dict(row)
            bid=v.get("batch_id")
            if not bid:
                v["batch_count"]=1
                grouped.append(v)
                continue
            if bid in seen:
                g=seen[bid]
                g["batch_count"] += 1
                g["view_count"] = int(g.get("view_count") or 0) + int(v.get("view_count") or 0)
                continue
            try:
                brow=conn.execute("SELECT * FROM batches WHERE id=?",(bid,)).fetchone()
                b=dict(brow) if brow else {}
            except Exception:
                b={}
            v["batch_count"]=1
            v["batch_title"]=b.get("title") or v.get("title")
            # The batch cover is shared; prefer it over an item-level cover.
            if b.get("cover_file_id"):
                v["cover_file_id"]=b.get("cover_file_id")
            seen[bid]=v
            grouped.append(v)

    total=len(grouped)
    offset=(page-1)*limit
    page_rows=grouped[offset:offset+limit]
    pages=max(1,(total+limit-1)//limit)
    public=[]
    for v in page_rows:
        item=video_public(v)
        if item is not None:
            public.append(item)
    return {"videos":public,"total":total,"page":page,"pages":pages}

def calendar_counts(month):
    # month = YYYY-MM
    try:
        y,m=map(int,month.split("-"))
        if m<1 or m>12: raise ValueError
    except Exception:
        return {}
    prefix=f"{y:04d}-{m:02d}-"
    with db.get_conn() as conn:
        rows=conn.execute(
            f"""SELECT upload_date, COUNT(*) c FROM videos
                WHERE {visible_clause()} AND upload_date LIKE ?
                GROUP BY upload_date""",
            tuple(visibility_params()+[prefix+"%"])
        ).fetchall()
    return {r["upload_date"]:int(r["c"]) for r in rows if r["upload_date"]}

def get_video(video_id):
    with db.get_conn() as conn:
        row=conn.execute(f"SELECT * FROM videos WHERE {visible_clause()} AND id=?", tuple(visibility_params()+[video_id])).fetchone()
    return video_public(dict(row)) if row else None

def _rate_allowed(h, bucket, limit, window):
    ip = h.client_address[0] if h.client_address else "unknown"
    now = time.monotonic()
    key=(bucket, ip)
    with _RATE_GUARD:
        arr=_RATE.setdefault(key, [])
        cutoff=now-window
        while arr and arr[0] <= cutoff:
            arr.pop(0)
        if len(arr) >= limit:
            return False
        arr.append(now)
        # Bound idle keys as a lightweight process-local limiter.
        if len(_RATE) > 5000:
            for k in list(_RATE)[:1000]:
                if not _RATE[k]: _RATE.pop(k, None)
        return True

def send_json(h, code, payload, cache_control="no-store"):
    raw=json.dumps(payload,ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type","application/json; charset=utf-8")
    h.send_header("Content-Length",str(len(raw)))
    h.send_header("Access-Control-Allow-Origin","*")
    h.send_header("Access-Control-Allow-Headers","Content-Type")
    h.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
    h.send_header("Cache-Control", cache_control)
    h.send_header("X-VideoVault-Request-ID", getattr(h, "request_id", ""))
    h.send_header("X-VideoVault-API-Version", "9.8.0")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    h.send_header("X-Frame-Options", "SAMEORIGIN")
    h.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    h.end_headers()
    h.wfile.write(raw)


def _safe_catalog_rows(mode="today", page=1, limit=24, q="", date_value=None):
    """Compatibility catalogue endpoint with Python-side filtering/sorting.

    This intentionally avoids the heavier legacy /api/videos SQL path so a
    single malformed legacy row cannot blank the Mini App. It reuses the same
    visibility gate and public serializer, skipping only rows that cannot be
    serialized safely.
    """
    try: page=max(1,int(page)); limit=max(1,min(100,int(limit)))
    except Exception: page,limit=1,24
    q=str(q or "").strip().lower()
    target=date_value or db.today_str()
    with db.get_conn() as conn:
        rows=conn.execute(f"SELECT * FROM videos WHERE {visible_clause()}", visibility_params()).fetchall()
    values=[]
    for rr in rows:
        try:
            r=dict(rr)
            if not _safe_visible(r): continue
            access=str(r.get("access_tier") or "free").lower()
            premium=access not in ("free", "")
            dates={str(r.get("upload_date") or "")[:10], str(r.get("created_at") or "")[:10], str(r.get("publish_at") or "")[:10]}
            dates.discard("")
            is_today=target in dates
            if mode == "today" and not is_today: continue
            if mode == "premium_today" and (not premium or not is_today): continue
            if mode in ("premium", "premium_popular") and not premium: continue
            if mode == "date" and target not in dates: continue
            if q:
                hay=" ".join(str(r.get(k) or "") for k in ("title","tags","description","video_number")).lower()
                if q not in hay: continue
            values.append(r)
        except Exception as exc:
            print("[VideoVault API] safe catalog skipped row:", repr(exc), flush=True)
    def num(r):
        try: return int(r.get("view_count") or 0)
        except Exception: return 0
    def created(r): return str(r.get("created_at") or r.get("upload_date") or "")
    if mode in ("popular", "premium_popular"):
        values.sort(key=lambda r:(num(r),created(r)), reverse=True)
    elif mode == "date":
        values.sort(key=created)
    else:
        values.sort(key=created, reverse=True)
    # Collapse batches exactly like the main endpoint, but defensively.
    grouped=[]; seen={}
    for r in values:
        bid=r.get("batch_id")
        if not bid:
            r["batch_count"]=1; grouped.append(r); continue
        key=str(bid)
        if key in seen:
            seen[key]["batch_count"]=int(seen[key].get("batch_count") or 1)+1
            seen[key]["view_count"]=num(seen[key])+num(r)
            continue
        r["batch_count"]=1
        grouped.append(r); seen[key]=r
    total=len(grouped); offset=(page-1)*limit
    public=[]
    for r in grouped[offset:offset+limit]:
        try:
            p=video_public(r)
            if p is not None: public.append(p)
        except Exception as exc:
            print("[VideoVault API] safe catalog serialize skipped row:", repr(exc), flush=True)
    return {"videos":public,"total":total,"page":page,"pages":max(1,(total+limit-1)//limit)}


def _raw_catalog_rows(mode="today", page=1, limit=24, q="", date_value=None):
    """Last-resort catalogue reader using only SQLite primitives.

    This intentionally avoids db.is_visible(), advanced serializers, and the
    permanent-bot store.  It exists so a legacy/mixed videos.db schema can
    still populate the Mini App instead of returning HTTP 500.
    """
    try:
        page=max(1,int(page)); limit=max(1,min(100,int(limit)))
    except Exception:
        page,limit=1,24
    target=str(date_value or datetime.now(config.TIMEZONE).date())[:10]
    q=str(q or '').strip().lower()
    with db.get_conn() as conn:
        cols=[str(r[1]) for r in conn.execute("PRAGMA table_info(videos)").fetchall()]
        if not cols or 'id' not in cols:
            return {"videos":[],"total":0,"page":page,"pages":1,"error":"videos_table_unavailable"}
        rows=conn.execute("SELECT * FROM videos").fetchall()
        batch_titles={}
        if 'batch_id' in cols:
            try:
                bcols=[str(r[1]) for r in conn.execute("PRAGMA table_info(batches)").fetchall()]
                if bcols and 'id' in bcols and 'title' in bcols:
                    for br in conn.execute("SELECT id,title FROM batches").fetchall():
                        batch_titles[str(br[0])]=br[1]
            except Exception:
                pass
    vals=[]
    for rr in rows:
        try:
            r=dict(rr)
            access=str(r.get('access_tier') or 'free').lower()
            premium=access not in ('free','')
            dates=set()
            for k in ('upload_date','created_at','publish_at','date'):
                v=str(r.get(k) or '')[:10]
                if len(v)==10: dates.add(v)
            is_today=target in dates
            if mode == 'today' and not is_today: continue
            if mode == 'premium_today' and (not premium or not is_today): continue
            if mode in ('premium','premium_popular') and not premium: continue
            if mode == 'date' and target not in dates: continue
            if q:
                hay=' '.join(str(r.get(k) or '') for k in ('title','tags','description','video_number','id')).lower()
                if q not in hay: continue
            bid=r.get('batch_id')
            if bid and str(bid) in batch_titles:
                r['batch_title']=batch_titles[str(bid)]
            vals.append(r)
        except Exception:
            continue
    def views(r):
        for k in ('view_count','views','views_count'):
            try: return int(r.get(k) or 0)
            except Exception: pass
        return 0
    def created(r): return str(r.get('created_at') or r.get('upload_date') or r.get('date') or '')
    if mode in ('popular','premium_popular'):
        vals.sort(key=lambda r:(views(r),created(r)),reverse=True)
    else:
        vals.sort(key=created,reverse=True)
    # Collapse collections to one card while preserving the aggregate count.
    grouped=[]; seen={}
    for r in vals:
        bid=r.get('batch_id')
        if not bid:
            r['batch_count']=1; grouped.append(r); continue
        key=str(bid)
        if key in seen:
            seen[key]['batch_count']=int(seen[key].get('batch_count') or 1)+1
            seen[key]['view_count']=views(seen[key])+views(r)
        else:
            r['batch_count']=1; grouped.append(r); seen[key]=r
    total=len(grouped); offset=(page-1)*limit
    out=[]
    for r in grouped[offset:offset+limit]:
        try:
            vid=r.get('id'); bid=r.get('batch_id'); cover_id=r.get('cover_file_id')
            cover_url=(f"{PUBLIC_API}/api/cover?id={urllib.parse.quote(str(vid))}" if PUBLIC_API else f"/api/cover?id={urllib.parse.quote(str(vid))}") if cover_id else ''
            target_info=None
            try:
                target_info=_direct_delivery_target(batch_id=bid) if bid else _direct_delivery_target(video_id=vid)
            except Exception:
                target_info=None
            out.append({
                'id':vid,'n':r.get('video_number'),'number':r.get('video_number'),
                'title':r.get('title') or 'Untitled video','description':r.get('description') or '',
                'tags':r.get('tags') or '','date':r.get('upload_date') or '',
                'upload_date':r.get('upload_date') or '','created_at':r.get('created_at') or '',
                'views':views(r),'duration':r.get('duration_seconds'),
                'premium':str(r.get('access_tier') or 'free') not in ('free',''),
                'media_type':r.get('media_type') or 'video','batch_id':bid or '',
                'collection_id':bid or '','collection_count':int(r.get('batch_count') or 1),
                'collection':bool(bid),'collection_title':r.get('batch_title') or r.get('title') or 'Untitled collection',
                'thumbnail':cover_url,'cover_url':cover_url,
                'watch_url':(target_info or {}).get('url',''),'delivery_url':(target_info or {}).get('url',''),
                'delivery_bot_id':(target_info or {}).get('bot_id',''),'delivery_username':(target_info or {}).get('username','')
            })
        except Exception:
            continue
    return {'videos':out,'total':total,'page':page,'pages':max(1,(total+limit-1)//limit)}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[VideoVault API] "+fmt%args, flush=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("X-VideoVault-Request-ID", getattr(self, "request_id", ""))
        self.send_header("X-VideoVault-API-Version", "v9")
        self.end_headers()

    def do_GET(self):
        self.request_id = uuid.uuid4().hex[:12]
        u=urllib.parse.urlparse(self.path)
        path=u.path.rstrip("/") or "/"
        q=urllib.parse.parse_qs(u.query)
        try:
            if path=="/watch":
                if not _rate_allowed(self, "resolver", _WATCH_LIMIT, _WATCH_WINDOW):
                    self.send_response(429); self.send_header("Content-Type","text/plain; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(b"Too many resolver requests. Please try again."); return
                target=_resolve_delivery_target(q.get("start",[""])[0])
                if not target:
                    self.send_response(503); self.send_header("Content-Type","text/plain; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(b"Delivery is temporarily unavailable. Please try again."); return
                self.send_response(302); self.send_header("Location", target); self.send_header("Cache-Control", "no-store, no-cache, must-revalidate"); self.send_header("Pragma", "no-cache"); self.send_header("X-Robots-Tag","noindex, nofollow, noarchive"); self.send_header("Referrer-Policy","no-referrer"); self.send_header("Content-Length","0"); self.end_headers(); return
            if path=="/api/resolve":
                if not _rate_allowed(self, "resolve_api", _WATCH_LIMIT, _WATCH_WINDOW):
                    return send_json(self,429,{"ok":False,"error":"rate_limited"})
                start_param = q.get("start", [""])[0]
                target = _resolve_delivery_target(start_param)
                if not target:
                    return send_json(self,503,{"ok":False,"error":"delivery_unavailable"})
                return send_json(self,200,{"ok":True,"start":start_param,"watch_url":target,"delivery_url":target})
            if path=="/api/delivery-bots":
                bots=_active_delivery_bots()
                safe=[{"id":str(b.get("id")),"username":str(b.get("username") or "").lstrip("@")} for b in bots if b.get("username")]
                return send_json(self,200,{"ok":True,"count":len(safe),"bots":safe}, "public, max-age=10")
            if path=="/api" or path=="/api/health":
                pool_file=str(getattr(permanent_bot_store,"STORE_PATH", "")) if permanent_bot_store is not None else ""
                try:
                    migration_stats = db.get_delivery_link_stats()
                except Exception:
                    migration_stats = {}
                return send_json(self,200,{"ok":True,"service":"video-vault","db":"shared","time":now_iso(),"api_version":"9.8.0","permanent_pool_store_loaded":bool(permanent_bot_store is not None),"permanent_pool_file":pool_file,"resolver_url":_watch_resolver_base()+"/watch","delivery_link_stats":migration_stats})
            if path=="/api/resolver-status":
                rows = permanent_bot_store.bot_health_snapshot(90) if permanent_bot_store is not None else []
                routable=permanent_bot_store.routable_delivery_bots(180) if permanent_bot_store is not None else []
                return send_json(self,200,{"ok":True,"resolver":"/watch","active":sum(1 for r in rows if r.get("active")),"healthy":sum(1 for r in rows if r.get("healthy")),"routable":len(routable),"store_loaded":bool(permanent_bot_store is not None),"bots":[{"id":str(r.get("id")),"username":str(r.get("username") or "").lstrip("@"),"active":bool(r.get("active")),"healthy":bool(r.get("healthy")),"ready":bool(r.get("ready")),"age_seconds":r.get("age_seconds"),"delivery_success":int(r.get("delivery_success_count") or 0),"delivery_failures":int(r.get("delivery_failure_count") or 0)} for r in rows]})
            if path=="/api/delivery-link-stats":
                try:
                    stats = db.get_delivery_link_stats()
                except Exception:
                    stats = {}
                return send_json(self,200,{"ok":True,"stats":stats})
            if path=="/api/search":
                qv=q.get("q",[""])[0]
                lim=max(1,min(100,int(q.get("limit",[20])[0])))
                pg=max(1,int(q.get("page",[1])[0]))
                return send_json(self,200,_paged_from_rows(db.advanced_search(qv,limit=200,visible_only=True),pg,lim))
            if path=="/api/home":
                payload = {
                    "ok": True,
                    "version": "9.8.0",
                    "today": _raw_catalog_rows("today", 1, 100),
                    "popular": _raw_catalog_rows("popular", 1, 100),
                    "premium_today": _raw_catalog_rows("premium_today", 1, 100),
                    "premium_popular": _raw_catalog_rows("premium_popular", 1, 100),
                }
                return send_json(self,200,payload, "public, max-age=15, stale-while-revalidate=30")
            if path=="/api/meta":
                with db.get_conn() as conn:
                    total=conn.execute(f"SELECT COUNT(*) c FROM videos WHERE {visible_clause()}",visibility_params()).fetchone()["c"]
                    premium=conn.execute(f"SELECT COUNT(*) c FROM videos WHERE {visible_clause()} AND COALESCE(access_tier,'free') NOT IN ('free','')",visibility_params()).fetchone()["c"]
                    collections=conn.execute(f"SELECT COUNT(DISTINCT batch_id) c FROM videos WHERE {visible_clause()} AND batch_id IS NOT NULL",visibility_params()).fetchone()["c"]
                return send_json(self,200,{"ok":True,"videos":int(total),"premium":int(premium),"collections":int(collections),"time":now_iso()}, "public, max-age=30, stale-while-revalidate=60")
            if path=="/api/catalog":
                return send_json(self,200,_raw_catalog_rows(
                    q.get("mode",["today"])[0],
                    q.get("page",[1])[0],
                    q.get("limit",[24])[0],
                    q.get("q",[""])[0],
                    q.get("date",[db.today_str()])[0]
                ), "public, max-age=8, stale-while-revalidate=20")
            if path=="/api/videos":
                if q.get("id"):
                    v=get_video(q["id"][0])
                    return send_json(self,200,{"video":v} if v else {"videos":[],"total":0})
                if q.get("batch_id"):
                    bid=q["batch_id"][0]
                    with db.get_conn() as conn:
                        rows=conn.execute(f"SELECT * FROM videos WHERE {visible_clause()} AND batch_id=? ORDER BY created_at ASC",visibility_params()+[bid]).fetchall()
                        brow=conn.execute("SELECT * FROM batches WHERE id=?",(bid,)).fetchone()
                    if not rows:
                        return send_json(self,200,{"videos":[],"total":0,"pages":1,"collection":None})
                    # Return every visible item so the web UI can render the collection instead of
                    # pretending the collection contains a single video.
                    b=dict(brow) if brow else {}
                    if b.get("cover_file_id"):
                        rows2=[]
                        for r in rows:
                            d=dict(r); d["cover_file_id"]=b["cover_file_id"]; rows2.append(d)
                        rows=rows2
                    collection=batch_public(bid,rows,brow)
                    public_rows=[]
                    for r in rows:
                        item=video_public(dict(r))
                        if item is not None:
                            public_rows.append(item)
                    return send_json(self,200,{"videos":public_rows,"total":len(public_rows),"page":1,"pages":1,"collection":collection})
                mode=q.get("mode",["today"])[0]
                date_value=q.get("date",[db.today_str()])[0]
                return send_json(self,200,_raw_catalog_rows(mode,q.get("page",[1])[0],q.get("limit",[24])[0],q.get("q",[""])[0],date_value))
            if path=="/api/calendar":
                return send_json(self,200,{"dates":calendar_counts(q.get("month",[db.today_str()[:7]])[0])})
            if path=="/api/cover":
                return self.cover(q.get("id",[""])[0])
            return send_json(self,404,{"error":"not_found"})
        except Exception as e:
            print("[VideoVault API] error",self.request_id,repr(e),flush=True)
            return send_json(self,500,{"error":"server_error","request_id":self.request_id,"route":path})

    def do_POST(self):
        self.request_id = uuid.uuid4().hex[:12]
        u=urllib.parse.urlparse(self.path)
        if u.path.rstrip("/")!="/api/watch":
            return send_json(self,404,{"error":"not_found"})
        try:
            if not _rate_allowed(self, "watch", _WATCH_LIMIT, _WATCH_WINDOW):
                return send_json(self,429,{"error":"rate_limited","request_id":self.request_id})
            n=int(self.headers.get("Content-Length","0") or 0)
            body=json.loads(self.rfile.read(n) or b"{}")
            vid=str(body.get("video_id") or "")
            if not vid: return send_json(self,400,{"error":"video_id_required"})
            v=db.get_video(vid)
            if not v or not db.is_visible(v):
                return send_json(self,404,{"error":"video_not_found","request_id":self.request_id})
            db.increment_view(vid)
            target = stable_watch_url(video_id=vid)
            return send_json(self,200,{"ok":True,"watch_url":target})
        except Exception as e:
            print("[VideoVault API] watch error",self.request_id,repr(e),flush=True)
            return send_json(self,500,{"error":"server_error","request_id":self.request_id,"route":"/api/watch"})

    def _send_cover_placeholder(self):
        # A real image response prevents the browser from treating a missing
        # cover as a transient network failure and showing an endless Retry UI.
        svg=b"<svg xmlns=\"http://www.w3.org/2000/svg\" width=960 height=540 viewBox=\"0 0 960 540\"><rect width=\"960\" height=\"540\" fill=\"#171923\"/><text x=\"480\" y=\"280\" text-anchor=\"middle\" fill=\"#8b5cf6\" font-family=\"system-ui,sans-serif\" font-size=\"34\">Video Vault</text><text x=\"480\" y=\"325\" text-anchor=\"middle\" fill=\"#777b8b\" font-family=\"system-ui,sans-serif\" font-size=\"20\">No cover available</text></svg>"
        self.send_response(200)
        self.send_header("Content-Type","image/svg+xml; charset=utf-8")
        self.send_header("Cache-Control","public, max-age=300")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Content-Length",str(len(svg)))
        self.send_header("X-VideoVault-Request-ID", getattr(self,"request_id",""))
        self.send_header("X-VideoVault-API-Version","9.8.0")
        self.end_headers()
        self.wfile.write(svg)

    def cover(self, vid):
        if not _rate_allowed(self, "cover", _COVER_LIMIT, _COVER_WINDOW):
            return send_json(self,429,{"error":"rate_limited","request_id":self.request_id})
        # Cover requests are public too; never expose a future scheduled item's cover.
        with db.get_conn() as conn:
            row=conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        if not row or not _safe_visible(row) or not row["cover_file_id"]:
            return self._send_cover_placeholder()
        token=getattr(config,"STORAGE_BOT_TOKEN","")
        if not token:
            return send_json(self,503,{"error":"cover_service_unavailable","request_id":self.request_id})

        # Cache by Telegram file_id, not video id. Bulk collections often share
        # one cover, so this prevents duplicate Telegram downloads.
        file_id=str(row["cover_file_id"])
        key=hashlib.sha256(file_id.encode()).hexdigest()
        webp_path=os.path.join(COVER_CACHE_DIR, key+".webp")
        raw_path=os.path.join(COVER_CACHE_DIR, key+".bin")

        # Fast path: already optimized on disk.
        if os.path.exists(webp_path):
            try:
                return self._send_cover_file(webp_path, "image/webp")
            except OSError:
                pass
        if os.path.exists(raw_path):
            try:
                return self._send_cover_file(raw_path, "image/jpeg")
            except OSError:
                pass

        # Lock only this cover. Other covers can download in parallel.
        with _cover_lock(key):
            if os.path.exists(webp_path):
                return self._send_cover_file(webp_path, "image/webp")
            if os.path.exists(raw_path):
                return self._send_cover_file(raw_path, "image/jpeg")

            try:
                fp=_cached_file_path(file_id,token)
                with urllib.request.urlopen(
                    f"https://api.telegram.org/file/bot{token}/{fp}",timeout=25
                ) as r:
                    blob=r.read()
                    ctype=r.headers.get("Content-Type","image/jpeg")
            except Exception as e:
                print("[VideoVault API] cover download error",self.request_id,repr(e),flush=True)
                self.send_response(504); self.end_headers(); return

            if Image is not None:
                try:
                    from io import BytesIO
                    im=Image.open(BytesIO(blob))
                    if ImageOps is not None:
                        im=ImageOps.exif_transpose(im)
                    im.thumbnail((960,540), Image.Resampling.LANCZOS)
                    if im.mode not in ("RGB","RGBA"):
                        im=im.convert("RGB")
                    tmp=webp_path+".tmp"
                    im.save(tmp,"WEBP",quality=78,method=6)
                    os.replace(tmp,webp_path)
                    return self._send_cover_file(webp_path,"image/webp")
                except Exception:
                    pass

            tmp=raw_path+".tmp"
            with open(tmp,"wb") as f: f.write(blob)
            os.replace(tmp,raw_path)
            return self._send_cover_file(raw_path,ctype)

    def _send_cover_file(self, path, ctype):
        size=os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type",ctype)
        self.send_header("Cache-Control","public, max-age=604800, immutable")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Content-Length",str(size))
        self.send_header("X-VideoVault-Request-ID", getattr(self, "request_id", ""))
        self.send_header("X-VideoVault-API-Version", "v9")
        self.end_headers()
        with open(path,"rb") as f:
            while True:
                chunk=f.read(1024*64)
                if not chunk: break
                self.wfile.write(chunk)

if __name__=="__main__":
    db.init_db()
    print(f"🌐 Video Vault API listening on {HOST}:{PORT}",flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()


# Schedule hard-lock audit:
# Keep all public catalogue/search/random/collection responses filtered by
# _filter_public_videos() before serialization. Admin-only endpoints must not
# call this helper unless they intentionally want public visibility.


