# V6 exception triage: intentionally swallowed exceptions in this module
# are limited to best-effort cleanup/compatibility fallbacks; user-visible or
# persistence failures are logged or surfaced by their surrounding handlers.
"""
Shared SQLite database layer used by all three bots.
Single source of truth: video metadata + file references in both channels.
"""
import sqlite3
import logging
import secrets
import json
import random
import re
from datetime import datetime, date, timedelta
from contextlib import contextmanager

import config

log = logging.getLogger("video_vault_db")


def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode lets readers and writers coexist without blocking each other —
    # important here since 4 separate bot *processes* all share this one file.
    # busy_timeout makes a writer wait for a lock instead of failing instantly
    # with "database is locked" during a brief overlap.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,              -- short unique slug, used in deep links
                title TEXT NOT NULL,
                description TEXT,
                tags TEXT,                        -- comma-separated
                cover_file_id TEXT,                -- file_id as seen by storage bot (not portable to other bots)
                cover_msg_id INTEGER,              -- message id of the cover photo in PRIMARY_CHANNEL_ID (portable)
                primary_msg_id INTEGER NOT NULL,   -- message id in PRIMARY_CHANNEL_ID
                backup_msg_id INTEGER,             -- message id in BACKUP_CHANNEL_ID (nullable if backup pending)
                recovery_msg_id INTEGER,            -- optional 3rd content channel message id
                duration_seconds INTEGER,          -- video length, if Telegram provided it
                file_size_bytes INTEGER,           -- video file size, if Telegram provided it
                upload_date TEXT NOT NULL,         -- ISO date, e.g. 2026-08-16 (local tz)
                created_at TEXT NOT NULL,          -- ISO datetime, full timestamp
                view_count INTEGER NOT NULL DEFAULT 0,
                alerted INTEGER NOT NULL DEFAULT 0, -- 0 = not yet announced, 1 = included in an alert
                uploader_id INTEGER,
                publish_at TEXT,                   -- ISO datetime; NULL = visible now, future = hidden until then
                batch_id TEXT,                     -- NULL for normal uploads; shared by bulk uploads
                video_number INTEGER UNIQUE,        -- human-facing stable number; never reused
                media_type TEXT NOT NULL DEFAULT 'video', -- video | photo
                source_chat_id INTEGER,
                source_message_id INTEGER,
                source_media_group_id TEXT,
                media_group_id TEXT                -- legacy compatibility alias
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_uploads (
                uploader_id INTEGER PRIMARY KEY,   -- one in-progress upload per admin at a time
                video_file_id TEXT,
                primary_msg_id INTEGER,
                backup_msg_id INTEGER,
                recovery_msg_id INTEGER,
                cover_file_id TEXT,
                cover_msg_id INTEGER,
                duration_seconds INTEGER,
                file_size_bytes INTEGER,
                title TEXT,
                tags TEXT,
                description TEXT,
                scheduled_at TEXT,                  -- ISO datetime, carried into videos.publish_at on save
                stage TEXT NOT NULL DEFAULT 'awaiting_cover',
                edit_return INTEGER NOT NULL DEFAULT 0,  -- 1 = mid-edit from preview, return to preview after this field
                video_number INTEGER,                -- optional manual number; NULL = auto assign on save
                media_type TEXT NOT NULL DEFAULT 'video',
                source_chat_id INTEGER,
                source_message_id INTEGER,
                media_group_id TEXT,
                cover_source_chat_id INTEGER,
                cover_source_message_id INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bulk_sessions (
                uploader_id INTEGER PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_date ON videos(upload_date)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                tags TEXT,
                cover_file_id TEXT,
                cover_msg_id INTEGER,
                uploader_id INTEGER,
                created_at TEXT NOT NULL,
                publish_at TEXT
            )
        """)
        # Existing v19 databases need the new nullable column added in-place.
        # Pending uploads retain the original user message until final confirmation;
        # primary/backup copies are created only after the upload is finalized.
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(pending_uploads)").fetchall()}
        if "source_chat_id" not in pcols:
            conn.execute("ALTER TABLE pending_uploads ADD COLUMN source_chat_id INTEGER")
        if "source_message_id" not in pcols:
            conn.execute("ALTER TABLE pending_uploads ADD COLUMN source_message_id INTEGER")
        if "media_group_id" not in pcols:
            conn.execute("ALTER TABLE pending_uploads ADD COLUMN media_group_id TEXT")
        if "cover_source_chat_id" not in pcols:
            conn.execute("ALTER TABLE pending_uploads ADD COLUMN cover_source_chat_id INTEGER")
        if "cover_source_message_id" not in pcols:
            conn.execute("ALTER TABLE pending_uploads ADD COLUMN cover_source_message_id INTEGER")

        # Existing databases created before bulk access controls need these batch columns.
        bcols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)").fetchall()}
        for _col, _typ in (
            ("access_tier", "TEXT"),
            ("access_redeem_code", "TEXT"),
            ("access_user_ids", "TEXT"),
            ("category", "TEXT NOT NULL DEFAULT 'Global'"),
            ("subcategory", "TEXT"),
        ):
            if _col not in bcols:
                conn.execute(f"ALTER TABLE batches ADD COLUMN {_col} {_typ}")

        # ---- In-place schema migration for older Video Vault databases ----
        # Earlier builds created a smaller videos/pending_uploads schema but the
        # current save flow writes scheduling, access-control, media-source and
        # media-type fields.  Missing columns here caused Confirm & Save to fail
        # only when a scheduled item reached the final DB INSERT, which made the
        # bot appear to go silent after the button press.
        def _add_column_if_missing(table, column, definition):
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        # videos columns used by finalize_pending/finalize_bulk and audience filters
        for _col, _def in (
            ("view_count", "INTEGER NOT NULL DEFAULT 0"),
            ("alerted", "INTEGER NOT NULL DEFAULT 0"),
            ("uploader_id", "INTEGER"),
            ("publish_at", "TEXT"),
            ("batch_id", "TEXT"),
            ("access_tier", "TEXT DEFAULT 'free'"),
            ("access_redeem_code", "TEXT"),
            ("access_user_ids", "TEXT"),
            ("video_number", "INTEGER"),
            ("media_type", "TEXT NOT NULL DEFAULT 'video'"),
            ("source_chat_id", "INTEGER"),
            ("source_message_id", "INTEGER"),
            ("source_media_group_id", "TEXT"),
            ("media_group_id", "TEXT"),
            ("category", "TEXT NOT NULL DEFAULT 'Global'"),
            ("subcategory", "TEXT"),
        ):
            _add_column_if_missing("videos", _col, _def)

        # pending_uploads columns used by the multi-step save/schedule flow
        for _col, _def in (
            ("video_file_id", "TEXT"),
            ("primary_msg_id", "INTEGER"),
            ("backup_msg_id", "INTEGER"),
            ("cover_file_id", "TEXT"),
            ("cover_msg_id", "INTEGER"),
            ("duration_seconds", "INTEGER"),
            ("file_size_bytes", "INTEGER"),
            ("title", "TEXT"),
            ("tags", "TEXT"),
            ("description", "TEXT"),
            ("scheduled_at", "TEXT"),
            ("stage", "TEXT NOT NULL DEFAULT 'awaiting_cover'"),
            ("edit_return", "INTEGER NOT NULL DEFAULT 0"),
            ("video_number", "INTEGER"),
            ("media_type", "TEXT NOT NULL DEFAULT 'video'"),
            ("source_chat_id", "INTEGER"),
            ("source_message_id", "INTEGER"),
            ("media_group_id", "TEXT"),
            ("cover_source_chat_id", "INTEGER"),
            ("cover_source_message_id", "INTEGER"),
            ("category", "TEXT NOT NULL DEFAULT 'Global'"),
            ("subcategory", "TEXT"),
        ):
            _add_column_if_missing("pending_uploads", _col, _def)

        # Keep the idempotency lookup fast and safe for new uploads.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_source "
            "ON videos(source_chat_id, source_message_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_batch ON videos(batch_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS video_numbers (
                number INTEGER PRIMARY KEY,
                video_id TEXT,
                reserved_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_video_numbers_video ON video_numbers(video_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS batch_delivery_progress (
                user_id INTEGER NOT NULL,
                batch_id TEXT NOT NULL,
                next_index INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'paused',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, batch_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_progress_user ON batch_delivery_progress(user_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS storage_recovery_jobs (
                id TEXT PRIMARY KEY,
                admin_user_id INTEGER NOT NULL,
                source_channel_id INTEGER NOT NULL,
                dest_channel_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'paused',
                next_index INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                copied INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_storage_recovery_jobs_user ON storage_recovery_jobs(admin_user_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS storage_recovery_items (
                job_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                dest_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (job_id, video_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_storage_recovery_items_status ON storage_recovery_items(job_id, status)")
        # V9.6.5: richer recovery checkpoints for media + cover migration.
        # Safe for existing databases; ALTER only when the columns are missing.
        existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(storage_recovery_items)").fetchall()}
        if "dest_cover_message_id" not in existing_cols:
            conn.execute("ALTER TABLE storage_recovery_items ADD COLUMN dest_cover_message_id INTEGER")
        if "cover_status" not in existing_cols:
            conn.execute("ALTER TABLE storage_recovery_items ADD COLUMN cover_status TEXT NOT NULL DEFAULT 'pending'")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_deletes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_name TEXT NOT NULL,     -- which bot owns this (e.g. 'catalog', 'delivery')
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at TEXT NOT NULL     -- ISO datetime; survives restarts so nothing gets stranded
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT,           -- 'storage_bot', 'admin_bot', etc.
                action TEXT NOT NULL, -- short verb, e.g. 'upload', 'delete', 'alert_posted'
                detail TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS reactions (
                video_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                emoji_code TEXT NOT NULL,   -- short code, e.g. 'like', 'fire' — see reactions.py
                created_at TEXT NOT NULL,
                PRIMARY KEY (video_id, user_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reactions_video ON reactions(video_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_reactions (
                alert_key TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                emoji_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (alert_key, user_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_reactions_key ON alert_reactions(alert_key)")

# --- Product analytics: lightweight event stream used by Admin Analytics ---
        # Events are append-only and intentionally separate from activity_log so
        # public-user behaviour can be aggregated without exposing user IDs in the UI.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                event_type TEXT NOT NULL,   -- start | search | delivery | ad_unlock | redeem
                user_id INTEGER,
                video_id TEXT,
                value INTEGER NOT NULL DEFAULT 1,
                detail TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_ts ON analytics_events(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_type_ts ON analytics_events(event_type, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_video ON analytics_events(video_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_user ON analytics_events(user_id)")

        # Alert registry keeps the exact category totals attached to the alert
        # that was posted. This lets reaction taps rebuild the same buttons
        # without recalculating against newer uploads.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_registry (
                alert_key TEXT PRIMARY KEY,
                video_ids TEXT NOT NULL,
                indian_count INTEGER NOT NULL DEFAULT 0,
                global_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_registry_created ON alert_registry(created_at)")

        # Delivery-link registry for direct t.me catalogue buttons.  The
        # database records which permanent Delivery Bot owns each generated
        # link and which catalogue message contains it, so dead-bot links can
        # be regenerated and the message button can be updated automatically.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS delivery_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                delivery_bot_id TEXT NOT NULL,
                delivery_username TEXT,
                delivery_url TEXT NOT NULL,
                chat_id INTEGER,
                message_id INTEGER,
                button_label TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Direct-link migration bookkeeping. Older DBs are upgraded in-place.
        dcols = {r["name"] for r in conn.execute("PRAGMA table_info(delivery_links)").fetchall()}
        for _col, _def in (
            ("previous_delivery_bot_id", "TEXT"),
            ("previous_delivery_url", "TEXT"),
            ("migration_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("migration_error", "TEXT"),
            ("last_migration_at", "TEXT"),
        ):
            if _col not in dcols:
                conn.execute(f"ALTER TABLE delivery_links ADD COLUMN {_col} {_def}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_links_bot ON delivery_links(delivery_bot_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_links_target ON delivery_links(target_type, target_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_links_message ON delivery_links(chat_id, message_id)")

        # Legacy builds called the second category "Others". Migrate it to
        # the user-facing name "Global" once, while normalize_category still
        # accepts "Others" for old callbacks/config values.
        for _table in ("videos", "batches", "pending_uploads"):
            try:
                conn.execute(f"UPDATE {_table} SET category = 'Global' WHERE category IS NULL OR LOWER(TRIM(category)) IN ('others', 'other', 'global', '')")
            except sqlite3.OperationalError:
                pass

        # Tags are the single source of truth for catalogue subcategories.
        # Migrate any legacy dedicated subcategory into tags once, then clear
        # the legacy field so new/old records cannot drift apart.
        for _table in ("videos", "batches"):
            try:
                _rows = conn.execute(f"SELECT rowid, tags, subcategory FROM {_table} WHERE TRIM(COALESCE(subcategory, '')) <> ''").fetchall()
                for _r in _rows:
                    _sub = str(_r["subcategory"]).strip()
                    _tags = str(_r["tags"] or "").strip()
                    _parts = [x.strip() for x in _tags.split(",") if x.strip()]
                    if _sub.lower() not in {x.lower() for x in _parts}:
                        _parts.append(_sub)
                    conn.execute(f"UPDATE {_table} SET tags = ?, subcategory = NULL WHERE rowid = ?", (", ".join(_parts), _r["rowid"]))
            except sqlite3.OperationalError:
                pass

        # --- Access control: per-user daily limits, gated videos, redeem codes ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_access (
                user_id INTEGER PRIMARY KEY,
                premium_until TEXT,              -- ISO datetime; NULL/past = not premium (redeem membership)
                ad_member_until TEXT,             -- ISO datetime; NULL/past = no temporary ad membership
                daily_limit_override INTEGER,    -- NULL = use global default; -1 = unlimited for this user
                watch_count_today INTEGER NOT NULL DEFAULT 0,
                watch_count_date TEXT
            )
        """)

        # Migrate older databases that predate temporary 24h ad membership.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_access)").fetchall()}
        if "ad_member_until" not in cols:
            conn.execute("ALTER TABLE user_access ADD COLUMN ad_member_until TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code TEXT PRIMARY KEY,
                kind TEXT NOT NULL,              -- 'premium' | 'giveaway'
                duration_days INTEGER NOT NULL,  -- days of premium access granted on redemption
                max_redemptions INTEGER,         -- NULL = unlimited uses (typical for giveaways)
                redemption_count INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                note TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS code_redemptions (
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL,
                PRIMARY KEY (code, user_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS unlock_tokens (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                video_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_unlocks (
                user_id INTEGER NOT NULL,
                video_id TEXT NOT NULL,
                unlocked_at TEXT NOT NULL,
                expires_at TEXT,
                PRIMARY KEY (user_id, video_id)
            )
        """)
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(ad_unlocks)").fetchall()}
            if "expires_at" not in cols:
                conn.execute("ALTER TABLE ad_unlocks ADD COLUMN expires_at TEXT")
        except Exception:
            log.warning("Optional ad_unlocks migration check failed", exc_info=True)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS membership_warnings (
                user_id INTEGER NOT NULL,
                warning_type TEXT NOT NULL,
                premium_until TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (user_id, warning_type, premium_until)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_notifications (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_sent_at TEXT,
                last_video_id TEXT
            )
        """)

        # Migrations for databases created before these columns existed.
        for table, col, coltype in [
            ("videos", "video_number", "INTEGER"),
            ("videos", "media_type", "TEXT NOT NULL DEFAULT 'video'"),
            ("videos", "cover_msg_id", "INTEGER"),
            ("videos", "duration_seconds", "INTEGER"),
            ("videos", "file_size_bytes", "INTEGER"),
            ("videos", "source_chat_id", "INTEGER"),
            ("videos", "source_message_id", "INTEGER"),
            ("videos", "source_media_group_id", "TEXT"),
            ("videos", "media_group_id", "TEXT"),
            ("videos", "publish_at", "TEXT"),
            ("videos", "access_tier", "TEXT NOT NULL DEFAULT 'free'"),  # free | ad | redeem | redeem_or_ad | members | users | gated
            ("videos", "access_redeem_code", "TEXT"),
            ("videos", "access_user_ids", "TEXT"),
            ("batches", "access_tier", "TEXT NOT NULL DEFAULT 'free'"),
            ("batches", "access_redeem_code", "TEXT"),
            ("batches", "access_user_ids", "TEXT"),
            ("pending_uploads", "cover_msg_id", "INTEGER"),
            ("pending_uploads", "access_tier", "TEXT NOT NULL DEFAULT 'free'"),
            ("pending_uploads", "access_redeem_code", "TEXT"),
            ("pending_uploads", "access_user_ids", "TEXT"),
            ("pending_uploads", "edit_return", "INTEGER NOT NULL DEFAULT 0"),
            ("pending_uploads", "duration_seconds", "INTEGER"),
            ("pending_uploads", "media_type", "TEXT NOT NULL DEFAULT 'video'"),
            ("pending_uploads", "file_size_bytes", "INTEGER"),
            ("pending_uploads", "scheduled_at", "TEXT"),
            ("scheduled_deletes", "video_id", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists

        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_videos_source ON videos(source_chat_id, source_message_id) WHERE source_chat_id IS NOT NULL AND source_message_id IS NOT NULL")
        except sqlite3.IntegrityError:
            # Old databases can theoretically contain duplicate source rows from
            # a pre-idempotency build. Do not brick startup; keep a fast lookup
            # index and let the new save guard prevent any further duplicates.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_source ON videos(source_chat_id, source_message_id)")

        existing = conn.execute("SELECT id, video_number FROM videos ORDER BY created_at ASC, rowid ASC").fetchall()
        reserved = {int(r[0]) for r in conn.execute("SELECT number FROM video_numbers").fetchall()}
        next_n = max(reserved) + 1 if reserved else 1
        for row in existing:
            n = row["video_number"]
            if n is None:
                while next_n in reserved:
                    next_n += 1
                n = next_n
                conn.execute("UPDATE videos SET video_number = ? WHERE id = ?", (n, row["id"]))
                next_n += 1
            n = int(n)
            if n not in reserved:
                conn.execute("INSERT OR IGNORE INTO video_numbers(number, video_id, reserved_at) VALUES (?, ?, ?)", (n, row["id"], now_str()))
                reserved.add(n)


def md_escape(text) -> str:
    """Escape Telegram legacy-Markdown special characters so arbitrary
    user-supplied text (titles, tags, descriptions, custom alert captions,
    search keywords) can never break message formatting or cause a
    'can't parse entities' send failure. Order matters — backslash must be
    escaped first, or we'd double-escape the backslashes we just inserted."""
    if not text:
        return text
    text = str(text)
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def _video_id_exists(video_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM videos WHERE id = ?", (video_id,)).fetchone()
        return row is not None


def gen_id() -> str:
    """Short unique slug used in deep links. Generates extra source bytes so
    stripping URL-unsafe '-'/'_' before slicing can't leave a short ID, and
    retries on the (astronomically unlikely, but not impossible) chance of a
    collision with an existing video id."""
    for _ in range(10):
        raw = secrets.token_urlsafe(12).replace("-", "").replace("_", "")
        candidate = raw[:8]
        if len(candidate) == 8 and not _video_id_exists(candidate):
            return candidate
    # Fallback: essentially unreachable, but never return a malformed id.
    raise RuntimeError("Could not generate a unique video id after 10 attempts")


def today_str() -> str:
    return datetime.now(config.TIMEZONE).date().isoformat()


def now_str() -> str:
    return datetime.now(config.TIMEZONE).isoformat()


# ---------- stable human-facing video numbers ----------

def next_video_number(count: int = 1) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(MAX(number), 0) m FROM video_numbers").fetchone()
        return int(row["m"] or 0) + 1


def video_number_available(number: int, count: int = 1) -> tuple[bool, str]:
    try: start = int(number)
    except (TypeError, ValueError): return False, "Video number must be a positive integer."
    if start < 1: return False, "Video number must be 1 or higher."
    with get_conn() as conn:
        for n in range(start, start + max(1, int(count))):
            if conn.execute("SELECT 1 FROM video_numbers WHERE number = ?", (n,)).fetchone():
                return False, f"Video #{n} is already used or reserved. Deleted numbers are never reused."
    return True, ""


def get_video_by_number(number: int):
    try: n = int(number)
    except (TypeError, ValueError): return None
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM videos WHERE video_number = ?", (n,)).fetchone()
        return dict(row) if row else None


# ---------- upload source + persistent bulk sessions ----------

def find_possible_duplicate_videos(file_size_bytes: int | None, duration_seconds: int | None, limit: int = 5):
    """Find non-destructive upload warnings using stable Telegram media traits.

    This intentionally returns candidates rather than declaring a duplicate: size +
    duration can collide, so the uploader remains in control and can save anyway.
    """
    if not file_size_bytes or not duration_seconds:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE media_type = 'video' AND file_size_bytes = ? "
            "AND duration_seconds BETWEEN ? AND ? ORDER BY created_at DESC LIMIT ?",
            (int(file_size_bytes), max(0, int(duration_seconds) - 1), int(duration_seconds) + 1, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_video_by_source(source_chat_id: int, source_message_id: int):
    if source_chat_id is None or source_message_id is None:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE source_chat_id = ? AND source_message_id = ?",
            (int(source_chat_id), int(source_message_id)),
        ).fetchone()
        return dict(row) if row else None

def save_bulk_session(uploader_id: int, state: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bulk_sessions(uploader_id, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(uploader_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            (int(uploader_id), json.dumps(state, separators=(",", ":")), now_str()),
        )

def get_bulk_session(uploader_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT state_json FROM bulk_sessions WHERE uploader_id = ?", (int(uploader_id),)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["state_json"] or "{}")
            return value if isinstance(value, dict) else None
        except Exception:
            log.warning("Invalid bulk session JSON; treating it as empty")
            return None

def clear_bulk_session(uploader_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM bulk_sessions WHERE uploader_id = ?", (int(uploader_id),))

# ---------- pending upload (multi-step form state) ----------

def start_pending(uploader_id: int, video_file_id: str, primary_msg_id: int, backup_msg_id: int | None,
                   duration_seconds: int | None = None, file_size_bytes: int | None = None):
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_uploads WHERE uploader_id = ?", (uploader_id,))
        conn.execute(
            "INSERT INTO pending_uploads "
            "(uploader_id, video_file_id, primary_msg_id, backup_msg_id, duration_seconds, file_size_bytes, stage) "
            "VALUES (?, ?, ?, ?, ?, ?, 'awaiting_cover')",
            (uploader_id, video_file_id, primary_msg_id, backup_msg_id, duration_seconds, file_size_bytes),
        )


def get_pending(uploader_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pending_uploads WHERE uploader_id = ?", (uploader_id,)).fetchone()
        return dict(row) if row else None


def update_pending(uploader_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [uploader_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE pending_uploads SET {cols} WHERE uploader_id = ?", vals)


def clear_pending(uploader_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_uploads WHERE uploader_id = ?", (uploader_id,))


def _ensure_finalize_schema(conn):
    """Repair/verify the live SQLite schema immediately before a final save.

    Some deployed databases were created by older Video Vault releases.  The
    bot code may be newer than the persistent DB, so relying only on startup
    migration can leave a final INSERT with a stale column layout.  Keep this
    tiny repair pass idempotent and cheap, and make the final save self-healing.
    """
    specs = {
        "videos": {
            "access_tier": "TEXT DEFAULT 'free'",
            "access_redeem_code": "TEXT",
            "access_user_ids": "TEXT",
            "category": "TEXT NOT NULL DEFAULT 'Global'",
            "subcategory": "TEXT",
            "batch_id": "TEXT",
            "video_number": "INTEGER",
            "media_type": "TEXT NOT NULL DEFAULT 'video'",
            "source_chat_id": "INTEGER",
            "source_message_id": "INTEGER",
            "source_media_group_id": "TEXT",
            "media_group_id": "TEXT",
            "publish_at": "TEXT",
            "uploader_id": "INTEGER",
            "recovery_msg_id": "INTEGER",
        },
        "batches": {
            "access_tier": "TEXT",
            "access_redeem_code": "TEXT",
            "access_user_ids": "TEXT",
            "category": "TEXT NOT NULL DEFAULT 'Global'",
            "subcategory": "TEXT",
        },
        "pending_uploads": {
            "scheduled_at": "TEXT",
            "video_number": "INTEGER",
            "media_type": "TEXT NOT NULL DEFAULT 'video'",
            "source_chat_id": "INTEGER",
            "source_message_id": "INTEGER",
            "media_group_id": "TEXT",
            "cover_source_chat_id": "INTEGER",
            "cover_source_message_id": "INTEGER",
            "recovery_msg_id": "INTEGER",
            "category": "TEXT NOT NULL DEFAULT 'Global'",
            "subcategory": "TEXT",
        },
    }
    for table, wanted in specs.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in wanted.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def finalize_pending(uploader_id: int) -> str:
    p = get_pending(uploader_id)
    if not p: raise ValueError("No pending upload to finalize")
    existing = get_video_by_source(p.get("source_chat_id"), p.get("source_message_id"))
    if existing:
        clear_pending(uploader_id)
        return existing["id"]
    vid = gen_id()
    with get_conn() as conn:
        _ensure_finalize_schema(conn)
        manual = p.get("video_number")
        if manual is None:
            row = conn.execute("SELECT COALESCE(MAX(number), 0) m FROM video_numbers").fetchone()
            video_number = int(row["m"] or 0) + 1
            while conn.execute("SELECT 1 FROM video_numbers WHERE number = ?", (video_number,)).fetchone(): video_number += 1
        else:
            video_number = int(manual)
            if video_number < 1 or conn.execute("SELECT 1 FROM video_numbers WHERE number = ?", (video_number,)).fetchone():
                raise ValueError(f"Video #{video_number} is already used/reserved.")
        conn.execute(
            """INSERT INTO videos
               (id, title, description, tags, cover_file_id, cover_msg_id, primary_msg_id, backup_msg_id, recovery_msg_id,
                duration_seconds, file_size_bytes, upload_date, created_at, uploader_id, publish_at,
                access_tier, access_redeem_code, access_user_ids, video_number, media_type,
                source_chat_id, source_message_id, source_media_group_id, media_group_id, category, subcategory)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) """,
            (vid, p["title"], p.get("description"), p.get("tags"), p.get("cover_file_id"), p.get("cover_msg_id"),
             p["primary_msg_id"], p.get("backup_msg_id"), p.get("recovery_msg_id"), p.get("duration_seconds"), p.get("file_size_bytes"),
             ((datetime.fromisoformat(p.get("scheduled_at")).astimezone(config.TIMEZONE).date().isoformat()) if p.get("scheduled_at") else today_str()), now_str(), uploader_id, p.get("scheduled_at"),
             p.get("access_tier") or "free", p.get("access_redeem_code"), p.get("access_user_ids"), video_number, p.get("media_type") or "video",
             p.get("source_chat_id"), p.get("source_message_id"), p.get("media_group_id"), p.get("media_group_id"),
             p.get("category") or "Global", p.get("subcategory")))
        conn.execute("INSERT INTO video_numbers(number, video_id, reserved_at) VALUES (?, ?, ?)", (video_number, vid, now_str()))
    clear_pending(uploader_id)
    return vid



def gen_batch_id() -> str:
    for _ in range(10):
        raw = secrets.token_urlsafe(12).replace("-", "").replace("_", "")
        candidate = "b" + raw[:9]
        with get_conn() as conn:
            if not conn.execute("SELECT 1 FROM batches WHERE id = ?", (candidate,)).fetchone():
                return candidate
    raise RuntimeError("Could not generate a unique batch id")


def finalize_bulk(uploader_id: int, items: list[dict], title: str, tags: str = "", description: str = "",
                  cover_file_id: str | None = None, cover_msg_id: int | None = None,
                  scheduled_at: str | None = None, access_tier: str = "free",
                  access_redeem_code: str | None = None, access_user_ids: str | None = None,
                  start_number: int | None = None, category: str = "Global", subcategory: str | None = None) -> tuple[str, list[str]]:
    if not items:
        raise ValueError("Bulk upload is empty")
    batch_id = gen_batch_id()
    created = now_str()
    day = ((datetime.fromisoformat(scheduled_at).astimezone(config.TIMEZONE).date().isoformat()) if scheduled_at else today_str())
    video_ids = []
    with get_conn() as conn:
        _ensure_finalize_schema(conn)
        conn.execute(
            """INSERT INTO batches
               (id, title, description, tags, cover_file_id, cover_msg_id, uploader_id, created_at, publish_at,
                access_tier, access_redeem_code, access_user_ids, category, subcategory)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, title, description, tags, cover_file_id, cover_msg_id, uploader_id, created, scheduled_at,
             access_tier or "free", access_redeem_code, access_user_ids, category or "Global", subcategory),
        )
        if start_number is None:
            row = conn.execute("SELECT COALESCE(MAX(number), 0) m FROM video_numbers").fetchone()
            start_number = int(row["m"] or 0) + 1
        start_number = int(start_number)
        if start_number < 1: raise ValueError("Starting video number must be 1 or higher.")
        for offset in range(len(items)):
            n = start_number + offset
            if conn.execute("SELECT 1 FROM video_numbers WHERE number = ?", (n,)).fetchone():
                raise ValueError(f"Video #{n} is already used/reserved. Deleted numbers are never reused.")
        for offset, item in enumerate(items):
            vid = gen_id(); video_number = start_number + offset; video_ids.append(vid)
            conn.execute(
                """INSERT INTO videos
                   (id, title, description, tags, cover_file_id, cover_msg_id, primary_msg_id, backup_msg_id, recovery_msg_id,
                    duration_seconds, file_size_bytes, upload_date, created_at, uploader_id, publish_at, batch_id,
                    access_tier, access_redeem_code, access_user_ids, video_number, media_type,
                    source_chat_id, source_message_id, source_media_group_id, media_group_id, category, subcategory)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (vid, title, description, tags, cover_file_id, cover_msg_id,
                 item["primary_msg_id"], item.get("backup_msg_id"), item.get("recovery_msg_id"), item.get("duration_seconds"),
                 item.get("file_size_bytes"), day, created, uploader_id, scheduled_at, batch_id,
                 access_tier or "free", access_redeem_code, access_user_ids, video_number, item.get("media_type") or "video",
                 item.get("source_chat_id"), item.get("source_message_id"), item.get("media_group_id"), item.get("media_group_id"),
                 category or "Global", subcategory))
            conn.execute("INSERT INTO video_numbers(number, video_id, reserved_at) VALUES (?, ?, ?)", (video_number, vid, now_str()))
    return batch_id, video_ids


def get_batch(batch_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return dict(row) if row else None


def get_batch_videos(batch_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE batch_id = ? ORDER BY created_at ASC", (batch_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_batch_count(batch_id: str) -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM videos WHERE batch_id = ?", (batch_id,)).fetchone()["c"]

def update_batch_fields(batch_id: str, **fields):
    """Update collection-level metadata and propagate it to every member video.

    Collection metadata is authoritative for title/description/tags/category/access
    and schedule. This keeps the collection and all of its child videos in sync.
    """
    if not fields:
        return
    allowed = {
        "title", "description", "tags", "cover_file_id", "cover_msg_id",
        "publish_at", "access_tier", "access_redeem_code", "access_user_ids",
        "category", "subcategory",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unsupported batch fields: {', '.join(sorted(unknown))}")
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not row:
            raise ValueError("Collection not found.")
        bcols = ", ".join(f"{k} = ?" for k in fields)
        bvals = list(fields.values()) + [batch_id]
        conn.execute(f"UPDATE batches SET {bcols} WHERE id = ?", bvals)
        vcols = ", ".join(f"{k} = ?" for k in fields)
        vvals = list(fields.values()) + [batch_id]
        conn.execute(f"UPDATE videos SET {vcols} WHERE batch_id = ?", vvals)

def update_batch_cover(batch_id: str, cover_file_id: str | None, cover_msg_id: int | None):
    """Update a bulk collection's shared cover and propagate it to every item."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not row:
            raise ValueError("Bulk batch not found.")
        conn.execute(
            "UPDATE batches SET cover_file_id = ?, cover_msg_id = ? WHERE id = ?",
            (cover_file_id, cover_msg_id, batch_id),
        )
        conn.execute(
            "UPDATE videos SET cover_file_id = ?, cover_msg_id = ? WHERE batch_id = ?",
            (cover_file_id, cover_msg_id, batch_id),
        )

# ---------- videos ----------

def get_video(video_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        return dict(row) if row else None


def get_videos_by_date(day_iso: str):
    """Return videos belonging to the selected local calendar date.

    The catalog historically stored the day in ``upload_date``; scheduled and
    newer rows also carry timezone-aware ``created_at``/``publish_at``. We
    match the explicit date fields and then normalize timestamps into the app
    timezone so a late-night upload cannot disappear from the calendar.
    """
    try:
        target_date = date.fromisoformat(day_iso)
    except ValueError:
        return []

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE upload_date = ? OR substr(created_at,1,10) = ? "
            "OR (publish_at IS NOT NULL AND substr(publish_at,1,10) = ?) "
            "ORDER BY created_at ASC",
            (day_iso, day_iso, day_iso),
        ).fetchall()
        all_rows = list(rows)

        # Also inspect timezone-aware timestamps. This is intentionally bounded
        # to the videos table and is cheap for this app's expected catalog size.
        extra = conn.execute(
            "SELECT * FROM videos WHERE created_at IS NOT NULL OR publish_at IS NOT NULL"
        ).fetchall()

    seen = {row["id"] for row in all_rows}
    for row in extra:
        for field in ("created_at", "publish_at"):
            raw = row[field]
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=config.TIMEZONE)
                local_day = dt.astimezone(config.TIMEZONE).date()
            except Exception:
                continue
            if local_day == target_date and row["id"] not in seen:
                all_rows.append(row)
                seen.add(row["id"])
                break

    all_rows.sort(key=lambda row: row["created_at"] or "")
    return [dict(row) for row in all_rows]


def get_unalerted():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE alerted = 0 ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_alerted(video_ids: list[str]):
    if not video_ids:
        return
    with get_conn() as conn:
        conn.executemany("UPDATE videos SET alerted = 1 WHERE id = ?", [(v,) for v in video_ids])


def search_videos(keyword: str, limit: int = 200, visible_only: bool = False):
    keyword = (keyword or "").strip(); like = f"%{keyword.lower()}%"
    number = int(keyword) if keyword.isdigit() else None
    now = datetime.now(config.TIMEZONE).isoformat()
    visibility = " AND (publish_at IS NULL OR publish_at = '' OR publish_at <= ?)" if visible_only else ""
    with get_conn() as conn:
        if number is not None:
            sql = f"""SELECT * FROM videos
                   WHERE (video_number = ? OR LOWER(title) LIKE ? OR LOWER(tags) LIKE ?){visibility}
                   ORDER BY CASE WHEN video_number = ? THEN 0 ELSE 1 END, created_at DESC LIMIT ?"""
            params = (number, like, like, now, number, limit) if visible_only else (number, like, like, number, limit)
            rows = conn.execute(sql, params).fetchall()
        else:
            sql = f"SELECT * FROM videos WHERE (LOWER(title) LIKE ? OR LOWER(tags) LIKE ?){visibility} ORDER BY created_at DESC LIMIT ?"
            params = (like, like, now, limit) if visible_only else (like, like, limit)
            rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def advanced_search(keyword: str, limit: int = 100, visible_only: bool = True, category: str = '', tag: str = '', date_value: str = ''):
    """Token-aware search across title, tags and description with light operators.
    Supports quoted phrases and operators such as cat:Indian and tag:romance."""
    raw = (keyword or '').strip()
    tokens = re.findall(r'"([^"]+)"|([^\s]+)', raw) if raw else []
    terms=[]; exact=[]
    for a,b in tokens:
        term=(a or b).strip()
        if not term: continue
        low=term.lower()
        if low.startswith('cat:'): category = term.split(':',1)[1].strip(); continue
        if low.startswith('tag:'): tag = term.split(':',1)[1].strip(); continue
        if low.startswith('date:'): date_value = term.split(':',1)[1].strip(); continue
        if low.startswith('#') and low[1:].isdigit():
            terms.append(('number', int(low[1:]))); continue
        if a: exact.append(a.lower())
        else: terms.append(('text', low))
    now = datetime.now(config.TIMEZONE).isoformat()
    where=[]; params=[]
    if visible_only:
        where.append("(publish_at IS NULL OR publish_at = '' OR publish_at <= ?)"); params.append(now)
    if category: where.append("LOWER(COALESCE(category,'')) = LOWER(?)"); params.append(category)
    if tag: where.append("(',' || LOWER(COALESCE(tags,'')) || ',') LIKE LOWER(?)"); params.append(f'%,{tag.strip().lower()},%')
    if date_value: where.append("(upload_date=? OR substr(created_at,1,10)=?)"); params += [date_value, date_value]
    for kind, value in terms:
        if kind=='number':
            where.append('video_number=?'); params.append(value)
        else:
            like=f'%{value}%'
            where.append("(LOWER(COALESCE(title,'')) LIKE ? OR LOWER(COALESCE(tags,'')) LIKE ? OR LOWER(COALESCE(description,'')) LIKE ?)"); params += [like,like,like]
    for phrase in exact:
        like=f'%{phrase}%'
        where.append("(LOWER(COALESCE(title,'')) LIKE ? OR LOWER(COALESCE(tags,'')) LIKE ? OR LOWER(COALESCE(description,'')) LIKE ?)"); params += [like,like,like]
    sql='SELECT * FROM videos' + ((' WHERE ' + ' AND '.join(where)) if where else '') + ' ORDER BY view_count DESC, created_at DESC LIMIT ?'
    params.append(max(1,min(200,int(limit))))
    with get_conn() as conn:
        rows=conn.execute(sql,tuple(params)).fetchall()
    return [dict(r) for r in rows]

def increment_view(video_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE videos SET view_count = view_count + 1 WHERE id = ?", (video_id,))


def edit_video(video_id: str, **fields):
    """Update allowed catalog metadata without permitting accidental column changes.

    Storage-message mapping replacement has its own dedicated atomic helper;
    keeping mapping IDs out of this generic editor prevents an unrelated admin
    edit flow from silently corrupting delivery sources.
    """
    if not fields:
        return
    allowed = {
        "title", "description", "tags", "cover_file_id", "cover_msg_id",
        "publish_at", "access_tier", "access_redeem_code", "access_user_ids",
        "category", "subcategory",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unsupported video fields: {', '.join(sorted(unknown))}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [video_id]
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE videos SET {cols} WHERE id = ?", vals)
        if cur.rowcount != 1:
            raise ValueError("Video not found")


def delete_video(video_id: str):
    """Delete one catalog item and its non-historical video-scoped references.

    Historical analytics/activity records are intentionally retained, and
    scheduled_deletes are retained so already-published Telegram messages can
    still be cleaned up. Runtime catalogue/delivery/access state must not be
    left pointing at a deleted video.
    """
    vid = str(video_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM delivery_links WHERE target_type = 'video' AND target_id = ?", (vid,))
        conn.execute("DELETE FROM reactions WHERE video_id = ?", (vid,))
        conn.execute("DELETE FROM unlock_tokens WHERE video_id = ?", (vid,))
        conn.execute("DELETE FROM ad_unlocks WHERE video_id = ?", (vid,))
        conn.execute("DELETE FROM video_numbers WHERE video_id = ?", (vid,))
        conn.execute("DELETE FROM videos WHERE id = ?", (vid,))


def all_videos(limit: int = 100, offset: int = 0):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM videos ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


def create_storage_recovery_job(job_id: str, admin_user_id: int, source_channel_id: int, dest_channel_id: int, total: int, video_ids: list[str]):
    ts = now_str()
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM storage_recovery_jobs WHERE id = ?", (job_id,)).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO storage_recovery_jobs
                   (id, admin_user_id, source_channel_id, dest_channel_id, status, next_index, total, copied, failed, last_error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'paused', 0, ?, 0, 0, NULL, ?, ?)""",
                (job_id, int(admin_user_id), int(source_channel_id), int(dest_channel_id), int(total), ts, ts),
            )
        conn.executemany(
            """INSERT OR IGNORE INTO storage_recovery_items(job_id, video_id, status, updated_at)
               VALUES (?, ?, 'pending', ?)""",
            [(job_id, str(vid), ts) for vid in video_ids],
        )


def get_storage_recovery_job(job_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM storage_recovery_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_active_storage_recovery_job(admin_user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM storage_recovery_jobs WHERE admin_user_id = ? AND status IN ('running','paused','stopped','error') ORDER BY updated_at DESC LIMIT 1",
            (int(admin_user_id),),
        ).fetchone()
        return dict(row) if row else None


def update_storage_recovery_job(job_id: str, **fields):
    if not fields:
        return
    fields['updated_at'] = now_str()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [job_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE storage_recovery_jobs SET {cols} WHERE id = ?", vals)


def set_storage_recovery_item(job_id: str, video_id: str, status: str, dest_message_id: int | None = None, error: str | None = None, dest_cover_message_id: int | None = None, cover_status: str | None = None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE storage_recovery_items
               SET status = ?,
                   dest_message_id = COALESCE(?, dest_message_id),
                   dest_cover_message_id = COALESCE(?, dest_cover_message_id),
                   cover_status = COALESCE(?, cover_status),
                   error = ?, updated_at = ?
               WHERE job_id = ? AND video_id = ?""",
            (status, dest_message_id, dest_cover_message_id, cover_status, (str(error)[:500] if error else None), now_str(), job_id, str(video_id)),
        )


def get_storage_recovery_item(job_id: str, video_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM storage_recovery_items WHERE job_id = ? AND video_id = ?", (job_id, str(video_id))).fetchone()
        return dict(row) if row else None


def storage_recovery_counts(job_id: str):
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) c FROM storage_recovery_items WHERE job_id = ? GROUP BY status", (job_id,)).fetchall()
    return {r['status']: int(r['c']) for r in rows}


def clear_storage_recovery_job(job_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM storage_recovery_items WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM storage_recovery_jobs WHERE id = ?", (job_id,))


def apply_storage_recovery_job(job_id: str):
    """Commit successfully copied recovery mappings to the live catalog in one transaction."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE videos
               SET primary_msg_id = (
                     SELECT i.dest_message_id FROM storage_recovery_items i
                     WHERE i.job_id = ? AND i.video_id = videos.id AND i.status = 'copied' AND i.dest_message_id IS NOT NULL
               ),
                   cover_msg_id = COALESCE((
                     SELECT i.dest_cover_message_id FROM storage_recovery_items i
                     WHERE i.job_id = ? AND i.video_id = videos.id AND i.status = 'copied' AND i.dest_cover_message_id IS NOT NULL
                   ), cover_msg_id)
             WHERE id IN (SELECT video_id FROM storage_recovery_items WHERE job_id = ? AND status = 'copied' AND dest_message_id IS NOT NULL)""",
            (job_id, job_id, job_id),
        )


def set_primary_msg_id(video_id: str, primary_msg_id: int):
    """Update only the Primary storage message mapping for repair workflows."""
    with get_conn() as conn:
        cur = conn.execute("UPDATE videos SET primary_msg_id = ? WHERE id = ?", (int(primary_msg_id), str(video_id)))
        if cur.rowcount != 1:
            raise ValueError("Video not found")


def set_backup_msg_id(video_id: str, backup_msg_id: int | None):
    with get_conn() as conn:
        cur = conn.execute("UPDATE videos SET backup_msg_id = ? WHERE id = ?", (backup_msg_id, str(video_id)))
        if cur.rowcount != 1:
            raise ValueError("Video not found")


def set_recovery_msg_id(video_id: str, recovery_msg_id: int | None):
    with get_conn() as conn:
        cur = conn.execute("UPDATE videos SET recovery_msg_id = ? WHERE id = ?", (recovery_msg_id, str(video_id)))
        if cur.rowcount != 1:
            raise ValueError("Video not found")


def replace_video_mappings(video_id: str, primary_msg_id: int, backup_msg_id: int | None, recovery_msg_id: int | None):
    """Atomically replace all live storage message mappings for one catalog item.

    This is intentionally narrower than ``edit_video`` so a replacement cannot
    accidentally modify title/category/access/etc. If the row is missing, the
    transaction raises and no mapping is committed.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE videos SET primary_msg_id = ?, backup_msg_id = ?, recovery_msg_id = ? WHERE id = ?",
            (int(primary_msg_id), int(backup_msg_id) if backup_msg_id is not None else None,
             int(recovery_msg_id) if recovery_msg_id is not None else None, str(video_id)),
        )
        if cur.rowcount != 1:
            raise ValueError("Video mapping target was not found")


def recount_storage_recovery_job(job_id: str):
    counts = storage_recovery_counts(job_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE storage_recovery_jobs SET copied = ?, failed = ?, updated_at = ? WHERE id = ?",
            (int(counts.get("copied", 0)), int(counts.get("failed", 0)), now_str(), job_id),
        )
    return counts



def content_dashboard() -> dict:
    """Exact inventory for the admin/storage dashboard."""
    now = datetime.now(config.TIMEZONE).isoformat()
    with get_conn() as conn:
        total_videos = conn.execute("SELECT COUNT(*) c FROM videos").fetchone()["c"]
        collections = conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"]
        live_videos = conn.execute("SELECT COUNT(*) c FROM videos WHERE publish_at IS NULL OR publish_at = '' OR publish_at <= ?", (now,)).fetchone()["c"]
        scheduled_videos = conn.execute("SELECT COUNT(*) c FROM videos WHERE publish_at IS NOT NULL AND publish_at != '' AND publish_at > ?", (now,)).fetchone()["c"]
        scheduled_collections = conn.execute("SELECT COUNT(*) c FROM batches WHERE publish_at IS NOT NULL AND publish_at != '' AND publish_at > ?", (now,)).fetchone()["c"]
        scheduled_collection_items = conn.execute("SELECT COUNT(*) c FROM videos WHERE batch_id IN (SELECT id FROM batches WHERE publish_at IS NOT NULL AND publish_at != '' AND publish_at > ?)", (now,)).fetchone()["c"]
    return {"total_videos":int(total_videos or 0),"collections":int(collections or 0),"live_videos":int(live_videos or 0),"scheduled_videos":int(scheduled_videos or 0),"scheduled_collections":int(scheduled_collections or 0),"scheduled_collection_items":int(scheduled_collection_items or 0)}


def daily_content_analysis(day_iso: str | None = None) -> dict:
    """Content and product activity for one local calendar day."""
    if not day_iso: day_iso = datetime.now(config.TIMEZONE).date().isoformat()
    try: day = date.fromisoformat(day_iso)
    except ValueError: day = datetime.now(config.TIMEZONE).date(); day_iso = day.isoformat()
    start=datetime.combine(day,datetime.min.time(),tzinfo=config.TIMEZONE).isoformat(); end=datetime.combine(day+timedelta(days=1),datetime.min.time(),tzinfo=config.TIMEZONE).isoformat(); current_now=datetime.now(config.TIMEZONE).isoformat()
    with get_conn() as conn:
        nv=conn.execute("SELECT COUNT(*) c FROM videos WHERE created_at >= ? AND created_at < ?",(start,end)).fetchone()["c"]
        nc=conn.execute("SELECT COUNT(*) c FROM batches WHERE created_at >= ? AND created_at < ?",(start,end)).fetchone()["c"]
        sv=conn.execute("SELECT COUNT(*) c FROM videos WHERE publish_at >= ? AND publish_at < ?",(start,end)).fetchone()["c"]
        sc=conn.execute("SELECT COUNT(*) c FROM batches WHERE publish_at >= ? AND publish_at < ?",(start,end)).fetchone()["c"]
        pv=conn.execute("SELECT COUNT(*) c FROM videos WHERE publish_at >= ? AND publish_at < ? AND publish_at <= ?",(start,end,current_now)).fetchone()["c"]
        row=conn.execute("""SELECT COUNT(DISTINCT CASE WHEN event_type='start' THEN user_id END) active_users,
            COALESCE(SUM(CASE WHEN event_type='delivery' THEN value ELSE 0 END),0) deliveries,
            COALESCE(SUM(CASE WHEN event_type='search' THEN value ELSE 0 END),0) searches,
            COALESCE(SUM(CASE WHEN event_type='ad_unlock' THEN value ELSE 0 END),0) ad_unlocks,
            COALESCE(SUM(CASE WHEN event_type='redeem' THEN value ELSE 0 END),0) redemptions
            FROM analytics_events WHERE ts >= ? AND ts < ?""",(start,end)).fetchone()
        top=conn.execute("""SELECT e.video_id,COALESCE(v.title,e.video_id) title,COALESCE(SUM(e.value),0) deliveries FROM analytics_events e LEFT JOIN videos v ON v.id=e.video_id WHERE e.event_type='delivery' AND e.ts >= ? AND e.ts < ? AND e.video_id IS NOT NULL GROUP BY e.video_id ORDER BY deliveries DESC LIMIT 5""",(start,end)).fetchall()
    return {"day":day_iso,"new_videos":int(nv or 0),"new_collections":int(nc or 0),"scheduled_videos":int(sv or 0),"scheduled_collections":int(sc or 0),"published_videos":int(pv or 0),"active_users":int(row['active_users'] or 0),"deliveries":int(row['deliveries'] or 0),"searches":int(row['searches'] or 0),"ad_unlocks":int(row['ad_unlocks'] or 0),"redemptions":int(row['redemptions'] or 0),"top_videos":[dict(r) for r in top]}

def stats():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM videos").fetchone()["c"]
        views = conn.execute("SELECT COALESCE(SUM(view_count),0) v FROM videos").fetchone()["v"]
        missing_backup = conn.execute(
            "SELECT COUNT(*) c FROM videos WHERE backup_msg_id IS NULL"
        ).fetchone()["c"]
        return {"total_videos": total, "total_views": views, "missing_backup": missing_backup}


def find_similar_titles(title: str, limit: int = 3):
    """Case-insensitive exact/partial title match, used to warn about possible duplicates at upload time."""
    like = f"%{title.strip().lower()}%"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, upload_date FROM videos WHERE LOWER(title) LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (like, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_popular_tags(limit: int = 8):
    """Aggregate tag counts across all videos (case-insensitive), most common first."""
    with get_conn() as conn:
        rows = conn.execute("SELECT tags FROM videos WHERE tags IS NOT NULL AND tags != ''").fetchall()
    counts = {}
    display = {}
    for row in rows:
        for t in row["tags"].split(","):
            t = t.strip()
            if not t:
                continue
            key = t.lower()
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, t)
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [(display[k], c) for k, c in ranked]


def top_videos(limit: int = 10, visible_only: bool = False):
    with get_conn() as conn:
        if visible_only:
            now = datetime.now(config.TIMEZONE).isoformat()
            rows = conn.execute(
                "SELECT * FROM videos WHERE publish_at IS NULL OR publish_at = '' OR publish_at <= ? ORDER BY view_count DESC, created_at DESC LIMIT ?",
                (now, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM videos ORDER BY view_count DESC, created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def distinct_upload_dates():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT upload_date FROM videos ORDER BY upload_date DESC"
        ).fetchall()
        return [r["upload_date"] for r in rows]


def count_by_date(limit_days: int = 14):
    """Counts per upload_date, most recent first. Nothing in this system ever
    auto-deletes catalog data — this exists purely so you can see for yourself
    that older days' entries are still there. The 30-min auto-delete feature
    only removes the Telegram *messages* announcing/showing a video, never the
    underlying database row or the files sitting in the channels."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT upload_date, COUNT(*) c FROM videos GROUP BY upload_date "
            "ORDER BY upload_date DESC LIMIT ?", (limit_days,)
        ).fetchall()
        return [(r["upload_date"], r["c"]) for r in rows]


# ---------- delivery-link registry / bot migration ----------

def register_delivery_link(*, target_type: str, target_id: str, delivery_bot_id: str,
                           delivery_username: str, delivery_url: str, chat_id=None,
                           message_id=None, button_label: str = ""):
    if not target_type or not target_id or not delivery_bot_id or not delivery_url:
        return None
    now = now_str()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO delivery_links
               (target_type, target_id, delivery_bot_id, delivery_username, delivery_url,
                chat_id, message_id, button_label, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (str(target_type), str(target_id), str(delivery_bot_id),
             str(delivery_username or '').lstrip('@'), str(delivery_url),
             int(chat_id) if chat_id is not None else None,
             int(message_id) if message_id is not None else None,
             str(button_label or ''), now, now),
        )
        return int(cur.lastrowid)


def get_delivery_links_for_bot(bot_id: str, statuses=("active", "pending_update")):
    if not bot_id:
        return []
    placeholders = ','.join('?' for _ in statuses)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM delivery_links WHERE delivery_bot_id = ? AND status IN ({placeholders}) ORDER BY id ASC",
            [str(bot_id), *statuses],
        ).fetchall()
        return [dict(r) for r in rows]


def update_delivery_link(link_id: int, **fields):
    allowed = {
        'delivery_bot_id','delivery_username','delivery_url','button_label','status','updated_at',
        'previous_delivery_bot_id','previous_delivery_url','migration_attempts','migration_error','last_migration_at'
    }
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return False
    fields.setdefault('updated_at', now_str())
    cols = ', '.join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [int(link_id)]
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE delivery_links SET {cols} WHERE id = ?", vals)
        return cur.rowcount > 0


def mark_delivery_link_stale(link_id: int):
    return update_delivery_link(link_id, status='stale')


def mark_delivery_link_active(link_id: int):
    return update_delivery_link(link_id, status='active')


def mark_delivery_link_pending(link_id: int, *, bot_id: str, username: str, url: str,
                               from_bot_id: str = "", from_url: str = "", error: str = ""):
    now = now_str()
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE delivery_links SET
               delivery_bot_id = ?, delivery_username = ?, delivery_url = ?,
               status = 'pending_update', updated_at = ?,
               previous_delivery_bot_id = COALESCE(NULLIF(?, ''), previous_delivery_bot_id),
               previous_delivery_url = COALESCE(NULLIF(?, ''), previous_delivery_url),
               migration_attempts = COALESCE(migration_attempts, 0) + 1,
               migration_error = ?, last_migration_at = ?
               WHERE id = ? AND status IN ('active','pending_update','orphaned')""",
            (str(bot_id), str(username or '').lstrip('@'), str(url), now,
             str(from_bot_id or ''), str(from_url or ''), str(error or '')[:500], now, int(link_id)),
        )
        return cur.rowcount > 0


def mark_delivery_link_orphaned(link_id: int, *, error: str = "", from_bot_id: str = "", from_url: str = ""):
    now = now_str()
    fields = {
        'status': 'orphaned',
        'migration_error': str(error or '')[:500],
        'updated_at': now,
        'last_migration_at': now,
    }
    if from_bot_id:
        fields['previous_delivery_bot_id'] = str(from_bot_id)
    if from_url:
        fields['previous_delivery_url'] = str(from_url)
    return update_delivery_link(link_id, **fields)


def get_orphaned_delivery_links(limit: int = 500):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM delivery_links WHERE status = 'orphaned' ORDER BY updated_at ASC, id ASC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_delivery_link_stats():
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) c FROM delivery_links GROUP BY status").fetchall()
        out = {str(r['status']): int(r['c'] or 0) for r in rows}
        out['total'] = sum(out.values())
        return out


def get_delivery_link_owner_ids():
    """Return unique Delivery Bot ids referenced by live/migrating links."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT delivery_bot_id FROM delivery_links "
            "WHERE status IN ('active','pending_update','orphaned') "
            "AND COALESCE(delivery_bot_id, '') <> '' ORDER BY delivery_bot_id"
        ).fetchall()
        return [str(r["delivery_bot_id"]) for r in rows]


def get_delivery_links_for_bot_all(bot_id: str):
    if not bot_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM delivery_links WHERE delivery_bot_id = ? AND status IN ('active','pending_update','orphaned') ORDER BY id ASC",
            (str(bot_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_delivery_link_updates(limit: int = 100):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM delivery_links WHERE status = 'pending_update' "
            "ORDER BY updated_at ASC, id ASC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [dict(r) for r in rows]


def create_delivery_migrations_for_bot(bot_id: str, candidate_bots: list[dict]):
    """Reassign direct links away from an unavailable Delivery Bot.

    If no healthy replacement exists, links become orphaned and are retried when
    any Delivery Bot becomes healthy again. The original media is never touched.
    """
    source = str(bot_id)
    candidates = [b for b in (candidate_bots or [])
                  if str(b.get('id')) != source and b.get('username')]
    rows = get_delivery_links_for_bot_all(source)
    if not rows:
        return {'found': 0, 'migrated': 0, 'pending': 0, 'unassigned': 0, 'orphaned': 0}
    if not candidates:
        orphaned = 0
        for row in rows:
            if mark_delivery_link_orphaned(
                row['id'],
                error='No healthy Delivery Bot available',
                from_bot_id=str(row.get('delivery_bot_id') or source),
                from_url=str(row.get('delivery_url') or ''),
            ):
                orphaned += 1
        return {'found': len(rows), 'migrated': 0, 'pending': 0, 'unassigned': len(rows), 'orphaned': orphaned}
    import random
    from urllib.parse import quote
    migrated = 0
    for row in rows:
        target = random.choice(candidates)
        username = str(target.get('username') or '').lstrip('@')
        param = f"b_{row['target_id']}" if row.get('target_type') == 'batch' else f"v_{row['target_id']}"
        url = f"https://t.me/{username}?start={quote(param)}"
        if mark_delivery_link_pending(
            row['id'], bot_id=str(target.get('id')), username=username, url=url,
            from_bot_id=source, from_url=str(row.get('delivery_url') or ''),
        ):
            migrated += 1
    return {'found': len(rows), 'migrated': migrated, 'pending': migrated, 'unassigned': len(rows)-migrated, 'orphaned': 0}


def retry_orphaned_delivery_links(candidate_bots: list[dict], limit: int = 500):
    candidates = [b for b in (candidate_bots or []) if b.get('username')]
    rows = get_orphaned_delivery_links(limit)
    if not rows or not candidates:
        return {'found': len(rows), 'migrated': 0, 'pending': 0}
    import random
    from urllib.parse import quote
    migrated = 0
    for row in rows:
        target = random.choice(candidates)
        username = str(target.get('username') or '').lstrip('@')
        param = f"b_{row['target_id']}" if row.get('target_type') == 'batch' else f"v_{row['target_id']}"
        url = f"https://t.me/{username}?start={quote(param)}"
        if mark_delivery_link_pending(row['id'], bot_id=str(target.get('id')), username=username, url=url,
                                      from_bot_id=str(row.get('previous_delivery_bot_id') or ''),
                                      from_url=str(row.get('previous_delivery_url') or '')):
            migrated += 1
    return {'found': len(rows), 'migrated': migrated, 'pending': migrated}


def delivery_link_health_counts(bot_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) c FROM delivery_links WHERE delivery_bot_id = ? GROUP BY status",
            (str(bot_id),)
        ).fetchall()
        return {str(r['status']): int(r['c'] or 0) for r in rows}


# ---------- scheduled message deletion (survives bot restarts) ----------

def scheduled_videos(limit: int = 200):
    """Return future-scheduled rows for admin management."""
    now = datetime.now(config.TIMEZONE).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE publish_at IS NOT NULL AND publish_at != '' "
            "AND publish_at > ? ORDER BY publish_at ASC LIMIT ?",
            (now, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def future_str(seconds: int) -> str:
    return (datetime.now(config.TIMEZONE) + timedelta(seconds=seconds)).isoformat()


def add_scheduled_delete(bot_name: str, chat_id: int, message_id: int, delete_at_iso: str,
                          video_id: str = None) -> int:
    """video_id is optional context carried along for bots that want to do
    something after the delete (e.g. Delivery Bot leaving behind a "Watch
    Again" button) — bots that don't need it (Catalog Bot) just omit it."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scheduled_deletes (bot_name, chat_id, message_id, delete_at, video_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (bot_name, chat_id, message_id, delete_at_iso, video_id),
        )
        return cur.lastrowid


def remove_scheduled_delete(row_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM scheduled_deletes WHERE id = ?", (row_id,))


def get_pending_deletes(bot_name: str):
    """Used on startup to reschedule (or immediately fire) anything a previous
    run didn't get to before the process restarted."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_deletes WHERE bot_name = ?", (bot_name,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- default cover pool / selection ----------

def get_default_cover_pool():
    raw = get_setting("default_cover_pool", "[]")
    try:
        data = json.loads(raw or "[]")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict) and x.get("msg_id")]
    except Exception:
        log.warning("Invalid persisted cover list JSON; treating it as empty")
    return []

def save_default_cover_pool(pool):
    set_setting("default_cover_pool", json.dumps(pool, separators=(",", ":")))

def add_default_cover(file_id: str, msg_id: int):
    pool = get_default_cover_pool()
    pool = [x for x in pool if str(x.get("msg_id")) != str(msg_id)]
    pool.append({"file_id": file_id, "msg_id": int(msg_id)})
    save_default_cover_pool(pool)

def clear_default_cover_pool():
    clear_setting("default_cover_pool")

def remove_default_cover(msg_id: int):
    """Remove one stale/unusable cover from the random-cover pool."""
    pool = get_default_cover_pool()
    filtered = [x for x in pool if str(x.get("msg_id")) != str(msg_id)]
    if len(filtered) != len(pool):
        save_default_cover_pool(filtered)
    return len(filtered)

def choose_default_cover(randomize: bool = False):
    """Choose a configured cover. Random mode avoids immediately repeating the
    previous random choice when more than one cover is available."""
    pool = get_default_cover_pool()
    if randomize and pool:
        if len(pool) == 1:
            return pool[0]
        last = get_setting("last_random_cover_msg_id")
        candidates = [x for x in pool if str(x.get("msg_id")) != str(last)]
        chosen = random.choice(candidates or pool)
        set_setting("last_random_cover_msg_id", str(chosen.get("msg_id")))
        return chosen
    file_id = get_setting("default_cover_file_id")
    msg_id = get_setting("default_cover_msg_id")
    if file_id and msg_id:
        return {"file_id": file_id, "msg_id": int(msg_id)}
    if pool:
        return pool[0]
    return None

# ---------- settings (key-value store, e.g. default cover / title template) ----------

def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value):
    """Persist a small runtime setting in the shared SQLite settings table."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), "" if value is None else str(value)),
        )


def set_video_publish_at(video_id: str, publish_at: str | None):
    """Set/clear a video's scheduled publish time."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not row:
            raise ValueError("Video not found")
        conn.execute("UPDATE videos SET publish_at = ? WHERE id = ?", (publish_at, video_id))


def get_unscheduled_videos(limit: int = 100):
    """Return catalog items that have no publish time, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE publish_at IS NULL OR publish_at = '' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_setting(key: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# ---------- scheduled visibility (scheduled uploads) ----------

def is_visible(v: dict) -> bool:
    """Strict audience visibility gate; malformed schedule metadata fails closed."""
    publish_at = v.get("publish_at")
    if publish_at is None or str(publish_at).strip() == "":
        return True
    try:
        raw = str(publish_at).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=config.TIMEZONE)
        return dt <= datetime.now(dt.tzinfo)
    except Exception:
        return False


def parse_schedule_input(text: str):
    """Accepts an absolute 'YYYY-MM-DD HH:MM' or a relative '+2h' / '+30m' / '+1d'.
    Returns an ISO datetime string, or None if it couldn't be parsed."""
    text = text.strip()
    now = datetime.now(config.TIMEZONE)
    if text.startswith("+"):
        try:
            amount = int(text[1:-1])
            unit = text[-1].lower()
            if amount <= 0:
                return None
            if unit == "m":
                delta = timedelta(minutes=amount)
            elif unit == "h":
                delta = timedelta(hours=amount)
            elif unit == "d":
                delta = timedelta(days=amount)
            else:
                return None
            return (now + delta).isoformat()
        except (ValueError, IndexError):
            return None
    try:
        raw = text.replace("T", " ").strip()
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=config.TIMEZONE)
        return dt.astimezone(config.TIMEZONE).isoformat()
    except (ValueError, TypeError):
        return None


# ---------- activity log ----------

def log_activity(actor: str, action: str, detail: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (ts, actor, action, detail) VALUES (?, ?, ?, ?)",
            (now_str(), actor, action, detail),
        )


def get_recent_activity(limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- advanced product analytics ----------

def log_analytics(event_type: str, user_id: int = None, video_id: str = None, value: int = 1, detail: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO analytics_events (ts, event_type, user_id, video_id, value, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (now_str(), event_type, user_id, video_id, value, detail[:500] if detail else ""),
        )


def analytics_overview(days: int = 7) -> dict:
    days = max(1, min(int(days), 365))
    since = (datetime.now(config.TIMEZONE) - timedelta(days=days - 1)).isoformat()
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
              COUNT(*) AS events,
              COUNT(DISTINCT CASE WHEN event_type='start' THEN user_id END) AS active_users,
              COUNT(DISTINCT CASE WHEN event_type='delivery' THEN user_id END) AS unique_viewers,
              COALESCE(SUM(CASE WHEN event_type='delivery' THEN value ELSE 0 END),0) AS deliveries,
              COALESCE(SUM(CASE WHEN event_type='ad_unlock' THEN value ELSE 0 END),0) AS ad_unlocks,
              COALESCE(SUM(CASE WHEN event_type='redeem' THEN value ELSE 0 END),0) AS redemptions,
              COUNT(DISTINCT CASE WHEN event_type='search' THEN user_id END) AS searchers
            FROM analytics_events WHERE ts >= ?
        """, (since,)).fetchone()
        total_users = conn.execute("SELECT COUNT(DISTINCT user_id) c FROM analytics_events WHERE user_id IS NOT NULL").fetchone()["c"]
        premium = conn.execute("""
            SELECT COUNT(*) c FROM user_access
            WHERE premium_until IS NOT NULL AND premium_until > ?
        """, (datetime.now(config.TIMEZONE).isoformat(),)).fetchone()["c"]
    return {**dict(row), "total_users": total_users, "premium_users": premium, "days": days}


def analytics_daily(days: int = 7):
    days = max(1, min(int(days), 31))
    since = (datetime.now(config.TIMEZONE) - timedelta(days=days - 1)).date().isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT substr(ts,1,10) day,
              COUNT(DISTINCT CASE WHEN event_type='start' THEN user_id END) active_users,
              COALESCE(SUM(CASE WHEN event_type='delivery' THEN value ELSE 0 END),0) deliveries,
              COALESCE(SUM(CASE WHEN event_type='ad_unlock' THEN value ELSE 0 END),0) ad_unlocks,
              COALESCE(SUM(CASE WHEN event_type='redeem' THEN value ELSE 0 END),0) redemptions
            FROM analytics_events WHERE substr(ts,1,10) >= ?
            GROUP BY substr(ts,1,10) ORDER BY day ASC
        """, (since,)).fetchall()
        return [dict(r) for r in rows]


def analytics_top_videos(days: int = 30, limit: int = 10):
    days = max(1, min(int(days), 365))
    since = (datetime.now(config.TIMEZONE) - timedelta(days=days - 1)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT e.video_id, COALESCE(v.title, e.video_id) title,
                   COALESCE(SUM(e.value),0) deliveries,
                   COUNT(DISTINCT e.user_id) unique_viewers
            FROM analytics_events e LEFT JOIN videos v ON v.id=e.video_id
            WHERE e.event_type='delivery' AND e.ts >= ? AND e.video_id IS NOT NULL
            GROUP BY e.video_id ORDER BY deliveries DESC, unique_viewers DESC LIMIT ?
        """, (since, limit)).fetchall()
        return [dict(r) for r in rows]


def analytics_top_tags(days: int = 30, limit: int = 12):
    """Rank tags by real delivery activity, not merely how often they were assigned."""
    days = max(1, min(int(days), 365))
    since = (datetime.now(config.TIMEZONE) - timedelta(days=days - 1)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT e.video_id, COALESCE(SUM(e.value),0) deliveries
            FROM analytics_events e
            WHERE e.event_type='delivery' AND e.ts >= ? AND e.video_id IS NOT NULL
            GROUP BY e.video_id
        """, (since,)).fetchall()
        weights = {str(r['video_id']): int(r['deliveries'] or 0) for r in rows}
        metas = conn.execute("SELECT id, tags FROM videos WHERE tags IS NOT NULL AND TRIM(tags) != ''").fetchall()
    counts = {}; display = {}
    for r in metas:
        w = weights.get(str(r['id']), 0)
        if not w: continue
        for raw in str(r['tags'] or '').split(','):
            t = raw.strip()
            if not t: continue
            k = t.lower(); display.setdefault(k, t); counts[k] = counts.get(k, 0) + w
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{'tag': display[k], 'deliveries': v} for k,v in ranked]

def analytics_temp_bots(days: int = 30, limit: int = 20):
    days = max(1, min(int(days), 365))
    since = (datetime.now(config.TIMEZONE) - timedelta(days=days - 1)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT detail bot_id,
                   COALESCE(SUM(CASE WHEN event_type='temp_start' THEN value ELSE 0 END),0) starts,
                   COALESCE(SUM(CASE WHEN event_type='temp_click' THEN value ELSE 0 END),0) clicks,
                   COUNT(DISTINCT CASE WHEN event_type='temp_start' THEN user_id END) unique_users
            FROM analytics_events
            WHERE event_type IN ('temp_start','temp_click') AND ts >= ?
            GROUP BY detail ORDER BY clicks DESC, starts DESC LIMIT ?
        """, (since, limit)).fetchall()
    out=[]
    for r in rows:
        d=dict(r); starts=int(d.get('starts') or 0); clicks=int(d.get('clicks') or 0)
        d['ctr'] = round((clicks/starts*100.0), 1) if starts else 0.0
        out.append(d)
    return out

def analytics_searches(days: int = 30, limit: int = 8):
    days = max(1, min(int(days), 365))
    since = (datetime.now(config.TIMEZONE) - timedelta(days=days - 1)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT detail query, COUNT(*) searches, COUNT(DISTINCT user_id) users
            FROM analytics_events
            WHERE event_type='search' AND ts >= ? AND detail != ''
            GROUP BY LOWER(detail) ORDER BY searches DESC LIMIT ?
        """, (since, limit)).fetchall()
        return [dict(r) for r in rows]


def analytics_access(days: int = 30) -> dict:
    days = max(1, min(int(days), 365))
    since = (datetime.now(config.TIMEZONE) - timedelta(days=days - 1)).isoformat()
    with get_conn() as conn:
        gated = conn.execute("SELECT COUNT(*) c FROM videos WHERE access_tier='gated'").fetchone()["c"]
        codes = conn.execute("SELECT COUNT(*) c FROM redeem_codes WHERE active=1").fetchone()["c"]
        code_uses = conn.execute("SELECT COALESCE(SUM(redemption_count),0) c FROM redeem_codes").fetchone()["c"]
        unlocks = conn.execute("SELECT COALESCE(SUM(value),0) c FROM analytics_events WHERE event_type='ad_unlock' AND ts >= ?", (since,)).fetchone()["c"]
        redemptions = conn.execute("SELECT COALESCE(SUM(value),0) c FROM analytics_events WHERE event_type='redeem' AND ts >= ?", (since,)).fetchone()["c"]
    return {"gated_videos": gated, "active_codes": codes, "all_code_uses": code_uses, "ad_unlocks": unlocks, "redemptions": redemptions}


# ---------- categories / subcategories ----------

CATEGORIES = ("Indian", "Global")

def normalize_category(value: str | None) -> str:
    value = str(value or "Global").strip().lower()
    return "Indian" if value in ("indian", "india", "🇮🇳") else "Global"

def get_category_counts(visible_only: bool = True) -> dict:
    where = "WHERE (publish_at IS NULL OR publish_at = '' OR publish_at <= ?)" if visible_only else ""
    params = (datetime.now(config.TIMEZONE).isoformat(),) if visible_only else ()
    with get_conn() as conn:
        rows = conn.execute(f"SELECT COALESCE(category, 'Global') category, COUNT(*) c FROM videos {where} GROUP BY COALESCE(category, 'Global')", params).fetchall()
    out = {c: 0 for c in CATEGORIES}
    for r in rows:
        out[normalize_category(r["category"])] = int(r["c"] or 0)
    return out

def get_subcategories(category: str, limit: int = 12, visible_only: bool = True):
    category = normalize_category(category)
    where = "WHERE COALESCE(category, 'Global') = ?"
    params = [category]
    if visible_only:
        where += " AND (publish_at IS NULL OR publish_at = '' OR publish_at <= ?)"
        params.append(datetime.now(config.TIMEZONE).isoformat())
    with get_conn() as conn:
        rows = conn.execute(f"SELECT tags FROM videos {where}", tuple(params)).fetchall()
    counts = {}
    display = {}
    for r in rows:
        # Tags are the catalogue subcategories. There is intentionally no
        # separate subcategory taxonomy anymore.
        values = [x.strip() for x in (r["tags"] or "").split(",") if x.strip()]
        for value in values:
            key = value.lower()
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, value)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:int(limit)]
    return [(display[k], c) for k, c in ranked]


def get_videos_by_category(category: str, subcategory: str | None = None, visible_only: bool = True, limit: int = 10000):
    category = normalize_category(category)
    where = "COALESCE(category, 'Global') = ?"
    params = [category]
    if subcategory:
        where += " AND (',' || LOWER(COALESCE(tags, '')) || ',') LIKE LOWER(?)"
        params.append(f"%,{subcategory.lower()},%")
    if visible_only:
        where += " AND (publish_at IS NULL OR publish_at = '' OR publish_at <= ?)"
        params.append(datetime.now(config.TIMEZONE).isoformat())
    params.append(int(limit))
    with get_conn() as conn:
        rows = conn.execute(f"SELECT * FROM videos WHERE {where} ORDER BY created_at DESC LIMIT ?", tuple(params)).fetchall()
    return [dict(r) for r in rows]


def set_video_category(video_id: str, category: str, subcategory: str | None = None, sync_batch: bool = True):
    category = normalize_category(category)
    subcategory = (str(subcategory).strip() or None) if subcategory is not None else None
    with get_conn() as conn:
        row = conn.execute("SELECT batch_id FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not row:
            raise ValueError("Video not found")
        batch_id = row["batch_id"]
        conn.execute("UPDATE videos SET category = ?, subcategory = ? WHERE id = ?", (category, subcategory, video_id))
        if sync_batch and batch_id:
            conn.execute("UPDATE videos SET category = ?, subcategory = ? WHERE batch_id = ?", (category, subcategory, batch_id))
            conn.execute("UPDATE batches SET category = ?, subcategory = ? WHERE id = ?", (category, subcategory, batch_id))

def alert_category_counts(video_ids: list[str]) -> dict:
    if not video_ids:
        return {c: 0 for c in CATEGORIES}
    marks = ",".join("?" for _ in video_ids)
    with get_conn() as conn:
        rows = conn.execute(f"SELECT COALESCE(category, 'Global') category, COUNT(*) c FROM videos WHERE id IN ({marks}) GROUP BY COALESCE(category, 'Global')", tuple(video_ids)).fetchall()
    out = {c: 0 for c in CATEGORIES}
    for r in rows:
        out[normalize_category(r["category"])] = int(r["c"] or 0)
    return out

def register_alert(alert_key: str, video_ids: list[str], category_counts: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO alert_registry(alert_key, video_ids, indian_count, global_count, created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(alert_key) DO UPDATE SET video_ids=excluded.video_ids, indian_count=excluded.indian_count, global_count=excluded.global_count",
            (str(alert_key), json.dumps([str(x) for x in (video_ids or [])]),
             int((category_counts or {}).get('Indian', 0)), int((category_counts or {}).get('Global', 0)), now_str()),
        )

def get_alert_video_ids(alert_key: str) -> list[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT video_ids FROM alert_registry WHERE alert_key = ?", (str(alert_key),)).fetchone()
    if not row:
        return []
    try:
        value = json.loads(row["video_ids"] or "[]")
        return [str(x) for x in value if x]
    except Exception:
        return []

def get_alert_category_counts_by_key(alert_key: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT indian_count, global_count FROM alert_registry WHERE alert_key = ?", (str(alert_key),)).fetchone()
    if not row:
        return {"Indian": 0, "Global": 0}
    return {"Indian": int(row["indian_count"] or 0), "Global": int(row["global_count"] or 0)}

def toggle_alert_reaction(alert_key: str, user_id: int, emoji_code: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT emoji_code FROM alert_reactions WHERE alert_key = ? AND user_id = ?", (alert_key, user_id)).fetchone()
        if row and row["emoji_code"] == emoji_code:
            conn.execute("DELETE FROM alert_reactions WHERE alert_key = ? AND user_id = ?", (alert_key, user_id))
            return False
        conn.execute("INSERT INTO alert_reactions(alert_key,user_id,emoji_code,created_at) VALUES(?,?,?,?) ON CONFLICT(alert_key,user_id) DO UPDATE SET emoji_code=excluded.emoji_code, created_at=excluded.created_at", (alert_key,user_id,emoji_code,now_str()))
        return True

def get_alert_reaction_counts(alert_key: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT emoji_code, COUNT(*) c FROM alert_reactions WHERE alert_key = ? GROUP BY emoji_code", (alert_key,)).fetchall()
    return {r["emoji_code"]: int(r["c"] or 0) for r in rows}

def get_user_alert_reaction(alert_key: str, user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT emoji_code FROM alert_reactions WHERE alert_key = ? AND user_id = ?", (alert_key,user_id)).fetchone()
    return row["emoji_code"] if row else None

# ---------- bulk operations ----------

def bulk_delete(video_ids: list):
    """Bulk-delete catalog items without leaving live video-scoped orphans."""
    ids = [str(v) for v in video_ids if v is not None]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with get_conn() as conn:
        conn.execute(f"DELETE FROM delivery_links WHERE target_type = 'video' AND target_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM reactions WHERE video_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM unlock_tokens WHERE video_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM ad_unlocks WHERE video_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM video_numbers WHERE video_id IN ({placeholders})", ids)
        conn.executemany("DELETE FROM videos WHERE id = ?", [(v,) for v in ids])


def bulk_add_tags(video_ids: list, new_tags: list):
    with get_conn() as conn:
        for vid in video_ids:
            row = conn.execute("SELECT tags FROM videos WHERE id = ?", (vid,)).fetchone()
            if not row:
                continue
            existing = [t.strip() for t in (row["tags"] or "").split(",") if t.strip()]
            merged = existing + [t for t in new_tags if t not in existing]
            conn.execute("UPDATE videos SET tags = ? WHERE id = ?", (", ".join(merged), vid))


def random_video():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE publish_at IS NULL OR publish_at <= ? ORDER BY RANDOM() LIMIT 1",
            (datetime.now(config.TIMEZONE).isoformat(),),
        ).fetchone()
        return dict(row) if row else None


def videos_by_tag(tag: str, exclude_id: str = None, limit: int = 5, visible_only: bool = False):
    like = f"%{tag.lower()}%"
    with get_conn() as conn:
        if visible_only:
            now = datetime.now(config.TIMEZONE).isoformat()
            rows = conn.execute(
                "SELECT * FROM videos WHERE LOWER(tags) LIKE ? AND id != ? AND (publish_at IS NULL OR publish_at = '' OR publish_at <= ?) ORDER BY created_at DESC LIMIT ?",
                (like, exclude_id or "", now, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM videos WHERE LOWER(tags) LIKE ? AND id != ? ORDER BY created_at DESC LIMIT ?",
                (like, exclude_id or "", limit),
            ).fetchall()
        return [dict(r) for r in rows]


def export_rows():
    """All fields useful for a CSV export, in a stable column order."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, tags, description, upload_date, view_count, "
            "duration_seconds, file_size_bytes, publish_at, alerted FROM videos "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- reactions (👍❤️🔥😂😮👎 — one per user per video, tap again to remove) ----------

def toggle_reaction(video_id: str, user_id: int, emoji_code: str) -> bool:
    """Sets the user's reaction on this video to emoji_code, or removes it
    entirely if they tap the same one again. Returns True if a reaction is
    now set, False if it was just removed."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT emoji_code FROM reactions WHERE video_id = ? AND user_id = ?",
            (video_id, user_id),
        ).fetchone()
        if row and row["emoji_code"] == emoji_code:
            conn.execute(
                "DELETE FROM reactions WHERE video_id = ? AND user_id = ?", (video_id, user_id)
            )
            return False
        conn.execute(
            "INSERT INTO reactions (video_id, user_id, emoji_code, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(video_id, user_id) DO UPDATE SET emoji_code = excluded.emoji_code, "
            "created_at = excluded.created_at",
            (video_id, user_id, emoji_code, now_str()),
        )
        return True


def get_reaction_counts(video_id: str) -> dict:
    """{emoji_code: count} for one video."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT emoji_code, COUNT(*) c FROM reactions WHERE video_id = ? GROUP BY emoji_code",
            (video_id,),
        ).fetchall()
        return {r["emoji_code"]: r["c"] for r in rows}


def get_user_reaction(video_id: str, user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT emoji_code FROM reactions WHERE video_id = ? AND user_id = ?",
            (video_id, user_id),
        ).fetchone()
        return row["emoji_code"] if row else None


# ---------- video access tier: 'free' (default) or 'gated' (ad/redeem-code unlock) ----------

def set_video_access_tier(video_id: str, tier: str):
    assert tier in ("free", "gated")
    edit_video(video_id, access_tier=tier)


# ---------- per-user daily watch limits ----------

def _get_or_create_user_access(conn, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM user_access WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        return dict(row)
    today = today_str()
    conn.execute(
        "INSERT INTO user_access (user_id, watch_count_today, watch_count_date) VALUES (?, 0, ?)",
        (user_id, today),
    )
    return {"user_id": user_id, "premium_until": None, "ad_member_until": None, "daily_limit_override": None,
            "watch_count_today": 0, "watch_count_date": today}


def get_default_daily_limit():
    """None means unlimited."""
    val = get_setting("default_daily_limit")
    if val is None:
        return None
    try:
        n = int(val)
    except ValueError:
        return None
    return None if n < 0 else n


def set_default_daily_limit(limit):
    """limit: None (or any negative number) for unlimited."""
    set_setting("default_daily_limit", str(-1 if limit is None else limit))


def get_default_ad_daily_limit():
    """Daily limit for users unlocking content through ads. None = unlimited."""
    val = get_setting("default_ad_daily_limit")
    if val is None:
        # Keep a safe, bounded default for ad-based access unless configured.
        return 5
    try:
        n = int(val)
    except ValueError:
        return 5
    return None if n < 0 else n


def set_default_ad_daily_limit(limit):
    """limit: None (or any negative number) for unlimited ad-access watches."""
    set_setting("default_ad_daily_limit", str(-1 if limit is None else limit))


def set_user_limit(user_id: int, limit):
    """limit: None clears the override (falls back to the global default),
    a negative number means unlimited for this user specifically."""
    with get_conn() as conn:
        _get_or_create_user_access(conn, user_id)
        val = None if limit is None else (-1 if limit < 0 else limit)
        conn.execute("UPDATE user_access SET daily_limit_override = ? WHERE user_id = ?", (val, user_id))


def is_premium(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT premium_until FROM user_access WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row or not row["premium_until"]:
        return False
    try:
        return datetime.fromisoformat(row["premium_until"]) > datetime.now(config.TIMEZONE)
    except ValueError:
        return False


def grant_premium_days(user_id: int, days: int) -> str:
    """Extends from the later of now or the user's current premium_until, so
    stacking redeem codes adds up instead of overwriting. Returns the new
    premium_until as an ISO string."""
    now = datetime.now(config.TIMEZONE)
    with get_conn() as conn:
        access = _get_or_create_user_access(conn, user_id)
        base = now
        current = access.get("premium_until")
        if current:
            try:
                cur_dt = datetime.fromisoformat(current)
                if cur_dt > now:
                    base = cur_dt
            except ValueError:
                pass
        new_until = (base + timedelta(days=days)).isoformat()
        conn.execute("UPDATE user_access SET premium_until = ? WHERE user_id = ?", (new_until, user_id))
        return new_until


def is_ad_member(user_id: int) -> bool:
    """Temporary membership granted by a completed ad; valid for 24 hours."""
    with get_conn() as conn:
        row = conn.execute("SELECT ad_member_until FROM user_access WHERE user_id = ?", (user_id,)).fetchone()
    if not row or not row["ad_member_until"]:
        return False
    try:
        return datetime.fromisoformat(row["ad_member_until"]) > datetime.now(config.TIMEZONE)
    except ValueError:
        return False


def ad_member_days_remaining(user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT ad_member_until FROM user_access WHERE user_id = ?", (user_id,)).fetchone()
    if not row or not row["ad_member_until"]:
        return None, None
    try:
        expiry = datetime.fromisoformat(row["ad_member_until"])
        now = datetime.now(config.TIMEZONE)
        if expiry <= now:
            return 0, expiry
        return max(1, int(((expiry-now).total_seconds()+86399)//86400)), expiry
    except ValueError:
        return None, None


def grant_ad_member_24h(user_id: int) -> str:
    """Grant/extend temporary ad membership for exactly 24 hours."""
    now = datetime.now(config.TIMEZONE)
    with get_conn() as conn:
        access = _get_or_create_user_access(conn, user_id)
        base = now
        current = access.get("ad_member_until")
        if current:
            try:
                cur_dt = datetime.fromisoformat(current)
                if cur_dt > now:
                    base = cur_dt
            except ValueError:
                pass
        new_until = (base + timedelta(hours=24)).isoformat()
        conn.execute("UPDATE user_access SET ad_member_until = ? WHERE user_id = ?", (new_until, user_id))
        return new_until


def membership_days_remaining(user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT premium_until FROM user_access WHERE user_id = ?", (user_id,)).fetchone()
    if not row or not row["premium_until"]:
        return None, None
    try:
        expiry = datetime.fromisoformat(row["premium_until"])
        now = datetime.now(config.TIMEZONE)
        if expiry <= now:
            return 0, expiry
        seconds = (expiry - now).total_seconds()
        days = max(1, int((seconds + 86399) // 86400))
        return days, expiry
    except ValueError:
        return None, None



def upsert_notification_user(user_id: int):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO user_notifications(user_id, enabled) VALUES (?, 1)", (int(user_id),))

def notification_recipients(limit: int = 500):
    with get_conn() as conn:
        rows=conn.execute(
            "SELECT user_id FROM user_notifications WHERE enabled=1 "
            "ORDER BY COALESCE(last_sent_at, '') ASC LIMIT ?", (int(limit),)
        ).fetchall()
    return [int(r["user_id"]) for r in rows]

def notification_can_send(user_id: int, video_id: str, cooldown_hours: int = 24):
    with get_conn() as conn:
        row=conn.execute(
            "SELECT last_sent_at,last_video_id FROM user_notifications WHERE user_id=? AND enabled=1",
            (int(user_id),)
        ).fetchone()
    if not row or row["last_video_id"] == str(video_id): return False
    if not row["last_sent_at"]: return True
    try:
        from datetime import datetime, timedelta
        return datetime.now(config.TIMEZONE)-datetime.fromisoformat(row["last_sent_at"]) >= timedelta(hours=cooldown_hours)
    except Exception:
        return True

def mark_notification_sent(user_id: int, video_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_notifications SET last_sent_at=?,last_video_id=? WHERE user_id=?",
            (now_str(),str(video_id),int(user_id))
        )

def should_send_membership_warning(user_id: int, warning_type: str, premium_until: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM membership_warnings WHERE user_id=? AND warning_type=? AND premium_until=?",
            (user_id, warning_type, premium_until),
        ).fetchone() is None


def mark_membership_warning_sent(user_id: int, warning_type: str, premium_until: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO membership_warnings(user_id, warning_type, premium_until, sent_at) VALUES (?, ?, ?, ?)",
            (user_id, warning_type, premium_until, now_str()),
        )


def _current_watch_limit(access: dict):
    limit = access.get("daily_limit_override")
    if limit is None:
        limit = get_default_daily_limit()
    else:
        limit = None if limit < 0 else limit
    return limit


def can_watch(user_id: int):
    """Check quota without consuming it. Delivery code calls this before sending."""
    if is_premium(user_id) or is_ad_member(user_id):
        return True, "", None, None
    today = today_str()
    with get_conn() as conn:
        access = _get_or_create_user_access(conn, user_id)
        used = int(access.get("watch_count_today") or 0)
        if access.get("watch_count_date") != today:
            used = 0
        limit = _current_watch_limit(access)
        if limit is not None and used >= limit:
            return False, f"Daily watch limit reached ({limit}/day).", limit, 0
        remaining = None if limit is None else max(0, limit - used)
        return True, "", limit, remaining


def register_watch(user_id: int):
    """Consume one daily watch only after Telegram delivery succeeds.

    The increment is performed with a conditional SQL UPDATE so separate
    permanent Delivery Bot processes cannot both read the same remaining
    quota and then increment it past the configured daily limit.
    Premium and 24h ad-members are unlimited and do not increment the counter.
    """
    if is_premium(user_id) or is_ad_member(user_id):
        return True, "", None, None
    today = today_str()
    with get_conn() as conn:
        access = _get_or_create_user_access(conn, user_id)
        limit = _current_watch_limit(access)

        # A date rollover is part of the same write decision. For limited users,
        # the WHERE clause is the actual quota gate, not a Python read/modify/write
        # sequence that can race another Delivery Bot process.
        if limit is None:
            cur = conn.execute(
                "UPDATE user_access SET watch_count_today = CASE WHEN watch_count_date = ? THEN COALESCE(watch_count_today, 0) + 1 ELSE 1 END, "
                "watch_count_date = ? WHERE user_id = ?",
                (today, today, user_id),
            )
            if cur.rowcount != 1:
                return False, "Could not register this watch safely. Please retry.", limit, None
            return True, "", limit, None

        cur = conn.execute(
            "UPDATE user_access SET watch_count_today = CASE WHEN watch_count_date = ? THEN COALESCE(watch_count_today, 0) + 1 ELSE 1 END, "
            "watch_count_date = ? WHERE user_id = ? AND (watch_count_date IS NULL OR watch_count_date != ? OR COALESCE(watch_count_today, 0) < ?)",
            (today, today, user_id, today, int(limit)),
        )
        if cur.rowcount != 1:
            return False, f"Daily watch limit reached ({limit}/day).", limit, 0

        row = conn.execute(
            "SELECT watch_count_today FROM user_access WHERE user_id = ?", (user_id,)
        ).fetchone()
        used = int(row["watch_count_today"] or 0) if row else 0
        remaining = max(0, int(limit) - used)
        return True, "", limit, remaining


def check_and_register_watch(user_id: int, quota_type: str = "free"):
    """Backward-compatible helper. New delivery code separates quota check from
    consumption so failed Telegram sends never consume a watch."""
    ok, reason, limit, remaining = can_watch(user_id)
    if not ok:
        return False, reason
    ok, reason, _limit, _remaining = register_watch(user_id)
    return ok, reason

def get_user_access_summary(user_id: int) -> dict:
    with get_conn() as conn:
        access = _get_or_create_user_access(conn, user_id)
    limit = access["daily_limit_override"]
    access["effective_limit"] = get_default_daily_limit() if limit is None else (None if limit < 0 else limit)
    access["is_premium"] = is_premium(user_id)
    access["is_ad_member"] = is_ad_member(user_id)
    return access


# ---------- ad-unlock flow for gated videos ----------

def has_ad_unlock(user_id: int, video_id: str) -> bool:
    """Return True only while the 24-hour ad unlock is active."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT expires_at FROM ad_unlocks WHERE user_id = ? AND video_id = ?",
            (user_id, video_id),
        ).fetchone()
    if not row or not row["expires_at"]:
        return False
    try:
        return datetime.fromisoformat(row["expires_at"]) > datetime.now(config.TIMEZONE)
    except ValueError:
        return False

def create_unlock_token(user_id: int, video_id: str) -> str:
    token = secrets.token_urlsafe(14).replace("-", "").replace("_", "")[:16]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO unlock_tokens (token, user_id, video_id, created_at) VALUES (?, ?, ?, ?)",
            (token, user_id, video_id, now_str()),
        )
    return token


def purge_unused_unlock_tokens(older_than_days: int = 7) -> int:
    """Delete used or stale unlock tokens; returns rows removed."""
    cutoff = (datetime.now(config.TIMEZONE) - timedelta(days=max(1, int(older_than_days)))).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM unlock_tokens WHERE used = 1 OR created_at < ?",
            (cutoff,),
        )
        return int(cur.rowcount or 0)


def resolve_unlock_token(token: str, user_id: int):
    """Consume a valid ad token and grant exactly 24 hours of access."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM unlock_tokens WHERE token = ?", (token,)).fetchone()
        if not row or row["used"] or row["user_id"] != user_id:
            return None
        now = datetime.now(config.TIMEZONE)
        expires = (now + timedelta(days=1)).isoformat()
        conn.execute("UPDATE unlock_tokens SET used = 1 WHERE token = ?", (token,))
        # A completed ad grants temporary MEMBER access for 24h.
        current = conn.execute("SELECT ad_member_until FROM user_access WHERE user_id = ?", (user_id,)).fetchone()
        base = now
        if current and current["ad_member_until"]:
            try:
                cur_dt = datetime.fromisoformat(current["ad_member_until"])
                if cur_dt > now:
                    base = cur_dt
            except ValueError:
                pass
        temp_until = (base + timedelta(hours=24)).isoformat()
        conn.execute("UPDATE user_access SET ad_member_until = ? WHERE user_id = ?", (temp_until, user_id))
        conn.execute(
            "INSERT INTO ad_unlocks (user_id, video_id, unlocked_at, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, video_id) DO UPDATE SET unlocked_at=excluded.unlocked_at, expires_at=excluded.expires_at",
            (user_id, row["video_id"], now.isoformat(), temp_until),
        )
        conn.execute(
            "INSERT INTO analytics_events (ts, event_type, user_id, video_id, value, detail) VALUES (?, 'ad_unlock', ?, ?, 1, ?)",
            (now_str(), user_id, row["video_id"], "24h"),
        )
        return row["video_id"]

def _gen_code(length: int = 10) -> str:
    import string
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_redeem_code(kind: str, duration_days: int, max_redemptions=None, note: str = "") -> str:
    assert kind in ("premium", "giveaway")
    for _ in range(10):
        code = _gen_code()
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM redeem_codes WHERE code = ?", (code,)).fetchone():
                continue
            conn.execute(
                "INSERT INTO redeem_codes (code, kind, duration_days, max_redemptions, active, created_at, note) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (code, kind, duration_days, max_redemptions, now_str(), note),
            )
            return code
    raise RuntimeError("Could not generate a unique redeem code after 10 attempts")


def get_redeem_code(code: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM redeem_codes WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None


def has_redeemed_code(code: str, user_id: int) -> bool:
    if not code:
        return False
    with get_conn() as conn:
        return bool(conn.execute("SELECT 1 FROM code_redemptions WHERE code = ? AND user_id = ?", (code.strip().upper(), user_id)).fetchone())


def has_user_video_access(video_id: str, user_id: int) -> bool:
    v = get_video(video_id)
    if not v:
        return False
    raw = (v.get("access_user_ids") or "").strip()
    if not raw:
        return False
    try:
        return str(user_id) in {x.strip() for x in raw.split(",") if x.strip()}
    except Exception:
        return False


def redeem_code(code: str, user_id: int):
    """Returns (ok, message). On success, grants the code's duration as
    premium days (stacking on any existing premium time)."""
    c = get_redeem_code(code)
    if not c or not c["active"]:
        return False, "❌ Invalid or inactive code."
    if c["max_redemptions"] is not None and c["redemption_count"] >= c["max_redemptions"]:
        return False, "❌ This code has already reached its redemption limit."

    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM code_redemptions WHERE code = ? AND user_id = ?", (code, user_id)
        ).fetchone():
            return False, "❌ You've already redeemed this code."

        # Reserve one redemption atomically so concurrent users can never
        # push a limited code past its maximum-user allowance.
        if c["max_redemptions"] is None:
            cur = conn.execute(
                "UPDATE redeem_codes SET redemption_count = redemption_count + 1 "
                "WHERE code = ? AND active = 1", (code,)
            )
        else:
            cur = conn.execute(
                "UPDATE redeem_codes SET redemption_count = redemption_count + 1 "
                "WHERE code = ? AND active = 1 AND redemption_count < ?",
                (code, c["max_redemptions"]),
            )
        if cur.rowcount != 1:
            return False, "❌ This code has reached its maximum user limit."

        conn.execute(
            "INSERT INTO code_redemptions (code, user_id, redeemed_at) VALUES (?, ?, ?)",
            (code, user_id, now_str()),
        )

    until = grant_premium_days(user_id, c["duration_days"])
    log_analytics("redeem", user_id=user_id, value=1, detail=code)
    return True, f"✅ Redeemed! Premium access until {until[:16].replace('T', ' ')} ({c['duration_days']} day(s) added)."


def deactivate_code(code: str):
    with get_conn() as conn:
        conn.execute("UPDATE redeem_codes SET active = 0 WHERE code = ?", (code,))


def list_codes(active_only: bool = True, limit: int = 30):
    with get_conn() as conn:
        q = "SELECT * FROM redeem_codes"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY created_at DESC LIMIT ?"
        rows = conn.execute(q, (limit,)).fetchall()
        return [dict(r) for r in rows]

# ---------- resumable bulk delivery ----------
def get_batch_progress(user_id: int, batch_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM batch_delivery_progress WHERE user_id = ? AND batch_id = ?",
            (user_id, batch_id),
        ).fetchone()
        return dict(row) if row else None


def set_batch_progress(user_id: int, batch_id: str, next_index: int, status: str = "paused"):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO batch_delivery_progress (user_id, batch_id, next_index, status, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, batch_id) DO UPDATE SET
                 next_index=excluded.next_index, status=excluded.status, updated_at=excluded.updated_at""",
            (user_id, batch_id, max(0, int(next_index)), status, now_str()),
        )


def clear_batch_progress(user_id: int, batch_id: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM batch_delivery_progress WHERE user_id = ? AND batch_id = ?",
            (user_id, batch_id),
        )
