# V6 exception triage: intentionally swallowed exceptions in this module
# are limited to best-effort cleanup/compatibility fallbacks; user-visible or
# persistence failures are logged or surfaced by their surrounding handlers.
import html
"""
Admin Bot
---------
Commands (admin-only):
  /menu       - button hub: Queue / Post Alert / List / Stats / Verify
  /queue      - show uploads since the last alert, waiting to be announced
  /postalert  - build + preview the alert, then confirm to post it to ALERT_CHANNEL_ID
  /list       - browse all videos (paginated), each with Edit/Delete buttons
  /stats      - totals + storage health
  /verify     - confirms every video's backup copy exists (flags missing ones)
"""
import logging
import asyncio
import csv
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Bot
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

import config
import storage_config
import db
import bgtasks
import boterror
import botutil
import admin_store
import temp_bot_store
import permanent_bot_store

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("admin_bot")

PAGE_SIZE = getattr(config, "PAGE_SIZE", 6)


check_channel_access = botutil.check_channel_access



# --- Telegram Web App / Mini App configuration ---
WEBAPP_URL = os.getenv("TELEGRAM_WEBAPP_URL", "https://stellar-speculoos-1d5c7e.netlify.app/").strip()

def _webapp_button():
    if not WEBAPP_URL.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        return None
    return InlineKeyboardButton("🌐 Open Web App", web_app=WebAppInfo(url=WEBAPP_URL))
# --- END Telegram Web App configuration ---

def _required_channels():
    return [
        ("Primary Channel", storage_config.primary()),
        ("Alert Channel", config.ALERT_CHANNEL_ID),
    ]


def _validate_config():
    """Basic sanity checks so an obviously wrong setting is caught at startup
    instead of failing confusingly later, mid-operation."""
    issues = []
    if not config.ADMIN_BOT_TOKEN or "PUT_" in config.ADMIN_BOT_TOKEN:
        issues.append("ADMIN_BOT_TOKEN looks unset")
    if not storage_config.primary():
        issues.append("PRIMARY_CHANNEL_ID is 0 — not configured")
    if not config.ALERT_CHANNEL_ID:
        issues.append("ALERT_CHANNEL_ID is 0 — not configured")
    if not config.ADMIN_USER_IDS:
        issues.append("ADMIN_USER_IDS is empty — nobody will be able to use this bot")
    return issues


async def _maybe_run_auto_alert(app):
    """Opt-in: if /setautoalert HH:MM is set, post whatever's queued (and
    already visible) once a day at/after that time, default wording, no
    manual confirm — enabling this at all is the opt-in.

    Uses a persisted 'last fired date' rather than an in-memory variable, and
    checks >= the target time rather than an exact minute match. Both matter
    for a bot that restarts often: the old approach could silently skip a day
    if the process wasn't running at the exact minute, or lose track of
    having already fired after a restart. This version also naturally
    catches up — if the bot was offline at 18:00 and comes back online at
    18:15, it still fires once, immediately, instead of waiting for tomorrow."""
    target = db.get_setting("auto_alert_time")
    if not target:
        return
    now = datetime.now(config.TIMEZONE)
    today_str = now.date().isoformat()
    if db.get_setting("last_auto_alert_date") == today_str:
        return  # already fired today
    try:
        target_time = datetime.strptime(target, "%H:%M").time()
    except ValueError:
        return
    if now.time() < target_time:
        return  # not time yet today
    pending = _unalerted_visible()
    if pending:
        await _post_alert_to(None, app, pending, is_channel=True)
        db.mark_alerted([v["id"] for v in pending])
        db.log_activity("admin_bot", "auto_alert_posted", f"{len(pending)} video(s)")
    # Mark fired even if the queue was empty — otherwise it re-checks (and
    # potentially posts nothing, repeatedly) every minute for the rest of the day.
    db.set_setting("last_auto_alert_date", today_str)


async def _maybe_run_nightly_verify(app):
    """Checks backup integrity roughly every 6 hours and only messages the
    admin if something's actually missing. Interval is measured from a
    persisted last-run timestamp, not a sleep() timer, so frequent restarts
    don't reset the countdown and delay it indefinitely."""
    now = datetime.now(config.TIMEZONE)
    last = db.get_setting("last_verify_run")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < 6 * 3600:
                return
        except Exception:
            log.debug("Ignoring malformed persisted scheduler timestamp")
    videos = db.all_videos(limit=10000)
    missing = [v for v in videos if not v["backup_msg_id"]]
    if missing and config.ADMIN_USER_IDS:
        lines = [f"`{v['id']}` — {db.md_escape(v['title'])}" for v in missing[:15]]
        more = f"\n...and {len(missing) - 15} more" if len(missing) > 15 else ""
        await app.bot.send_message(
            chat_id=config.ADMIN_USER_IDS[0],
            text=f"🔎 *Nightly backup check:* {len(missing)} video(s) missing a backup copy.\n\n"
                 + "\n".join(lines) + more,
            parse_mode="Markdown",
        )
    db.set_setting("last_verify_run", now.isoformat())


async def _maybe_run_weekly_backup(app):
    """Legacy weekly DB backup kept for compatibility with older settings."""
    now = datetime.now(config.TIMEZONE)
    last = db.get_setting("last_backup_run")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < 7 * 24 * 3600:
                return
        except Exception:
            log.debug("Ignoring malformed persisted scheduler timestamp")
    if config.ADMIN_USER_IDS:
        await _send_db_backup(app.bot, config.ADMIN_USER_IDS[0], manual=False)
    db.set_setting("last_backup_run", now.isoformat())


def _nightly_backup_time():
    """Nightly backup time, defaulting to 02:00 in config.TIMEZONE.

    It can be changed from Admin Bot with /setbackup HH:MM.
    """
    value = db.get_setting("nightly_backup_time") or os.getenv("NIGHTLY_BACKUP_TIME", "02:00")
    try:
        return datetime.strptime(value, "%H:%M").time(), value
    except ValueError:
        return datetime.strptime("02:00", "%H:%M").time(), "02:00"


def _build_backup_bundle():
    """Create a consistent, secret-free backup bundle for Telegram delivery."""
    base = Path(__file__).resolve().parent
    stamp = datetime.now(config.TIMEZONE).strftime("%Y-%m-%d_%H-%M-%S")
    tmpdir = Path(tempfile.mkdtemp(prefix="video_vault_backup_"))
    db_snapshot = tmpdir / "videos.db"
    zip_path = tmpdir / f"video_vault_backup_{stamp}.zip"

    # SQLite backup API gives us a consistent snapshot even while the bots write.
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(db_snapshot)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    manifest = {
        "created_at": datetime.now(config.TIMEZONE).isoformat(),
        "timezone": str(config.TIMEZONE),
        "database": "videos.db",
        "includes": [
            "videos.db",
            "managed_admins.json",
            "bot_editor_store.json",
            "catalog_export.csv",
            "backup_manifest.json",
        ],
        "note": "No bot tokens, API keys, or source secrets are included.",
    }

    rows = db.export_rows()
    csv_path = tmpdir / "catalog_export.csv"
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")

    manifest_path = tmpdir / "backup_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(db_snapshot, "videos.db")
        for name in ("managed_admins.json", "bot_editor_store.json"):
            src_file = base / name
            if src_file.exists():
                zf.write(src_file, name)
        zf.write(csv_path, "catalog_export.csv")
        zf.write(manifest_path, "backup_manifest.json")

    return zip_path, tmpdir, stamp, len(rows)


async def _send_nightly_backup(bot, chat_id):
    """Build and send the complete safe catalog backup to the admin."""
    zip_path = tmpdir = None
    try:
        zip_path, tmpdir, stamp, count = _build_backup_bundle()
        with zip_path.open("rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=zip_path.name,
                caption=(
                    "🌙 *Nightly Video Vault backup*\n\n"
                    f"📦 Catalog: {count} item(s)\n"
                    f"🕐 {stamp.replace('_', ' ')}\n"
                    "🔐 No bot tokens/secrets included.\n"
                    "💾 Keep this ZIP somewhere safe."
                ),
                parse_mode="Markdown",
            )
        return True
    except Exception:
        log.exception("Nightly backup failed")
        try:
            await bot.send_message(chat_id=chat_id, text="❌ Nightly backup failed. Check admin_bot logs.")
        except Exception:
            log.warning("Nightly backup notification could not be delivered", exc_info=True)
        return False
    finally:
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


async def _maybe_run_nightly_backup(app):
    """Run once per local calendar day at the configured night time.

    Persisted date makes this restart-safe: if VPS is offline at the exact
    minute, the backup is sent as soon as the bot comes back after the target.
    """
    if not config.ADMIN_USER_IDS:
        return
    now = datetime.now(config.TIMEZONE)
    target, target_text = _nightly_backup_time()
    today = now.date().isoformat()
    if db.get_setting("last_nightly_backup_date") == today:
        return
    if now.time() < target:
        return
    ok = await _send_nightly_backup(app.bot, config.ADMIN_USER_IDS[0])
    if ok:
        db.set_setting("last_nightly_backup_date", today)
        db.set_setting("last_nightly_backup_at", now.isoformat())
        db.log_activity("admin_bot", "nightly_backup", f"sent to owner at {target_text}")



def _weekly_slots():
    """Persisted weekly content-release slots: [{day, time, count}]."""
    import json
    raw = db.get_setting("weekly_content_slots") or "[]"
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        log.warning("Invalid persisted JSON setting; treating it as empty")
        return []


def _save_weekly_slots(slots):
    import json
    db.set_setting("weekly_content_slots", json.dumps(slots, separators=(",", ":")))


def _next_weekday_time(now, day_name, hhmm):
    days = {"mon":0,"monday":0,"tue":1,"tues":1,"tuesday":1,"wed":2,"wednesday":2,
            "thu":3,"thur":3,"thurs":3,"thursday":3,"fri":4,"friday":4,
            "sat":5,"saturday":5,"sun":6,"sunday":6}
    d = days.get(str(day_name).lower())
    if d is None:
        return None
    try:
        tm = datetime.strptime(hhmm, "%H:%M").time()
    except ValueError:
        return None
    candidate = now.replace(hour=tm.hour, minute=tm.minute, second=0, microsecond=0) + timedelta(days=(d-now.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _weekly_due_slots(now):
    due=[]
    for i, slot in enumerate(_weekly_slots()):
        nxt=_next_weekday_time(now, slot.get("day"), slot.get("time"))
        if not nxt:
            continue
        # A slot is due when its next occurrence would have been <= 60 seconds ago.
        candidate=nxt-timedelta(days=7)
        if candidate <= now and (now-candidate).total_seconds() < 90:
            due.append((i,slot,candidate))
    return due


async def _run_weekly_content_scheduler(app):
    """Release the next queued item(s) at configured weekly slots.

    A slot schedules the oldest unscheduled catalog items for immediate release.
    The persisted last-fire key prevents duplicate releases across restarts.
    """
    now = datetime.now(config.TIMEZONE)
    slots = _weekly_slots()
    for idx, slot in enumerate(slots):
        candidate = _next_weekday_time(now, slot.get("day"), slot.get("time"))
        if not candidate:
            continue
        occurrence = candidate - timedelta(days=7) if candidate > now else candidate
        if occurrence > now or (now-occurrence).total_seconds() > 90:
            continue
        fire_key=f"weekly_slot_last_{idx}"
        stamp=occurrence.isoformat()
        if db.get_setting(fire_key) == stamp:
            continue
        count=max(1,int(slot.get("count",1)))
        queued=db.get_unscheduled_videos(limit=count)
        if not queued:
            db.set_setting(fire_key, stamp)
            continue
        released=[]
        for video in queued:
            db.set_video_publish_at(video["id"], now.isoformat())
            released.append(video["id"])
        db.set_setting(fire_key, stamp)
        db.log_activity("admin_bot", "weekly_release", f"slot {slot.get('day')} {slot.get('time')}: released {len(released)}")
        if db.get_setting("weekly_auto_alert") == "1":
            try:
                visible=[db.get_video(v) for v in released]
                visible=[v for v in visible if v and db.is_visible(v)]
                if visible:
                    await _post_alert_to(None, app, visible, is_channel=True)
                    db.mark_alerted(released)
            except Exception:
                log.exception("Weekly auto-alert failed")


async def _maybe_purge_unlock_tokens(app):
    """Daily cleanup for used/stale ad-unlock token rows."""
    today = datetime.now(config.TIMEZONE).date().isoformat()
    key = "unlock_tokens_last_purge"
    if db.get_setting(key) == today:
        return
    removed = db.purge_unused_unlock_tokens(older_than_days=7)
    db.set_setting(key, today)
    if removed:
        db.log_activity("admin_bot", "purge_unlock_tokens", f"removed {removed} stale/used token(s)")


async def _scheduler_loop(app):
    """One loop, short poll interval (60s), every check backed by persisted
    state in the settings table — this is what makes all three jobs above
    survive restarts correctly instead of silently drifting or double-firing."""
    first_pass = True
    while True:
        # Run one pass immediately after startup so a VPS restart does not
        # introduce an unnecessary extra 60-second delay. Every individual
        # job remains persisted/idempotent, so this cannot double-fire a job.
        if not first_pass:
            await asyncio.sleep(60)
        first_pass = False
        for job in (_maybe_run_auto_alert, _maybe_run_nightly_verify, _maybe_run_nightly_backup, _maybe_run_weekly_backup, _run_weekly_content_scheduler, _maybe_purge_unlock_tokens):
            try:
                await job(app)
            except Exception:
                log.exception(f"Scheduler job {job.__name__} failed")


async def _startup_check(app):
    log.info("Checking channel access...")
    await check_channel_access(app.bot, _required_channels())
    for issue in _validate_config():
        log.warning(f"⚠️ Config check: {issue}")
    bgtasks.spawn(_scheduler_loop(app), name="admin-scheduler-loop")
    await botutil.configure_bot_ui(app, "admin")


def is_admin(user_id: int) -> bool:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid in admin_store.admin_ids(config.ADMIN_USER_IDS)


def is_owner(user_id: int) -> bool:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    try:
        owners = {int(x) for x in config.ADMIN_USER_IDS}
    except Exception:
        log.warning("Invalid ADMIN_USER_IDS configuration")
        owners = set()
    return bool(owners) and uid == min(owners)


async def guard(update: Update) -> bool:
    # Channel posts (from being a channel admin) have no effective_user and
    # aren't private chats — ignore them silently. This is what closes the
    # "reply into the channel that caused the crash" loop.
    if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        return False
    if not is_admin(update.effective_user.id):
        if update.message:
            await update.message.reply_text("🚫 You're not authorized to use this bot.")
        return False
    return True


def _optional_channel_links():
    """Read optional public links without making them mandatory config fields."""
    pairs = [
        ("📦 Primary Channel", "PRIMARY_CHANNEL_LINK"),
        ("🛡 Backup Channel", "BACKUP_CHANNEL_LINK"),
        ("📢 Alert Channel", "ALERT_CHANNEL_LINK"),
        ("👥 Catalogue Group", "CATALOG_GROUP_LINK"),
        ("📚 Catalogue Channel", "CATALOG_CHANNEL_LINK"),
    ]
    out=[]
    for label, attr in pairs:
        url = str(getattr(config, attr, "") or "").strip()
        if url.startswith("https://t.me/"):
            out.append((label, url))
    return out


def _mandatory_join_admin_config():
    return botutil.get_mandatory_join_config(config)

def _mandatory_join_admin_text():
    channel_id, link = _mandatory_join_admin_config()
    message = botutil.get_mandatory_join_message(config, "{channel}")
    image_state = "✅ Custom image set" if os.path.exists(botutil.MANDATORY_JOIN_IMAGE) else "⚠️ No image set"
    return (f"🔐 *Mandatory Join Gate*\n\n"
            f"📢 Target: `{db.md_escape(channel_id)}`\n"
            f"🔗 Join link: {db.md_escape(link)}`\n"
            f"🖼️ Image: *{image_state}*\n\n"
            f"💬 *Join message template:*\n`{db.md_escape(message)}`\n\n"
            "Users must join this channel before Catalogue/Delivery access.\n"
            "Use *{channel}* where you want the channel title inserted.\n\n"
            "Public: `@username` or `https://t.me/username`\n"
            "Private: `-1001234567890 | https://t.me/+invite`")

def _mandatory_join_admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Mandatory Channel", callback_data="mandatory_join_edit")],
        [InlineKeyboardButton("💬 Edit Join Message", callback_data="mandatory_join_message_edit")],
        [InlineKeyboardButton("🖼️ Change Join Image", callback_data="mandatory_join_image_edit")],
        [InlineKeyboardButton("👀 Preview Join Gate", callback_data="mandatory_join_preview")],
        [InlineKeyboardButton("🔄 Reset Join Settings", callback_data="mandatory_join_reset")],
        [InlineKeyboardButton("📡 Channel Hub", callback_data="menu_channels")],
    ])

def _channel_hub_text(results=None):
    lines=["📡 *Vault Channel Hub*", "", "Quick access to your connected Telegram destinations.", ""]
    if results:
        lines.append("*Bot access check*")
        lines.extend(f"{status}  {label}" for label, _, status in results)
        lines.append("")
    links=_optional_channel_links()
    if links:
        lines.append("*Public links*")
        lines.extend(f"• {label}" for label,_ in links)
    else:
        lines.append("No public channel links are configured yet. Add optional `*_LINK` values in config to make them appear here.")
    return "\n".join(lines)


def _channel_hub_kb():
    rows=[]
    links=_optional_channel_links()
    for label,url in links:
        rows.append([InlineKeyboardButton(label, url=url)])
    rows.append([InlineKeyboardButton("🔐 Mandatory Join Channel", callback_data="mandatory_join_admin")])
    rows.append([InlineKeyboardButton("🔎 Check Bot Access", callback_data="menu_channels_check")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")])
    return InlineKeyboardMarkup(rows)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Queue", callback_data="menu_queue"),
         InlineKeyboardButton("📢 Post Alert", callback_data="menu_postalert")],
        [InlineKeyboardButton("🖼️ Default Alert Cover", callback_data="menu_default_alert_cover")],
        [InlineKeyboardButton("📚 List Videos", callback_data="menu_list_0"),
         InlineKeyboardButton("🎬 Video Management", callback_data="menu_video_manage")],
        [InlineKeyboardButton("📊 Stats", callback_data="menu_stats"),
         InlineKeyboardButton("📈 Analytics", callback_data="menu_analytics")],
        [InlineKeyboardButton("🎛️ Control Center", callback_data="menu_control")],
        [InlineKeyboardButton("🔎 Verify", callback_data="menu_verify"),
         InlineKeyboardButton("🔥 Top Videos", callback_data="menu_top")],
        [InlineKeyboardButton("📅 Schedule / By Date", callback_data="menu_bydate"),
         InlineKeyboardButton("🩺 Health", callback_data="menu_health")],
        [InlineKeyboardButton("📡 Channel Hub", callback_data="menu_channels"),
         InlineKeyboardButton("🧰 Diagnostics", callback_data="menu_diagnostics")],
        [InlineKeyboardButton("🗓️ Content Scheduler", callback_data="menu_scheduler")],
        [InlineKeyboardButton("📜 Log", callback_data="menu_log"),
         InlineKeyboardButton("📄 Export", callback_data="menu_export")],
        [InlineKeyboardButton("💾 Backup DB", callback_data="menu_backupdb")],
        [InlineKeyboardButton("🔐 Access Control", callback_data="menu_access")],
        [InlineKeyboardButton("👥 Admin Management", callback_data="admin_manage"),
         InlineKeyboardButton("🤖 Bot Editor", callback_data="bot_editor")],
        [InlineKeyboardButton("🤖 Temporary Bot Pool", callback_data="tempbots")],
        [InlineKeyboardButton("🚀 Permanent Delivery Bots", callback_data="permbots")],
    ] + ([[_webapp_button()]] if _webapp_button() else []))


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        admin_store.get_template("admin_start", "🛠 Admin Bot"), reply_markup=main_menu_kb()
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        "✨ *Vault HQ* ✨\n\nBoss mode is online 😌💅\nPick a module below — I'll handle the boring stuff. 🛠️", parse_mode="Markdown", reply_markup=main_menu_kb()
    )


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    results = await check_channel_access(context.bot, _required_channels())
    lines = [f"{status}  {label}" for label, _, status in results]
    await update.message.reply_text("🔎 *Channel Access Check*\n\n" + "\n".join(lines), parse_mode="Markdown")



async def _control_center_text(context) -> str:
    """Compact operational dashboard for the admin: catalog, queue, scheduler,
    access-control and channel state in one screen. Read-only; it does not
    mutate the existing working flows."""
    s = db.stats()
    videos = db.all_videos(limit=10000)
    scheduled = [v for v in videos if v.get("publish_at") and not db.is_visible(v)]
    waiting = db.get_unalerted()
    slots = _weekly_slots()
    active_codes = db.list_codes(active_only=True, limit=1000)
    channels = await check_channel_access(context.bot, _required_channels())
    ok = sum(1 for _, _, status in channels if str(status).startswith("✅"))
    total = len(channels)

    lines = [
        "🎛️ *Admin Control Center*",
        "",
        "📚 *Catalog*",
        f"• Videos: *{s.get('total_videos', 0)}*",
        f"• Views: *{s.get('total_views', 0)}*",
        f"• Waiting for alert: *{len(waiting)}*",
        f"• Scheduled: *{len(scheduled)}*",
        f"• Next scheduled: *{scheduled[0].get('publish_at') if scheduled else '—'}*",
        "",
        "🗓️ *Automation*",
        f"• Weekly slots: *{len(slots)}*",
        f"• Auto alert: *{'ON' if db.get_setting('weekly_auto_alert') == '1' else 'OFF'}*",
        "",
        "🔐 *Access*",
        f"• Active redeem codes: *{len(active_codes)}*",
        "",
        "📡 *Channels*",
        f"• Required channels reachable: *{ok}/{total}*",
        "",
        "Use the buttons below to jump directly to the relevant module.",
    ]
    return "\n".join(lines)

async def _health_text(context) -> str:
    """One combined dashboard: channel access + stats + backup integrity —
    the three things worth checking together instead of running separately."""
    channel_results = await check_channel_access(context.bot, _required_channels())
    channel_lines = [f"{status}  {label}" for label, _, status in channel_results]

    s = db.stats()
    videos = db.all_videos(limit=10000)
    missing = [v for v in videos if not v["backup_msg_id"]]
    unalerted = len(db.get_unalerted())
    auto_alert_time = db.get_setting("auto_alert_time")
    scheduled_count = len([v for v in videos if v.get("publish_at") and not db.is_visible(v)])
    last_verify = db.get_setting("last_verify_run")
    last_backup = db.get_setting("last_backup_run")

    lines = [
        "🩺 *System Health*",
        "",
        "*Channel Access*",
        *channel_lines,
        "",
        "*Catalog*",
        f"Total videos: {s['total_videos']}",
        f"Total views: {s['total_views']}",
        f"Waiting to announce: {unalerted}",
        f"Scheduled for later: {scheduled_count}",
        f"Missing backup copies: {len(missing)}" + (" ⚠️" if missing else " ✅"),
        f"Auto-alert: {'🟢 ' + auto_alert_time if auto_alert_time else '⚪ off'}",
        f"Last verify sweep: {last_verify[:16] if last_verify else 'never yet'}",
        f"Last DB backup: {last_backup[:16] if last_backup else 'never yet — run /backupdb'}",
    ]
    return "\n".join(lines)


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(await _health_text(context), parse_mode="Markdown", reply_markup=main_menu_kb())


# ---------- access control: daily limits, gated videos, redeem codes ----------

def _limit_label(limit) -> str:
    return "unlimited" if limit is None else f"{limit}/day"


def access_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟️ Create Premium Code", callback_data="access_gencode"),
         InlineKeyboardButton("🎁 Create Giveaway", callback_data="access_gengiveaway")],
        [InlineKeyboardButton("📋 Manage Active Codes", callback_data="access_codes")],
        [InlineKeyboardButton("⚙️ Free Daily Limit", callback_data="access_limit")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
    ])


def _access_codes_text(codes) -> str:
    if not codes:
        return "🎟️ *Your Active Redeem Codes* ✨\n\nNothing active yet — let's make one. 👀"
    lines = ["📋 *Active Redeem Codes*", ""]
    for c in codes:
        uses = (f"{c['redemption_count']}/{c['max_redemptions']}"
                if c["max_redemptions"] is not None else f"{c['redemption_count']}/∞")
        kind = "🎟️ Premium" if c["kind"] == "premium" else "🎁 Giveaway"
        lines.append(f"*{kind}*\n`{c['code']}` · {c['duration_days']}d · users {uses}")
    return "\n\n".join(lines)


def access_kb_for_codes(codes):
    rows = []
    for c in codes:
        rows.append([InlineKeyboardButton(f"📢 Alert {c['code']}", callback_data=f"redeem_alert_{c['code']}"), InlineKeyboardButton("🗑 Revoke", callback_data=f"access_revoke_{c['code']}")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="access_codes"),
                 InlineKeyboardButton("🔙 Access Control", callback_data="menu_access")])
    return InlineKeyboardMarkup(rows)


async def _access_text() -> str:
    default_limit = db.get_default_daily_limit()
    codes = db.list_codes(active_only=True, limit=10)
    lines = [
        "🔐 *Access Control*",
        "",
        f"📊 Free-user daily limit: *{_limit_label(default_limit)}*",
        "",
        "🎟️ *Redeem Codes*",
    ]
    if not codes:
        lines.append("No active codes yet. Create one below.")
    else:
        for c in codes[:5]:
            uses = f"{c['redemption_count']}/{c['max_redemptions']}" if c["max_redemptions"] is not None else f"{c['redemption_count']}/∞"
            lines.append(f"`{c['code']}` · {c['duration_days']}d · {uses} uses")
        if len(codes) > 5:
            lines.append(f"_+ {len(codes)-5} more — tap Manage Active Codes._")
    lines += [
        "",
        "Use the buttons below to create, review, or revoke codes. No command memorizing needed. 💗"
    ]
    return "\n".join(lines)


async def access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(await _access_text(), parse_mode="Markdown", reply_markup=access_menu_kb())


def _parse_limit_arg(raw: str):
    """Returns (ok, value). value is None for 'unlimited', else a non-negative int."""
    raw = raw.strip().lower()
    if raw == "unlimited":
        return True, None
    try:
        n = int(raw)
        return (True, n) if n >= 0 else (False, None)
    except ValueError:
        return False, None


async def setdefaultlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/setdefaultlimit <n>` or `/setdefaultlimit unlimited`",
                                         parse_mode="Markdown")
        return
    ok, limit = _parse_limit_arg(context.args[0])
    if not ok:
        await update.message.reply_text("Give a non-negative number, or `unlimited`.", parse_mode="Markdown")
        return
    db.set_default_daily_limit(limit)
    db.log_activity("admin_bot", "set_default_limit", _limit_label(limit))
    await update.message.reply_text(f"✅ Default daily limit: *{_limit_label(limit)}*.", parse_mode="Markdown")


async def setlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/setlimit <user_id> <n|unlimited>`", parse_mode="Markdown")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("First argument must be a numeric Telegram user ID.")
        return
    ok, limit = _parse_limit_arg(context.args[1])
    if not ok:
        await update.message.reply_text("Give a non-negative number, or `unlimited`.", parse_mode="Markdown")
        return
    db.set_user_limit(user_id, -1 if limit is None else limit)
    db.log_activity("admin_bot", "set_user_limit", f"{user_id} -> {_limit_label(limit)}")
    await update.message.reply_text(f"✅ `{user_id}`: *{_limit_label(limit)}*.", parse_mode="Markdown")


async def gate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/gate <video_id>`", parse_mode="Markdown")
        return
    video_id = context.args[0]
    if not db.get_video(video_id):
        await update.message.reply_text("No video with that ID.")
        return
    db.set_video_access_tier(video_id, "gated")
    db.log_activity("admin_bot", "gate_video", video_id)
    await update.message.reply_text(
        f"🔒 `{video_id}` now requires unlocking (ad or redeem code) before it's delivered.",
        parse_mode="Markdown",
    )


async def ungate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/ungate <video_id>`", parse_mode="Markdown")
        return
    video_id = context.args[0]
    if not db.get_video(video_id):
        await update.message.reply_text("No video with that ID.")
        return
    db.set_video_access_tier(video_id, "free")
    db.log_activity("admin_bot", "ungate_video", video_id)
    await update.message.reply_text(f"🔓 `{video_id}` is free again.", parse_mode="Markdown")


async def _post_redeem_code_alert(context, code: str, kind: str, days: int, max_users):
    """Post a clean redeem-code announcement to the configured alert channel."""
    kind_label = "🎟️ PREMIUM ACCESS" if kind == "premium" else "🎁 GIVEAWAY CODE"
    limit_line = f"👥 *First {max_users} users can redeem*" if max_users is not None else "👥 *Unlimited redemptions*"
    caption = (
        "✨ *NEW REDEEM CODE*\n"
        "━━━━━━━━━━━━━━\n\n"
        f"{kind_label}\n\n"
        f"🔑 Code: `{code}`\n"
        f"⏳ Access: *{days} day(s)*\n"
        f"{limit_line}\n\n"
        "🎁 Redeem it before the code reaches its user limit.\n"
        "👇 *Tap below to redeem*"
    )
    buy_url = getattr(config, "MEMBERSHIP_BUY_URL", "") or "https://t.me/deodrant0"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎟️ Redeem Code", url=f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem"),
        InlineKeyboardButton("💳 Buy Membership", url=buy_url),
    ]])
    await context.bot.send_message(
        chat_id=config.ALERT_CHANNEL_ID,
        text=caption,
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def gencode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/gencode <days> [max_uses]` — e.g. `/gencode 30` for a single-use 30-day code.",
            parse_mode="Markdown",
        )
        return
    try:
        days = int(context.args[0])
        if days <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Days must be a positive number.")
        return
    max_uses = 1
    if len(context.args) > 1:
        try:
            max_uses = int(context.args[1])
            if max_uses <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Max uses must be a positive number.")
            return
    code = db.create_redeem_code("premium", days, max_redemptions=max_uses)
    db.log_activity("admin_bot", "gencode", f"{code} ({days}d x{max_uses})")
    await update.message.reply_text(
        f"🎟️ *Premium Code Created*\n\n"
        f"🔑 Code: `{code}`\n"
        f"⏳ Duration: *{days} day(s)*\n"
        f"👥 Uses: *{max_uses}*\n\n"
        "Share the code with the user. They can open Delivery Bot and tap *Redeem Code*, then send `/redeem CODE`.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎟️ Open Delivery Bot", url=f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem"),
            InlineKeyboardButton("📢 Post Code Alert", callback_data=f"redeem_alert_{code}")
        ]]),
    )


async def gengiveaway_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/gengiveaway <days> [max_uses]` — omit max_uses for unlimited redemptions.",
            parse_mode="Markdown",
        )
        return
    try:
        days = int(context.args[0])
        if days <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Days must be a positive number.")
        return
    max_uses = None
    if len(context.args) > 1:
        try:
            max_uses = int(context.args[1])
            if max_uses <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Max uses must be a positive number.")
            return
    code = db.create_redeem_code("giveaway", days, max_redemptions=max_uses)
    db.log_activity("admin_bot", "gengiveaway", f"{code} ({days}d x{max_uses or 'unlimited'})")
    await update.message.reply_text(
        f"🎁 *Giveaway Code Created*\n\n"
        f"🔑 Code: `{code}`\n"
        f"⏳ Duration: *{days} day(s) per user*\n"
        f"👥 Redemptions: *{max_uses or 'Unlimited'}*\n\n"
        "Share the code. Users can tap *Redeem Code* in Delivery Bot and enter it with `/redeem CODE`.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎟️ Open Delivery Bot", url=f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem"),
            InlineKeyboardButton("📢 Post Code Alert", callback_data=f"redeem_alert_{code}")
        ]]),
    )


async def codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(await _access_text(), parse_mode="Markdown", reply_markup=access_menu_kb())


async def revokecode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/revokecode <code>`", parse_mode="Markdown")
        return
    code = context.args[0].strip().upper()
    if not db.get_redeem_code(code):
        await update.message.reply_text("No such code.")
        return
    db.deactivate_code(code)
    db.log_activity("admin_bot", "revoke_code", code)
    await update.message.reply_text(f"🗑 Code `{code}` deactivated.", parse_mode="Markdown")


# ---------- queue ----------

def _unalerted_visible():
    """Uploads waiting to be announced, excluding anything scheduled for a
    future publish time — those stay out of the queue (and out of alerts)
    until they actually become visible to the audience."""
    return [v for v in db.get_unalerted() if db.is_visible(v)]


async def _queue_text() -> str:
    pending = _unalerted_visible()
    scheduled_count = len(db.scheduled_videos(limit=10000)) if hasattr(db, "scheduled_videos") else 0
    if not pending:
        extra = f"\n({scheduled_count} scheduled for later)" if scheduled_count else ""
        return f"📋 Queue is empty — nothing new to announce.{extra}"
    lines = [f"• #{v.get('video_number') or '?'} · `{v['id']}` — {db.md_escape(v['title'])}" for v in pending]
    extra = f"\n_{scheduled_count} more scheduled for later, not shown yet_" if scheduled_count else ""
    return f"📋 *{len(pending)} upload(s) waiting to be announced:*\n\n" + "\n".join(lines) + extra


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Post Alert Now", callback_data="menu_postalert")]])
    await update.message.reply_text(await _queue_text(), parse_mode="Markdown", reply_markup=kb)


# ---------- default alert cover ----------
async def _default_alert_cover_menu(chat_id, context):
    cover_id = db.get_setting("default_alert_cover_msg_id")
    if cover_id:
        text = (
            "🖼️ <b>Default Alert Cover</b>\n\n"
            "✅ A default cover is configured.\n"
            "Queue alerts will always use this cover and will never borrow the latest video's cover.\n\n"
            "You can replace it anytime or clear it below."
        )
    else:
        text = (
            "🖼️ <b>Default Alert Cover</b>\n\n"
            "⚠️ No default cover is set yet.\n"
            "Send one photo once and every normal queue alert will use it automatically.\n\n"
            "No latest-video cover will be selected as a fallback."
        )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Set / Change Cover", callback_data="default_alert_cover_set")],
        *([[InlineKeyboardButton("🗑️ Clear Default", callback_data="default_alert_cover_clear")]] if cover_id else []),
        [InlineKeyboardButton("📢 Post Alert", callback_data="menu_postalert")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
    ])
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)

# ---------- /postalert (with preview + confirm) ----------

def _deep_link(param: str) -> str:
    return f"https://t.me/{config.CATALOG_BOT_USERNAME}?start={param}"


def _mini_app_deep_link(start_param: str = "today") -> str:
    """Open the Catalog bot's Telegram Main Mini App, never the raw Netlify
    page. If a named Main Mini App is configured, use its direct app route;
    otherwise use Telegram's Main Mini App startapp deep link.
    """
    configured = getattr(config, "CATALOG_MINI_APP_URL", "") or getattr(config, "MAIN_MINI_APP_URL", "")
    if configured:
        base = str(configured).rstrip("&?")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}startapp={start_param}"
    return f"https://t.me/{config.CATALOG_BOT_USERNAME}?startapp={start_param}"


async def _send_cover(bot, target_chat_id, v, caption=None, reply_markup=None, parse_mode="Markdown"):
    """Use the video's own cover first, then the configured default cover."""
    cover_msg_id = v.get("cover_msg_id") or db.get_setting("default_cover_msg_id")
    if not cover_msg_id:
        return await bot.send_message(chat_id=target_chat_id, text=caption or "✨ New content", parse_mode=parse_mode if caption else None, reply_markup=reply_markup)
    return await bot.copy_message(
        chat_id=target_chat_id,
        from_chat_id=storage_config.primary(),
        message_id=int(cover_msg_id),
        caption=caption,
        parse_mode=parse_mode if caption else None,
        reply_markup=reply_markup,
    )


# Edit this to change the alert's wording/branding — it stays a short teaser,
# full details (title/tags/description) only ever show inside Catalog Bot.
ALERT_HEADER = "✨ <b>NEW CONTENT JUST DROPPED</b>"


def _alert_key(videos: list, user_data: dict = None) -> str:
    """Stable reaction pools: daily queue vs isolated special/re-alert."""
    import secrets
    mode = (user_data or {}).get("alert_mode")
    if mode == "queue":
        date_key = (videos[-1].get("upload_date") if videos else None) or db.today_str()
        return "today:" + str(date_key)[:10]
    if videos:
        return "special:" + str(videos[0].get("id") or secrets.token_hex(7))
    return "special:" + secrets.token_hex(7)

ALERT_REACTION_CODES = [("like", "👍"), ("love", "❤️"), ("fire", "🔥"), ("laugh", "😂"), ("wow", "😮"), ("dislike", "👎")]

def _alert_reaction_rows(alert_key: str, user_id: int = 0):
    counts = db.get_alert_reaction_counts(alert_key)
    mine = db.get_user_alert_reaction(alert_key, user_id) if user_id else None
    buttons=[]
    for code, emoji in ALERT_REACTION_CODES:
        n=counts.get(code,0); label=f"{emoji} {n}" if n else emoji
        if mine == code: label="• " + label
        buttons.append(InlineKeyboardButton(label, callback_data=f"arx_{code}_{alert_key}"))
    return [buttons[:3], buttons[3:]]

def _alert_category_buttons(videos: list, alert_key: str | None = None):
    counts = db.get_alert_category_counts_by_key(alert_key) if alert_key else db.alert_category_counts([v.get("id") for v in videos if v])
    return [[InlineKeyboardButton(f"🇮🇳 Indian · {counts.get('Indian',0)}", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=cat_indian"), InlineKeyboardButton(f"🌍 Global · {counts.get('Global',0)}", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=cat_global")]]

def _build_alert_caption(videos: list, user_data: dict = None) -> str:
    """Build the polished SHIVANI'S FANBASE announcement style.

    Default alerts intentionally avoid Telegram blockquotes. The visual frame
    is made entirely from Unicode characters so it looks consistent in both
    channel posts and admin previews.
    """
    if user_data and user_data.get("custom_alert_caption"):
        return user_data["custom_alert_caption"]

    mode = (user_data or {}).get("alert_mode")
    if mode == "single" and videos:
        v = videos[0]
        access_labels = {
            "free":"🌍 Public", "ad":"📺 Ad unlock", "redeem":"🎟️ Redeem membership",
            "redeem_or_ad":"🎟️ + 📺 Redeem OR Ad", "members":"💎 Redeem membership only",
            "users":"👤 Selected users", "gated":"🔒 Premium / Ad",
        }
        access = access_labels.get((v.get("access_tier") or "free").lower(), "Restricted access")
        media_label = "🖼️ IMAGE" if (v.get("media_type") or "video") == "photo" else "🎬 VIDEO"
        title = html.escape(str((v.get("title") or "New upload").strip()))
        return (
            "╭━━━━━━━༺✨༻━━━━━━━╮\n"
            "      ✨ <b>SHIVANI'S FANBASE</b> ✨\n"
            "╰━━━━━━━༺✨༻━━━━━━━╯\n\n"
            "📦 <b>NEW CONTENT DROPPED</b>\n\n"
            f"• {media_label} : <b>{title}</b>\n"
            f"• 🔢 <b>Video #{html.escape(str(v.get('video_number') or '?'))}</b>\n"
            f"• 🔐 <b>{html.escape(access)}</b>\n\n"
            "╭───────༺🍿༻───────╮\n"
            "        <b>Fresh Content Is Waiting…</b>\n"
            "╰───────༺🍿༻───────╯\n\n"
            "✨ <b>Available right now.</b>\n"
            "🔗 <b>Tap below to open and watch.</b> 👀"
        )

    if mode == "collection" and videos:
        batch = db.get_batch((user_data or {}).get("alert_batch_id")) if (user_data or {}).get("alert_batch_id") else None
        title = html.escape(str((batch or {}).get("title") or videos[0].get("title") or "Collection"))
        count = len(videos)
        return (
            "╭━━━━━━━༺✨༻━━━━━━━╮\n"
            "      ✨ <b>SHIVANI'S FANBASE</b> ✨\n"
            "╰━━━━━━━༺✨༻━━━━━━━╯\n\n"
            "📦 <b>NEW COLLECTION DROPPED</b>\n\n"
            f"• 📦 <b>{title}</b>\n"
            f"• 🎬 <b>{count} Fresh Videos</b>\n\n"
            "╭───────༺🍿༻───────╮\n"
            "       <b>Full Set Is Waiting…</b>\n"
            "╰───────༺🍿༻───────╯\n\n"
            "✨ <b>Everything is ready to watch.</b>\n"
            "🔗 <b>Tap below to watch the full collection.</b> 👀"
        )

    count = len(videos)
    date_str = videos[-1].get("upload_date") if videos else db.today_str()
    cat_counts = db.alert_category_counts([v.get("id") for v in videos if v])
    return (
        "╭━━━━━━━༺✨༻━━━━━━━╮\n"
        "      ✨ <b>SHIVANI'S FANBASE</b> ✨\n"
        "╰━━━━━━━༺✨༻━━━━━━━╯\n\n"
        "📦 <b>NEW CONTENT DROPPED</b>\n\n"
        f"• <b>{count} Fresh Upload{'s' if count != 1 else ''} Available</b>\n"
        f"• 🇮🇳 <b>Indian</b> : <b>{cat_counts.get('Indian',0)}</b> • 🌍 <b>Global</b> : <b>{cat_counts.get('Global',0)}</b>\n"
        f"• 📅 <b>Updated</b> : <b>{html.escape(str(date_str)[:10])}</b>\n\n"
        "╭───────༺🍿༻───────╮\n"
        "      🔗 <b>Tap below to explore</b> ✨\n"
        "╰───────༺🍿༻───────╯"
    )


def _build_alert_markup(videos: list, user_data: dict = None) -> InlineKeyboardMarkup:
    """Targeted alerts get only their own deep-link; queue alerts retain the
    discovery buttons. This prevents a single alert from looking like a global
    announcement."""
    user_data = user_data or {}
    mode = user_data.get("alert_mode")
    alert_key = _alert_key(videos, user_data)
    category_counts = db.alert_category_counts([v.get("id") for v in videos if v])
    db.register_alert(alert_key, [v.get("id") for v in videos if v], category_counts)

    if mode == "single" and videos:
        url = botutil.direct_delivery_url(video_id=videos[0]['id'])
        rows = [[InlineKeyboardButton("🎬 Open This Video", url=url)]] if url else _alert_category_buttons(videos, alert_key)
    elif mode == "collection" and user_data.get("alert_batch_id"):
        url = botutil.direct_delivery_url(batch_id=user_data['alert_batch_id'])
        rows = [[InlineKeyboardButton(f"📦 Watch Collection · {len(videos)}", url=url)]] if url else _alert_category_buttons(videos, alert_key)
    else:
        rows = _alert_category_buttons(videos, alert_key)
        rows.append([InlineKeyboardButton("🌐 Open In WebView", url=_mini_app_deep_link("today"))])
        rows.append([InlineKeyboardButton("🆕 Get Fresh Content", url=_deep_link("today"))])

    rows.extend(_alert_reaction_rows(alert_key))
    return InlineKeyboardMarkup(rows)


async def _post_alert_to(chat_id_or_target, context, videos, is_channel: bool, user_data: dict = None):
    """Send the single custom announcement (hero cover + teaser text), not a
    per-video content dump. Used for both the admin preview and the real post
    so they always match exactly. Honors a custom caption/cover from user_data
    if one was set via the Edit Message / Change Cover buttons.

    A custom caption is free-form Markdown the admin typed on purpose (so we
    don't escape it — that would break intentional bold/italic formatting),
    but that also means a stray unmatched *, _, `, or [ in what they typed
    would otherwise crash the whole send. If that happens, retry once as
    plain text rather than losing the post."""
    hero = videos[-1]  # most recently uploaded
    caption = _build_alert_caption(videos, user_data)
    kb = _build_alert_markup(videos, user_data)
    target = config.ALERT_CHANNEL_ID if is_channel else chat_id_or_target
    is_custom = bool(user_data and user_data.get("custom_alert_caption"))
    alert_parse_mode = "Markdown" if is_custom else "HTML"

    custom_cover_msg_id = user_data.get("custom_alert_cover_msg_id") if user_data else None
    default_cover_msg_id = db.get_setting("default_alert_cover_msg_id")
    mode = (user_data or {}).get("alert_mode")
    # Targeted alerts must use their own asset. Collections use the shared
    # collection cover; single alerts use the selected video's cover.
    if mode == "collection":
        batch = db.get_batch((user_data or {}).get("alert_batch_id"))
        cover_msg_id = custom_cover_msg_id or ((batch or {}).get("cover_msg_id"))
    elif mode == "single":
        cover_msg_id = custom_cover_msg_id or (hero.get("cover_msg_id"))
    else:
        cover_msg_id = custom_cover_msg_id or default_cover_msg_id
    try:
        if cover_msg_id:
            await context.bot.copy_message(
                chat_id=target, from_chat_id=storage_config.primary(), message_id=int(cover_msg_id),
                caption=caption, parse_mode=alert_parse_mode, reply_markup=kb,
            )
        else:
            # Queue alerts intentionally do NOT fall back to the newest video's
            # cover. A queue announcement without a configured default cover
            # is text-only rather than unexpectedly using a random/latest asset.
            if mode == "queue":
                await context.bot.send_message(
                    chat_id=target, text=caption, parse_mode=alert_parse_mode, reply_markup=kb,
                )
            else:
                await _send_cover(context.bot, target, hero, caption=caption, reply_markup=kb, parse_mode=alert_parse_mode)
    except Exception:
        if not is_custom:
            raise
        log.warning("Custom alert caption failed to parse as Markdown, retrying as plain text")
        if cover_msg_id:
            await context.bot.copy_message(
                chat_id=target, from_chat_id=storage_config.primary(), message_id=int(cover_msg_id),
                caption=caption, parse_mode=None, reply_markup=kb,
            )
        else:
            await context.bot.send_message(
                chat_id=target, text=caption, parse_mode=None, reply_markup=kb,
            )


async def _build_postalert_preview(chat_id, context, user_data):
    # Targeted alerts keep their exact target even when the normal queue changes.
    mode = user_data.get("alert_mode")
    if mode in ("single", "collection") and user_data.get("alert_batch"):
        pending = [db.get_video(v_id) for v_id in user_data["alert_batch"]]
        pending = [v for v in pending if v and db.is_visible(v)]
    else:
        pending = _unalerted_visible()
        user_data["alert_batch"] = [v["id"] for v in pending]
        user_data["alert_batch_id"] = None
        user_data["alert_mode"] = "queue"
    if not pending:
        await context.bot.send_message(chat_id=chat_id, text="📭 *Nothing queued for the alert yet.*\n\nAdd some fresh content and we'll make it shine. ✨")
        return

    await _post_alert_to(chat_id, context, pending, is_channel=False, user_data=user_data)

    buttons = [
        [
            InlineKeyboardButton("✅ Post to Alert Channel", callback_data="alert_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="alert_cancel"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Message", callback_data="alert_edit_text"),
            InlineKeyboardButton("🖼 One-Time Cover", callback_data="alert_edit_cover"),
        ],
    ]
    if user_data.get("alert_mode") == "queue":
        default_cover = db.get_setting("default_alert_cover_msg_id")
        buttons.append([InlineKeyboardButton(
            "🖼 Set Default Alert Cover" if not default_cover else "🖼 Change Default Alert Cover",
            callback_data="alert_default_cover",
        )])
    if user_data.get("custom_alert_caption") or user_data.get("custom_alert_cover_msg_id"):
        buttons.append([InlineKeyboardButton("🔄 Reset to Default", callback_data="alert_reset")])
    confirm_kb = InlineKeyboardMarkup(buttons)
    await context.bot.send_message(
        chat_id=chat_id, text="👆 This is exactly what will post. Go ahead?",
        reply_markup=confirm_kb,
    )


async def postalert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await _build_postalert_preview(update.effective_chat.id, context, context.user_data)


async def alert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    batch = context.user_data.get("alert_batch")
    if not batch:
        await query.edit_message_text("⌛ *That preview has expired.*\n\nRun `/postalert` again and I'll cook up a fresh one. 🔄")
        return

    if query.data == "alert_cancel":
        context.user_data.pop("alert_batch", None)
        context.user_data.pop("custom_alert_caption", None)
        context.user_data.pop("custom_alert_cover_msg_id", None)
        context.user_data.pop("alert_mode", None)
        context.user_data.pop("alert_batch_id", None)
        context.user_data.pop("alert_reaction_key", None)
        await query.edit_message_text("🫡 *Cancelled!*\n\nNothing was posted. Your queue is untouched. 💗")
        return

    if query.data == "alert_default_cover":
        context.user_data["awaiting_default_alert_cover"] = True
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "🖼️ <b>Default Alert Cover</b>\n\n"
                "Send one photo now. I'll save it as the permanent default for queue alerts.\n"
                "You won't need to choose a cover for every announcement.\n\n"
                "Queue alerts will never automatically use the latest video's cover."
            ),
            parse_mode="HTML",
        )
        return

    if query.data == "alert_edit_text":
        context.user_data["awaiting_alert_text"] = True
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✍️ Send the custom message to use for this alert (replaces the default wording).",
        )
        return

    if query.data == "alert_edit_cover":
        context.user_data["awaiting_alert_cover"] = True
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🖼 Send the photo to use as this alert's cover (just for this one announcement).",
        )
        return

    if query.data == "alert_default_cover":
        context.user_data["awaiting_default_alert_cover"] = True
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "🖼 <b>Default Alert Cover</b>\n\n"
                "Send one photo now. I'll save it as the permanent default for queue alerts, "
                "so you won't need to choose a cover every time.\n\n"
                "Your video covers will not be used automatically for queue alerts.",
            ),
            parse_mode="HTML",
        )
        return

    if query.data == "alert_reset":
        context.user_data.pop("custom_alert_caption", None)
        context.user_data.pop("custom_alert_cover_msg_id", None)
        videos = [db.get_video(vid) for vid in batch]
        videos = [v for v in videos if v]
        await context.bot.send_message(chat_id=query.message.chat_id, text="🔄 Reset — showing default alert:")
        await _build_postalert_preview(query.message.chat_id, context, context.user_data)
        return

    videos = [db.get_video(vid) for vid in batch]
    videos = [v for v in videos if v]

    try:
        await _post_alert_to(None, context, videos, is_channel=True, user_data=context.user_data)

        db.mark_alerted(batch)
        context.user_data.pop("alert_batch", None)
        context.user_data.pop("custom_alert_caption", None)
        context.user_data.pop("custom_alert_cover_msg_id", None)
        context.user_data.pop("alert_mode", None)
        context.user_data.pop("alert_batch_id", None)
        context.user_data.pop("alert_reaction_key", None)
        db.log_activity("admin_bot", "alert_posted", f"{len(videos)} video(s)")
        await query.edit_message_text(f"🚀 *Posted!*\n\n{len(videos)} fresh upload(s) are now announced. 🔥")
    except Exception as e:
        log.exception("Failed to post alert")
        await query.edit_message_text(f"🥲 *Post failed:* {e}\n\nNothing else was changed.")


# ---------- /list with pagination + per-video edit/delete ----------

async def _send_list_page(chat_id, context, page: int):
    """Admin catalog view grouped by collection. A bulk collection is one
    management card; opening it shows every item together with per-item controls."""
    all_v = db.all_videos(limit=10000)
    if not all_v:
        await context.bot.send_message(chat_id=chat_id, text="📭 *The catalog is empty for now.*\n\nUpload something spicy and let's get it moving. 🚀")
        return

    groups = []
    seen_batches = set()
    for v in all_v:
        bid = v.get("batch_id")
        if bid:
            if bid in seen_batches:
                continue
            seen_batches.add(bid)
            members = [x for x in all_v if x.get("batch_id") == bid]
            groups.append({"kind": "collection", "batch_id": bid, "videos": members})
        else:
            groups.append({"kind": "single", "videos": [v]})

    total = len(groups)
    start = page * PAGE_SIZE
    page_groups = groups[start:start + PAGE_SIZE]
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    text = f"📚 *Video Management* — page {page + 1}/{total_pages}\n\n"
    rows = []

    for g in page_groups:
        if g["kind"] == "collection":
            members = g["videos"]
            batch = db.get_batch(g["batch_id"]) or {}
            title = batch.get("title") or members[0].get("title") or "Collection"
            nums = [v.get("video_number") for v in members if v.get("video_number") is not None]
            videos_count = sum(1 for v in members if (v.get("media_type") or "video") == "video")
            photos_count = len(members) - videos_count
            number_range = f"#{min(nums)}–#{max(nums)}" if nums else "#?"
            publish_at = batch.get("publish_at")
            scheduled = bool(publish_at and not all(db.is_visible(x) for x in members))
            schedule_state = f"⏰ Scheduled · {publish_at[:16].replace('T',' ')}" if scheduled else "🟢 Live"
            text += (f"📦 *COLLECTION* · {db.md_escape(title)}\n"
                     f"   `{g['batch_id']}` · {len(members)} items · 🔢 {number_range}\n"
                     f"   🎬 {videos_count} video(s) · 🖼️ {photos_count} image(s) · {schedule_state}\n\n")
            short_title = (title[:22] + "…") if len(title) > 23 else title
            rows.append([
                InlineKeyboardButton(f"📦 Manage · {short_title}", callback_data=f"batchmanage_{g['batch_id']}"),
                InlineKeyboardButton("📢 Alert Collection", callback_data=f"batchalert_{g['batch_id']}"),
            ])
            if scheduled:
                rows.append([
                    InlineKeyboardButton("🔁 Reschedule", callback_data=f"batchschedule_{g['batch_id']}"),
                    InlineKeyboardButton("⚡ Publish Now", callback_data=f"batchpublish_{g['batch_id']}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"batchunschedule_{g['batch_id']}"),
                ])
        else:
            v = g["videos"][0]
            lock = "🔒 " if v.get("access_tier") == "gated" else ""
            scheduled = bool(v.get("publish_at") and not db.is_visible(v))
            state = f"⏰ {v['publish_at'][:16].replace('T',' ')}" if scheduled else "🟢 Live"
            text += f"{lock}🔢 #{v.get('video_number') or '?'} · `{v['id']}` — {db.md_escape(v['title'])} · {state}\n\n"
            rows.append([
                InlineKeyboardButton(f"✏️ #{v.get('video_number') or '?'}", callback_data=f"editmenu_{v['id']}"),
                InlineKeyboardButton(f"🗑 #{v.get('video_number') or '?'}", callback_data=f"delconfirm_{v['id']}"),
                InlineKeyboardButton(f"📢 #{v.get('video_number') or '?'}", callback_data=f"realert_{v['id']}"),
            ])
            if scheduled:
                rows.append([
                    InlineKeyboardButton("🔁 Reschedule", callback_data=f"video_schedule_{v['id']}"),
                    InlineKeyboardButton("⚡ Publish Now", callback_data=f"video_publish_now_{v['id']}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"video_unschedule_{v['id']}"),
                ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"listpage_{page-1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"listpage_{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu_home")])
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def _send_collection_manage(chat_id, context, batch_id: str):
    batch = db.get_batch(batch_id)
    videos = db.get_batch_videos(batch_id)
    if not batch or not videos:
        await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found or empty.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="menu_list_0")]]))
        return
    title = batch.get("title") or videos[0].get("title") or "Collection"
    nums = [v.get("video_number") for v in videos if v.get("video_number") is not None]
    first_num, last_num = (min(nums), max(nums)) if nums else (None, None)
    videos_count = sum(1 for v in videos if (v.get("media_type") or "video") == "video")
    images_count = len(videos) - videos_count
    text = (
        f"📦 *COLLECTION MANAGEMENT*\n\n"
        f"🎞️ *{db.md_escape(title)}*\n"
        f"🆔 `{batch_id}`\n"
        f"🔢 Items: *{len(videos)}*  ·  Numbers: *#{first_num}–#{last_num}*\n"
        f"🎬 Videos: *{videos_count}*  ·  🖼️ Images: *{images_count}*\n\n"
        "⚙️ *Collection controls below apply to EVERY item.*\n"
        "✏️ Individual controls only change that one video/image."
    )
    rows = [
        [InlineKeyboardButton("✏️ Edit Collection", callback_data=f"batchedit_{batch_id}")],
        [InlineKeyboardButton("📢 Alert Entire Collection", callback_data=f"batchalert_{batch_id}")],
    ]
    for v in videos:
        num = v.get("video_number") or "?"
        media = "🖼️" if (v.get("media_type") or "video") == "photo" else "🎬"
        label = f"{media} #{num} · {(v.get('title') or 'Untitled')[:24]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"editmenu_{v['id']}"), InlineKeyboardButton("📢", callback_data=f"realert_{v['id']}"), InlineKeyboardButton("🗑", callback_data=f"delconfirm_{v['id']}")])
    rows.append([InlineKeyboardButton("🔙 Back to Collections", callback_data="menu_list_0")])

    # Show the shared collection cover when available; management should feel like
    # one collection rather than a flat list of unrelated videos.
    cover_msg_id = batch.get("cover_msg_id")
    if cover_msg_id:
        try:
            await context.bot.copy_message(
                chat_id=chat_id, from_chat_id=storage_config.primary(),
                message_id=int(cover_msg_id), caption=text, parse_mode="Markdown",
            )
            await context.bot.send_message(chat_id=chat_id, text="Choose a collection-level action:", reply_markup=InlineKeyboardMarkup(rows))
            return
        except Exception:
            log.warning("Could not show collection cover %s", cover_msg_id, exc_info=True)
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def _send_collection_edit_menu(chat_id, context, batch_id: str):
    batch = db.get_batch(batch_id)
    videos = db.get_batch_videos(batch_id)
    if not batch or not videos:
        await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found.")
        return
    title = batch.get("title") or "Collection"
    tier = batch.get("access_tier") or videos[0].get("access_tier") or "free"
    tags = batch.get("tags") if batch.get("tags") is not None else videos[0].get("tags")
    description = batch.get("description") if batch.get("description") is not None else videos[0].get("description")
    category = batch.get("category") or videos[0].get("category") or "Global"
    text = (
        f"📦 *EDIT COLLECTION*\n\n"
        f"Title: *{db.md_escape(title)}*\n"
        f"Category: *{db.md_escape(db.normalize_category(category))}*\n"
        f"Tags: {db.md_escape(tags) or '—'}\n"
        f"Description: {db.md_escape(description) or '—'}\n"
        f"Access: {'🔒 gated' if tier == 'gated' else '🔓 free'}\n"
        f"Schedule: {batch.get('publish_at') or '🟢 Live'}\n\n"
        "Any change here is applied to the *whole collection* and all its items. 💗"
    )
    gate_label = "🔓 Make Collection Free" if tier == "gated" else "🔒 Gate Collection"
    schedule_buttons = [InlineKeyboardButton("⏰ Schedule Collection", callback_data=f"batchschedule_{batch_id}")]
    if batch.get("publish_at") and not all(db.is_visible(v) for v in videos):
        schedule_buttons = [InlineKeyboardButton("⚡ Publish Collection Now", callback_data=f"batchpublish_{batch_id}"), InlineKeyboardButton("❌ Cancel Schedule", callback_data=f"batchunschedule_{batch_id}")]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Title", callback_data=f"batcheditfield_{batch_id}_title"), InlineKeyboardButton("Tags", callback_data=f"batcheditfield_{batch_id}_tags")],
        [InlineKeyboardButton("Description", callback_data=f"batcheditfield_{batch_id}_description")],
        [InlineKeyboardButton("📂 Category", callback_data=f"batcheditcategory_{batch_id}"), InlineKeyboardButton("🖼️ Change Cover", callback_data=f"batcheditcover_{batch_id}")],
        [InlineKeyboardButton(gate_label, callback_data=f"batchtoggle_{batch_id}"), InlineKeyboardButton("🔐 Access", callback_data=f"batchaccessmenu_{batch_id}")],
        schedule_buttons,
        [InlineKeyboardButton("🔙 Back to Collection", callback_data=f"batchmanage_{batch_id}")],
    ])
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await _send_list_page(update.effective_chat.id, context, 0)


# ---------- edit (button-driven) ----------

async def _edit_menu(video_id: str) -> tuple[str, InlineKeyboardMarkup]:
    v = db.get_video(video_id)
    if not v:
        return "Video not found.", InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu_home")]])
    tier = v.get("access_tier", "free")
    text = (
        f"✏️ *Editing* `{video_id}`\n\n"
        f"Title: {db.md_escape(v['title'])}\n"
        f"Category: {db.md_escape(db.normalize_category(v.get('category')))}\n"
        f"Tags / Subcategories: {db.md_escape(v.get('tags')) or '—'}\n"
        f"Description: {db.md_escape(v.get('description')) or '—'}\n"
        f"Access: {'🔒 gated (ad/redeem code required)' if tier == 'gated' else '🔓 free'}\n\n"
        "What do you want to change?"
    )
    gate_label = "🔓 Make Free" if tier == "gated" else "🔒 Gate This Video"
    schedule_buttons = []
    if v.get("publish_at") and not db.is_visible(v):
        schedule_buttons.append(InlineKeyboardButton("🔁 Reschedule", callback_data=f"video_schedule_{video_id}"))
        schedule_buttons.append(InlineKeyboardButton("⚡ Publish Now", callback_data=f"video_publish_now_{video_id}"))
        schedule_buttons.append(InlineKeyboardButton("❌ Cancel Schedule", callback_data=f"video_unschedule_{video_id}"))
    else:
        schedule_buttons.append(InlineKeyboardButton("⏰ Schedule", callback_data=f"video_schedule_{video_id}"))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Title", callback_data=f"editfield_{video_id}_title"),
         InlineKeyboardButton("Tags", callback_data=f"editfield_{video_id}_tags")],
        [InlineKeyboardButton("Description", callback_data=f"editfield_{video_id}_description")],
        [InlineKeyboardButton("📂 Category", callback_data=f"editcategory_{video_id}"),
         InlineKeyboardButton("🖼️ Change Cover", callback_data=f"editcover_{video_id}")],
        [InlineKeyboardButton(gate_label, callback_data=f"togglegate_{video_id}"),
         InlineKeyboardButton("🔐 Access", callback_data=f"accessmenu_{video_id}")],
        schedule_buttons,
        [InlineKeyboardButton("🔙 Back to List", callback_data="menu_list_0")],
    ])
    return text, kb


async def _send_edit_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, video_id: str):
    """Show the edit controls with a best-effort current-cover preview.

    Telegram ``file_id`` values are bot-scoped, so an old ``cover_file_id``
    may be unusable in Admin Bot.  The durable source is the cover message
    copied into PRIMARY_CHANNEL_ID.  Older records can still have a stale
    cover message id, so fall back to the primary media thumbnail when
    possible instead of throwing ``Wrong file identifier`` and aborting the
    whole edit screen.
    """
    v = db.get_video(video_id)
    text, kb = await _edit_menu(video_id)
    if not v:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        return

    cover_file_id = v.get("cover_file_id")
    cover_msg_id = v.get("cover_msg_id")
    primary_msg_id = v.get("primary_msg_id")
    cover_caption = f"🖼️ *Current Cover*\n\n{db.md_escape(v.get('title') or video_id)}"
    shown_cover = False

    # 1) Preferred: portable cover message stored in the primary channel.
    if cover_msg_id:
        try:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=storage_config.primary(),
                message_id=int(cover_msg_id),
                caption=cover_caption,
                parse_mode="Markdown",
            )
            shown_cover = True
        except Exception:
            log.warning(
                "Stored cover message %s is unavailable for %s; trying fallbacks",
                cover_msg_id, video_id, exc_info=True,
            )

    # 2) Legacy fallback: cover_file_id can be stale or belong to another bot.
    if not shown_cover and cover_file_id:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=cover_file_id,
                caption=cover_caption,
                parse_mode="Markdown",
            )
            shown_cover = True
        except Exception:
            log.warning(
                "Stored cover file_id is unavailable for %s; trying primary media thumbnail",
                video_id, exc_info=True,
            )

    # 3) Repair-friendly fallback for older records: copy the primary media,
    #    extract its Telegram-generated thumbnail, and send that as a preview.
    if not shown_cover and primary_msg_id:
        primary_copy = None
        try:
            primary_copy = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=storage_config.primary(),
                message_id=int(primary_msg_id),
            )

            thumb = None
            if getattr(primary_copy, "photo", None):
                thumb = primary_copy.photo[-1]
            elif getattr(primary_copy, "video", None):
                thumb = getattr(primary_copy.video, "thumbnail", None) or getattr(primary_copy.video, "thumb", None)
            elif getattr(primary_copy, "document", None):
                thumb = getattr(primary_copy.document, "thumbnail", None) or getattr(primary_copy.document, "thumb", None)

            if thumb:
                from io import BytesIO
                tg_file = await asyncio.wait_for(context.bot.get_file(thumb.file_id), timeout=10)
                raw = BytesIO()
                await asyncio.wait_for(tg_file.download_to_memory(raw), timeout=12)
                raw.seek(0)
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=raw,
                    caption=cover_caption + "\n\n_(preview from the stored media)_",
                    parse_mode="Markdown",
                )
                shown_cover = True
        except Exception:
            log.warning("Primary-media thumbnail fallback failed for %s", video_id, exc_info=True)
        finally:
            # The temporary copied media is only used to obtain its thumbnail.
            if primary_copy and getattr(primary_copy, "message_id", None):
                try:
                    await context.bot.delete_message(chat_id, primary_copy.message_id)
                except Exception:
                    pass

    if not shown_cover:
        await context.bot.send_message(
            chat_id=chat_id,
            text=("🖼️ *Current Cover*\n\n"
                  "No preview is available for this older cover. You can use "
                  "*🖼️ Change Cover* to replace it."),
            parse_mode="Markdown",
        )

    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb
    )


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/edit <id>`", parse_mode="Markdown")
        return
    await _send_edit_menu(update.effective_chat.id, context, context.args[0])


# ---------- /delete ----------

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/delete <id>`", parse_mode="Markdown")
        return
    video_id = context.args[0]
    if not db.get_video(video_id):
        await update.message.reply_text("No video with that ID.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚠️ Confirm Delete", callback_data=f"deldo_{video_id}"),
        InlineKeyboardButton("Cancel", callback_data="menu_home"),
    ]])
    await update.message.reply_text(f"Delete `{video_id}` from the catalog?", parse_mode="Markdown", reply_markup=kb)


# ---------- bulk operations ----------

async def bulkdelete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/bulkdelete id1,id2,id3`", parse_mode="Markdown")
        return
    ids = [x.strip() for x in " ".join(context.args).split(",") if x.strip()]
    found = [i for i in ids if db.get_video(i)]
    missing = [i for i in ids if i not in found]
    if not found:
        await update.message.reply_text("None of those IDs exist.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⚠️ Confirm Delete {len(found)}", callback_data="bulkdeldo_" + ",".join(found)),
        InlineKeyboardButton("Cancel", callback_data="menu_home"),
    ]])
    missing_note = f"\n(not found, skipped: {', '.join(missing)})" if missing else ""
    await update.message.reply_text(
        f"Delete {len(found)} video(s) from the catalog?{missing_note}", reply_markup=kb
    )


async def bulktag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/bulktag id1,id2 tag1,tag2` — adds tags to each video, keeping existing ones.",
            parse_mode="Markdown",
        )
        return
    ids = [x.strip() for x in context.args[0].split(",") if x.strip()]
    new_tags = [x.strip() for x in " ".join(context.args[1:]).split(",") if x.strip()]
    found = [i for i in ids if db.get_video(i)]
    if not found:
        await update.message.reply_text("None of those IDs exist.")
        return
    db.bulk_add_tags(found, new_tags)
    db.log_activity("admin_bot", "bulk_tag", f"{len(found)} video(s): +{', '.join(new_tags)}")
    await update.message.reply_text(f"✅ Added tags to {len(found)} video(s).")


# ---------- /export ----------

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    import csv
    import io
    rows = db.export_rows()
    if not rows:
        await update.message.reply_text("Nothing to export yet.")
        return
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.name = f"catalog_export_{db.today_str()}.csv"
    await update.message.reply_document(document=data, filename=data.name,
                                         caption=f"📄 {len(rows)} video(s) exported.")


# ---------- /backupdb ----------

async def backupdb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await _send_db_backup(context.bot, update.effective_chat.id, manual=True)


async def setbackup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        _, current = _nightly_backup_time()
        await update.message.reply_text(f"🌙 Nightly backup time: {current} ({getattr(config, 'TIMEZONE', 'configured timezone')})\nUse /setbackup HH:MM to change it.")
        return
    value = context.args[0].strip()
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        await update.message.reply_text("❌ Use 24-hour time like `/setbackup 02:00`.", parse_mode="Markdown")
        return
    db.set_setting("nightly_backup_time", value)
    # Allow the new schedule to fire today if the target is still ahead.
    db.log_activity("admin_bot", "set_nightly_backup", value)
    await update.message.reply_text(f"✅ Nightly backup set to *{value}* ({config.TIMEZONE}).", parse_mode="Markdown")


async def backupnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text("📦 Building a safe backup ZIP…")
    ok = await _send_nightly_backup(context.bot, update.effective_chat.id)
    if ok:
        await update.message.reply_text("✅ Backup sent above. Nothing sensitive was bundled.")


async def _send_db_backup(bot, chat_id, manual=False):
    """Sends the actual videos.db file. This is the only copy of catalog
    metadata (titles, tags, view counts, schedules) — the video files
    themselves are safe on Telegram regardless, but this file only ever
    lives on the phone unless you save a copy somewhere. Forward it to
    Saved Messages or a cloud-synced chat occasionally."""
    try:
        with open(config.DB_PATH, "rb") as f:
            caption = "💾 Manual backup" if manual else "💾 Weekly automatic backup"
            await bot.send_document(
                chat_id=chat_id, document=f, filename=f"videos_backup_{db.today_str()}.db",
                caption=f"{caption} of your catalog database.\n\n"
                        "This is your only copy of titles/tags/view-history metadata — "
                        "video files themselves are always safe on Telegram regardless. "
                        "Consider forwarding this to Saved Messages or a cloud-synced chat.",
            )
    except Exception as e:
        log.exception("DB backup failed")
        if manual:
            await bot.send_message(chat_id=chat_id, text=f"❌ Backup failed: {e}")


# ---------- /log ----------

async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    entries = db.get_recent_activity(20)
    if not entries:
        await update.message.reply_text("No activity logged yet.")
        return
    lines = [f"{e['ts'][:16]} · {e['actor']} · {e['action']}" + (f" — {e['detail']}" if e['detail'] else "")
             for e in entries]
    await update.message.reply_text("📜 *Recent Activity*\n\n" + "\n".join(lines), parse_mode="Markdown")


# ---------- content scheduler ----------
def _scheduler_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Weekly Slot", callback_data="scheduler_add"),
         InlineKeyboardButton("📋 View Schedule", callback_data="scheduler_view")],
        [InlineKeyboardButton("⏰ Schedule One Video", callback_data="scheduler_one"),
         InlineKeyboardButton("⚡ Release Now", callback_data="scheduler_release")],
        [InlineKeyboardButton("🔔 Auto Alert: ON/OFF", callback_data="scheduler_alert")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
    ])


def _scheduler_text():
    slots=_weekly_slots()
    auto='ON' if db.get_setting('weekly_auto_alert')=='1' else 'OFF'
    lines=['🗓️ *Content Scheduler*','',f'Weekly auto-alert: *{auto}*','']
    if not slots:
        lines.append('No weekly slots yet.')
    else:
        for i,x in enumerate(slots,1):
            lines.append(f"{i}. *{x.get('day','?').title()} {x.get('time','?')}* → {x.get('count',1)} item(s)")
    lines += ['', 'Use `/weekly Mon 18:00 1` to add a slot.', 'Use `/weeklyremove 2` to remove a slot.', 'Use `/weeklyclear` to clear the weekly plan.', 'Use `/schedule VIDEO_ID YYYY-MM-DD HH:MM` for one video.']
    return '\n'.join(lines)


async def scheduler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text(_scheduler_text(), parse_mode='Markdown', reply_markup=_scheduler_kb())


async def weekly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if len(context.args) < 2:
        await update.message.reply_text('Usage: `/weekly Mon 18:00 1`\nDay + time + number of queued items.', parse_mode='Markdown'); return
    day,time_str=context.args[0],context.args[1]
    try: datetime.strptime(time_str,'%H:%M')
    except ValueError:
        await update.message.reply_text('Time must be HH:MM (24h).'); return
    try: count=max(1,int(context.args[2])) if len(context.args)>2 else 1
    except ValueError:
        await update.message.reply_text('Count must be a number.'); return
    if _next_weekday_time(datetime.now(config.TIMEZONE),day,time_str) is None:
        await update.message.reply_text('Day must be Mon/Tue/Wed/Thu/Fri/Sat/Sun.'); return
    slots=_weekly_slots(); slots.append({'day':day.lower(),'time':time_str,'count':count}); _save_weekly_slots(slots)
    await update.message.reply_text(f'✅ Weekly slot added: {day.title()} {time_str} → {count} item(s).')


async def weeklyremove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text("Usage: `/weeklyremove SLOT_NUMBER`", parse_mode="Markdown")
        return
    try: idx=int(context.args[0])-1
    except ValueError:
        await update.message.reply_text("Slot number must be numeric."); return
    slots=_weekly_slots()
    if idx < 0 or idx >= len(slots):
        await update.message.reply_text("❌ That weekly slot does not exist."); return
    removed=slots.pop(idx); _save_weekly_slots(slots)
    db.log_activity("admin_bot","weekly_remove",str(removed))
    await update.message.reply_text(f"🗑 Removed *{removed.get('day','?').title()} {removed.get('time','?')}* slot.", parse_mode="Markdown", reply_markup=_scheduler_kb())


async def weeklyclear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    _save_weekly_slots([])
    db.set_setting("weekly_auto_alert", "0")
    db.log_activity("admin_bot","weekly_clear", "all weekly slots cleared")
    await update.message.reply_text("🧹 Weekly scheduler cleared and auto-alert switched OFF.", reply_markup=_scheduler_kb())


async def weeklytoggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    new="0" if db.get_setting("weekly_auto_alert")=="1" else "1"
    db.set_setting("weekly_auto_alert", new)
    await update.message.reply_text(f"{'🟢 Weekly auto-alert ON' if new=='1' else '⚪ Weekly auto-alert OFF'}", reply_markup=_scheduler_kb())


async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    results=await check_channel_access(context.bot, _required_channels())
    await update.message.reply_text(_channel_hub_text(results), parse_mode="Markdown", reply_markup=_channel_hub_kb())


async def diagnostics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    issues=_validate_config()
    results=await check_channel_access(context.bot, _required_channels())
    s=db.stats()
    lines=["🧰 *Vault Diagnostics*", "", f"Python process: *online*", f"Catalog rows: *{s.get('total_videos',0)}*", f"Missing backups: *{s.get('missing_backup',0)}*", ""]
    lines.append("*Config*")
    lines.extend(f"• {x}" for x in issues) if issues else lines.append("• ✅ Required config looks present")
    lines.append("")
    lines.append("*Telegram access*")
    lines.extend(f"• {status} {label}" for label,_,status in results)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu_kb())


async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if len(context.args) < 3:
        await update.message.reply_text('Usage: `/schedule VIDEO_ID YYYY-MM-DD HH:MM`', parse_mode='Markdown'); return
    vid=context.args[0]; when=' '.join(context.args[1:3])
    if not db.get_video(vid):
        await update.message.reply_text('❌ Video ID not found.'); return
    try:
        dt=datetime.strptime(when,'%Y-%m-%d %H:%M').replace(tzinfo=config.TIMEZONE)
    except ValueError:
        await update.message.reply_text('Use YYYY-MM-DD HH:MM.'); return
    if dt <= datetime.now(config.TIMEZONE):
        await update.message.reply_text('⏰ Time must be in the future.'); return
    db.set_video_publish_at(vid,dt.isoformat()); db.log_activity('admin_bot','schedule_video',f'{vid} at {dt.isoformat()}')
    await update.message.reply_text(f'✅ Scheduled `{vid}` for *{dt.strftime("%d %b %Y, %H:%M")}*.',parse_mode='Markdown')


async def scheduled_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    videos=[v for v in db.all_videos(limit=10000) if v.get('publish_at') and not db.is_visible(v)]
    videos.sort(key=lambda v:v.get('publish_at') or '')
    if not videos:
        await update.message.reply_text('📭 No upcoming scheduled content.',reply_markup=_scheduler_kb()); return
    lines=['🗓️ *Upcoming Content*','']
    for v in videos[:20]: lines.append(f"`{v['id']}` · {db.md_escape(v.get('title') or 'Untitled')} · {v['publish_at'][:16]}")
    await update.message.reply_text('\n'.join(lines),parse_mode='Markdown',reply_markup=_scheduler_kb())


async def unschedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args: await update.message.reply_text('Usage: `/unschedule VIDEO_ID`',parse_mode='Markdown'); return
    vid=context.args[0]
    if not db.get_video(vid): await update.message.reply_text('❌ Video ID not found.'); return
    db.set_video_publish_at(vid,None); await update.message.reply_text(f'✅ `{vid}` is live immediately again.',parse_mode='Markdown')


# ---------- scheduled (auto) alerts ----------

async def setautoalert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/setautoalert HH:MM` (24h, your configured timezone)",
                                         parse_mode="Markdown")
        return
    time_str = context.args[0]
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text("Use 24h `HH:MM` format, e.g. `18:30`.", parse_mode="Markdown")
        return
    db.set_setting("auto_alert_time", time_str)
    await update.message.reply_text(
        f"✅ Auto-alert enabled — whatever's queued will post automatically at {time_str} daily. "
        "Uses the default alert wording (no manual confirm, since this is opt-in). "
        "/clearautoalert to disable."
    )


async def clearautoalert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    db.clear_setting("auto_alert_time")
    await update.message.reply_text("🗑 Auto-alert disabled.")


# ---------- Advanced Analytics ----------

def _analytics_kb(days: int = 7):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 7 Days", callback_data="analytics_7"),
         InlineKeyboardButton("📅 30 Days", callback_data="analytics_30")],
        [InlineKeyboardButton("🏆 Top Videos", callback_data=f"analytics_top_{days}"),
         InlineKeyboardButton("🏷 Popular Tags", callback_data=f"analytics_tags_{days}")],
        [InlineKeyboardButton("🤖 Temp Bots", callback_data=f"analytics_temp_{days}"),
         InlineKeyboardButton("🔐 Access", callback_data=f"analytics_access_{days}")],
        [InlineKeyboardButton("🔎 Searches", callback_data=f"analytics_search_{days}"),
         InlineKeyboardButton("📈 Refresh", callback_data=f"analytics_{days}")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
    ])


def _bar(value: int, maximum: int, width: int = 10) -> str:
    if maximum <= 0:
        return "░" * width
    filled = max(0, min(width, round(value / maximum * width)))
    return "█" * filled + "░" * (width - filled)


async def _analytics_text(days: int = 7) -> str:
    a = db.analytics_overview(days)
    daily = db.analytics_daily(min(days, 7))
    access = db.analytics_access(days)
    lines = [
        "📈 *Advanced Analytics*",
        f"_Last {days} day(s)_",
        "",
        "👥 *Audience*",
        f"• Total tracked users: *{a['total_users']:,}*",
        f"• Active users: *{a['active_users']:,}*",
        f"• Unique viewers: *{a['unique_viewers']:,}*",
        f"• Premium users now: *{a['premium_users']:,}*",
        "",
        "🎬 *Content Performance*",
        f"• Deliveries: *{a['deliveries']:,}*",
        f"• Ad unlocks: *{a['ad_unlocks']:,}*",
        f"• Redeem activations: *{a['redemptions']:,}*",
        f"• Searchers: *{a['searchers']:,}*",
        "",
        "🔐 *Access*",
        f"• Gated videos: *{access['gated_videos']:,}*",
        f"• Active redeem codes: *{access['active_codes']:,}*",
        f"• All-time code uses: *{access['all_code_uses']:,}*",
    ]
    if daily:
        peak = max((r['deliveries'] for r in daily), default=0)
        lines += ["", "📊 *Recent Daily Deliveries*"]
        for r in daily[-7:]:
            lines.append(f"`{r['day']}` {_bar(r['deliveries'], peak)} {r['deliveries']:,}")
    else:
        lines += ["", "📊 *Recent Daily Deliveries*", "No delivery events recorded yet."]
    lines += ["", "_Analytics starts collecting from this version onward; old views remain in the catalog stats._"]
    return "\n".join(lines)


async def _analytics_top_text(days: int = 30) -> str:
    rows = db.analytics_top_videos(days, 10)
    if not rows:
        return "🏆 *Top Content*\n\nNo delivery analytics yet."
    lines = [f"🏆 *Top Content — {days}d*", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"*{i}.* {db.md_escape(r['title'])}\n   ▶️ {r['deliveries']:,} deliveries · 👥 {r['unique_viewers']:,} viewers")
    return "\n".join(lines)


async def _analytics_tags_text(days: int = 30) -> str:
    rows=db.analytics_top_tags(days,12)
    if not rows: return f"🏷 *Popular Tags — {days}d*\n\nNo delivery-tag analytics yet."
    lines=[f"🏷 *Popular Tags — {days}d*", ""]
    for i,r in enumerate(rows,1): lines.append(f"*{i}.* {db.md_escape(r['tag'])} — 🔥 {r['deliveries']:,} deliveries")
    return "\n".join(lines)

async def _analytics_temp_text(days: int = 30) -> str:
    rows=db.analytics_temp_bots(days,20)
    if not rows: return f"🤖 *Temporary Bot Analytics — {days}d*\n\nNo temporary bot clicks yet."
    lines=[f"🤖 *Temporary Bot Analytics — {days}d*", ""]
    for i,r in enumerate(rows,1): lines.append(f"*{i}.* `{db.md_escape(str(r['bot_id']))}`\n   ▶️ Starts: {r['starts']:,} · 🔗 Clicks: {r['clicks']:,} · CTR: *{r['ctr']:.1f}%* · 👥 {r['unique_users']:,} users")
    return "\n".join(lines)

async def _analytics_access_text(days: int = 30) -> str:
    a = db.analytics_access(days)
    return (
        f"🔐 *Access Analytics — {days}d*\n\n"
        f"🔒 Gated videos: *{a['gated_videos']:,}*\n"
        f"📺 Ad unlocks: *{a['ad_unlocks']:,}*\n"
        f"🎟 Redeems: *{a['redemptions']:,}*\n"
        f"🎟 Active codes: *{a['active_codes']:,}*\n"
        f"📦 All-time code uses: *{a['all_code_uses']:,}*\n\n"
        "Use this view to compare ad-unlock activity against premium-code usage."
    )


async def _analytics_search_text(days: int = 30) -> str:
    rows = db.analytics_searches(days, 10)
    if not rows:
        return "🔎 *Search Analytics*\n\nNo searches recorded yet."
    lines = [f"🔎 *Top Searches — {days}d*", ""]
    for i, r in enumerate(rows, 1):
        q = db.md_escape(r['query'])
        lines.append(f"*{i}.* `{q}` — {r['searches']:,} searches · {r['users']:,} users")
    return "\n".join(lines)


async def analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(await _analytics_text(7), parse_mode="Markdown", reply_markup=_analytics_kb(7))


# ---------- /stats ----------

async def _stats_text() -> str:
    s = db.stats()
    return (
        f"📊 *Stats*\n"
        f"Total videos: {s['total_videos']}\n"
        f"Total views: {s['total_views']}\n"
        f"Missing backup copies: {s['missing_backup']}"
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(await _stats_text(), parse_mode="Markdown", reply_markup=main_menu_kb())


# ---------- /verify (with per-video retry buttons) ----------

async def _send_verify_report(chat_id, context):
    videos = db.all_videos(limit=10000)
    missing = [v for v in videos if not v["backup_msg_id"]]
    if not missing:
        await context.bot.send_message(chat_id=chat_id, text="✅ All videos have a confirmed backup copy.",
                                        reply_markup=main_menu_kb())
        return
    await context.bot.send_message(
        chat_id=chat_id, text=f"⚠️ *{len(missing)} video(s) missing a backup copy:*",
        parse_mode="Markdown",
    )
    for v in missing:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Retry Backup", callback_data=f"retryback_{v['id']}")]])
        await context.bot.send_message(chat_id=chat_id, text=f"`{v['id']}` — {db.md_escape(v['title'])}",
                                        parse_mode="Markdown", reply_markup=kb)
    await context.bot.send_message(chat_id=chat_id, text="🏠 Menu", reply_markup=main_menu_kb())


async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await _send_verify_report(update.effective_chat.id, context)


# ---------- /top ----------

async def _top_text() -> str:
    top = db.top_videos(10)
    if not top:
        return "No videos yet."
    lines = [f"{i+1}. *{db.md_escape(v['title'])}* — 👁 {v['view_count']} views" for i, v in enumerate(top)]
    return "🔥 *Top Videos*\n\n" + "\n".join(lines)


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(await _top_text(), parse_mode="Markdown", reply_markup=main_menu_kb())


# ---------- date-wise publishing planner ----------
def _schedule_day_iso(offset: int = 0) -> str:
    return (datetime.now(config.TIMEZONE).date() + timedelta(days=offset)).isoformat()


def _schedule_day_kb(day_iso: str):
    d = datetime.fromisoformat(day_iso).date()
    prev = (d - timedelta(days=1)).isoformat()
    nxt = (d + timedelta(days=1)).isoformat()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Previous", callback_data=f"admin_sched_day_{prev}"),
         InlineKeyboardButton("Today", callback_data=f"admin_sched_day_{_schedule_day_iso(0)}"),
         InlineKeyboardButton("Next ▶️", callback_data=f"admin_sched_day_{nxt}")],
        [InlineKeyboardButton("📋 Upcoming 7 Days", callback_data="admin_sched_week"),
         InlineKeyboardButton("🔄 Refresh", callback_data=f"admin_sched_day_{day_iso}")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
    ])


def _schedule_day_text(day_iso: str) -> str:
    try:
        target = datetime.fromisoformat(day_iso).date()
    except ValueError:
        target = datetime.now(config.TIMEZONE).date()
        day_iso = target.isoformat()
    rows = db.all_videos(limit=10000)
    scheduled = [v for v in rows if v.get("publish_at") and str(v.get("publish_at"))[:10] == day_iso and not db.is_visible(v)]
    batches = {}
    for v in scheduled:
        bid = v.get("batch_id")
        if bid:
            batches.setdefault(bid, 0)
            batches[bid] += 1
    lines = [f"📅 *Publishing — {target.strftime('%A, %d %B %Y')}*", "",
             f"🎬 Scheduled videos: *{len(scheduled):,}*",
             f"📦 Collections: *{len(batches):,}*",
             f"🎞️ Collection items: *{sum(batches.values()):,}*"]
    if scheduled:
        lines += ["", "*Queue*"]
        for v in sorted(scheduled, key=lambda x: str(x.get("publish_at") or ""))[:25]:
            tm = str(v.get("publish_at") or "")[11:16] or "--:--"
            title = str(v.get("title") or v.get("id") or "Untitled").replace("*", "")[:48]
            suffix = f" · 📦 {v.get('batch_id')}" if v.get('batch_id') else ""
            lines.append(f"• *{tm}* · `{v.get('video_number') or v.get('id')}` · {title}{suffix}")
        if len(scheduled) > 25:
            lines.append(f"_…and {len(scheduled)-25} more._")
    else:
        lines += ["", "✅ Nothing scheduled for this date."]
    return "\n".join(lines)


def _schedule_week_text() -> str:
    today = datetime.now(config.TIMEZONE).date()
    rows = db.all_videos(limit=10000)
    lines = ["🗓️ *Publishing — Next 7 Days*", ""]
    any_items = False
    for i in range(7):
        d = today + timedelta(days=i)
        iso = d.isoformat()
        scheduled = [v for v in rows if v.get("publish_at") and str(v.get("publish_at"))[:10] == iso and not db.is_visible(v)]
        if scheduled:
            any_items = True
            batches = len({v.get("batch_id") for v in scheduled if v.get("batch_id")})
            lines.append(f"*{d.strftime('%a %d %b')}* — 🎬 {len(scheduled)} · 📦 {batches}")
        else:
            lines.append(f"*{d.strftime('%a %d %b')}* — 0 scheduled")
    if not any_items:
        lines += ["", "🌙 No upcoming content is scheduled."]
    return "\n".join(lines)


# ---------- /bydate — proof that old data is never auto-removed ----------

async def _bydate_text() -> str:
    counts = db.count_by_date(30)
    if not counts:
        return "No videos yet."
    total = sum(c for _, c in counts)
    lines = [f"{d} — {c} video(s)" for d, c in counts]
    return (
        "📅 *Uploads by Date* (last 30 days with activity)\n\n" + "\n".join(lines) +
        f"\n\n_{total} video(s) shown. Nothing here ever auto-deletes — the 30-min "
        "auto-delete only clears Telegram notification messages, never catalog data._"
    )


async def bydate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(await _bydate_text(), parse_mode="Markdown", reply_markup=main_menu_kb())


# ---------- text input for edit flow ----------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if context.user_data.get("awaiting_video_cover") and (update.message.text or "").strip().lower() in {"cancel", "/cancel"}:
        context.user_data.pop("awaiting_video_cover", None)
        await update.message.reply_text("↩️ Cover change cancelled. Existing cover is unchanged.")
        return


    if context.user_data.get("awaiting_permbot_token"):
        bid=str(context.user_data.pop("awaiting_permbot_token"))
        token=(update.message.text or "").strip()
        if not token or token.lower() in {"cancel","/cancel"}:
            await update.message.reply_text("↩️ Token update cancelled.", reply_markup=_permbots_kb()); return
        bot=None
        try:
            bot=Bot(token=token)
            me=await bot.get_me()
            if not me or not me.id or not me.username:
                raise ValueError("Telegram did not return a usable bot identity")
            if str(me.id)==str(context.bot.id):
                await update.message.reply_text("❌ Admin Bot token cannot be used as a Delivery Bot.", reply_markup=_permbots_kb()); return
            ok, reason=permanent_bot_store.replace_token(bid,token,me.username or "",me.first_name or "")
            if not ok:
                msg={"duplicate":"❌ This token is already registered to another bot.","not_found":"❌ Delivery Bot not found."}.get(reason,"❌ Token update failed.")
                await update.message.reply_text(msg,reply_markup=_permbots_kb()); return
            db.log_activity("admin_bot","permbot_token_update",f"bot={bid} username=@{me.username}")
            await update.message.reply_text(f"✅ *Token updated successfully*\n\n🤖 @{db.md_escape(me.username)}\n🆔 `{me.id}`\n🔄 Worker restart requested automatically.",parse_mode="Markdown",reply_markup=_permbots_kb())
        except Exception as exc:
            await update.message.reply_text(f"❌ Telegram token validation failed: {db.md_escape(str(exc)[:160])}",parse_mode="Markdown",reply_markup=_permbots_kb())
        finally:
            if bot:
                try: await bot.close()
                except Exception: pass
        return

    if context.user_data.get("awaiting_permbot_add"):
        context.user_data.pop("awaiting_permbot_add", None)
        raw_lines=[line.strip() for line in (update.message.text or "").splitlines() if line.strip()]
        if not raw_lines or raw_lines[0].lower() in {"cancel","/cancel"}:
            await update.message.reply_text("↩️ Permanent Delivery Bot add cancelled.", reply_markup=_permbots_kb()); return
        added=[]; skipped=[]; failed=[]
        for token in raw_lines:
            try:
                bot=Bot(token=token); me=await bot.get_me()
                if int(me.id)==int(getattr(context.bot,"id",0) or 0): skipped.append(f"@{me.username or me.id} (this Admin Bot)"); await bot.close(); continue
                ok=permanent_bot_store.add_bot(bot_id=str(me.id),username=me.username or "",first_name=me.first_name or "",token=token,enabled=True,primary=False)
                (added if ok else skipped).append(f"@{me.username or me.id}")
                await bot.close()
            except Exception as exc:
                failed.append(f"❌ Telegram validation failed — {db.md_escape(str(exc)[:140])}")
        lines=[f"✅ Added: {len(added)}",f"↩️ Skipped: {len(skipped)}",f"❌ Failed: {len(failed)}",""]
        if added: lines.append("Added: "+", ".join(added[:20]))
        if skipped: lines.append("Skipped: "+", ".join(skipped[:20]))
        if failed: lines.append("Failed:\n"+"\n".join(failed[:10]))
        db.log_activity("admin_bot","permbots_add",f"added={len(added)} skipped={len(skipped)} failed={len(failed)}")
        if added:
            lines.append("\n🟢 *Worker:* queued for automatic startup + health check.")
            lines.append("💬 *Start test:* open the bot in Telegram and send `/start`.")
        elif not failed:
            lines.append("\n⚠️ No new bot was added. Check whether the token was already registered.")
        await update.message.reply_text("🚀 *Permanent Delivery Bot Pool Updated*\n\n"+"\n".join(lines),parse_mode="Markdown",reply_markup=_permbots_kb()); return

    if context.user_data.get("awaiting_tempbot_token"):
        bot_id=str(context.user_data.pop("awaiting_tempbot_token"))
        raw=(update.message.text or "").strip()
        if not raw or raw.lower() in {"cancel","/cancel"}:
            await update.message.reply_text("↩️ Token update cancelled.",reply_markup=_tempbots_kb()); return
        bot=None
        try:
            bot=Bot(token=raw); me=await bot.get_me()
            if not me or not me.id or not me.username: raise ValueError("Telegram did not return a usable bot identity")
            if int(me.id)==int(getattr(context.bot,"id",0) or 0):
                await update.message.reply_text("❌ Admin Bot token cannot be used as a Temporary Bot.",reply_markup=_tempbots_kb()); return
            ok, reason = temp_bot_store.replace_token(bot_id, raw, me.username or "", me.first_name or "")
            if not ok:
                raise ValueError("This token is already registered to another temporary bot" if reason == "duplicate" else "Temporary bot not found")
            db.log_activity("admin_bot","tempbot_token_update",f"bot={bot_id} username=@{me.username}")
            await update.message.reply_text(f"✅ *Token updated*\n\n🤖 @{db.md_escape(me.username)}\n🔄 Worker restart queued automatically.",parse_mode='Markdown',reply_markup=_tempbots_kb())
        except Exception as exc:
            await update.message.reply_text(f"❌ Token validation failed: {db.md_escape(str(exc)[:180])}",parse_mode='Markdown',reply_markup=_tempbots_kb())
        finally:
            if bot:
                try: await bot.close()
                except Exception: pass
        return

    if context.user_data.get("awaiting_tempbot_add"):
        context.user_data.pop("awaiting_tempbot_add", None)
        raw_lines = [line.strip() for line in (update.message.text or "").splitlines() if line.strip()]
        if not raw_lines or raw_lines[0].lower() in {"cancel", "/cancel"}:
            await update.message.reply_text("↩️ Temporary bot add cancelled.", reply_markup=_tempbots_kb())
            return
        added, skipped, failed = [], [], []
        for token in raw_lines:
            bot = None
            try:
                bot = Bot(token=token)
                me = await bot.get_me()
                if int(me.id) == int(getattr(context.bot, "id", 0) or 0):
                    skipped.append(f"@{me.username or me.id} (this Admin Bot)")
                    continue
                ok = temp_bot_store.add_bot(bot_id=str(me.id), username=me.username or "", first_name=me.first_name or "", token=token)
                if ok:
                    added.append(f"@{me.username or me.id}")
                else:
                    skipped.append(f"@{me.username or me.id} (already added)")
            except Exception as exc:
                failed.append(f"❌ Telegram validation failed — {db.md_escape(str(exc)[:140])}")
            finally:
                if bot is not None:
                    try:
                        await bot.close()
                    except Exception:
                        pass
        lines = [f"✅ Added: {len(added)}", f"↩️ Skipped: {len(skipped)}", f"❌ Failed: {len(failed)}", ""]
        if added: lines.append("Added: " + ", ".join(added[:20]))
        if skipped: lines.append("Skipped: " + ", ".join(skipped[:20]))
        if failed: lines.append("Failed:\n" + "\n".join(failed[:10]))
        db.log_activity("admin_bot", "tempbots_add", f"added={len(added)} skipped={len(skipped)} failed={len(failed)}")
        await update.message.reply_text(
            "🤖 *Temporary Bot Pool Updated*\n\n" + "\n".join(lines) +
            "\n\n🟢 *Status:* Telegram token validation completed.\n"
            "Enabled bots will be started automatically by the supervisor. Use 👁 Preview or `/start` on the bot to verify it.",
            parse_mode="Markdown", reply_markup=_tempbots_kb()
        )
        return

    specific_image = context.user_data.get("awaiting_tempbot_specific_image")
    if specific_image:
        raw = (update.message.text or "").strip()
        bot_id = str(specific_image.get("bot_id"))
        if raw.lower() in {"cancel", "/cancel"}:
            context.user_data.pop("awaiting_tempbot_specific_image", None)
            await _tempbot_settings_message(update.effective_chat.id, context, bot_id)
            return
        if raw.lower() == "default":
            cfg=temp_bot_store.get_bot_config(bot_id, default_link=_default_temp_channel_link())
            old=cfg["message"].get("image_path") if cfg["overrides"]["image"] else ""
            temp_bot_store.clear_bot_message_field(bot_id, "image_path")
            try:
                path=Path(str(old or "")); asset_dir=Path(os.path.dirname(botutil.MANDATORY_JOIN_IMAGE)) / "temp_bots"
                if path.is_file() and asset_dir in path.parents: path.unlink(missing_ok=True)
            except Exception: pass
            context.user_data.pop("awaiting_tempbot_specific_image", None)
            db.log_activity("admin_bot","tempbot_specific_image_reset",bot_id)
            await update.message.reply_text("✅ Bot image reset to the pool default.", reply_markup=_tempbot_settings_kb(bot_id))
            return
        await update.message.reply_text("🖼 Please send a photo, or send `default` / `cancel`.", parse_mode="Markdown")
        return

    specific = context.user_data.get("awaiting_tempbot_specific")
    if specific:
        raw = update.message.text or ""
        bot_id = str(specific.get("bot_id"))
        field = str(specific.get("field"))
        row = temp_bot_store.get_bot(bot_id)
        if not row:
            context.user_data.pop("awaiting_tempbot_specific", None)
            await update.message.reply_text("❌ Temporary bot no longer exists.", reply_markup=_tempbots_kb())
            return
        if raw.strip().lower() in {"cancel", "/cancel"}:
            context.user_data.pop("awaiting_tempbot_specific", None)
            await _tempbot_settings_message(update.effective_chat.id, context, bot_id)
            return
        if raw.strip().lower() == "default":
            if field == "channel":
                temp_bot_store.clear_bot_channel(bot_id)
            elif field == "button":
                temp_bot_store.set_bot_message(bot_id, button_text=None)
                # Remove only the override key via reset/merge helper below.
                temp_bot_store.clear_bot_message_field(bot_id, "button_text")
            elif field == "message":
                temp_bot_store.clear_bot_message_field(bot_id, "text")
            context.user_data.pop("awaiting_tempbot_specific", None)
            db.log_activity("admin_bot", "tempbot_specific_reset", f"{bot_id}:{field}")
            await _tempbot_settings_message(update.effective_chat.id, context, bot_id)
            return
        if field == "channel":
            if "|" not in raw:
                await update.message.reply_text("❌ Use `Channel Name | https://t.me/username` or `default`.", parse_mode="Markdown")
                return
            name, link = [x.strip() for x in raw.split("|", 1)]
            if not name or not link.startswith(("https://t.me/", "http://t.me/")):
                await update.message.reply_text("❌ Channel name can't be empty and link must start with `https://t.me/`.", parse_mode="Markdown")
                return
            temp_bot_store.set_bot_channel(bot_id, name, link)
        elif field == "button":
            if not raw.strip():
                await update.message.reply_text("❌ Button label can't be empty.")
                return
            temp_bot_store.set_bot_message(bot_id, button_text=raw[:64])
        elif field == "message":
            if not raw.strip():
                await update.message.reply_text("❌ Message can't be empty.")
                return
            temp_bot_store.set_bot_message(bot_id, text=raw[:4000])
        else:
            await update.message.reply_text("❌ Unknown setting.")
            return
        context.user_data.pop("awaiting_tempbot_specific", None)
        db.log_activity("admin_bot", "tempbot_specific_updated", f"{bot_id}:{field}")
        await update.message.reply_text("✅ Bot-specific setting saved.", reply_markup=_tempbot_settings_kb(bot_id))
        return

    if context.user_data.get("awaiting_tempbot_channel"):
        raw = (update.message.text or "").strip()
        if raw.lower() in {"cancel", "/cancel"}:
            context.user_data.pop("awaiting_tempbot_channel", None)
            await update.message.reply_text("↩️ Channel change cancelled.", reply_markup=_tempbots_kb())
            return
        if "|" not in raw:
            context.user_data["awaiting_tempbot_channel"] = True
            await update.message.reply_text("❌ Use `Channel Name | https://t.me/username`.", parse_mode="Markdown")
            return
        name, link = [x.strip() for x in raw.split("|", 1)]
        if not name or not link.startswith(("https://t.me/", "http://t.me/")):
            context.user_data["awaiting_tempbot_channel"] = True
            await update.message.reply_text("❌ Channel name can't be empty and link must start with `https://t.me/`.", parse_mode="Markdown")
            return
        temp_bot_store.set_channel(name, link)
        context.user_data.pop("awaiting_tempbot_channel", None)
        db.log_activity("admin_bot", "tempbots_channel_updated", f"{name} | {link}")
        await update.message.reply_text("✅ Temporary Bot Pool main channel updated.", reply_markup=_tempbots_kb())
        return

    if context.user_data.get("awaiting_tempbot_button"):
        raw = (update.message.text or "").strip()
        if raw.lower() in {"cancel", "/cancel"}:
            context.user_data.pop("awaiting_tempbot_button", None)
            await update.message.reply_text("↩️ Button change cancelled.", reply_markup=_tempbots_kb())
            return
        if not raw:
            context.user_data["awaiting_tempbot_button"] = True
            await update.message.reply_text("❌ Button label can't be empty.")
            return
        temp_bot_store.set_message(raw[:64])
        context.user_data.pop("awaiting_tempbot_button", None)
        db.log_activity("admin_bot", "tempbots_button_updated", raw[:64])
        await update.message.reply_text("✅ Temporary bot start button updated.", reply_markup=_tempbots_kb())
        return

    if context.user_data.get("awaiting_tempbot_message"):
        raw = update.message.text or ""
        if raw.strip().lower() in {"cancel", "/cancel"}:
            context.user_data.pop("awaiting_tempbot_message", None)
            await update.message.reply_text("↩️ Start message change cancelled.", reply_markup=_tempbots_kb())
            return
        if not raw.strip():
            await update.message.reply_text("❌ Message can't be empty. Send some text or `cancel`.", parse_mode="Markdown")
            return
        context.user_data.pop("awaiting_tempbot_message", None)
        temp_bot_store.set_start_text(raw[:4000])
        db.log_activity("admin_bot", "tempbots_message_updated", raw[:120])
        await update.message.reply_text("✅ Temporary bot start message updated.", reply_markup=_tempbots_kb())
        return

    if context.user_data.get("awaiting_mandatory_join_message"):
        raw = (update.message.text or "").strip()
        if not raw or raw.lower() in {"cancel", "/cancel"}:
            context.user_data.pop("awaiting_mandatory_join_message", None)
            await update.message.reply_text("↩️ Join message unchanged.", reply_markup=_mandatory_join_admin_kb())
            return
        db.set_setting("mandatory_join_message", raw)
        db.log_activity("admin_bot", "mandatory_join_message_updated", raw[:120])
        context.user_data.pop("awaiting_mandatory_join_message", None)
        await update.message.reply_text("✅ Mandatory join message updated.\n\n" + _mandatory_join_admin_text(), parse_mode="Markdown", reply_markup=_mandatory_join_admin_kb())
        return

    if context.user_data.get("awaiting_mandatory_join"):
        raw = (update.message.text or "").strip()
        if raw.lower() == "reset":
            db.clear_setting("mandatory_join_channel_id"); db.clear_setting("mandatory_join_channel_link"); db.clear_setting("mandatory_join_message"); context.user_data.pop("awaiting_mandatory_join", None)
            await update.message.reply_text("✅ Mandatory join channel reset to config default.", reply_markup=_mandatory_join_admin_kb()); return
        try:
            if "|" in raw:
                channel_id, link = [x.strip() for x in raw.split("|", 1)]
            else:
                link = raw
                if raw.startswith(("https://t.me/", "http://t.me/")):
                    tail = raw.rstrip("/").split("/")[-1]
                    if tail.startswith("+"): raise ValueError("Private invite links require the numeric channel ID too.")
                    channel_id = "@" + tail.lstrip("@")
                elif raw.startswith("@"): channel_id = raw
                else: raise ValueError("Use @username, https://t.me/username, or CHANNEL_ID | INVITE_LINK")
            if not (channel_id.startswith("@") or channel_id.lstrip("-").isdigit()): raise ValueError("Channel target must be @username or numeric channel ID")
            if not link.startswith(("https://t.me/", "http://t.me/")): raise ValueError("Join link must start with https://t.me/")
            db.set_setting("mandatory_join_channel_id", channel_id); db.set_setting("mandatory_join_channel_link", link)
            db.log_activity("admin_bot", "mandatory_join_updated", f"{channel_id} | {link}")
            context.user_data.pop("awaiting_mandatory_join", None)
            await update.message.reply_text("✅ Mandatory join channel updated.\n\n"+_mandatory_join_admin_text(), parse_mode="Markdown", reply_markup=_mandatory_join_admin_kb())
        except ValueError as exc:
            await update.message.reply_text(f"❌ {db.md_escape(str(exc))}\n\nTry again.", parse_mode="Markdown")
        return

    if context.user_data.get("awaiting_admin_add"):
        context.user_data.pop("awaiting_admin_add", None)
        raw = update.message.text.strip()
        parts = raw.split()
        try:
            uid = int(parts[0])
            role = parts[1] if len(parts) > 1 else "admin"
            if uid <= 0:
                raise ValueError
        except (ValueError, IndexError):
            context.user_data["awaiting_admin_add"] = True
            await update.message.reply_text("❌ Send a numeric Telegram user ID, optionally followed by a role. Example: `123456789 editor`", parse_mode="Markdown")
            return
        admin_store.add_admin(uid, role)
        await update.message.reply_text(f"✅ Admin `{uid}` added with role `{role}`.", parse_mode="Markdown", reply_markup=_admin_manage_kb())
        return

    if context.user_data.get("awaiting_admin_remove"):
        context.user_data.pop("awaiting_admin_remove", None)
        try:
            uid = int(update.message.text.strip())
        except ValueError:
            context.user_data["awaiting_admin_remove"] = True
            await update.message.reply_text("❌ Send only the numeric Telegram user ID.")
            return
        protected = list(config.ADMIN_USER_IDS or [])
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("🚫 Only the primary owner can remove admins.", reply_markup=_admin_manage_kb())
            return
        if not admin_store.remove_admin(uid, protected):
            await update.message.reply_text("⚠️ That admin was not found, or it is a protected owner.", reply_markup=_admin_manage_kb())
            return
        await update.message.reply_text(f"🗑 Admin `{uid}` removed.", parse_mode="Markdown", reply_markup=_admin_manage_kb())
        return

    if context.user_data.get("awaiting_batch_schedule"):
        batch_id = context.user_data.pop("awaiting_batch_schedule")
        raw = update.message.text.strip()
        parsed = db.parse_schedule_input(raw)
        if not parsed:
            context.user_data["awaiting_batch_schedule"] = batch_id
            await update.message.reply_text("❌ Invalid time. Use `YYYY-MM-DD HH:MM` or `+2h`, `+1d`.", parse_mode="Markdown")
            return
        try:
            dt = datetime.fromisoformat(parsed)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=config.TIMEZONE)
            dt = dt.astimezone(config.TIMEZONE)
        except Exception:
            context.user_data["awaiting_batch_schedule"] = batch_id
            await update.message.reply_text("❌ Couldn't read that time. Try again.")
            return
        if dt <= datetime.now(config.TIMEZONE):
            context.user_data["awaiting_batch_schedule"] = batch_id
            await update.message.reply_text("⏰ That time is already past. Send a future time.")
            return
        if not db.get_batch(batch_id):
            await update.message.reply_text("❌ Collection no longer exists.")
            return
        db.update_batch_fields(batch_id, publish_at=dt.isoformat())
        db.log_activity("admin_bot", "schedule_collection", f"{batch_id} at {dt.isoformat()}")
        await update.message.reply_text(f"✅ *Collection scheduled!* `{batch_id}`\n\n⏰ {dt.strftime('%d %b %Y, %H:%M %Z')}\n\n🔒 All items remain hidden until this time.", parse_mode="Markdown")
        await _send_collection_edit_menu(update.effective_chat.id, context, batch_id)
        return

    if context.user_data.get("awaiting_batch_access_code"):
        batch_id = context.user_data.pop("awaiting_batch_access_code")
        code = update.message.text.strip().upper()
        if not code:
            context.user_data["awaiting_batch_access_code"] = batch_id
            await update.message.reply_text("❌ Send a redeem code, or /cancel.")
            return
        db.update_batch_fields(batch_id, access_tier="redeem", access_redeem_code=code, access_user_ids=None)
        await update.message.reply_text("✅ Redeem access applied to the entire collection.")
        await _send_collection_edit_menu(update.effective_chat.id, context, batch_id)
        return

    if context.user_data.get("awaiting_batch_access_users"):
        batch_id = context.user_data.pop("awaiting_batch_access_users")
        raw = update.message.text.strip()
        try:
            ids = [str(int(x.strip())) for x in raw.split(",") if x.strip()]
            if not ids:
                raise ValueError
        except ValueError:
            context.user_data["awaiting_batch_access_users"] = batch_id
            await update.message.reply_text("❌ Send numeric Telegram user IDs separated by commas.")
            return
        db.update_batch_fields(batch_id, access_tier="users", access_redeem_code=None, access_user_ids=",".join(ids))
        await update.message.reply_text("✅ Specific-user access applied to the entire collection.")
        await _send_collection_edit_menu(update.effective_chat.id, context, batch_id)
        return

    if context.user_data.get("awaiting_video_schedule"):
        vid = context.user_data.pop("awaiting_video_schedule")
        raw = update.message.text.strip()
        parsed = db.parse_schedule_input(raw)
        if not parsed:
            context.user_data["awaiting_video_schedule"] = vid
            await update.message.reply_text("❌ Invalid time. Use `YYYY-MM-DD HH:MM` or `+2h`, `+1d`.", parse_mode="Markdown")
            return
        try:
            dt = datetime.fromisoformat(parsed)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=config.TIMEZONE)
            dt = dt.astimezone(config.TIMEZONE)
        except Exception:
            context.user_data["awaiting_video_schedule"] = vid
            await update.message.reply_text("❌ Couldn't read that time. Try again.")
            return
        if dt <= datetime.now(config.TIMEZONE):
            context.user_data["awaiting_video_schedule"] = vid
            await update.message.reply_text("⏰ That time is already past. Send a future time.")
            return
        if not db.get_video(vid):
            await update.message.reply_text("❌ Video no longer exists.", reply_markup=main_menu_kb())
            return
        db.set_video_publish_at(vid, dt.isoformat())
        db.log_activity("admin_bot", "reschedule_video", f"{vid} at {dt.isoformat()}")
        await update.message.reply_text(
            f"✅ *Rescheduled!* `{vid}`\n\n⏰ {dt.strftime('%d %b %Y, %H:%M %Z')}\n\n🔒 Audience visibility will remain OFF until this time.",
            parse_mode="Markdown",
            reply_markup=_video_manage_kb(),
        )
        return

    if context.user_data.get("awaiting_bot_template"):
        key = context.user_data.pop("awaiting_bot_template")
        text = update.message.text
        admin_store.set_template(key, text)
        bot_key = next((k for k,v in BOT_TEMPLATE_KEYS.items() if v == key), "")
        await update.message.reply_text("✅ Welcome message saved.\n\nThis text is now used by that bot's plain `/start` welcome.", reply_markup=_bot_template_kb(bot_key))
        return

    if context.user_data.get("awaiting_video_access_code"):
        video_id = context.user_data.pop("awaiting_video_access_code")
        code = update.message.text.strip().upper()
        c = db.get_redeem_code(code)
        if not c or not c.get("active"):
            context.user_data["awaiting_video_access_code"] = video_id
            await update.message.reply_text("❌ Invalid/inactive code. Send another active code.")
            return
        db.edit_video(video_id, access_tier="redeem", access_redeem_code=code, access_user_ids=None)
        db.log_activity("admin_bot", "set_video_access", f"{video_id} -> redeem:{code}")
        await update.message.reply_text(f"🎟️ `{video_id}` now uses redeem code `{code}`.", parse_mode="Markdown", reply_markup=main_menu_kb())
        return

    if context.user_data.get("awaiting_video_access_users"):
        video_id = context.user_data.pop("awaiting_video_access_users")
        raw = update.message.text.strip()
        ids=[]
        for part in raw.replace(" ", ",").split(","):
            if part.strip():
                try: ids.append(str(int(part.strip())))
                except ValueError:
                    context.user_data["awaiting_video_access_users"] = video_id
                    await update.message.reply_text("❌ User IDs must be numeric and comma-separated.")
                    return
        if not ids:
            context.user_data["awaiting_video_access_users"] = video_id
            await update.message.reply_text("Send at least one user ID.")
            return
        csv_ids=",".join(dict.fromkeys(ids))
        db.edit_video(video_id, access_tier="users", access_redeem_code=None, access_user_ids=csv_ids)
        db.log_activity("admin_bot", "set_video_access", f"{video_id} -> users:{csv_ids}")
        await update.message.reply_text(f"👤 Specific-user access set for `{video_id}` ({len(ids)} user(s)).", parse_mode="Markdown", reply_markup=main_menu_kb())
        return

    if context.user_data.get("awaiting_redeem_create"):
        kind = context.user_data.pop("awaiting_redeem_create")
        parts = update.message.text.strip().split()
        try:
            if not parts or len(parts) > 2:
                raise ValueError
            days = int(parts[0])
            if days <= 0:
                raise ValueError
            if kind == "premium":
                max_uses = int(parts[1]) if len(parts) == 2 else 1
                if max_uses <= 0:
                    raise ValueError
                code = db.create_redeem_code("premium", days, max_redemptions=max_uses)
                kind_label = "🎟️ Premium"
                uses_label = str(max_uses)
            else:
                max_uses = int(parts[1]) if len(parts) == 2 else None
                if max_uses is not None and max_uses <= 0:
                    raise ValueError
                code = db.create_redeem_code("giveaway", days, max_redemptions=max_uses)
                kind_label = "🎁 Giveaway"
                uses_label = str(max_uses) if max_uses is not None else "Unlimited"
        except (ValueError, TypeError):
            context.user_data["awaiting_redeem_create"] = kind
            await update.message.reply_text(
                "❌ Invalid format. Send `DAYS [USES]` — e.g. `30 10`.",
                parse_mode="Markdown",
            )
            return
        db.log_activity("admin_bot", "create_redeem_code", f"{code} ({days}d x{uses_label})")
        await update.message.reply_text(
            f"{kind_label} *Code Created*\n\n"
            f"🔑 Code: `{code}`\n"
            f"⏳ Duration: *{days} day(s)*\n"
            f"👥 Redemptions: *{uses_label}*\n\n"
            "Users can tap *Redeem Code* in Delivery Bot and enter this code.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎟️ Open Delivery Bot", url=f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem")],
                [InlineKeyboardButton("📢 Post Code Alert", callback_data=f"redeem_alert_{code}")],
                [InlineKeyboardButton("🔐 Access Control", callback_data="menu_access")]
            ])
        )
        return

    if context.user_data.get("awaiting_default_limit"):
        raw = update.message.text.strip()
        ok, limit = _parse_limit_arg(raw)
        if not ok:
            await update.message.reply_text("❌ Send a non-negative number or `unlimited`.")
            return
        context.user_data.pop("awaiting_default_limit", None)
        db.set_default_daily_limit(limit)
        db.log_activity("admin_bot", "set_default_limit", _limit_label(limit))
        await update.message.reply_text(
            f"✅ Default daily limit set to *{_limit_label(limit)}*.",
            parse_mode="Markdown", reply_markup=access_menu_kb()
        )
        return

    if context.user_data.get("awaiting_alert_text"):
        context.user_data["awaiting_alert_text"] = False
        context.user_data["custom_alert_caption"] = update.message.text
        await update.message.reply_text("✅ Custom message set — updated preview:")
        await _build_postalert_preview(update.effective_chat.id, context, context.user_data)
        return

    editing_batch = context.user_data.get("editing_batch")
    if editing_batch:
        batch_id, field = editing_batch["batch_id"], editing_batch["field"]
        value = update.message.text.strip()
        if field == "title" and not value:
            await update.message.reply_text("Collection title can't be empty — send some text, or /cancel.")
            return
        try:
            db.update_batch_fields(batch_id, **{field: value})
            context.user_data.pop("editing_batch", None)
            await update.message.reply_text(f"✅ Collection *{field}* updated for all items.", parse_mode="Markdown", reply_markup=main_menu_kb())
        except Exception as exc:
            await update.message.reply_text(f"❌ Collection update failed: {db.md_escape(str(exc))}", parse_mode="Markdown")
        return

    editing = context.user_data.get("editing")
    if not editing:
        return
    video_id, field = editing["video_id"], editing["field"]
    value = update.message.text.strip()
    if field == "title" and not value:
        await update.message.reply_text("Title can't be empty — send some text, or /cancel.")
        return
    db.edit_video(video_id, **{field: value})
    context.user_data.pop("editing", None)
    await update.message.reply_text(f"✅ Updated *{field}* for `{video_id}`.", parse_mode="Markdown",
                                     reply_markup=main_menu_kb())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    specific_photo = context.user_data.get("awaiting_tempbot_specific_image")
    if specific_photo:
        bot_id = str(specific_photo.get("bot_id"))
        row = temp_bot_store.get_bot(bot_id)
        if not row:
            context.user_data.pop("awaiting_tempbot_specific_image", None)
            await update.message.reply_text("❌ Temporary bot no longer exists.", reply_markup=_tempbots_kb())
            return
        try:
            base_dir = Path(os.path.dirname(botutil.MANDATORY_JOIN_IMAGE)) / "temp_bots"
            base_dir.mkdir(parents=True, exist_ok=True)
            image_path = str(base_dir / f"{bot_id}.jpg")
            tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
            await tg_file.download_to_drive(image_path)
            temp_bot_store.set_bot_message(bot_id, image_path=image_path)
            context.user_data.pop("awaiting_tempbot_specific_image", None)
            db.log_activity("admin_bot", "tempbot_specific_image_updated", f"{bot_id}:{image_path}")
            await update.message.reply_text("✅ Bot-specific start image saved.", reply_markup=_tempbot_settings_kb(bot_id))
        except Exception as exc:
            log.exception("Failed to save specific temporary bot image")
            await update.message.reply_text(f"❌ Could not save image: {db.md_escape(str(exc))}", parse_mode="Markdown")
        return

    if context.user_data.get("awaiting_tempbot_image"):
        context.user_data.pop("awaiting_tempbot_image", None)
        try:
            image_path = os.path.join(os.path.dirname(botutil.MANDATORY_JOIN_IMAGE), "temp_bot_start.jpg")
            os.makedirs(os.path.dirname(image_path), exist_ok=True)
            tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
            await tg_file.download_to_drive(image_path)
            temp_bot_store.set_start_image(image_path)
            db.log_activity("admin_bot", "tempbots_image_updated", image_path)
            await update.message.reply_text("✅ Temporary bot start image saved. All temporary bots will use it on `/start`.", parse_mode="Markdown", reply_markup=_tempbots_kb())
        except Exception as e:
            log.exception("Failed to save temporary bot start image")
            await update.message.reply_text(f"❌ Could not save temporary bot image: {db.md_escape(str(e))}", parse_mode="Markdown")
        return
    if context.user_data.get("awaiting_mandatory_join_image"):
        context.user_data.pop("awaiting_mandatory_join_image", None)
        try:
            os.makedirs(os.path.dirname(botutil.MANDATORY_JOIN_IMAGE), exist_ok=True)
            tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
            await tg_file.download_to_drive(botutil.MANDATORY_JOIN_IMAGE)
            db.log_activity("admin_bot", "mandatory_join_image_updated", os.path.basename(botutil.MANDATORY_JOIN_IMAGE))
            await update.message.reply_text("✅ Mandatory join image saved. It will now be shown to users who haven't joined.", reply_markup=_mandatory_join_admin_kb())
        except Exception as e:
            log.exception("Failed to save mandatory join image")
            await update.message.reply_text(f"❌ Could not save join image: {db.md_escape(str(e))}", parse_mode="Markdown")
        return

    if context.user_data.get("awaiting_batch_cover"):
        batch_id = context.user_data.pop("awaiting_batch_cover")
        cover_file_id = update.message.photo[-1].file_id
        try:
            cover_msg = await context.bot.send_photo(chat_id=storage_config.primary(), photo=cover_file_id)
            db.update_batch_fields(batch_id, cover_file_id=cover_file_id, cover_msg_id=cover_msg.message_id)
            await update.message.reply_text(f"✅ Collection cover updated for *all items* in `{batch_id}`.", parse_mode="Markdown")
            await _send_collection_edit_menu(update.effective_chat.id, context, batch_id)
        except Exception as e:
            log.exception("Collection cover update failed")
            await update.message.reply_text(f"❌ Could not update collection cover: {db.md_escape(str(e))}", parse_mode="Markdown")
        return
    if context.user_data.get("awaiting_video_cover"):
        video_id = context.user_data.pop("awaiting_video_cover")
        cover_file_id = update.message.photo[-1].file_id
        try:
            cover_msg = await context.bot.send_photo(chat_id=storage_config.primary(), photo=cover_file_id)
            db.edit_video(video_id, cover_file_id=cover_file_id, cover_msg_id=cover_msg.message_id)
            v = db.get_video(video_id)
            # Keep a bulk collection's shared cover synchronized when this item belongs to one.
            if v and v.get("batch_id"):
                db.update_batch_cover(v["batch_id"], cover_file_id, cover_msg.message_id)
            await update.message.reply_text(
                f"✅ Cover updated for `{video_id}`." + (f"\n📦 Shared collection cover updated for `{v['batch_id']}`." if v and v.get('batch_id') else ""),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.exception("Video cover update failed")
            await update.message.reply_text(f"❌ Could not update cover: {db.md_escape(str(e))}", parse_mode="Markdown")
        return
    if context.user_data.get("awaiting_default_alert_cover"):
        context.user_data["awaiting_default_alert_cover"] = False
        try:
            cover_msg = await context.bot.send_photo(
                chat_id=storage_config.primary(), photo=update.message.photo[-1].file_id
            )
        except Exception as e:
            log.exception("Failed to store default alert cover")
            await update.message.reply_text(f"❌ Failed to save default alert cover: {e}")
            return
        db.set_setting("default_alert_cover_msg_id", str(cover_msg.message_id))
        db.log_activity("admin_bot", "set_default_alert_cover", "")
        await update.message.reply_text(
            "✅ Default alert cover saved — every `/postalert` will use it from now on, "
            "unless you pick a one-off cover with 🖼 Change Cover for that announcement.",
            parse_mode="Markdown",
        )
        return
    if not context.user_data.get("awaiting_alert_cover"):
        return
    context.user_data["awaiting_alert_cover"] = False
    cover_file_id = update.message.photo[-1].file_id
    try:
        cover_msg = await context.bot.send_photo(chat_id=storage_config.primary(), photo=cover_file_id)
    except Exception as e:
        log.exception("Failed to store custom alert cover")
        await update.message.reply_text(f"❌ Failed to save cover: {e}")
        return
    context.user_data["custom_alert_cover_msg_id"] = cover_msg.message_id
    await update.message.reply_text("✅ Custom cover set — updated preview:")
    await _build_postalert_preview(update.effective_chat.id, context, context.user_data)


async def setdefaultalertcover_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data["awaiting_default_alert_cover"] = True
    await update.message.reply_text("📸 Send the photo to use as the default alert cover.")


async def cleardefaultalertcover_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    db.clear_setting("default_alert_cover_msg_id")
    db.log_activity("admin_bot", "clear_default_alert_cover", "")
    await update.message.reply_text(
        "🗑 Default alert cover cleared.\n\n"
        "Normal queue alerts will now remain coverless until you set a new default; "
        "they will not use the latest video's cover automatically."
    )


# ---------- admin management + bot editor ----------
def _admin_manage_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Admin", callback_data="admin_add"),
         InlineKeyboardButton("➖ Remove Admin", callback_data="admin_remove")],
        [InlineKeyboardButton("📋 List Admins", callback_data="admin_list")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_home")],
    ])


def _bot_editor_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠 Admin Bot", callback_data="editbot_admin"),
         InlineKeyboardButton("🗄 Storage Bot", callback_data="editbot_storage")],
        [InlineKeyboardButton("📚 Catalog Bot", callback_data="editbot_catalog"),
         InlineKeyboardButton("🎬 Delivery Bot", callback_data="editbot_delivery")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_home")],
    ])


def _bot_template_kb(bot_key):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Welcome", callback_data=f"edittext_{bot_key}")],
        [InlineKeyboardButton("👁 Preview", callback_data=f"previewtext_{bot_key}"),
         InlineKeyboardButton("♻️ Reset", callback_data=f"resettext_{bot_key}")],
        [InlineKeyboardButton("🔙 Bot Editor", callback_data="bot_editor")],
    ])

BOT_TEMPLATE_KEYS = {
    "admin": "admin_start",
    "storage": "storage_start",
    "catalog": "catalog_start",
    "delivery": "delivery_start",
}

async def _admin_management_message(chat_id, context):
    admins = admin_store.list_admins(config.ADMIN_USER_IDS)
    lines = ["👥 *Admin Management*", ""]
    if not admins:
        lines.append("No admins configured.")
    else:
        for uid, role in admins.items():
            lines.append(f"• `{uid}` — {role}")
    lines.append("\nOwner(s) from config cannot be removed.")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown", reply_markup=_admin_manage_kb())


# ---------- temporary bot pool ----------
def _default_temp_channel_link():
    configured = str(getattr(config, "PRIMARY_CHANNEL_LINK", "") or "").strip()
    if configured.startswith(("https://t.me/", "http://t.me/")):
        return configured
    try:
        _id, link = _mandatory_join_admin_config()
        if link.startswith(("https://t.me/", "http://t.me/")):
            return link
    except Exception:
        pass
    return "https://t.me/shivanifanbase"


def _tempbots_text():
    bots = temp_bot_store.list_bots()
    enabled = sum(1 for b in bots if b.get("enabled"))
    healthy = sum(1 for b in bots if b.get("enabled") and str(b.get("status") or "").upper() == "HEALTHY")
    degraded = sum(1 for b in bots if b.get("enabled") and str(b.get("status") or "").upper() in {"DEGRADED", "STARTING"})
    quarantined = sum(1 for b in bots if b.get("quarantined"))
    channel = temp_bot_store.get_channel(_default_temp_channel_link())
    msg = temp_bot_store.get_message()
    lines = [
        "🤖 *Temporary Bot Pool*",
        "",
        f"🟢 Enabled: *{enabled}/{len(bots)}* · ✅ Healthy: *{healthy}* · 🟠 Starting/Degraded: *{degraded}* · 🔴 Quarantined: *{quarantined}*",
        f"📢 Main Channel: *{db.md_escape(channel['name'])}*",
        f"🔗 {db.md_escape(channel['link'])}",
        f"🔘 Button: *{db.md_escape(msg['button_text'])}*",
        f"💬 Message: *{'Set' if msg.get('text') else 'Not set'}* · 🖼 Image: *{'Set' if msg.get('image_path') else 'None'}*",
        "",
        "Every temporary bot has one job: `/start` → the configured Main Channel button.",
        "The pool supervisor keeps enabled bots isolated, monitored, and auto-restarted with backoff.",
    ]
    if bots:
        lines.append("\n*Bot Pool*")
        for b in bots:
            state = str(b.get("status") or ("ENABLED" if b.get("enabled") else "DISABLED")).upper()
            icon = {"HEALTHY":"🟢", "STARTING":"🟡", "DEGRADED":"🟠", "QUARANTINED":"🔴", "DISABLED":"⚪"}.get(state, "⚪")
            user = "@" + str(b.get("username") or "unknown").lstrip("@")
            err = f" · ⚠️ {str(b.get('last_error'))[:55]}" if b.get('last_error') else ""
            overrides = temp_bot_store.bot_override_summary(str(b.get('id')), default_link=_default_temp_channel_link())
            ov = f" · ✨ {','.join(overrides)}" if overrides else ""
            lines.append(f"• {icon} {db.md_escape(user)} · {state}{db.md_escape(ov)}{db.md_escape(err)}")
    else:
        lines.extend(["\n*Bot Pool*", "• No temporary bots added yet."])
    return "\n".join(lines)


def _tempbots_kb():
    rows = [
        [InlineKeyboardButton("➕ Add Bot(s)", callback_data="tempbots_add")],
        [InlineKeyboardButton("📋 Manage Bots", callback_data="tempbots_manage"),
         InlineKeyboardButton("🩺 Health", callback_data="tempbots_health")],
        [InlineKeyboardButton("🧪 Test Pool", callback_data="tempbots_test"),
         InlineKeyboardButton("♻️ Recover All", callback_data="tempbots_recover_all")],
        [InlineKeyboardButton("📢 Main Channel", callback_data="tempbots_channel"),
         InlineKeyboardButton("🔘 Start Button", callback_data="tempbots_button")],
        [InlineKeyboardButton("💬 Start Message", callback_data="tempbots_message"),
         InlineKeyboardButton("🖼 Start Image", callback_data="tempbots_image")],
        [InlineKeyboardButton("👁 Preview", callback_data="tempbots_preview"),
         InlineKeyboardButton("🔄 Refresh", callback_data="tempbots")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
    ]
    return InlineKeyboardMarkup(rows)


def _tempbot_manage_kb():
    bots = temp_bot_store.list_bots()
    rows = []
    for b in bots[:30]:
        bid = str(b.get("id"))
        label = "🟢 Disable" if b.get("enabled") else "⚪ Enable"
        user = "@" + str(b.get("username") or bid).lstrip("@")
        rows.append([InlineKeyboardButton(f"{label} {user}", callback_data=f"tempbot_toggle_{bid}"),
                     InlineKeyboardButton("⚙️ Settings", callback_data=f"tempbot_settings_{bid}")])
        rows.append([InlineKeyboardButton("♻️ Recover", callback_data=f"tempbot_recover_{bid}"),
                     InlineKeyboardButton("🔑 Token", callback_data=f"tempbot_token_{bid}"),
                     InlineKeyboardButton("🗑", callback_data=f"tempbot_remove_{bid}")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="tempbots_manage"),
                 InlineKeyboardButton("🔙 Pool", callback_data="tempbots")])
    return InlineKeyboardMarkup(rows)


async def _tempbots_manage_message(chat_id, context):
    bots = temp_bot_store.list_bots()
    if not bots:
        text = "📋 *Temporary Bot Manager*\n\n📭 No temporary bots yet."
    else:
        lines = ["📋 *Temporary Bot Manager*", ""]
        for b in bots[:30]:
            raw_state = str(b.get("status") or ("ENABLED" if b.get("enabled") else "DISABLED")).upper()
            icon = {"HEALTHY":"🟢", "STARTING":"🟡", "DEGRADED":"🟠", "QUARANTINED":"🔴", "DISABLED":"⚪"}.get(raw_state, "⚪")
            user = "@" + str(b.get("username") or b.get("id") or "unknown").lstrip("@")
            lines.append(f"{icon} *{db.md_escape(user)}* · `{raw_state}` · starts {int(b.get('start_count') or 0)} · ok {int(b.get('success_count') or 0)} · fail {int(b.get('failure_count') or 0)}")
            if b.get("last_seen_at"):
                lines.append(f"  Seen: {str(b['last_seen_at'])[:19].replace('T',' ')}")
            if b.get("last_error"):
                lines.append(f"  ⚠️ {db.md_escape(str(b['last_error'])[:120])}")
        if len(bots) > 30:
            lines.append(f"\n…and {len(bots)-30} more")
        text = "\n".join(lines)
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=_tempbot_manage_kb())


async def _tempbots_health_message(chat_id, context):
    rows = temp_bot_store.health_snapshot(100)
    lines=["🩺 *Temporary Bot Health*", ""]
    if not rows:
        lines.append("📭 No temporary bots registered.")
    else:
        for b in rows:
            state=str(b.get("status") or ("ENABLED" if b.get("enabled") else "DISABLED")).upper()
            icon={"HEALTHY":"🟢","STARTING":"🟡","DEGRADED":"🟠","QUARANTINED":"🔴","DISABLED":"⚪"}.get(state,"⚪")
            user='@'+str(b.get('username') or b.get('id') or 'unknown').lstrip('@')
            age=b.get('age_seconds')
            age_txt=f"{age}s ago" if age is not None else "never"
            lines.append(f"{icon} *{db.md_escape(user)}* · `{state}` · seen {age_txt}")
            lines.append(f"   Starts {int(b.get('start_count') or 0)} · ✅ {int(b.get('success_count') or 0)} · ❌ {int(b.get('failure_count') or 0)} · 🔥 {int(b.get('consecutive_failures') or 0)}")
            if b.get('last_error'): lines.append(f"   ⚠️ {db.md_escape(str(b['last_error'])[:110])}")
    await context.bot.send_message(chat_id=chat_id,text='\n'.join(lines),parse_mode='Markdown',reply_markup=_tempbots_kb())


async def _tempbots_test_message(chat_id, context):
    rows = temp_bot_store.list_bots()
    candidates = [r for r in rows if r.get("enabled") and not r.get("quarantined")][:15]
    if not candidates:
        await context.bot.send_message(chat_id=chat_id,text="🧪 *Temporary Pool Test*\n\n❌ No enabled bot is available.",parse_mode='Markdown',reply_markup=_tempbots_kb()); return
    lines=["🧪 *Temporary Pool Live Test*", ""]
    for b in candidates:
        label='@'+str(b.get('username') or b.get('id')).lstrip('@')
        probe=None
        try:
            probe=Bot(token=str(b.get('token') or ''))
            me=await probe.get_me()
            temp_bot_store.touch_status(str(b.get('id')), username=me.username or b.get('username') or '', first_name=me.first_name or '', status='HEALTHY', last_seen_at=__import__('datetime').datetime.now().astimezone().isoformat(), last_error=None, last_error_at=None, consecutive_failures=0)
            lines.append(f"✅ *{db.md_escape('@'+str(me.username or me.id).lstrip('@'))}* · Telegram OK")
        except Exception as exc:
            hard = exc.__class__.__name__ in {"InvalidToken"}
            temp_bot_store.mark_failure(str(b.get('id')), f"live test: {str(exc)[:300]}", hard=hard)
            lines.append(f"❌ *{db.md_escape(label)}* · {db.md_escape(str(exc)[:120])}")
        finally:
            if probe:
                try: await probe.close()
                except Exception: pass
    lines.append("\n🔄 Enabled bots continue under the supervisor with automatic backoff/recovery.")
    await context.bot.send_message(chat_id=chat_id,text='\n'.join(lines),parse_mode='Markdown',reply_markup=_tempbots_kb())


async def _tempbots_channel_message(chat_id, context):
    current = temp_bot_store.get_channel(_default_temp_channel_link())
    await context.bot.send_message(
        chat_id=chat_id,
        text=("📢 *Temporary Bot Main Channel*\n\n"
              f"Current name: *{db.md_escape(current['name'])}*\n"
              f"Current link: `{db.md_escape(current['link'])}`\n\n"
              "Send: `Channel Name | https://t.me/username`\n"
              "Example: `SHIVANI'S FANBASE | https://t.me/shivanifanbase`\n\n"
              "This one setting controls every temporary bot."),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="tempbots_cancel")]])
    )


async def _tempbots_message_message(chat_id, context):
    current = temp_bot_store.get_message()
    await context.bot.send_message(
        chat_id=chat_id,
        text=("💬 *Temporary Bot Start Message*\n\n"
              f"Current:\n{db.md_escape(current['text'])}\n\n"
              "Send the new message text. Multiple lines are allowed.\n"
              "Send `cancel` to keep the current message."),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="tempbots_cancel")]])
    )


async def _tempbots_image_message(chat_id, context):
    current = temp_bot_store.get_message()
    rows = []
    if current.get("image_path"):
        rows.append([InlineKeyboardButton("🗑 Clear Image", callback_data="tempbots_image_clear")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="tempbots_cancel")])
    await context.bot.send_message(
        chat_id=chat_id,
        text=("🖼 *Temporary Bot Start Image*\n\n"
              f"Current: {'✅ Image configured' if current.get('image_path') else '⚪ No image'}\n\n"
              "Send a new photo now. It will be used by every temporary bot on `/start`."),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def _tempbots_button_message(chat_id, context):
    current = temp_bot_store.get_message()
    await context.bot.send_message(
        chat_id=chat_id,
        text=("🔘 *Temporary Bot Button*\n\n"
              f"Current: *{db.md_escape(current['button_text'])}*\n\n"
              "Send the new button label.\n"
              "Example: `📢 Join Main Channel`"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="tempbots_cancel")]])
    )



def _tempbot_settings_text(bot_id: str) -> str:
    row = temp_bot_store.get_bot(bot_id)
    if not row:
        return "❌ Temporary bot not found."
    cfg = temp_bot_store.get_bot_config(bot_id, default_link=_default_temp_channel_link())
    ov = cfg["overrides"]
    user = "@" + str(row.get("username") or bot_id).lstrip("@")
    def mark(value): return "🟢 Custom" if value else "⚪ Pool default"
    image = cfg["message"].get("image_path")
    image_state = "🟢 Custom" if ov["image"] and image else "⚪ Pool default" if not ov["image"] else "⚪ None"
    return (
        f"⚙️ *Temporary Bot Settings*\n\n"
        f"🤖 *{db.md_escape(user)}*\n\n"
        f"📢 Channel: {mark(ov['channel'])}\n"
        f"   {db.md_escape(cfg['channel']['name'])}\n"
        f"   `{db.md_escape(cfg['channel']['link'])}`\n\n"
        f"🔘 Join Button: {mark(ov['button'])}\n"
        f"   *{db.md_escape(cfg['message']['button_text'])}*\n\n"
        f"💬 Start Message: {mark(ov['message'])}\n"
        f"   {'✅ Custom text' if ov['message'] else 'Uses pool message'}\n\n"
        f"🖼 Start Image: {image_state}\n\n"
        "Bot-specific settings override the Temporary Pool defaults only for this bot."
    )


def _tempbot_settings_kb(bot_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Channel", callback_data=f"tempbot_channel_{bot_id}"),
         InlineKeyboardButton("🔘 Button", callback_data=f"tempbot_button_{bot_id}")],
        [InlineKeyboardButton("💬 Message", callback_data=f"tempbot_message_{bot_id}"),
         InlineKeyboardButton("🖼 Image", callback_data=f"tempbot_image_{bot_id}")],
        [InlineKeyboardButton("👁 Preview", callback_data=f"tempbot_preview_{bot_id}"),
         InlineKeyboardButton("♻️ Use Pool Defaults", callback_data=f"tempbot_reset_{bot_id}")],
        [InlineKeyboardButton("🔙 Manage Bots", callback_data="tempbots_manage")],
    ])


def _tempbot_specific_prompt(field: str, bot_id: str, current=None):
    prompts = {
        "channel": ("📢 *Bot-Specific Join Channel*\n\n"
                     "Send `Channel Name | https://t.me/username`.\n"
                     "This only changes the Join button destination for this bot.\n\n"
                     "Send `default` to use the pool channel, or `cancel`."),
        "button": ("🔘 *Bot-Specific Join Button*\n\n"
                    "Send the button label (max 64 chars).\n"
                    "Send `default` to use the pool button, or `cancel`."),
        "message": ("💬 *Bot-Specific Start Message*\n\n"
                    "Send the exact message users should see on `/start`.\n"
                    "Multiple lines are allowed.\n"
                    "Send `default` to use the pool message, or `cancel`."),
    }
    return prompts[field]


async def _tempbot_settings_message(chat_id, context, bot_id):
    await context.bot.send_message(chat_id=chat_id, text=_tempbot_settings_text(bot_id), parse_mode="Markdown", reply_markup=_tempbot_settings_kb(bot_id))


async def _tempbot_preview_specific(chat_id, context, bot_id):
    row = temp_bot_store.get_bot(bot_id)
    if not row:
        await context.bot.send_message(chat_id=chat_id, text="❌ Temporary bot not found.")
        return
    cfg = temp_bot_store.get_bot_config(bot_id, default_link=_default_temp_channel_link())
    link = cfg["channel"]["link"].strip()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(cfg["message"]["button_text"], url=link)]]) if link else None
    text = cfg["message"]["text"]
    image_path = cfg["message"].get("image_path") or ""
    if image_path and os.path.isfile(image_path):
        try:
            with open(image_path, "rb") as photo:
                await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=kb)
        except Exception as exc:
            log.warning("Specific temp bot preview image failed: %s", exc)
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    await _tempbot_settings_message(chat_id, context, bot_id)

async def tempbots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await context.bot.send_message(chat_id=update.effective_chat.id, text=_tempbots_text(), parse_mode="Markdown", reply_markup=_tempbots_kb())


async def _tempbots_preview_message(chat_id, context):
    channel = temp_bot_store.get_channel(_default_temp_channel_link())
    msg = temp_bot_store.get_message()
    link = channel.get("link", "").strip()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(msg["button_text"], url=link)]]) if link else None
    text = msg.get("text") or "✨ Welcome!\n\nTap below to join our Main Channel."
    if msg.get("image_path") and os.path.isfile(msg["image_path"]):
        try:
            with open(msg["image_path"], "rb") as photo:
                await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=kb)
        except Exception as exc:
            # Keep the configured image path intact. A temporary Telegram/API
            # failure must not destroy the admin's saved start image.
            log.warning("Temporary bot preview image send failed: %s", exc)
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    await context.bot.send_message(chat_id=chat_id, text=_tempbots_text(), parse_mode="Markdown", reply_markup=_tempbots_kb())


async def permbots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await context.bot.send_message(chat_id=update.effective_chat.id, text=_permbots_text(), parse_mode="Markdown", reply_markup=_permbots_kb())


# ---------- permanent Delivery Bot manager ----------
def _permbots_text():
    bots = permanent_bot_store.list_bots()
    active = sum(1 for b in bots if b.get("enabled") and not b.get("retired"))
    try:
        stats = db.get_delivery_link_stats()
    except Exception:
        stats = {}
    lines = [
        "🚀 *Permanent Delivery Bots*", "",
        f"🟢 Active delivery bots: *{active}/{len(bots)}*",
        f"🔗 Direct links: *{int(stats.get('active',0))} active · {int(stats.get('pending_update',0))} pending · {int(stats.get('orphaned',0))} orphaned*", "",
        "🎲 New Watch links select a random healthy/routable Delivery Bot.",
        "♻️ When a bot dies, its registered catalogue links are regenerated to another healthy bot and the saved Telegram buttons are updated automatically.",
    ]
    if bots:
        lines.append("\n*Pool*")
        for b in bots:
            state="🟢 ON" if b.get("enabled") and not b.get("retired") else ("🛡️ RETIRED" if b.get("retired") else "🟡 DISABLED")
            primary=" · ⭐ Primary" if b.get("primary") else ""
            user="@"+str(b.get("username") or b.get("id") or "unknown").lstrip("@")
            err=f" · ⚠️ {str(b.get('last_error'))[:55]}" if b.get("last_error") else ""
            succ=int(b.get('success_count') or 0); fail=int(b.get('failure_count') or 0)
            lines.append(f"• {state} {db.md_escape(user)}{primary} · ✅{succ} / ❌{fail}{err}")
    else:
        lines.append("\nNo permanent Delivery Bot is registered yet. The existing config Delivery Bot is added automatically when the supervisor starts.")
    return "\n".join(lines)


def _permbots_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Delivery Bot(s)", callback_data="permbots_add")],
        [InlineKeyboardButton("⭐ Register Existing Primary", callback_data="permbots_primary")],
        [InlineKeyboardButton("📋 Manage Bots", callback_data="permbots_manage")],
        [InlineKeyboardButton("🩺 Health Monitor", callback_data="permbots_health"), InlineKeyboardButton("🔑 Token Update", callback_data="permbots_token")],
        [InlineKeyboardButton("🌐 Resolver Status", callback_data="permbots_resolver_status"), InlineKeyboardButton("🧪 Test Resolver", callback_data="permbots_resolver_test")],
        [InlineKeyboardButton("🎲 Random Resolver", callback_data="permbots_resolver")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="permbots"), InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
    ])


def _permbots_manage_kb():
    rows=[]
    for b in permanent_bot_store.list_bots()[:30]:
        bid=str(b.get("id")); user="@"+str(b.get("username") or bid).lstrip("@")
        toggle="🟢 Disable" if b.get("enabled") and not b.get("retired") else "🟡 Enable"
        if b.get("primary"):
            rows.append([InlineKeyboardButton(f"{toggle} {user}", callback_data=f"permbot_toggle_{bid}"), InlineKeyboardButton("⭐ Primary", callback_data="permbots")])
        else:
            rows.append([InlineKeyboardButton(f"{toggle} {user}", callback_data=f"permbot_toggle_{bid}"), InlineKeyboardButton("🛡️ Retire", callback_data=f"permbot_remove_{bid}")])
            rows.append([InlineKeyboardButton("🛟 Recover / Clear Error", callback_data=f"permbot_recover_{bid}")])
            rows.append([InlineKeyboardButton(f"🗑️ Delete {user} Permanently", callback_data=f"permbot_delete_confirm_{bid}")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="permbots_manage"), InlineKeyboardButton("🔙 Pool", callback_data="permbots")])
    return InlineKeyboardMarkup(rows)


async def _permbots_manage_message(chat_id, context):
    bots=permanent_bot_store.list_bots()
    lines=["📋 *Permanent Delivery Bot Manager*",""]
    for b in bots[:30]:
        user="@"+str(b.get("username") or b.get("id") or "unknown").lstrip("@")
        state="🟢 Active" if b.get("enabled") and not b.get("retired") else ("🛡️ Retired · Out of resolver pool" if b.get("retired") else "🟡 Disabled · Out of resolver pool")
        primary=" · ⭐ Primary" if b.get("primary") else ""
        lines.append(f"{state} · *{db.md_escape(user)}*{primary}")
        if b.get("last_started_at"): lines.append(f"  Started: {str(b['last_started_at'])[:19].replace('T',' ')}")
        if b.get("last_error"): lines.append(f"  ⚠️ {db.md_escape(str(b['last_error'])[:100])}")
    if not bots: lines.append("📭 No bots registered.")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown", reply_markup=_permbots_manage_kb())


def _permbots_resolver_text():
    base=os.getenv("VIDEO_VAULT_WATCH_RESOLVER_URL", "").rstrip("/") or getattr(config,"VIDEO_VAULT_WATCH_RESOLVER_URL","").rstrip("/") or os.getenv("VIDEO_VAULT_API_PUBLIC_URL","").rstrip("/") or getattr(config,"VIDEO_VAULT_API_PUBLIC_URL","").rstrip("/") or "https://us1.visihost.in:5059"
    try:
        stats = db.get_delivery_link_stats()
    except Exception:
        stats = {}
    return ("🔗 *Direct t.me Delivery Links*\n\n"
            "New catalogue links point directly to a healthy Permanent Delivery Bot. "
            "When a bot dies, the database migrates affected links and the Catalogue Bot updates saved message buttons.\n\n"
            f"📦 Registered links: *{int(stats.get('total', 0))}*\n"
            f"🟢 Active: *{int(stats.get('active', 0))}* · ⏳ Pending: *{int(stats.get('pending_update', 0))}*\n"
            f"🛟 Orphaned: *{int(stats.get('orphaned', 0))}* · ⚪ Stale: *{int(stats.get('stale', 0))}*")


async def _permbots_health_text() -> str:
    rows = permanent_bot_store.bot_health_snapshot(90)
    if not rows:
        return "🩺 *Delivery Bot Health*\n\nNo permanent bots registered."

    try:
        link_stats = db.get_delivery_link_stats() or {}
    except Exception:
        link_stats = {}

    active = [r for r in rows if r.get("active")]
    healthy = [r for r in rows if r.get("healthy")]
    routable = [r for r in rows if r.get("routable")]
    stale = [r for r in rows if r.get("stale_heartbeat")]
    cooldown = [r for r in rows if r.get("cooldown_active")]
    unhealthy = [r for r in active if not r.get("healthy")]
    restarts = sum(int(r.get("watchdog_restart_count") or 0) for r in rows)
    delivery_ok = sum(int(r.get("delivery_success_count") or 0) for r in rows)
    delivery_fail = sum(int(r.get("delivery_failure_count") or 0) for r in rows)
    delivery_total = delivery_ok + delivery_fail
    success_rate = (delivery_ok * 100.0 / delivery_total) if delivery_total else None

    lines = [
        "🩺 *Delivery Bot Operations*",
        "",
        f"Pool: *{len(healthy)}/{len(active)} healthy* · 🎯 *{len(routable)} routable*",
        f"⚠️ Unhealthy: *{len(unhealthy)}* · ⏱ Stale heartbeat: *{len(stale)}* · 🧊 Cooldown: *{len(cooldown)}*",
        f"♻️ Watchdog restarts: *{restarts}*",
        (f"📈 Delivery success: *{success_rate:.1f}%* ({delivery_ok}/{delivery_total})"
         if success_rate is not None else "📈 Delivery success: *No delivery samples yet*"),
        "",
        f"🔗 Links: {int(link_stats.get('active',0))} active · {int(link_stats.get('pending_update',0))} pending · "
        f"{int(link_stats.get('orphaned',0))} orphaned",
        "",
    ]

    for r in rows:
        user='@'+str(r.get('username') or r.get('id') or 'unknown').lstrip('@')
        if r.get('retired'):
            state='🛡️ RETIRED'
        elif r.get('healthy'):
            state='🟢 HEALTHY'
        elif r.get('active'):
            state='🔴 UNHEALTHY'
        else:
            state='🟡 INACTIVE'
        age=r.get('age_seconds')
        age_txt=(f"{age}s ago" if age is not None else "unknown")
        flags=[]
        if r.get('stale_heartbeat'): flags.append("STALE")
        if r.get('cooldown_active'): flags.append("COOLDOWN")
        flag_txt=f" · ⚠️ {','.join(flags)}" if flags else ""
        err=f"\n   Error: {db.md_escape(str(r.get('last_error'))[:90])}" if r.get('last_error') else ''
        succ=int(r.get('success_count') or 0); fail=int(r.get('failure_count') or 0); streak=int(r.get('consecutive_failures') or 0)
        ds=int(r.get('delivery_success_count') or 0); df=int(r.get('delivery_failure_count') or 0)
        wd=int(r.get('watchdog_restart_count') or 0)
        lines.append(
            f"{state} · *{db.md_escape(user)}*{flag_txt}\n"
            f"   Heartbeat: {age_txt} · Worker ✅{succ}/❌{fail} · Delivery ✅{ds}/❌{df} · 🔥{streak} · ♻️{wd}{err}"
        )
    return "\n".join(lines)

async def _permbots_resolver_status_text() -> str:
    base=(os.getenv("VIDEO_VAULT_WATCH_RESOLVER_URL", "").rstrip("/")
          or getattr(config, "VIDEO_VAULT_WATCH_RESOLVER_URL", "").rstrip("/")
          or os.getenv("VIDEO_VAULT_API_PUBLIC_URL", "").rstrip("/")
          or getattr(config, "VIDEO_VAULT_API_PUBLIC_URL", "").rstrip("/")
          or "https://us1.visihost.in:5059")
    rows=permanent_bot_store.bot_health_snapshot(120)
    active=[r for r in rows if r["active"]]
    healthy=[r for r in rows if r["healthy"]]
    routable=permanent_bot_store.routable_delivery_bots(180)
    return (
        "🌐 *Resolver Status*\n\n"
        f"Endpoint: `{db.md_escape(base)}/watch`\n"
        f"Active: *{len(active)}*\n"
        f"Healthy: *{len(healthy)}*\n"
        f"Routable now: *{len(routable)}*\n"
        f"Out of pool: *{sum(1 for r in rows if not r['active'])}*\n\n"
        "🔗 Direct t.me links are migrated in the database when their owning bot becomes unavailable; future links use only routable bots."
    )

async def _permbots_resolver_test_text() -> str:
    rows=permanent_bot_store.routable_delivery_bots(180)
    if not rows:
        return "🧪 *Resolver Test*\n\n❌ No active Delivery Bot is available."
    import random
    pick=random.choice(rows)
    user='@'+str(pick.get('username') or pick.get('id') or 'unknown').lstrip('@')
    return ("🧪 *Resolver Test*\n\n" f"Pool reachable: ✅\nSelected target: *{db.md_escape(user)}*\n" f"Target token stored: {'✅' if pick.get('token') else '❌'}\n\nResult: *READY*")

async def _permbots_token_start(chat_id, context):
    rows=permanent_bot_store.list_bots()
    if not rows:
        await context.bot.send_message(chat_id=chat_id,text="🔑 No Delivery Bots registered.",reply_markup=_permbots_kb()); return
    lines=["🔑 *Update Delivery Bot Token*", "", "Tap a bot then send the NEW BotFather token."]
    buttons=[]
    for r in rows[:30]:
        bid=str(r.get('id')); user='@'+str(r.get('username') or bid).lstrip('@')
        buttons.append([InlineKeyboardButton(user, callback_data=f"permbot_token_{bid}")])
    buttons.append([InlineKeyboardButton("🔙 Pool", callback_data="permbots")])
    await context.bot.send_message(chat_id=chat_id,text="\n".join(lines),parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(buttons))

# ---------- video management ----------
def _video_manage_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗓️ Upcoming Scheduled", callback_data="video_manage_scheduled"),
         InlineKeyboardButton("📅 Date-wise Schedule", callback_data="video_schedule_dates")],
        [InlineKeyboardButton("🆕 Recent Videos", callback_data="video_manage_recent")],
        [InlineKeyboardButton("📚 All Videos", callback_data="menu_list_0")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
    ])

def _video_manage_text():
    videos = db.all_videos(limit=10000)
    scheduled = [v for v in videos if v.get("publish_at") and not db.is_visible(v)]
    visible = [v for v in videos if db.is_visible(v)]
    return (
        "🎬 *Video Management*\n\n"
        f"📚 Total: *{len(videos)}*\n"
        f"🟢 Live: *{len(visible)}*\n"
        f"⏰ Scheduled: *{len(scheduled)}*\n\n"
        "Use this panel to reschedule, publish immediately, or cancel a future publish.\n"
        "Scheduled content remains hidden from the audience until its exact publish time."
    )

def _schedule_date_kb(day):
    iso = day.isoformat()
    prev_day = (day - timedelta(days=1)).isoformat()
    next_day = (day + timedelta(days=1)).isoformat()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Previous", callback_data=f"video_schedule_date_{prev_day}"),
         InlineKeyboardButton("Today", callback_data=f"video_schedule_date_{datetime.now(config.TIMEZONE).date().isoformat()}"),
         InlineKeyboardButton("Next ▶️", callback_data=f"video_schedule_date_{next_day}")],
        [InlineKeyboardButton("🗓️ Upcoming", callback_data="video_manage_scheduled"),
         InlineKeyboardButton("🔄 Refresh", callback_data=f"video_schedule_date_{iso}")],
        [InlineKeyboardButton("🔙 Management", callback_data="menu_video_manage")],
    ])


async def _scheduled_date_message(chat_id, context, day=None, edit_message=None):
    day = day or datetime.now(config.TIMEZONE).date()
    iso = day.isoformat()
    next_iso = (day + timedelta(days=1)).isoformat()
    videos = [v for v in db.all_videos(limit=10000) if v.get("publish_at") and iso <= str(v.get("publish_at"))[:10] < next_iso and not db.is_visible(v)]
    batches = []
    seen = set()
    for v in videos:
        bid = v.get("batch_id")
        if bid and bid not in seen:
            b = db.get_batch(bid) or {}
            if str(b.get("publish_at") or "")[:10] == iso:
                seen.add(bid); batches.append(b)
    collection_items = sum(1 for v in videos if v.get("batch_id"))
    direct_videos = len(videos) - collection_items
    lines = [
        f"📅 *Schedule for {day.strftime('%a, %d %b %Y')}*", "",
        f"⏰ Video entries: *{len(videos)}*",
        f"🎬 Direct videos: *{direct_videos}*",
        f"📦 Collections: *{len(batches)}*",
        f"🎞️ Collection items: *{collection_items}*", "",
    ]
    rows = []
    seen_batches = set()
    for v in sorted(videos, key=lambda x: str(x.get("publish_at") or "")):
        bid = v.get("batch_id")
        if bid:
            if bid in seen_batches: continue
            seen_batches.add(bid)
            members = [x for x in videos if x.get("batch_id") == bid]
            batch = db.get_batch(bid) or {}
            title = batch.get("title") or "Collection"
            when = str(batch.get("publish_at") or members[0].get("publish_at") or "")[:16].replace("T", " ")
            lines.append(f"📦 *{db.md_escape(title)}* · {len(members)} items · ⏰ {when}")
            rows.append([InlineKeyboardButton(f"📦 Edit {str(title)[:18]}", callback_data=f"batchmanage_{bid}"),
                         InlineKeyboardButton("🔁 Reschedule", callback_data=f"batchschedule_{bid}")])
        else:
            when = str(v.get("publish_at") or "")[:16].replace("T", " ")
            title = db.md_escape(v.get("title") or "Untitled")
            lines.append(f"• #{v.get('video_number') or '?'} · {title}\n  ⏰ {when}")
            rows.append([InlineKeyboardButton(f"✏️ Edit #{v.get('video_number') or '?'}", callback_data=f"editmenu_{v['id']}"),
                         InlineKeyboardButton("🔁 Reschedule", callback_data=f"video_schedule_{v['id']}")])
    if not videos:
        lines.append("📭 Nothing is scheduled for this date.")
    rows.append([InlineKeyboardButton("🗓️ Upcoming", callback_data="video_manage_scheduled")])
    rows.extend(_schedule_date_kb(day).inline_keyboard)
    text = "\n".join(lines)
    if edit_message is not None:
        try:
            await edit_message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
            return
        except Exception:
            log.debug("Date-wise schedule edit failed; sending fresh message")
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def _scheduled_management_message(chat_id, context):
    """Upcoming schedules, grouped by collection when a batch shares publish_at."""
    videos = [v for v in db.all_videos(limit=10000) if v.get("publish_at") and not db.is_visible(v)]
    if not videos:
        await context.bot.send_message(chat_id=chat_id, text="🗓️ *Upcoming Scheduled*\n\n📭 Nothing is scheduled right now.", parse_mode="Markdown", reply_markup=_video_manage_kb())
        return

    groups = []
    seen_batches = set()
    for v in sorted(videos, key=lambda x: x.get("publish_at") or ""):
        bid = v.get("batch_id")
        if bid:
            if bid in seen_batches:
                continue
            seen_batches.add(bid)
            members = [x for x in videos if x.get("batch_id") == bid]
            batch = db.get_batch(bid) or {}
            groups.append(("collection", batch, members))
        else:
            groups.append(("video", None, [v]))

    rows = []
    lines = ["🗓️ *Upcoming Scheduled*", ""]
    for kind, batch, members in groups[:25]:
        if kind == "collection":
            title = batch.get("title") or "Collection"
            when = (batch.get("publish_at") or members[0].get("publish_at") or "")[:16].replace("T", " ")
            nums = [x.get("video_number") for x in members if x.get("video_number") is not None]
            rng = f"#{min(nums)}–#{max(nums)}" if nums else "#?"
            lines.append(f"📦 *{db.md_escape(title)}* · {len(members)} items · {rng}\n  ⏰ {when}")
            rows.append([
                InlineKeyboardButton("📦 Edit Collection", callback_data=f"batchmanage_{batch.get('id')}"),
                InlineKeyboardButton("🔁 Reschedule", callback_data=f"batchschedule_{batch.get('id')}"),
            ])
            rows.append([
                InlineKeyboardButton("⚡ Publish Now", callback_data=f"batchpublish_{batch.get('id')}"),
                InlineKeyboardButton("❌ Cancel Schedule", callback_data=f"batchunschedule_{batch.get('id')}"),
            ])
        else:
            v = members[0]
            when = v.get("publish_at", "")[:16].replace("T", " ")
            title = db.md_escape(v.get("title") or "Untitled")
            lines.append(f"• #{v.get('video_number') or '?'} · {title}\n  ⏰ {when}")
            rows.append([
                InlineKeyboardButton(f"✏️ Edit #{v.get('video_number') or '?'}", callback_data=f"editmenu_{v['id']}"),
                InlineKeyboardButton("🔁 Reschedule", callback_data=f"video_schedule_{v['id']}"),
            ])
            rows.append([
                InlineKeyboardButton("⚡ Publish Now", callback_data=f"video_publish_now_{v['id']}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"video_unschedule_{v['id']}"),
            ])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="video_manage_scheduled"), InlineKeyboardButton("🔙 Management", callback_data="menu_video_manage")])
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))

# ---------- unified callback router ----------

async def mandatory_join_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open Mandatory Join settings reliably from Channel Hub.

    This is kept as a dedicated callback handler so the generic admin router
    cannot swallow the button or fail silently on message-edit/Markdown issues.
    """
    query = update.callback_query
    if not query:
        return
    if not query.from_user or not is_admin(query.from_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()
    text = _mandatory_join_admin_text()
    markup = _mandatory_join_admin_kb()
    try:
        if query.message:
            await query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text=text, parse_mode="Markdown", reply_markup=markup)
    except Exception as exc:
        # A malformed custom value or an old Telegram message must never make
        # the button appear dead. Fall back to plain text while preserving UI.
        log.warning("Mandatory Join settings edit failed: %r", exc)
        safe_text = (
            "🔐 Mandatory Join Gate\n\n"
            + f"📢 Target: {_mandatory_join_admin_config()[0]}\n"
            + f"🔗 Join link: {_mandatory_join_admin_config()[1]}\n\n"
            + "Choose an action below."
        )
        try:
            if query.message:
                await query.message.edit_text(safe_text, reply_markup=markup)
            else:
                await context.bot.send_message(chat_id=query.from_user.id, text=safe_text, reply_markup=markup)
        except Exception:
            await context.bot.send_message(chat_id=query.from_user.id, text=safe_text, reply_markup=markup)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    if data.startswith("arx_"):
        parts = data.split("_", 2)
        if len(parts) != 3 or parts[1] not in dict(ALERT_REACTION_CODES):
            await query.answer(); return
        _, code, alert_key = parts
        active = db.toggle_alert_reaction(alert_key, query.from_user.id, code)
        await query.answer((dict(ALERT_REACTION_CODES).get(code) or "") + (" reacted!" if active else " removed"))
        try:
            alert_video_ids = db.get_alert_video_ids(alert_key)
            if len(alert_video_ids) == 1:
                url = botutil.direct_delivery_url(video_id=alert_video_ids[0])
                action_rows = [[InlineKeyboardButton("🎬 Open This Video", url=url)]] if url else [[InlineKeyboardButton("🌐 Open In WebView", url=_mini_app_deep_link("today"))]]
            else:
                batch_ids = { (db.get_video(vid) or {}).get("batch_id") for vid in alert_video_ids }
                batch_ids.discard(None)
                if len(batch_ids) == 1 and len(alert_video_ids) > 1:
                    bid = next(iter(batch_ids))
                    url = botutil.direct_delivery_url(batch_id=bid)
                    action_rows = [[InlineKeyboardButton(f"📦 Watch Collection · {len(alert_video_ids)}", url=url)]] if url else [[InlineKeyboardButton("🌐 Open In WebView", url=_mini_app_deep_link("today"))]]
                else:
                    counts = db.get_alert_category_counts_by_key(alert_key)
                    action_rows = [[InlineKeyboardButton(f"🇮🇳 Indian · {counts.get('Indian',0)}", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=cat_indian"), InlineKeyboardButton(f"🌍 Global · {counts.get('Global',0)}", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=cat_global")], [InlineKeyboardButton("🌐 Open In WebView", url=_mini_app_deep_link("today")), InlineKeyboardButton("🆕 Get Fresh Content", url=_deep_link("today"))]]
            action_rows.extend(_alert_reaction_rows(alert_key, query.from_user.id))
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(action_rows))
        except Exception:
            pass
        return
    if not is_admin(query.from_user.id):
        await query.answer()
        return
    data = query.data
    if data == "menu_video_manage":
        await query.answer()
        await query.edit_message_text(_video_manage_text(), parse_mode="Markdown", reply_markup=_video_manage_kb())
        return
    if data == "video_manage_scheduled":
        await query.answer()
        await _scheduled_management_message(query.message.chat_id, context)
        return
    if data == "video_schedule_dates":
        await query.answer()
        await _scheduled_date_message(query.message.chat_id, context)
        return
    if data.startswith("video_schedule_date_"):
        await query.answer()
        raw = data[len("video_schedule_date_"):]
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            day = datetime.now(config.TIMEZONE).date()
        await _scheduled_date_message(query.message.chat_id, context, day=day, edit_message=query.message)
        return
    if data == "video_manage_recent":
        await query.answer()
        await _send_list_page(query.message.chat_id, context, 0)
        return
    if data.startswith("video_schedule_"):
        await query.answer()
        vid = data[len("video_schedule_"):]
        v = db.get_video(vid)
        if not v:
            await context.bot.send_message(chat_id=query.message.chat_id, text="❌ Video not found.")
            return
        context.user_data["awaiting_video_schedule"] = vid
        current = v.get("publish_at") or "not scheduled"
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"⏰ *Schedule #{v.get('video_number') or '?'}*\n\nCurrent: `{db.md_escape(current)}`\n\nSend `YYYY-MM-DD HH:MM` or `+2h`, `+1d`.",
            parse_mode="Markdown",
        )
        return
    if data.startswith("video_publish_now_"):
        await query.answer()
        vid = data[len("video_publish_now_"):]
        v = db.get_video(vid)
        if not v:
            await context.bot.send_message(chat_id=query.message.chat_id, text="❌ Video not found.")
            return
        db.set_video_publish_at(vid, datetime.now(config.TIMEZONE).isoformat())
        db.log_activity("admin_bot", "publish_now", vid)
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"⚡ `{vid}` is live now.", parse_mode="Markdown")
        await _scheduled_management_message(query.message.chat_id, context)
        return
    if data.startswith("video_unschedule_"):
        await query.answer()
        vid = data[len("video_unschedule_"):]
        v = db.get_video(vid)
        if not v:
            await context.bot.send_message(chat_id=query.message.chat_id, text="❌ Video not found.")
            return
        db.set_video_publish_at(vid, None)
        db.log_activity("admin_bot", "cancel_schedule", vid)
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"✅ Schedule cancelled for `{vid}`. It is live immediately.", parse_mode="Markdown")
        await _scheduled_management_message(query.message.chat_id, context)
        return
    if data == "mandatory_join_admin":
        await mandatory_join_admin_callback(update, context)
        return
    if data == "mandatory_join_message_edit":
        await query.answer()
        context.user_data["awaiting_mandatory_join_message"] = True
        current = botutil.get_mandatory_join_message(config, "{channel}")
        await context.bot.send_message(chat_id=chat_id, text=(
            "💬 *Edit Mandatory Join Message*\n\n"
            "Send the message exactly as users should see it.\n"
            "Use `{channel}` to insert the channel name automatically.\n\n"
            f"Current:\n`{db.md_escape(current)}`\n\n"
            "Send `cancel` to keep the current message."
        ), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mandatory_join_cancel")]]))
        return
    if data == "mandatory_join_image_edit":
        await query.answer()
        context.user_data["awaiting_mandatory_join_image"] = True
        await context.bot.send_message(chat_id=chat_id, text="🖼️ *Change Mandatory Join Image*\n\nSend the new image/photo now.\nIt will be saved on the shared bot server and used by both Catalogue and Delivery join gates.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mandatory_join_cancel")]]))
        return
    if data == "mandatory_join_preview":
        await query.answer()
        channel_id, link = _mandatory_join_admin_config()
        title = "Mandatory Join Channel"
        try:
            chat = await context.bot.get_chat(channel_id)
            title = getattr(chat, "title", None) or title
        except Exception:
            pass
        await botutil.send_mandatory_join_prompt(query.message, title, link)
        return

    if data == "mandatory_join_edit":
        await query.answer()
        context.user_data["awaiting_mandatory_join"] = True
        await context.bot.send_message(chat_id=chat_id, text=("✏️ *Edit Mandatory Join Channel*\n\nSend `@username` or `https://t.me/username`.\nFor private: `-1001234567890 | https://t.me/+invite`\n\nSend `reset` to restore config defaults."), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mandatory_join_cancel")]]))
        return
    if data == "mandatory_join_reset":
        await query.answer()
        db.clear_setting("mandatory_join_channel_id"); db.clear_setting("mandatory_join_channel_link"); db.clear_setting("mandatory_join_message")
        await query.edit_message_text(_mandatory_join_admin_text(), parse_mode="Markdown", reply_markup=_mandatory_join_admin_kb())
        return
    if data == "mandatory_join_cancel":
        await query.answer(); context.user_data.pop("awaiting_mandatory_join", None); context.user_data.pop("awaiting_mandatory_join_message", None); context.user_data.pop("awaiting_mandatory_join_image", None)
        await context.bot.send_message(chat_id=chat_id, text=_mandatory_join_admin_text(), parse_mode="Markdown", reply_markup=_mandatory_join_admin_kb())
        return
    if data == "menu_channels":
        await query.answer()
        results=await check_channel_access(context.bot, _required_channels())
        await query.edit_message_text(_channel_hub_text(results), parse_mode="Markdown", reply_markup=_channel_hub_kb())
        return
    if data == "menu_channels_check":
        await query.answer("Checking Telegram access…")
        results=await check_channel_access(context.bot, _required_channels())
        await query.edit_message_text(_channel_hub_text(results), parse_mode="Markdown", reply_markup=_channel_hub_kb())
        return
    if data == "menu_diagnostics":
        await query.answer()
        issues=_validate_config(); results=await check_channel_access(context.bot, _required_channels()); s=db.stats()
        lines=["🧰 *Vault Diagnostics*","",f"Catalog: *{s.get('total_videos',0)} items*",f"Missing backups: *{s.get('missing_backup',0)}*","", "*Telegram access*"]
        lines.extend(f"• {status} {label}" for label,_,status in results)
        lines.append("")
        lines.append("*Config*")
        lines.extend(f"• {x}" for x in issues) if issues else lines.append("• ✅ Required config looks present")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu_kb())
        return
    if data == "menu_scheduler":
        await query.answer()
        await query.edit_message_text(_scheduler_text(), parse_mode="Markdown", reply_markup=_scheduler_kb())
        return
    if data == "scheduler_view":
        await query.answer()
        await query.edit_message_text(_scheduler_text(), parse_mode="Markdown", reply_markup=_scheduler_kb())
        return
    if data == "scheduler_alert":
        await query.answer()
        new = "0" if db.get_setting("weekly_auto_alert") == "1" else "1"
        db.set_setting("weekly_auto_alert", new)
        await query.edit_message_text(_scheduler_text(), parse_mode="Markdown", reply_markup=_scheduler_kb())
        return
    if data == "scheduler_add":
        await query.answer()
        await context.bot.send_message(query.message.chat_id, "🗓️ Add a slot with `/weekly Mon 18:00 1`", parse_mode="Markdown")
        return
    if data == "scheduler_one":
        await query.answer()
        await context.bot.send_message(query.message.chat_id, "⏰ Schedule one video with `/schedule VIDEO_ID YYYY-MM-DD HH:MM`", parse_mode="Markdown")
        return
    if data == "scheduler_release":
        await query.answer()
        q=db.get_unscheduled_videos(limit=1)
        if not q:
            await context.bot.send_message(query.message.chat_id,"📭 No unscheduled content in queue.")
        else:
            db.set_video_publish_at(q[0]["id"], datetime.now(config.TIMEZONE).isoformat())
            await context.bot.send_message(query.message.chat_id,f"⚡ Released `{q[0]['id']}` — {db.md_escape(q[0].get('title') or 'Untitled')}",parse_mode="Markdown")
        return
    chat_id = query.message.chat_id

    if data.startswith("alert_"):
        await alert_callback(update, context)
        return

    await query.answer()

    if data == "admin_manage":
        await _admin_management_message(chat_id, context)

    elif data == "admin_list":
        await _admin_management_message(chat_id, context)

    elif data == "admin_add":
        if not is_owner(query.from_user.id):
            await context.bot.send_message(chat_id=chat_id, text="🚫 Only the primary owner can add admins.", reply_markup=_admin_manage_kb())
        else:
            context.user_data["awaiting_admin_add"] = True
            await context.bot.send_message(chat_id=chat_id, text="➕ Send the Telegram user ID and optional role.\n\nExample: `123456789 admin`", parse_mode="Markdown")

    elif data == "admin_remove":
        if not is_owner(query.from_user.id):
            await context.bot.send_message(chat_id=chat_id, text="🚫 Only the primary owner can remove admins.", reply_markup=_admin_manage_kb())
        else:
            context.user_data["awaiting_admin_remove"] = True
            await context.bot.send_message(chat_id=chat_id, text="➖ Send the Telegram user ID to remove.")

    elif data == "bot_editor":
        await context.bot.send_message(chat_id=chat_id, text="🤖 *Bot Editor*\n\nChoose a bot. You can edit its plain `/start` welcome message, preview it, or reset it.", parse_mode="Markdown", reply_markup=_bot_editor_kb())

    elif data == "permbots":
        await context.bot.send_message(chat_id=chat_id, text=_permbots_text(), parse_mode="Markdown", reply_markup=_permbots_kb())

    elif data == "permbots_add":
        context.user_data["awaiting_permbot_add"]=True
        await context.bot.send_message(chat_id=chat_id, text=("➕ *Add Permanent Delivery Bot(s)*\n\nSend one BotFather token per line.\n"
            "Each token is verified with Telegram before saving.\n\n"
            "These bots share the same Delivery logic + database. They become part of the random Watch resolver when enabled.\n\nSend `cancel` to stop."), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="permbots_cancel")]]))

    elif data == "permbots_primary":
        token=getattr(config,"DELIVERY_BOT_TOKEN","") or ""
        user=getattr(config,"DELIVERY_BOT_USERNAME","") or ""
        try:
            ok=permanent_bot_store.ensure_primary(token,user)
            await query.answer("Primary registered" if ok else "Primary already registered")
        except Exception:
            await query.answer("Could not register primary",show_alert=True)
        await context.bot.send_message(chat_id=chat_id,text=_permbots_text(),parse_mode="Markdown",reply_markup=_permbots_kb())

    elif data == "permbots_manage":
        await _permbots_manage_message(chat_id, context)

    elif data == "permbots_resolver":
        await context.bot.send_message(chat_id=chat_id,text=_permbots_resolver_text(),parse_mode="Markdown",reply_markup=_permbots_kb())

    elif data == "permbots_health":
        health_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Resolver Status", callback_data="permbots_resolver_status"),
             InlineKeyboardButton("🧪 Test Resolver", callback_data="permbots_resolver_test")],
            [InlineKeyboardButton("📋 Manage Bots", callback_data="permbots_manage"),
             InlineKeyboardButton("🔄 Refresh", callback_data="permbots_health")],
            [InlineKeyboardButton("🔙 Pool", callback_data="permbots")],
        ])
        await context.bot.send_message(chat_id=chat_id,text=await _permbots_health_text(),parse_mode="Markdown",reply_markup=health_kb)

    elif data == "permbots_resolver_status":
        await context.bot.send_message(chat_id=chat_id,text=await _permbots_resolver_status_text(),parse_mode="Markdown",reply_markup=_permbots_kb())

    elif data == "permbots_resolver_test":
        await context.bot.send_message(chat_id=chat_id,text=await _permbots_resolver_test_text(),parse_mode="Markdown",reply_markup=_permbots_kb())

    elif data == "permbots_token":
        await _permbots_token_start(chat_id, context)

    elif data.startswith("permbot_token_"):
        bid=data[len("permbot_token_"):]; row=permanent_bot_store.get_bot(bid)
        if not row:
            await query.answer("Bot not found",show_alert=True)
        else:
            context.user_data["awaiting_permbot_token"] = bid
            await context.bot.send_message(chat_id=chat_id,text=f"🔑 *New token for @{db.md_escape(str(row.get('username') or bid).lstrip('@'))}*\n\nSend the new BotFather token.\n\n⚠️ The old token stays in place until Telegram validation succeeds.",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="permbots_cancel")]]))

    elif data == "permbots_cancel":
        context.user_data.pop("awaiting_permbot_add",None)
        await context.bot.send_message(chat_id=chat_id,text=_permbots_text(),parse_mode="Markdown",reply_markup=_permbots_kb())

    elif data.startswith("permbot_toggle_"):
        bid=data[len("permbot_toggle_"):]; row=permanent_bot_store.get_bot(bid)
        if not row: await query.answer("Bot not found",show_alert=True)
        else:
            new_state=not bool(row.get("enabled")); permanent_bot_store.set_enabled(bid,new_state)
            db.log_activity("admin_bot","permbot_toggle",f"{row.get('username') or bid} -> {new_state}")
            await query.answer("Enabled ✅" if new_state else "Disabled · removed from resolver pool")
            await _permbots_manage_message(chat_id,context)

    elif data.startswith("permbot_recover_"):
        bid=data[len("permbot_recover_"):]; row=permanent_bot_store.get_bot(bid)
        if not row:
            await query.answer("Bot not found",show_alert=True)
        else:
            ok=permanent_bot_store.recover_bot(bid)
            await query.answer("Recovered ✅" if ok else "Recovery failed",show_alert=not ok)
            await context.bot.send_message(chat_id=chat_id,text=_permbots_text(),parse_mode="Markdown",reply_markup=_permbots_kb())

    elif data.startswith("permbot_remove_"):
        bid=data[len("permbot_remove_"):]; row=permanent_bot_store.get_bot(bid)
        if not row: await query.answer("Bot not found",show_alert=True)
        elif row.get("primary"): await query.answer("Primary bot cannot be retired; disable it instead.",show_alert=True)
        else:
            ok=permanent_bot_store.remove_bot(bid)
            if ok:
                db.log_activity("admin_bot","permbot_retire",row.get("username") or bid)
                await query.answer("Retired · stable links remain protected")
            else:
                await query.answer("Could not retire this bot",show_alert=True)
            await _permbots_manage_message(chat_id,context)

    elif data.startswith("permbot_delete_confirm_"):
        bid=data[len("permbot_delete_confirm_"):]; row=permanent_bot_store.get_bot(bid)
        if not row:
            await query.answer("Bot not found",show_alert=True)
        elif row.get("primary"):
            await query.answer("Primary bot is protected.",show_alert=True)
        else:
            user='@'+str(row.get("username") or bid).lstrip('@')
            await query.answer()
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"⚠️ *Delete {db.md_escape(user)} permanently?*\n\n"
                      "This removes the bot from the pool and stops its worker.\n"
                      "✅ Stable Watch links are not tied to this bot, so existing links keep resolving through another routable bot.\n\n"
                      "Stable resolver links remain protected after deletion; the bot is simply removed from future selection."),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑️ YES, DELETE PERMANENTLY", callback_data=f"permbot_delete_{bid}")],
                    [InlineKeyboardButton("↩️ Cancel", callback_data="permbots_manage")],
                ]),
            )

    elif data.startswith("permbot_delete_"):
        bid=data[len("permbot_delete_"):]; row=permanent_bot_store.get_bot(bid)
        if not row:
            await query.answer("Already removed",show_alert=True)
        elif row.get("primary"):
            await query.answer("Primary bot is protected.",show_alert=True)
        else:
            ok, reason=permanent_bot_store.delete_bot(bid)
            if ok:
                db.log_activity("admin_bot","permbot_delete",row.get("username") or bid)
                await query.answer("Deleted permanently ✅")
                await context.bot.send_message(chat_id=chat_id,text=f"🗑️ *{db.md_escape('@'+str(row.get('username') or bid).lstrip('@'))} deleted permanently.*\n\nWorker stop queued automatically.\nNew Watch links will never select this bot again.",parse_mode="Markdown",reply_markup=_permbots_manage_kb())
            else:
                msg="Primary bot is protected." if reason=="primary" else "Bot could not be deleted."
                await query.answer(msg,show_alert=True)

    elif data == "tempbots":
        await context.bot.send_message(chat_id=chat_id, text=_tempbots_text(), parse_mode="Markdown", reply_markup=_tempbots_kb())

    elif data == "tempbots_health":
        await _tempbots_health_message(chat_id, context)

    elif data == "tempbots_test":
        await _tempbots_test_message(chat_id, context)

    elif data == "tempbots_recover_all":
        count=0
        for b in temp_bot_store.list_bots():
            if b.get("enabled"):
                temp_bot_store.recover(str(b.get("id"))); count += 1
        await query.answer(f"Recovery queued for {count} bot(s)")
        await context.bot.send_message(chat_id=chat_id,text=f"♻️ *Recovery queued*\n\n{count} enabled temporary bot(s) returned to `STARTING`. The supervisor will revalidate them automatically.",parse_mode='Markdown',reply_markup=_tempbots_kb())

    elif data == "tempbots_add":
        context.user_data["awaiting_tempbot_add"] = True
        await context.bot.send_message(
            chat_id=chat_id,
            text=("➕ *Add Temporary Bot(s)*\n\n"
                  "Send one BotFather token per line. You can add multiple bots in one message.\n\n"
                  "I'll validate each token with Telegram before saving it.\n"
                  "🔐 Tokens are stored separately from the catalog DB and are never included in normal DB backups.\n\n"
                  "Send `cancel` to stop."),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="tempbots_cancel")]])
        )

    elif data == "tempbots_manage":
        await _tempbots_manage_message(chat_id, context)

    elif data == "tempbots_channel":
        context.user_data["awaiting_tempbot_channel"] = True
        await _tempbots_channel_message(chat_id, context)

    elif data == "tempbots_button":
        context.user_data["awaiting_tempbot_button"] = True
        await _tempbots_button_message(chat_id, context)

    elif data == "tempbots_message":
        context.user_data["awaiting_tempbot_message"] = True
        await _tempbots_message_message(chat_id, context)

    elif data == "tempbots_image":
        context.user_data["awaiting_tempbot_image"] = True
        await _tempbots_image_message(chat_id, context)

    elif data == "tempbots_image_clear":
        context.user_data.pop("awaiting_tempbot_image", None)
        temp_bot_store.clear_start_image()
        db.log_activity("admin_bot", "tempbots_image_cleared", "")
        await query.answer("Image cleared")
        await _tempbots_message_message(chat_id, context)

    elif data == "tempbots_preview":
        await _tempbots_preview_message(chat_id, context)

    elif data == "tempbots_cancel":
        for key in ("awaiting_tempbot_add", "awaiting_tempbot_token", "awaiting_tempbot_channel", "awaiting_tempbot_button", "awaiting_tempbot_message", "awaiting_tempbot_image", "awaiting_tempbot_specific", "awaiting_tempbot_specific_image"):
            context.user_data.pop(key, None)
        await context.bot.send_message(chat_id=chat_id, text=_tempbots_text(), parse_mode="Markdown", reply_markup=_tempbots_kb())

    elif data.startswith("tempbot_settings_"):
        bot_id=data[len("tempbot_settings_"):]
        if not temp_bot_store.get_bot(bot_id): await query.answer("Bot not found", show_alert=True)
        else:
            await query.answer()
            await _tempbot_settings_message(chat_id, context, bot_id)

    elif data.startswith("tempbot_channel_"):
        bot_id=data[len("tempbot_channel_"):]
        row=temp_bot_store.get_bot(bot_id)
        if not row: await query.answer("Bot not found", show_alert=True)
        else:
            context.user_data["awaiting_tempbot_specific"]={"bot_id":bot_id,"field":"channel"}
            await query.answer()
            await context.bot.send_message(chat_id=chat_id,text=_tempbot_specific_prompt("channel",bot_id),parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"tempbot_settings_{bot_id}")]]))

    elif data.startswith("tempbot_button_"):
        bot_id=data[len("tempbot_button_"):]
        if not temp_bot_store.get_bot(bot_id): await query.answer("Bot not found", show_alert=True)
        else:
            context.user_data["awaiting_tempbot_specific"]={"bot_id":bot_id,"field":"button"}
            await query.answer()
            await context.bot.send_message(chat_id=chat_id,text=_tempbot_specific_prompt("button",bot_id),parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"tempbot_settings_{bot_id}")]]))

    elif data.startswith("tempbot_message_"):
        bot_id=data[len("tempbot_message_"):]
        if not temp_bot_store.get_bot(bot_id): await query.answer("Bot not found", show_alert=True)
        else:
            context.user_data["awaiting_tempbot_specific"]={"bot_id":bot_id,"field":"message"}
            await query.answer()
            await context.bot.send_message(chat_id=chat_id,text=_tempbot_specific_prompt("message",bot_id),parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"tempbot_settings_{bot_id}")]]))

    elif data.startswith("tempbot_image_"):
        suffix=data[len("tempbot_image_"):]
        if suffix.endswith("_clear"):
            bot_id=suffix[:-6]
            row=temp_bot_store.get_bot(bot_id)
            if not row: await query.answer("Bot not found", show_alert=True)
            else:
                cfg=temp_bot_store.get_bot_config(bot_id, default_link=_default_temp_channel_link())
                old=cfg["message"].get("image_path") if cfg["overrides"]["image"] else ""
                temp_bot_store.clear_bot_message_field(bot_id, "image_path")
                try:
                    path=Path(str(old or ""))
                    if path.is_file() and Path(os.path.dirname(botutil.MANDATORY_JOIN_IMAGE)) / "temp_bots" in path.parents: path.unlink(missing_ok=True)
                except Exception: pass
                await query.answer("Bot image reset")
                await _tempbot_settings_message(chat_id, context, bot_id)
        else:
            bot_id=suffix
            if not temp_bot_store.get_bot(bot_id): await query.answer("Bot not found", show_alert=True)
            else:
                context.user_data["awaiting_tempbot_specific_image"]={"bot_id":bot_id}
                await query.answer()
                await context.bot.send_message(chat_id=chat_id,text="🖼 *Bot-Specific Start Image*\n\nSend the new photo now.\nSend `default` as text to return to the pool image.",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Use Pool Image", callback_data=f"tempbot_image_{bot_id}_clear"),InlineKeyboardButton("❌ Cancel", callback_data=f"tempbot_settings_{bot_id}")]]))

    elif data.startswith("tempbot_preview_"):
        bot_id=data[len("tempbot_preview_"):]
        if not temp_bot_store.get_bot(bot_id): await query.answer("Bot not found", show_alert=True)
        else:
            await query.answer()
            await _tempbot_preview_specific(chat_id, context, bot_id)

    elif data.startswith("tempbot_reset_"):
        bot_id=data[len("tempbot_reset_"):]
        row=temp_bot_store.get_bot(bot_id)
        if not row: await query.answer("Bot not found", show_alert=True)
        else:
            temp_bot_store.reset_bot_config(bot_id)
            db.log_activity("admin_bot","tempbot_specific_reset_all",row.get("username") or bot_id)
            await query.answer("Pool defaults restored ✅")
            await _tempbot_settings_message(chat_id, context, bot_id)

    elif data.startswith("tempbot_toggle_"):
        bot_id = data[len("tempbot_toggle_"):]
        row = temp_bot_store.get_bot(bot_id)
        if not row:
            await query.answer("Bot not found", show_alert=True)
        else:
            new_state = not bool(row.get("enabled"))
            temp_bot_store.set_enabled(bot_id, new_state)
            db.log_activity("admin_bot", "tempbot_toggle", f"{row.get('username') or bot_id} -> {new_state}")
            await query.answer("Enabled" if new_state else "Disabled")
            await _tempbots_manage_message(chat_id, context)

    elif data.startswith("tempbot_recover_"):
        bot_id=data[len("tempbot_recover_"):]
        row=temp_bot_store.get_bot(bot_id)
        if not row: await query.answer("Bot not found",show_alert=True)
        else:
            temp_bot_store.recover(bot_id)
            db.log_activity("admin_bot","tempbot_recover",row.get("username") or bot_id)
            await query.answer("Recovery queued ✅")
            await _tempbots_manage_message(chat_id,context)

    elif data.startswith("tempbot_token_"):
        bot_id=data[len("tempbot_token_"):]
        if not temp_bot_store.get_bot(bot_id):
            await query.answer("Bot not found",show_alert=True)
        else:
            context.user_data["awaiting_tempbot_token"]=bot_id
            await query.answer("Send new BotFather token")
            await context.bot.send_message(chat_id=chat_id,text="🔑 *Update Temporary Bot Token*\n\nSend the new BotFather token for this bot.\n\nSend `cancel` to abort.",parse_mode='Markdown',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="tempbots_cancel")]]))

    elif data.startswith("tempbot_remove_confirm_"):
        bot_id = data[len("tempbot_remove_confirm_"):]
        row = temp_bot_store.get_bot(bot_id)
        context.user_data.pop("tempbot_remove_confirm", None)
        if not row:
            await query.answer("Already removed", show_alert=True)
            await _tempbots_manage_message(chat_id, context)
        else:
            removed = temp_bot_store.remove_bot(bot_id)
            db.log_activity("admin_bot", "tempbot_remove", row.get("username") or bot_id)
            await query.answer("Removed permanently" if removed else "Already removed")
            await _tempbots_manage_message(chat_id, context)

    elif data.startswith("tempbot_remove_"):
        bot_id = data[len("tempbot_remove_"):]
        row = temp_bot_store.get_bot(bot_id)
        if not row:
            await query.answer("Bot not found", show_alert=True)
        else:
            context.user_data["tempbot_remove_confirm"] = bot_id
            await query.answer("Confirmation required")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🗑 *Remove Temporary Bot?*\n\n`@{db.md_escape(row.get('username') or bot_id)}`\n\nThis permanently deletes the saved token and stops its worker. This cannot be undone.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚠️ Remove Permanently", callback_data=f"tempbot_remove_confirm_{bot_id}"),
                     InlineKeyboardButton("Cancel", callback_data="tempbots_manage")]
                ])
            )

    elif data.startswith("editbot_"):
        bot_key = data[len("editbot_"):]
        template_key = BOT_TEMPLATE_KEYS.get(bot_key)
        if not template_key:
            await context.bot.send_message(chat_id=chat_id, text="❌ Unknown bot.")
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"🤖 *{bot_key.title()} Bot Editor*\n\nCurrent welcome:\n\n{admin_store.get_template(template_key)}", parse_mode="Markdown", reply_markup=_bot_template_kb(bot_key))

    elif data.startswith("edittext_"):
        bot_key = data[len("edittext_"):]
        template_key = BOT_TEMPLATE_KEYS.get(bot_key)
        if not template_key:
            await context.bot.send_message(chat_id=chat_id, text="❌ Unknown bot.")
        else:
            context.user_data["awaiting_bot_template"] = template_key
            await context.bot.send_message(chat_id=chat_id, text=f"✏️ Send the new `/start` welcome text for the {bot_key.title()} Bot.\n\nMarkdown is not required; the text will be sent safely as plain text.", parse_mode="Markdown")

    elif data.startswith("previewtext_"):
        bot_key = data[len("previewtext_"):]
        template_key = BOT_TEMPLATE_KEYS.get(bot_key)
        await context.bot.send_message(chat_id=chat_id, text=admin_store.get_template(template_key, ""), reply_markup=_bot_template_kb(bot_key))

    elif data.startswith("resettext_"):
        bot_key = data[len("resettext_"):]
        template_key = BOT_TEMPLATE_KEYS.get(bot_key)
        if template_key:
            admin_store.set_template(template_key, admin_store.DEFAULT_TEMPLATES[template_key])
            await context.bot.send_message(chat_id=chat_id, text="♻️ Welcome message reset.", reply_markup=_bot_template_kb(bot_key))

    elif data == "menu_home":
        await context.bot.send_message(chat_id=chat_id, text="✨ *Vault HQ* ✨\n\nBoss mode is online 😌💅\nPick a module below — I'll handle the boring stuff. 🛠️", parse_mode="Markdown",
                                        reply_markup=main_menu_kb())

    elif data == "menu_queue":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Post Alert Now", callback_data="menu_postalert")]])
        await context.bot.send_message(chat_id=chat_id, text=await _queue_text(), parse_mode="Markdown",
                                        reply_markup=kb)

    elif data == "menu_postalert":
        try:
            await _build_postalert_preview(chat_id, context, context.user_data)
        except Exception as exc:
            log.exception("Main-menu Post Alert failed")
            await context.bot.send_message(chat_id=chat_id, text=f"🥲 *Post Alert couldn't open.*\n\n`{db.md_escape(str(exc))}`\n\nTry /postalert once and the error will be logged for repair.", parse_mode="Markdown")

    elif data == "menu_default_alert_cover":
        await _default_alert_cover_menu(chat_id, context)

    elif data == "default_alert_cover_set":
        context.user_data["awaiting_default_alert_cover"] = True
        await context.bot.send_message(
            chat_id=chat_id,
            text="🖼️ <b>Send the default alert cover photo.</b>\n\nIt will be saved and reused automatically for normal queue alerts.",
            parse_mode="HTML",
        )

    elif data == "default_alert_cover_clear":
        db.clear_setting("default_alert_cover_msg_id")
        db.log_activity("admin_bot", "clear_default_alert_cover", "")
        await _default_alert_cover_menu(chat_id, context)

    elif data.startswith("batchmanage_"):
        batch_id = data[len("batchmanage_"):]
        await query.answer()
        await _send_collection_manage(chat_id, context, batch_id)

    elif data.startswith("batchalert_"):
        batch_id = data[len("batchalert_"):]
        videos = [v for v in db.get_batch_videos(batch_id) if db.is_visible(v)]
        if not videos:
            await query.answer("Collection has no visible items.", show_alert=True)
            return
        context.user_data["alert_batch"] = [v["id"] for v in videos]
        context.user_data["alert_batch_id"] = batch_id
        context.user_data["alert_mode"] = "collection"
        context.user_data.pop("custom_alert_caption", None)
        context.user_data.pop("custom_alert_cover_msg_id", None)
        await query.answer()
        await _post_alert_to(chat_id, context, videos, is_channel=False, user_data=context.user_data)
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Post Collection Alert", callback_data="alert_confirm"), InlineKeyboardButton("❌ Cancel", callback_data="alert_cancel")],
            [InlineKeyboardButton("✏️ Edit Message", callback_data="alert_edit_text"), InlineKeyboardButton("🖼 Change Cover", callback_data="alert_edit_cover")],
        ])
        await context.bot.send_message(chat_id=chat_id, text="👆 This collection alert will contain only this collection. Post it?", reply_markup=confirm_kb)

    elif data.startswith("menu_list_"):
        page = int(data.rsplit("_", 1)[1])
        await _send_list_page(chat_id, context, page)

    elif data.startswith("listpage_"):
        page = int(data.rsplit("_", 1)[1])
        await _send_list_page(chat_id, context, page)

    elif data == "menu_control":
        try:
            text = await _control_center_text(context)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="menu_control"),
                 InlineKeyboardButton("📡 Channel Hub", callback_data="menu_channels")],
                [InlineKeyboardButton("🧰 Diagnostics", callback_data="menu_diagnostics"),
                 InlineKeyboardButton("💾 Backup", callback_data="menu_backupdb")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
            ])
            await context.bot.send_message(chat_id=chat_id, text=text,
                                            parse_mode="Markdown", reply_markup=kb)
        except Exception as exc:
            log.exception("Control Center failed")
            await context.bot.send_message(chat_id=chat_id,
                                            text=f"🥲 Control Center error: `{db.md_escape(str(exc))}`",
                                            parse_mode="Markdown")

    elif data == "menu_stats":
        await context.bot.send_message(chat_id=chat_id, text=await _stats_text(), parse_mode="Markdown",
                                        reply_markup=main_menu_kb())

    elif data == "menu_analytics":
        await context.bot.send_message(chat_id=chat_id, text=await _analytics_text(7), parse_mode="Markdown",
                                        reply_markup=_analytics_kb(7))

    elif data.startswith("analytics_top_"):
        days = int(data.rsplit("_", 1)[1])
        await context.bot.send_message(chat_id=chat_id, text=await _analytics_top_text(days), parse_mode="Markdown",
                                        reply_markup=_analytics_kb(days))

    elif data.startswith("analytics_access_"):
        days = int(data.rsplit("_", 1)[1])
        await context.bot.send_message(chat_id=chat_id, text=await _analytics_access_text(days), parse_mode="Markdown",
                                        reply_markup=_analytics_kb(days))

    elif data.startswith("analytics_search_"):
        days = int(data.rsplit("_", 1)[1])
        await context.bot.send_message(chat_id=chat_id, text=await _analytics_search_text(days), parse_mode="Markdown",
                                        reply_markup=_analytics_kb(days))

    elif data.startswith("analytics_"):
        days = int(data.rsplit("_", 1)[1])
        await context.bot.send_message(chat_id=chat_id, text=await _analytics_text(days), parse_mode="Markdown",
                                        reply_markup=_analytics_kb(days))

    elif data == "menu_verify":
        await _send_verify_report(chat_id, context)

    elif data == "menu_top":
        await context.bot.send_message(chat_id=chat_id, text=await _top_text(), parse_mode="Markdown",
                                        reply_markup=main_menu_kb())

    elif data == "menu_bydate":
        await context.bot.send_message(chat_id=chat_id, text=_schedule_day_text(_schedule_day_iso(0)), parse_mode="Markdown",
                                        reply_markup=_schedule_day_kb(_schedule_day_iso(0)))

    elif data.startswith("admin_sched_day_"):
        day_iso = data[len("admin_sched_day_"):]
        await context.bot.send_message(chat_id=chat_id, text=_schedule_day_text(day_iso), parse_mode="Markdown",
                                        reply_markup=_schedule_day_kb(day_iso))

    elif data == "admin_sched_week":
        await context.bot.send_message(chat_id=chat_id, text=_schedule_week_text(), parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📅 Open Today", callback_data=f"admin_sched_day_{_schedule_day_iso(0)}")], [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")]]))

    elif data == "menu_health":
        await context.bot.send_message(chat_id=chat_id, text=await _health_text(context), parse_mode="Markdown",
                                        reply_markup=main_menu_kb())

    elif data == "menu_log":
        entries = db.get_recent_activity(20)
        if not entries:
            text = "No activity logged yet."
        else:
            lines = [f"{e['ts'][:16]} · {e['actor']} · {e['action']}" + (f" — {e['detail']}" if e['detail'] else "")
                     for e in entries]
            text = "📜 *Recent Activity*\n\n" + "\n".join(lines)
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=main_menu_kb())

    elif data == "menu_export":
        import csv, io
        rows = db.export_rows()
        if not rows:
            await context.bot.send_message(chat_id=chat_id, text="Nothing to export yet.", reply_markup=main_menu_kb())
        else:
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            data_bytes = io.BytesIO(buf.getvalue().encode("utf-8"))
            data_bytes.name = f"catalog_export_{db.today_str()}.csv"
            await context.bot.send_document(chat_id=chat_id, document=data_bytes, filename=data_bytes.name,
                                             caption=f"📄 {len(rows)} video(s) exported.")

    elif data == "menu_backupdb":
        await _send_db_backup(context.bot, chat_id, manual=True)

    elif data == "menu_access":
        await context.bot.send_message(chat_id=chat_id, text=await _access_text(), parse_mode="Markdown",
                                        reply_markup=access_menu_kb())

    elif data == "access_gencode":
        context.user_data["awaiting_redeem_create"] = "premium"
        await context.bot.send_message(
            chat_id=chat_id,
            text=("🎟️ *Create Premium Redeem Code*\n\n"
                  "Send: `DAYS [USES]`\n\n"
                  "Example: `30 1` = 30 days, one redemption.\n"
                  "Example: `30 10` = 30 days, ten redemptions."),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="access_cancel_create")]])
        )

    elif data == "access_gengiveaway":
        context.user_data["awaiting_redeem_create"] = "giveaway"
        await context.bot.send_message(
            chat_id=chat_id,
            text=("🎁 *Create Giveaway Redeem Code*\n\n"
                  "Send: `DAYS [USES]`\n\n"
                  "Example: `30` = unlimited redemptions.\n"
                  "Example: `30 100` = maximum 100 redemptions."),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="access_cancel_create")]])
        )

    elif data == "access_codes":
        codes = db.list_codes(active_only=True, limit=30)
        await context.bot.send_message(chat_id=chat_id, text=_access_codes_text(codes),
                                       parse_mode="Markdown", reply_markup=access_kb_for_codes(codes))

    elif data == "access_limit":
        current = _limit_label(db.get_default_daily_limit())
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"⚙️ *Default Daily Limit*\n\nCurrent: *{current}*\n\n"
                  "Send a non-negative number, or `unlimited`.\nExample: `10`"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Access Control", callback_data="menu_access")]])
        )
        context.user_data["awaiting_default_limit"] = True

    elif data == "access_cancel_create":
        context.user_data.pop("awaiting_redeem_create", None)
        await context.bot.send_message(chat_id=chat_id, text=await _access_text(),
                                       parse_mode="Markdown", reply_markup=access_menu_kb())

    elif data.startswith("redeem_alert_"):
        code = data[len("redeem_alert_"):].strip().upper()
        c = db.get_redeem_code(code)
        if not c:
            await context.bot.send_message(chat_id=chat_id, text="❌ Code not found.", reply_markup=access_menu_kb())
        else:
            try:
                await _post_redeem_code_alert(context, c["code"], c["kind"], c["duration_days"], c["max_redemptions"])
                db.log_activity("admin_bot", "redeem_code_alert", code)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"📢 Redeem-code alert posted for `{code}`.",
                    parse_mode="Markdown",
                    reply_markup=access_menu_kb(),
                )
            except Exception as exc:
                log.exception("Redeem code alert failed")
                await context.bot.send_message(chat_id=chat_id, text=f"❌ Could not post the alert: {exc}", reply_markup=access_menu_kb())

    elif data.startswith("access_revoke_"):
        code = data[len("access_revoke_"):].strip().upper()
        c = db.get_redeem_code(code)
        if not c:
            await context.bot.send_message(chat_id=chat_id, text="❌ Code not found.", reply_markup=access_menu_kb())
        else:
            db.deactivate_code(code)
            db.log_activity("admin_bot", "revoke_code", code)
            await context.bot.send_message(chat_id=chat_id, text=f"🗑️ Redeem code `{code}` has been revoked.",
                                           parse_mode="Markdown", reply_markup=access_menu_kb())

    elif data.startswith("accessmenu_"):
        video_id = data[len("accessmenu_"):]
        v = db.get_video(video_id)
        if not v:
            await context.bot.send_message(chat_id=chat_id, text="Video not found.")
        else:
            labels = {"free":"🌍 Public", "ad":"📺 Ad unlock", "redeem":"🎟️ Redeem membership", "redeem_or_ad":"🎟️+📺 Redeem OR Ad", "members":"💎 Redeem membership only", "users":"👤 Specific users", "gated":"🎟️+📺 Redeem OR Ad"}
            text = f"🔐 *Access for* `{video_id}`\n\nCurrent: *{labels.get(v.get('access_tier','free'), v.get('access_tier','free'))}*"
            if v.get("access_redeem_code"): text += f"\nCode: `{db.md_escape(v['access_redeem_code'])}`"
            if v.get("access_user_ids"): text += f"\nUsers: {len([x for x in v['access_user_ids'].split(',') if x.strip()])}"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌍 Public", callback_data=f"aset_free_{video_id}"), InlineKeyboardButton("📺 Ad", callback_data=f"aset_ad_{video_id}")],
                [InlineKeyboardButton("🎟️ Redeem", callback_data=f"aset_redeem_{video_id}"), InlineKeyboardButton("🎟️+📺 OR", callback_data=f"aset_redeem_or_ad_{video_id}")],
                [InlineKeyboardButton("💎 Membership Only", callback_data=f"aset_members_{video_id}"), InlineKeyboardButton("👤 Specific Users", callback_data=f"aset_users_{video_id}")],
                [InlineKeyboardButton("🔙 Edit", callback_data=f"editmenu_{video_id}")],
            ])
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)

    elif data.startswith("aset_"):
        _, choice, video_id = data.split("_", 2)
        if choice in ("free", "ad", "members", "redeem_or_ad"):
            db.edit_video(video_id, access_tier=choice, access_redeem_code=None, access_user_ids=None)
            db.log_activity("admin_bot", "set_video_access", f"{video_id} -> {choice}")
            text, kb = await _edit_menu(video_id)
            await context.bot.send_message(chat_id=chat_id, text="✅ Access updated.\n\n" + text, parse_mode="Markdown", reply_markup=kb)
        elif choice == "redeem":
            context.user_data["awaiting_video_access_code"] = video_id
            await context.bot.send_message(chat_id=chat_id, text="🎟️ Send the active redeem code for this video.")
        elif choice == "users":
            context.user_data["awaiting_video_access_users"] = video_id
            await context.bot.send_message(chat_id=chat_id, text="👤 Send Telegram user IDs separated by commas.")

    elif data.startswith("togglegate_"):
        video_id = data[len("togglegate_"):]
        v = db.get_video(video_id)
        if not v:
            await context.bot.send_message(chat_id=chat_id, text="Video not found.")
        else:
            new_tier = "free" if v.get("access_tier") == "gated" else "gated"
            db.set_video_access_tier(video_id, new_tier)
            db.log_activity("admin_bot", "toggle_gate", f"{video_id} -> {new_tier}")
            text, kb = await _edit_menu(video_id)
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)

    elif data.startswith("retryback_"):
        video_id = data[len("retryback_"):]
        v = db.get_video(video_id)
        if not v:
            await context.bot.send_message(chat_id=chat_id, text="Video not found.")
        elif v.get("backup_msg_id"):
            await context.bot.send_message(chat_id=chat_id, text="✅ Already has a backup copy.")
        else:
            try:
                backup_msg = await context.bot.copy_message(
                    chat_id=storage_config.backup(),
                    from_chat_id=storage_config.primary(),
                    message_id=v["primary_msg_id"],
                )
                db.set_backup_msg_id(video_id, backup_msg.message_id)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    log.debug("Backup restore: reply markup was already unavailable")
                await context.bot.send_message(chat_id=chat_id, text=f"✅ Backup restored for `{video_id}`.",
                                                parse_mode="Markdown")
            except Exception as e:
                log.exception("Retry backup failed")
                await context.bot.send_message(chat_id=chat_id, text=f"❌ Retry failed: {e}")

    elif data.startswith("realert_"):
        video_id = data[len("realert_"):]
        v = db.get_video(video_id)
        if not v:
            await context.bot.send_message(chat_id=chat_id, text="Video not found.")
        else:
            context.user_data["alert_batch"] = [video_id]
            context.user_data["alert_batch_id"] = None
            context.user_data["alert_mode"] = "single"
            context.user_data.pop("custom_alert_caption", None)
            context.user_data.pop("custom_alert_cover_msg_id", None)
            await _post_alert_to(chat_id, context, [v], is_channel=False, user_data=context.user_data)
            confirm_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Post to Alert Channel", callback_data="alert_confirm"),
                    InlineKeyboardButton("❌ Cancel", callback_data="alert_cancel"),
                ],
                [
                    InlineKeyboardButton("✏️ Edit Message", callback_data="alert_edit_text"),
                    InlineKeyboardButton("🖼 Change Cover", callback_data="alert_edit_cover"),
                ],
            ])
            await context.bot.send_message(chat_id=chat_id, text="👆 Re-announce this video?",
                                            reply_markup=confirm_kb)

    elif data.startswith("batchedit_"):
        batch_id = data[len("batchedit_"):]
        await query.answer()
        await _send_collection_edit_menu(chat_id, context, batch_id)

    elif data.startswith("batcheditfield_"):
        _, batch_id, field = data.split("_", 2)
        if not db.get_batch(batch_id):
            await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found.")
            return
        context.user_data["editing_batch"] = {"batch_id": batch_id, "field": field}
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text=f"✍️ Send the new collection *{field}*.\n\nThis will update every item in the collection.", parse_mode="Markdown")

    elif data.startswith("batcheditcategory_"):
        batch_id = data[len("batcheditcategory_"):]
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text="📂 *Collection Category*\n\nChoose a category for the entire collection:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇮🇳 Indian", callback_data=f"batchsetcategory_Indian_{batch_id}"), InlineKeyboardButton("🌍 Global", callback_data=f"batchsetcategory_Global_{batch_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data=f"batchedit_{batch_id}")],
        ]))

    elif data.startswith("batchsetcategory_"):
        _, category, batch_id = data.split("_", 2)
        try:
            db.update_batch_fields(batch_id, category=db.normalize_category(category), subcategory=None)
            await query.answer("Collection category updated")
            await _send_collection_edit_menu(chat_id, context, batch_id)
        except Exception as exc:
            await query.answer("Update failed", show_alert=True)
            await context.bot.send_message(chat_id=chat_id, text=f"❌ {db.md_escape(str(exc))}")

    elif data.startswith("batcheditcover_"):
        batch_id = data[len("batcheditcover_"):]
        if not db.get_batch(batch_id):
            await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found.")
            return
        context.user_data["awaiting_batch_cover"] = batch_id
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text="🖼️ Send the new collection cover.\n\nIt will become the shared cover for *every item* in this collection.", parse_mode="Markdown")

    elif data.startswith("batchtoggle_"):
        batch_id = data[len("batchtoggle_"):]
        batch = db.get_batch(batch_id)
        if not batch:
            await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found.")
            return
        new_tier = "free" if (batch.get("access_tier") or "free") == "gated" else "gated"
        db.update_batch_fields(batch_id, access_tier=new_tier)
        await query.answer("Collection access updated")
        await _send_collection_edit_menu(chat_id, context, batch_id)

    elif data.startswith("batchschedule_"):
        batch_id = data[len("batchschedule_"):]
        if not db.get_batch(batch_id):
            await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found.")
            return
        context.user_data["awaiting_batch_schedule"] = batch_id
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text="⏰ Send the collection publish time.\n\nUse `YYYY-MM-DD HH:MM` or `+2h` / `+1d`.\nThis schedule will apply to *every item* in the collection.", parse_mode="Markdown")

    elif data.startswith("batchpublish_"):
        batch_id = data[len("batchpublish_"):]
        if not db.get_batch(batch_id):
            await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found.")
            return
        db.update_batch_fields(batch_id, publish_at=None)
        await query.answer("Collection published")
        await _send_collection_edit_menu(chat_id, context, batch_id)

    elif data.startswith("batchunschedule_"):
        batch_id = data[len("batchunschedule_"):]
        if not db.get_batch(batch_id):
            await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found.")
            return
        db.update_batch_fields(batch_id, publish_at=None)
        await query.answer("Schedule cancelled")
        await _send_collection_edit_menu(chat_id, context, batch_id)

    elif data.startswith("batchaccessmenu_"):
        batch_id = data[len("batchaccessmenu_"):]
        batch = db.get_batch(batch_id)
        if not batch:
            await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found.")
            return
        labels = {"free":"🌍 Public", "ad":"📺 Ad unlock", "redeem":"🎟️ Redeem membership", "redeem_or_ad":"🎟️+📺 Redeem OR Ad", "members":"💎 Membership Only", "users":"👤 Specific Users", "gated":"🎟️+📺 Redeem OR Ad"}
        text = f"🔐 *Collection Access*\n\nCurrent: *{labels.get(batch.get('access_tier','free'), batch.get('access_tier','free'))}*\n\nChoose an access mode for *every item* in this collection."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌍 Public", callback_data=f"batchaset_free_{batch_id}"), InlineKeyboardButton("📺 Ad", callback_data=f"batchaset_ad_{batch_id}")],
            [InlineKeyboardButton("🎟️ Redeem", callback_data=f"batchaset_redeem_{batch_id}"), InlineKeyboardButton("🎟️+📺 OR", callback_data=f"batchaset_redeem_or_ad_{batch_id}")],
            [InlineKeyboardButton("💎 Membership Only", callback_data=f"batchaset_members_{batch_id}"), InlineKeyboardButton("👤 Specific Users", callback_data=f"batchaset_users_{batch_id}")],
            [InlineKeyboardButton("🔙 Edit Collection", callback_data=f"batchedit_{batch_id}")],
        ])
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)

    elif data.startswith("batchaset_"):
        _, choice, batch_id = data.split("_", 2)
        if not db.get_batch(batch_id):
            await context.bot.send_message(chat_id=chat_id, text="❌ Collection not found.")
            return
        if choice in ("free", "ad", "members", "redeem_or_ad"):
            db.update_batch_fields(batch_id, access_tier=choice, access_redeem_code=None, access_user_ids=None)
            await query.answer("Collection access updated")
            await _send_collection_edit_menu(chat_id, context, batch_id)
        elif choice == "redeem":
            context.user_data["awaiting_batch_access_code"] = batch_id
            await query.answer()
            await context.bot.send_message(chat_id=chat_id, text="🎟️ Send the active redeem code for this collection.\n\nIt will apply to every item in the collection.")
        elif choice == "users":
            context.user_data["awaiting_batch_access_users"] = batch_id
            await query.answer()
            await context.bot.send_message(chat_id=chat_id, text="👤 Send Telegram user IDs separated by commas.\n\nThe restriction will apply to every item in the collection.")

    elif data.startswith("editmenu_"):
        video_id = data[len("editmenu_"):]
        await _send_edit_menu(chat_id, context, video_id)

    elif data.startswith("editcategory_"):
        video_id = data[len("editcategory_"):].strip()
        v = db.get_video(video_id)
        if not v:
            await context.bot.send_message(chat_id=chat_id, text="❌ Video not found.")
            return
        current = db.normalize_category(v.get("category"))
        await context.bot.send_message(chat_id=chat_id, text=f"📂 *Category*\n\nCurrent: *{current}*\n\nChoose:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇮🇳 Indian", callback_data=f"setcategory_Indian_{video_id}"), InlineKeyboardButton("🌍 Global", callback_data=f"setcategory_Global_{video_id}")],
            [InlineKeyboardButton("🔙 Back to Edit", callback_data=f"editmenu_{video_id}")],
        ]))

    elif data.startswith("setcategory_"):
        _, category, video_id = data.split("_", 2)
        try:
            before = db.get_video(video_id) or {}
            db.set_video_category(video_id, category, None, sync_batch=True)
            db.log_activity("admin_bot", "set_category", f"{video_id} -> {db.normalize_category(category)}")
            text, kb = await _edit_menu(video_id)
            note = " Shared collection updated too." if before.get("batch_id") else ""
            await context.bot.send_message(chat_id=chat_id, text=f"✅ Category updated to *{db.normalize_category(category)}*.{note}", parse_mode="Markdown", reply_markup=kb)
        except Exception as exc:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Could not update category: {db.md_escape(str(exc))}", parse_mode="Markdown")

    elif data.startswith("editcover_"):
        video_id = data[len("editcover_"):].strip()
        if not db.get_video(video_id):
            await context.bot.send_message(chat_id=chat_id, text="❌ Video not found.")
        else:
            context.user_data["awaiting_video_cover"] = video_id
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"🖼️ *Change Cover* for `{video_id}`\n\n"
                      "Send a photo now. It will become the new cover for this video.\n"
                      "The existing title, tags, description, access and schedule will stay unchanged.\n\n"
                      "Send `cancel` to keep the current cover."),
                parse_mode="Markdown",
            )

    elif data.startswith("editfield_"):
        _, video_id, field = data.split("_", 2)
        context.user_data["editing"] = {"video_id": video_id, "field": field}
        await context.bot.send_message(chat_id=chat_id, text=f"✍️ Send the new *{field}* for `{video_id}`.",
                                        parse_mode="Markdown")

    elif data.startswith("delconfirm_"):
        video_id = data[len("delconfirm_"):]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ Confirm Delete", callback_data=f"deldo_{video_id}"),
            InlineKeyboardButton("Cancel", callback_data="menu_home"),
        ]])
        await context.bot.send_message(chat_id=chat_id, text=f"Delete `{video_id}` from the catalog?",
                                        parse_mode="Markdown", reply_markup=kb)

    elif data.startswith("deldo_"):
        video_id = data[len("deldo_"):]
        db.delete_video(video_id)
        db.log_activity("admin_bot", "delete", video_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🗑 Removed `{video_id}` from the catalog.\n(Channel files were NOT deleted.)",
            parse_mode="Markdown", reply_markup=main_menu_kb(),
        )

    elif data.startswith("bulkdeldo_"):
        ids = data[len("bulkdeldo_"):].split(",")
        db.bulk_delete(ids)
        db.log_activity("admin_bot", "bulk_delete", f"{len(ids)} video(s)")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🗑 Removed {len(ids)} video(s) from the catalog.\n(Channel files were NOT deleted.)",
            reply_markup=main_menu_kb(),
        )


async def control_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = await _control_center_text(context)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="menu_control"),
         InlineKeyboardButton("📡 Channel Hub", callback_data="menu_channels")],
        [InlineKeyboardButton("🧰 Diagnostics", callback_data="menu_diagnostics"),
         InlineKeyboardButton("💾 Backup", callback_data="menu_backupdb")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")],
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Global fallback. Deliberately NEVER replies into update.effective_chat —
    for a channel-post update (received because we're a channel admin) that
    chat IS the channel, and replying there can re-trigger the same crash,
    creating an infinite spam loop. Only ever notify the admin's private chat."""
    if boterror.is_transient_network_error(context.error):
        log.warning(f"Transient network hiccup (self-recovers): {context.error!r}")
        return
    log.error("Unhandled exception", exc_info=context.error)
    try:
        if config.ADMIN_USER_IDS:
            await context.bot.send_message(chat_id=config.ADMIN_USER_IDS[0], text=f"⚠️ admin_bot error: {context.error}")
    except Exception:
        # Safe no-op: this secondary cleanup/notification failure must not mask the primary operation.
        pass


def main():
    # Python 3.14 removed automatic event-loop creation (PEP 719); some versions
    # of python-telegram-bot still expect it, so create/set one manually here.
    import asyncio
    # Python 3.13+: avoid deprecated get_event_loop() probing.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    db.init_db()
    import hashlib
    token_key = hashlib.sha256(str(config.ADMIN_BOT_TOKEN or "").encode()).hexdigest()[:24]
    instance_lock = botutil.acquire_single_instance(f"polling_token_{token_key}")
    if instance_lock is None:
        return
    app = Application.builder().token(config.ADMIN_BOT_TOKEN).post_init(_startup_check).build()

    handlers = [
        CommandHandler("start", start_cmd), CommandHandler("help", start_cmd),
        CommandHandler("menu", menu_cmd), CommandHandler("dashboard", menu_cmd), CommandHandler("queue", queue_cmd),
        CommandHandler("postalert", postalert_cmd), CommandHandler("list", list_cmd),
        CommandHandler("edit", edit_cmd), CommandHandler("delete", delete_cmd),
        CommandHandler("control", control_cmd), CommandHandler("stats", stats_cmd),
        CommandHandler("analytics", analytics_cmd), CommandHandler("verify", verify_cmd),
        CommandHandler("top", top_cmd), CommandHandler("bydate", bydate_cmd),
        CommandHandler("health", health_cmd), CommandHandler("ping", ping_cmd),
        CommandHandler("bulkdelete", bulkdelete_cmd), CommandHandler("bulktag", bulktag_cmd),
        CommandHandler("export", export_cmd), CommandHandler("backupdb", backupdb_cmd),
        CommandHandler("setbackup", setbackup_cmd), CommandHandler("backupnow", backupnow_cmd),
        CommandHandler("log", log_cmd), CommandHandler("scheduler", scheduler_cmd),
        CommandHandler("weekly", weekly_cmd), CommandHandler("weeklyremove", weeklyremove_cmd), CommandHandler("weeklyclear", weeklyclear_cmd), CommandHandler("weeklytoggle", weeklytoggle_cmd),
        CommandHandler("channels", channels_cmd), CommandHandler("diagnostics", diagnostics_cmd),
        CommandHandler("schedule", schedule_cmd),
        CommandHandler("scheduled", scheduled_cmd), CommandHandler("unschedule", unschedule_cmd),
        CommandHandler("setautoalert", setautoalert_cmd), CommandHandler("clearautoalert", clearautoalert_cmd),
        CommandHandler("access", access_cmd), CommandHandler("setdefaultlimit", setdefaultlimit_cmd),
        CommandHandler("setlimit", setlimit_cmd), CommandHandler("gate", gate_cmd),
        CommandHandler("ungate", ungate_cmd), CommandHandler("gencode", gencode_cmd),
        CommandHandler("gengiveaway", gengiveaway_cmd), CommandHandler("codes", codes_cmd),
        CommandHandler("revokecode", revokecode_cmd),
        CommandHandler("setdefaultalertcover", setdefaultalertcover_cmd),
        CommandHandler("cleardefaultalertcover", cleardefaultalertcover_cmd),
        CommandHandler("tempbots", tempbots_cmd),
        CommandHandler("permbots", permbots_cmd),
    ]
    for h in handlers:
        app.add_handler(h)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(mandatory_join_admin_callback, pattern=r"^mandatory_join_admin$"))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_error_handler(error_handler)

    log.info("Admin bot starting...")
    botutil.run_polling_resilient(app, "admin_bot")

if __name__ == "__main__":
    main()


