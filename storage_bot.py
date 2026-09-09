# V6 exception triage: intentionally swallowed exceptions in this module
# are limited to best-effort cleanup/compatibility fallbacks; user-visible or
# persistence failures are logged or surfaced by their surrounding handlers.
import os
"""
Storage Bot
-----------
Flow:
  1. Admin sends a video directly to this bot.
  2. Bot copies it into PRIMARY_CHANNEL and BACKUP_CHANNEL (two independent copies).
  3. Bot then asks, step by step: cover image -> title -> tags -> description.
     Every step has a ❌ Cancel button; Tags/Description also have ⏭ Skip.
  4. Before saving, shows a full preview card with buttons to edit any field
     (Title / Tags / Description / Cover) plus Confirm / Discard.
  5. On confirm, the video + metadata is saved to the database, ready for the next
     `/postalert` in the Admin Bot.

Only users listed in config.ADMIN_USER_IDS may use this bot.
"""
import logging
import asyncio
import functools
import random
import re
import shutil
import subprocess
import tempfile
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, MessageHandler, CommandHandler, CallbackQueryHandler,
    ContextTypes, filters
)

import config
import storage_config
import db
import boterror
import botutil
import admin_store
import permanent_bot_store

try:
    from PIL import Image, ImageFilter, ImageStat
except Exception:
    Image = ImageFilter = ImageStat = None

logging.basicConfig(level=logging.INFO)
from bgtasks import spawn
log = logging.getLogger("storage_bot")

# Per-user lock: prevents two near-simultaneous updates from the same admin
# (e.g. a video delivered twice by Telegram, or a double-tap on a button)
# from both starting an upload before either has "claimed" it. Without this,
# both could pass the "already in progress?" check and each copy the video
# into Primary/Backup independently — the exact duplicate-post bug this fixes.
_user_locks: dict[int, asyncio.Lock] = {}
# Per-video serialization: replacement and mapping repair must never race on the
# same catalogue item, even when two admins act at the same time.
_video_locks: dict[str, asyncio.Lock] = {}


def _video_lock_for(video_id: str) -> asyncio.Lock:
    key = str(video_id)
    lock = _video_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _video_locks[key] = lock
    return lock


def _per_video_serialized(fn):
    @functools.wraps(fn)
    async def wrapper(context, user_id: int, video_id: str, *args, **kwargs):
        async with _video_lock_for(video_id):
            return await fn(context, user_id, video_id, *args, **kwargs)
    return wrapper



# --- Telegram Web App / Mini App configuration ---
WEBAPP_URL = os.getenv("TELEGRAM_WEBAPP_URL", "https://stellar-speculoos-1d5c7e.netlify.app/").strip()

def _webapp_button():
    if not WEBAPP_URL.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        return None
    return InlineKeyboardButton("🌐 Open Web App", web_app=WebAppInfo(url=WEBAPP_URL))
# --- END Telegram Web App configuration ---

def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


STAGE_LABELS = {
    "awaiting_cover": "Step 2/4 · Cover image",
    "awaiting_title": "Step 3/4 · Title",
    "awaiting_tags": "Step 3/4 · Tags",
    "awaiting_description": "Step 4/4 · Description",
    "awaiting_schedule": "Setting schedule time",
}

CANCEL_BTN = InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")


check_channel_access = botutil.check_channel_access


async def _startup_check(app):
    log.info("Checking channel access...")
    results = await check_channel_access(app.bot, [
        ("Primary Channel", storage_config.primary()),
        ("Backup Channel", storage_config.backup()),
    ])
    # Do not block the Storage Bot when one storage channel is unavailable.
    # Recovery must remain reachable so an admin can migrate from the surviving backup.
    if results and all(status == "❌ No access" for _, _, status in results):
        log.error("Neither configured storage channel is accessible. Recovery UI remains available, but media operations will fail until a valid channel is configured.")
    await botutil.configure_bot_ui(app, "storage")


def fmt_duration(seconds) -> str:
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(num_bytes) -> str:
    if not num_bytes:
        return "—"
    mb = num_bytes / (1024 * 1024)
    return f"{mb:.1f} MB" if mb < 1024 else f"{mb/1024:.2f} GB"


def is_admin(user_id: int) -> bool:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid in admin_store.admin_ids(config.ADMIN_USER_IDS)


async def guard(update: Update) -> bool:
    # Channel posts (from being a channel admin) have no effective_user and
    # aren't private chats — ignore them silently. Without this check, calling
    # .id on a None user crashes, and if an error handler replies into that
    # same channel, it can trigger the same crash again — an infinite loop.
    if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        return False
    if not is_admin(update.effective_user.id):
        if update.message:
            await update.message.reply_text("🚫 You're not authorized to use this bot.")
        return False
    return True


def _storage_start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Upload Video", callback_data="storage_menu_upload"),
            InlineKeyboardButton("📦 Bulk Upload", callback_data="storage_menu_bulk"),
        ],
        [
            InlineKeyboardButton("⚙️ Defaults", callback_data="storage_menu_defaults"),
            InlineKeyboardButton("📊 Status", callback_data="storage_menu_status"),
        ],
        [InlineKeyboardButton("🛡️ Storage Recovery", callback_data="storage_menu_recovery")],
        [InlineKeyboardButton("🩺 Storage Health", callback_data="storage_menu_health")],
        [InlineKeyboardButton("📊 Content Overview", callback_data="storage_menu_content"),
         InlineKeyboardButton("📈 Daily Analysis", callback_data="storage_menu_daily")],
        [InlineKeyboardButton("🧰 Repair Specific Video", callback_data="storage_repair_video")],
        [
            InlineKeyboardButton("🛠 Commands", callback_data="storage_menu_commands"),
            InlineKeyboardButton("🔄 Refresh", callback_data="storage_menu_refresh"),
        ],
        [InlineKeyboardButton("❌ Cancel Current", callback_data="storage_menu_cancel")],
    ] + ([[_webapp_button()]] if _webapp_button() else []))


def _storage_start_text() -> str:
    try:
        c = db.content_dashboard()
        summary = (
            f"📚 *{c['total_videos']:,} videos* · 📦 *{c['collections']:,} collections* · "
            f"⏰ *{c['scheduled_videos']:,} scheduled videos* · *{c['scheduled_collections']:,} scheduled collections*"
        )
    except Exception:
        summary = "📊 Content overview temporarily unavailable."
    return (
        admin_store.get_template(
            "storage_start",
            "📥 Storage Bot\n\nSend me a video to store it. "
            "I will copy it to the primary and backup channels, then guide you through the upload details.",
        )
        + f"\n\n{summary}\n\n👇 Choose an action:"
    )


def _reset_upload_session(context):
    """Clear only upload-wizard state; keep harmless preferences intact."""
    keys = (
        "bulk_mode", "bulk_items", "bulk_stage", "bulk_progress_msg_id",
        "suggest_bulk_media", "edit_batch_id", "awaiting_batch_cover",
        "bulk_cover_file_id", "bulk_cover_msg_id", "bulk_cover_source_chat_id",
        "bulk_cover_source_message_id", "bulk_title", "bulk_tags",
        "bulk_description", "bulk_scheduled_at", "bulk_access_tier",
        "bulk_access_redeem_code", "bulk_access_user_ids", "bulk_start_number", "bulk_category", "bulk_subcategory",
        "awaiting_default_cover", "awaiting_add_default_cover",
        "awaiting_frame_shots", "frame_shot_options", "frame_shot_source",
        "frame_shot_count", "frame_shot_interval",
        "awaiting_access_input", "awaiting_repair_video", "replacement_video_id", "replacement_processing",
        "force_single_next",
        "last_saved_video_id", "last_saved_at",
    )
    for key in keys:
        context.user_data.pop(key, None)


def _media_counts(items):
    videos = sum(1 for item in items if item.get("media_type") == "video")
    photos = sum(1 for item in items if item.get("media_type") == "photo")
    return videos, photos


def _media_summary(items):
    videos, photos = _media_counts(items)
    parts = []
    if videos:
        parts.append(f"{videos} video" + ("s" if videos != 1 else ""))
    if photos:
        parts.append(f"{photos} image" + ("s" if photos != 1 else ""))
    return " + ".join(parts) or "0 media"


BULK_STATE_KEYS = (
    "bulk_mode", "bulk_items", "bulk_stage", "bulk_cover_file_id", "bulk_cover_msg_id",
    "bulk_cover_source_chat_id", "bulk_cover_source_message_id", "bulk_title", "bulk_tags",
    "bulk_description", "bulk_scheduled_at", "bulk_access_tier", "bulk_access_redeem_code",
    "bulk_access_user_ids", "bulk_start_number", "bulk_category", "bulk_subcategory",
)

def _bulk_state(context):
    state = {}
    for key in BULK_STATE_KEYS:
        value = context.user_data.get(key)
        if value is not None:
            state[key] = value
    return state

def _persist_bulk(context, user_id: int):
    if context.user_data.get("bulk_mode"):
        db.save_bulk_session(user_id, _bulk_state(context))
    else:
        db.clear_bulk_session(user_id)

def _restore_bulk(context, user_id: int) -> bool:
    state = db.get_bulk_session(user_id)
    if not state or not state.get("bulk_mode"):
        return False
    for key, value in state.items():
        context.user_data[key] = value
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    restored = _restore_bulk(context, update.effective_user.id)
    if restored:
        items = context.user_data.get("bulk_items") or []
        stage = context.user_data.get("bulk_stage") or "collecting"
        await update.message.reply_text(
            f"♻️ *Draft restored.*\n\n📦 Bulk collection: *{len(items)} item(s)* · {_media_summary(items)}\n🧭 Stage: `{stage}`\n\n"
            "Nothing was discarded. Use *Resume Bulk* below or /cancel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Resume Bulk", callback_data="storage_resume_bulk")],
                [InlineKeyboardButton("🗑 Discard Draft", callback_data="storage_menu_cancel")],
            ]),
        )
        return
    pending = db.get_pending(update.effective_user.id)
    if pending and pending.get("stage") == "collecting":
        _schedule_single_wait(context, update.effective_user.id)
        await update.message.reply_text("♻️ A media upload is still waiting for the single/bulk decision. I’ll continue watching briefly for another media.")
        return
    await update.message.reply_text(_storage_start_text(), reply_markup=_storage_start_kb())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    _cancel_single_wait(update.effective_user.id)
    _reset_upload_session(context)
    db.clear_pending(update.effective_user.id)
    db.clear_bulk_session(update.effective_user.id)
    await update.message.reply_text("🗑 *Flow cleared.* No media was added to the catalog.\n\nSend one media for Single mode, or use /bulk for a collection. ✨", parse_mode="Markdown")



async def bulk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if db.get_pending(update.effective_user.id):
        await update.message.reply_text("⚠️ Finish or /cancel the current single-video upload first.")
        return
    context.user_data["bulk_mode"] = True
    context.user_data["bulk_items"] = []
    context.user_data["bulk_category"] = "Global"
    context.user_data["bulk_subcategory"] = None
    context.user_data.pop("bulk_stage", None)
    _persist_bulk(context, update.effective_user.id)
    await update.message.reply_text(
        "📦 *Bulk upload mode started.*\n\n"
        "Send as many videos as you want, one after another. Each is copied to Primary + Backup.\n"
        "When you're done, send /bulkdone.\n"
        "Then you'll choose ONE cover source, then enter ONE title, tags and description for the whole batch.\n\n"
        "❌ /cancel clears this batch.", parse_mode="Markdown"
    )


async def single_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explicitly force the next upload to start as a single item."""
    if not await guard(update):
        return
    if db.get_pending(update.effective_user.id) or context.user_data.get("bulk_mode"):
        await update.message.reply_text("⚠️ A media flow is already active. Finish it or /cancel first.")
        return
    context.user_data["force_single_next"] = True
    await update.message.reply_text(
        "🎬 *Single mode armed.*\n\n"
        "Send exactly one video/image. I'll ask for its cover next.\n\n"
        "🖼️ The next separate cover image is treated as the cover — not as bulk.",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[CANCEL_BTN]])
    )


async def flow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        "🧭 *Smart Upload Flow*\n\n"
        "🎬 *One video/image* → Single item → Cover → Details → Preview → Save\n"
        "📦 *Telegram album / explicit /bulk* → Collection → Shared cover → Details → Preview → Save\n\n"
        "💡 A normal cover sent after a single item is NOT counted as bulk.\n"
        "🛑 Use /cancel anytime.\n\n"
        "Shortcuts: /single · /bulk · /status · /cancel",
        parse_mode="Markdown", reply_markup=_storage_start_kb()
    )


async def bulkdone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    items = context.user_data.get("bulk_items") or []
    if not context.user_data.get("bulk_mode"):
        await update.message.reply_text("No bulk upload is active. Use /bulk first.")
        return
    if not items:
        await update.message.reply_text("⚠️ No videos added yet. Send videos first, then /bulkdone.")
        return
    context.user_data["bulk_stage"] = "cover"
    _persist_bulk(context, update.effective_user.id)
    text, kb = _cover_prompt_text(bulk=True)
    await update.message.reply_text(f"✅ {len(items)} item(s) collected.\n\n{text}", parse_mode="Markdown", reply_markup=kb)


async def _bulk_finish_or_prompt_title(update, context):
    items = context.user_data.get("bulk_items") or []
    if not items:
        return
    context.user_data["bulk_stage"] = "title"
    await update.message.reply_text("✍️ Now send ONE title for the whole batch.")


async def editbulk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Modify an already-saved bulk collection's shared cover."""
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text(
            "🛠 *Edit Bulk Cover*\n\nUsage: `/editbulk BATCH_ID`\n\n"
            "Choose a default, random default, or manual cover.",
            parse_mode="Markdown",
        )
        return
    batch_id = context.args[0].strip()
    batch = db.get_batch(batch_id)
    if not batch:
        await update.message.reply_text("❌ Bulk batch not found. Check the Batch ID and try again.")
        return
    context.user_data["edit_batch_id"] = batch_id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Use Default Cover", callback_data="editbatch_cover_default")],
        [InlineKeyboardButton("🎲 Random Default Cover", callback_data="editbatch_cover_random")],
        [InlineKeyboardButton("✋ Upload Manual Cover", callback_data="editbatch_cover_manual")],
        [CANCEL_BTN],
    ])
    count = db.get_batch_count(batch_id)
    await update.message.reply_text(
        f"🛠 *Edit Bulk Cover*\n\n📦 `{batch_id}` · {count} item(s)\n\n"
        "Choose the new shared cover for this collection.",
        parse_mode="Markdown", reply_markup=kb,
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if context.user_data.get("bulk_mode"):
        n = len(context.user_data.get("bulk_items") or [])
        await update.message.reply_text(f"📦 Bulk upload: {n} item(s) collected — {sum(1 for i in (context.user_data.get('bulk_items') or []) if i.get('media_type') == 'video')} video(s) + {sum(1 for i in (context.user_data.get('bulk_items') or []) if i.get('media_type') == 'photo')} image(s).")
        return
    if context.user_data.get("replacement_processing"):
        vid = context.user_data.get("replacement_video_id") or "saved video"
        await update.message.reply_text(f"♻️ Replacement is currently processing for `{vid}`. Please wait for the result or /cancel.", parse_mode="Markdown")
        return
    pending = db.get_pending(update.effective_user.id)
    if not pending:
        await update.message.reply_text("Nothing in progress. Send a video to start.")
        return
    label = STAGE_LABELS.get(pending["stage"], pending["stage"])
    await update.message.reply_text(f"⏳ In progress: *{label}*", parse_mode="Markdown")


async def storage_health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        await _storage_health_text(context), parse_mode="Markdown", reply_markup=_storage_start_kb()
    )


# ---------- default cover / title (applied automatically to every new upload) ----------

def _cover_choice_kb(prefix="cover"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Use Default Cover", callback_data=f"{prefix}_default"),
         InlineKeyboardButton("🎲 Random Default", callback_data=f"{prefix}_random")],
        [InlineKeyboardButton("🧠 Smart Best Frame", callback_data=f"{prefix}_frame_smart"),
         InlineKeyboardButton("🎞️ Random Video Frame", callback_data=f"{prefix}_frame_random")],
        [InlineKeyboardButton("🎬 Pick Video Shots", callback_data=f"{prefix}_frame_shots")],
        [InlineKeyboardButton("✋ Upload Manual Cover", callback_data=f"{prefix}_manual")],
        [CANCEL_BTN],
    ])


def _cover_prompt_text(bulk=False):
    return (
        ("📸 *Batch cover* — choose a cover source." if bulk else "📸 *Step 2/4 · Cover* — choose a cover source."),
        _cover_choice_kb("bulkcover" if bulk else "cover")
    )

async def defaults_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cover_set = bool(db.get_setting("default_cover_msg_id"))
    title_template = db.get_setting("default_title_template")
    await update.message.reply_text(
        "⚙️ *Upload Defaults*\n\n"
        f"Default cover: {'✅ set' if cover_set else '— not set'}\n"
        f"Random cover pool: {len(db.get_default_cover_pool())} cover(s)\n"
        f"Default title template: {title_template or '— not set'}\n"
        "Video-frame cover tools: ✅ available during every new video upload\n\n"
        "/setdefaultcover — replace the default cover with one photo\n"
        "/adddefaultcover — add a photo to the random-cover pool\n"
        "/cleardefaultcover — remove the default cover\n"
        "/cleardefaultcovers — clear the random-cover pool\n"
        "/setdefaulttitle <template> — e.g. `New Upload — {date}`\n"
        "/cleardefaulttitle — remove it\n\n"
        "Either one, once set, is applied automatically to every new upload — "
        "still editable per-video from the preview screen before saving.",
        parse_mode="Markdown",
    )


async def setdefaultcover_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data["awaiting_default_cover"] = True
    await update.message.reply_text("🖼 Send the photo to use as the default cover for every future upload.")


async def cleardefaultcover_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    db.clear_setting("default_cover_file_id")
    db.clear_setting("default_cover_msg_id")
    await update.message.reply_text("🗑 Default cover cleared.")


async def adddefaultcover_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data["awaiting_add_default_cover"] = True
    await update.message.reply_text("🎲 Send a photo to add it to the random default-cover pool.")


async def cleardefaultcovers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    db.clear_default_cover_pool()
    await update.message.reply_text("🗑 Random default-cover pool cleared.")


async def setdefaulttitle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/setdefaulttitle New Upload — {date}`\n`{date}` is replaced with today's date.",
            parse_mode="Markdown",
        )
        return
    template = " ".join(context.args)
    db.set_setting("default_title_template", template)
    await update.message.reply_text(f"✅ Default title template set: {template}")


async def cleardefaulttitle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    db.clear_setting("default_title_template")
    await update.message.reply_text("🗑 Default title template cleared.")


def _apply_defaults(user_id: int) -> str:
    """Apply the title default, but leave cover selection explicit.

    The uploader can now choose Manual, Default, or Random Default cover instead
    of silently inheriting one cover. The catalog still has its own fallback
    logic when a saved item has no cover.
    """
    title_template = db.get_setting("default_title_template")
    updates = {"stage": "awaiting_cover"}
    if title_template:
        updates["title"] = title_template.replace("{date}", db.today_str())
    db.update_pending(user_id, **updates)
    return "awaiting_cover"


def _step_prompt_for(stage: str):
    """Returns (message_text, keyboard) for whichever step an upload lands on
    after defaults are applied — cover, title, or tags may all be pre-filled."""
    if stage == "awaiting_cover":
        return "📸 *Step 2/4 · Cover* — choose a cover source.", _cover_choice_kb("cover")
    if stage == "awaiting_title":
        return ("✍️ *Step 3/4 · Title* — send the title of the video. (Using your default cover.)",
                InlineKeyboardMarkup([[CANCEL_BTN]]))
    return ("🏷 *Step 3/4 · Tags* — send comma-separated tags, or tap Skip. "
            "(Using your default cover + title.)", _tags_kb())


async def _auto_convert_pending_to_bulk(update, context, pending, media_info):
    """Convert an in-progress single upload into bulk when the next media is
    part of the same Telegram album/media group.  The pending row is only
    cleared after all source identifiers needed for the first item are known.
    """
    user_id = update.effective_user.id
    first = {
        "source_chat_id": pending.get("source_chat_id"),
        "source_message_id": pending.get("source_message_id"),
        "video_file_id": pending.get("video_file_id") if (pending.get("media_type") or "video") == "video" else None,
        "duration_seconds": pending.get("duration_seconds"),
        "file_size_bytes": pending.get("file_size_bytes"),
        "media_type": pending.get("media_type") or "video",
        "media_group_id": pending.get("media_group_id"),
    }
    if not first["source_chat_id"] or not first["source_message_id"]:
        # Do not destroy the pending single upload.  Older DB rows may have
        # been created before source_message_id was introduced.
        await update.message.reply_text(
            "⚠️ I found another media from the same album, but the first upload "
            "doesn't have its original source message saved. Please /cancel and "
            "re-upload the album so I can detect it as a bulk collection safely."
        )
        return False

    db.clear_pending(user_id)
    context.user_data["bulk_mode"] = True
    context.user_data["bulk_items"] = [first, media_info]
    context.user_data["bulk_category"] = "Global"
    context.user_data["bulk_stage"] = None
    _persist_bulk(context, user_id)
    msg = await context.bot.send_message(
        chat_id=user_id,
        text=(
            "📦 *Multiple media detected — Bulk mode started.*\n\n"
            "The media are being kept as one collection. Send any remaining "
            "photos/videos, then tap/use `/bulkdone` to move to the shared cover.\n\n"
            "🖼️ The next cover will NOT be counted as a collection item."
        ),
        parse_mode="Markdown",
    )
    context.user_data["bulk_progress_msg_id"] = msg.message_id
    return True


def _photo_media_info(message, group_id=None):
    item = message.photo[-1] if message.photo else message.document
    return {
        "source_chat_id": message.chat_id,
        "source_message_id": message.message_id,
        "duration_seconds": None,
        "file_size_bytes": getattr(item, "file_size", None),
        "media_type": "photo",
        "media_group_id": group_id if group_id is not None else getattr(message, "media_group_id", None),
    }


def _video_media_info(message, video, group_id=None):
    return {
        "source_chat_id": message.chat_id,
        "source_message_id": message.message_id,
        "video_file_id": video.file_id,
        "duration_seconds": getattr(video, "duration", None),
        "file_size_bytes": getattr(video, "file_size", None),
        "media_type": "video",
        "media_group_id": group_id if group_id is not None else getattr(message, "media_group_id", None),
    }


async def _begin_single_after_wait(context, user_id: int):
    """Wait briefly for a second media before committing to single mode."""
    try:
        await asyncio.sleep(SINGLE_MEDIA_WAIT_SECONDS)
        async with _lock_for(user_id):
            pending = db.get_pending(user_id)
            if not pending or pending.get("stage") != "collecting" or context.user_data.get("bulk_mode"):
                return
            next_stage = _apply_defaults(user_id)
            step_text, step_kb = _step_prompt_for(next_stage)
            await context.bot.send_message(chat_id=user_id, text="🎬 No additional media arrived, so I’m continuing with this as a *single* upload.", parse_mode="Markdown")
            await context.bot.send_message(chat_id=user_id, text=step_text, parse_mode="Markdown", reply_markup=step_kb)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Single-media wait/finalization failed for user %s", user_id)
        try:
            await context.bot.send_message(chat_id=user_id, text="❌ I couldn't continue the upload flow. Please send the media again or use /cancel.")
        except Exception:
            log.exception("Could not report single-media wait failure")
    finally:
        _single_wait_tasks.pop(user_id, None)


def _schedule_single_wait(context, user_id: int):
    old = _single_wait_tasks.get(user_id)
    if old and not old.done():
        old.cancel()
    _single_wait_tasks[user_id] = spawn(_begin_single_after_wait(context, user_id), name=f"single-wait-{user_id}")


def _cancel_single_wait(user_id: int):
    task = _single_wait_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


async def _convert_pending_media_to_bulk(update, context, pending, media_info):
    user_id = update.effective_user.id
    _cancel_single_wait(user_id)
    first = {
        "source_chat_id": pending.get("source_chat_id"),
        "source_message_id": pending.get("source_message_id"),
        "duration_seconds": pending.get("duration_seconds"),
        "file_size_bytes": pending.get("file_size_bytes"),
        "media_type": pending.get("media_type") or "video",
        "media_group_id": pending.get("media_group_id"),
    }
    if not first["source_chat_id"] or not first["source_message_id"]:
        raise ValueError("The first upload is missing its source message. Please re-upload the media.")
    db.clear_pending(user_id)
    context.user_data["bulk_mode"] = True
    context.user_data["bulk_items"] = [first, media_info]
    context.user_data["bulk_category"] = "Global"
    context.user_data["bulk_stage"] = None
    _persist_bulk(context, user_id)
    msg = await context.bot.send_message(
        chat_id=user_id,
        text=(f"📦 *Bulk detected* — 2 media collected.\n\n"
              "Keep sending images/videos. When you're finished, tap *🏁 Bulk Done*.\n"
              "One cover + title + tags + description will be applied to the whole batch.\n\n"
              "Nothing is saved until you confirm."),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏁 Bulk Done", callback_data="storage_menu_bulkdone")], [CANCEL_BTN]]),
    )
    context.user_data["bulk_progress_msg_id"] = msg.message_id


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatch every Telegram media representation to the correct handler.

    Telegram can deliver an uploaded image/video either as native media or as
    a document depending on the client/upload path.  Keeping separate filters
    for VIDEO/PHOTO and Document.VIDEO/IMAGE made mixed rapid bulk uploads
    vulnerable to MIME/type classification differences.  Document.ALL is used
    only as a catch-all here; non-image/video documents are ignored.
    """
    msg = update.message
    if not msg:
        return

    if msg.video is not None:
        await handle_video(update, context)
        return

    if msg.photo:
        await handle_photo(update, context)
        return

    document = msg.document
    if document is None:
        return

    mime = (getattr(document, "mime_type", None) or "").lower()
    if mime.startswith("video/"):
        await handle_video(update, context)
    elif mime.startswith("image/"):
        await handle_photo(update, context)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    user_id = update.effective_user.id

    async with _lock_for(user_id):
        video = update.message.video or update.message.document
        if not video:
            return
        incoming_group = getattr(update.message, "media_group_id", None)

        # Replacement mode is checked before normal Single/Bulk handling so the
        # replacement media can never become a new catalogue item.
        replacement_id = context.user_data.get("replacement_video_id")
        if replacement_id:
            if context.user_data.get("replacement_processing"):
                await update.message.reply_text("⏳ A replacement is already being processed. Please wait.")
                return
            if db.get_pending(user_id) or context.user_data.get("bulk_mode"):
                context.user_data.pop("replacement_video_id", None)
                await update.message.reply_text("⚠️ A normal Single/Bulk upload is active, so replacement mode was cancelled. Finish it or /cancel, then start replacement again.")
                return
            context.user_data["replacement_processing"] = True
            try:
                await _replacement_video(context, user_id, str(replacement_id), update.message.message_id, update.effective_chat.id)
            finally:
                context.user_data.pop("replacement_processing", None)
            # Successful or failed replacement both return to the neutral state.
            context.user_data.pop("replacement_video_id", None)
            return

        # Explicit bulk mode: every video/document is a real collection item.
        if context.user_data.get("bulk_mode") and not context.user_data.get("bulk_stage"):
            context.user_data.setdefault("bulk_category", "Global")
            items = context.user_data.setdefault("bulk_items", [])
            source_id = update.message.message_id
            if any(i.get("source_chat_id") == update.effective_chat.id and i.get("source_message_id") == source_id for i in items):
                return
            items.append(_video_media_info(update.message, video, incoming_group))
            _persist_bulk(context, user_id)
            n = len(items)
            # Keep the visible count synchronized with the actual queue.
            pmid = context.user_data.get("bulk_progress_msg_id")
            text = (
                f"📦 Bulk queue: {n} item(s) collected — "
                f"{sum(1 for i in items if i.get('media_type') == 'video')} video(s) + "
                f"{sum(1 for i in items if i.get('media_type') == 'photo')} image(s).\n\n"
                "Send more media or /bulkdone."
            )
            if pmid:
                try:
                    await context.bot.edit_message_text(chat_id=user_id, message_id=pmid, text=text)
                except Exception:
                    log.debug("Progress message was unavailable while updating upload status")
            else:
                msg = await update.message.reply_text(text)
                context.user_data["bulk_progress_msg_id"] = msg.message_id
            return

        pending = db.get_pending(user_id)
        if pending:
            # While waiting to decide single vs bulk, a second media is always bulk.
            if pending.get("stage") == "collecting" and not context.user_data.get("force_single_next"):
                await _convert_pending_media_to_bulk(update, context, pending, _video_media_info(update.message, video, incoming_group))
                return
            pending_group = pending.get("media_group_id")
            if pending_group and incoming_group and pending_group == incoming_group and not context.user_data.get("force_single_next"):
                await _auto_convert_pending_to_bulk(update, context, pending, _video_media_info(update.message, video, incoming_group))
                return
            if pending.get("stage") != "collecting":
                await update.message.reply_text("⚠️ This single upload is already in its details flow. Finish it first, or /cancel before starting another media.")
                return

        # Start a brand-new single media session. Nothing is copied to the
        # channels until the admin confirms the final preview.
        duration = getattr(video, "duration", None)
        file_size = getattr(video, "file_size", None)
        db.start_pending(user_id, video.file_id, None, None, duration, file_size)
        db.update_pending(
            user_id,
            source_chat_id=update.effective_chat.id,
            source_message_id=update.message.message_id,
            media_group_id=incoming_group,
            media_type="video",
        )
        # Non-blocking duplicate safety check. We only warn; we never auto-delete
        # or prevent a legitimate re-upload.
        try:
            candidates = db.find_possible_duplicate_videos(file_size, duration, limit=3)
            if candidates:
                preview = []
                for c in candidates:
                    ref = c.get("video_number") or c.get("id")
                    title = str(c.get("title") or "Untitled").replace("*", "")[:34]
                    preview.append(f"• `{ref}` · {title}")
                await update.message.reply_text(
                    "⚠️ *Possible duplicate detected*\n\n"
                    "This video has the same size and nearly the same duration as existing content. "
                    "I won't block or delete it — review the candidates if needed.\n\n" + "\n".join(preview),
                    parse_mode="Markdown",
                )
        except Exception:
            log.exception("Duplicate candidate scan failed")
        force_single = bool(context.user_data.pop("force_single_next", None))
        if force_single:
            next_stage = _apply_defaults(user_id)
            step_text, step_kb = _step_prompt_for(next_stage)
            await update.message.reply_text("🎬 *Single mode* — continuing immediately. Storage/Backup happens only after Confirm & Save.", parse_mode="Markdown")
            await update.message.reply_text(step_text, parse_mode="Markdown", reply_markup=step_kb)
        else:
            db.update_pending(user_id, stage="collecting")
            await update.message.reply_text("⏳ Media received. I’ll wait a few seconds for another image/video.\n\n• Nothing else → *Single upload*\n• Another media → *Bulk upload* automatically ✨", parse_mode="Markdown")
            _schedule_single_wait(context, user_id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    user_id = update.effective_user.id
    msg = update.message
    incoming_group = getattr(msg, "media_group_id", None)
    photo = msg.photo[-1] if msg.photo else msg.document
    if not photo:
        return

    # Setting a default cover takes priority over any upload step.
    # Replacement mode owns the next media message. A photo/document image
    # must not accidentally become a default/batch cover or start a normal
    # pending upload while a replacement is armed.
    if context.user_data.get("replacement_video_id"):
        await msg.reply_text(
            "⚠️ Replacement mode is active. Please send the replacement *video* only, or /cancel to exit.",
            parse_mode="Markdown",
            reply_markup=_replacement_prompt_kb(),
        )
        return

    if context.user_data.get("awaiting_default_cover") or context.user_data.get("awaiting_add_default_cover"):
        adding = bool(context.user_data.get("awaiting_add_default_cover"))
        context.user_data["awaiting_default_cover"] = False
        context.user_data["awaiting_add_default_cover"] = False
        cover_file_id = photo.file_id
        try:
            cover_msg = await context.bot.send_photo(chat_id=storage_config.primary(), photo=cover_file_id)
        except Exception as e:
            log.exception("Failed to post default cover to primary channel")
            await msg.reply_text(f"❌ Failed to save default cover: {e}")
            return
        if adding:
            db.add_default_cover(cover_file_id, cover_msg.message_id)
            await msg.reply_text(f"🎲 Added to random cover pool — {len(db.get_default_cover_pool())} cover(s) available.")
        else:
            db.set_setting("default_cover_file_id", cover_file_id)
            db.set_setting("default_cover_msg_id", str(cover_msg.message_id))
            db.add_default_cover(cover_file_id, cover_msg.message_id)
            await msg.reply_text("✅ Default cover saved and added to the random-cover pool.")
        return

    if context.user_data.get("awaiting_batch_cover"):
        batch_id = context.user_data.get("edit_batch_id")
        if not batch_id or not db.get_batch(batch_id):
            context.user_data.pop("awaiting_batch_cover", None)
            context.user_data.pop("edit_batch_id", None)
            await msg.reply_text("⌛ Bulk edit session expired. Run /editbulk BATCH_ID again.")
            return
        try:
            cover_primary = await context.bot.copy_message(
                chat_id=storage_config.primary(),
                from_chat_id=update.effective_chat.id,
                message_id=msg.message_id,
            )
            db.update_batch_cover(batch_id, photo.file_id, cover_primary.message_id)
            await msg.reply_text(
                f"✅ Bulk `{batch_id}` cover updated for all {db.get_batch_count(batch_id)} item(s).",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.exception("Manual bulk cover update failed")
            await msg.reply_text(f"❌ Could not update bulk cover: {e}")
        finally:
            context.user_data.pop("awaiting_batch_cover", None)
            context.user_data.pop("edit_batch_id", None)
        return

    async with _lock_for(user_id):
        # In explicit bulk collection mode, photos are collection items until
        # /bulkdone moves the flow to the shared-cover stage.
        if context.user_data.get("bulk_mode") and not context.user_data.get("bulk_stage"):
            context.user_data.setdefault("bulk_category", "Global")
            items = context.user_data.setdefault("bulk_items", [])
            if any(i.get("source_chat_id") == msg.chat_id and i.get("source_message_id") == msg.message_id for i in items):
                return
            items.append(_photo_media_info(msg, incoming_group))
            _persist_bulk(context, user_id)
            n = len(items)
            # Keep the visible count synchronized with the actual queue.
            pmid = context.user_data.get("bulk_progress_msg_id")
            text = (
                f"📦 Bulk queue: {n} item(s) collected — "
                f"{sum(1 for i in items if i.get('media_type') == 'video')} video(s) + "
                f"{sum(1 for i in items if i.get('media_type') == 'photo')} image(s).\n\n"
                "Send more media or /bulkdone."
            )
            if pmid:
                try:
                    await context.bot.edit_message_text(chat_id=user_id, message_id=pmid, text=text)
                except Exception:
                    log.debug("Progress message was unavailable while updating upload status")
            else:
                progress = await msg.reply_text(text)
                context.user_data["bulk_progress_msg_id"] = progress.message_id
            return

        # Shared bulk cover stage: this image is NEVER added to the collection.
        if context.user_data.get("bulk_mode") and context.user_data.get("bulk_stage") == "cover":
            context.user_data["bulk_cover_file_id"] = photo.file_id
            context.user_data["bulk_cover_msg_id"] = None
            context.user_data["bulk_cover_source_chat_id"] = update.effective_chat.id
            context.user_data["bulk_cover_source_message_id"] = msg.message_id
            context.user_data["bulk_stage"] = "title"
            _persist_bulk(context, user_id)
            await msg.reply_text("🖼️ *Cover saved!*\n\n✍️ Now send ONE title for the whole batch.", parse_mode="Markdown")
            return

        pending = db.get_pending(user_id)
        if pending:
            pending_group = pending.get("media_group_id")
            if pending.get("stage") == "collecting" and not context.user_data.get("force_single_next"):
                await _convert_pending_media_to_bulk(update, context, pending, _photo_media_info(msg, incoming_group))
                return
            if pending_group and incoming_group and pending_group == incoming_group and not context.user_data.get("force_single_next"):
                await _auto_convert_pending_to_bulk(update, context, pending, _photo_media_info(msg, incoming_group))
                return

            if pending.get("stage") == "awaiting_cover":
                aspect_warning = ""
                if getattr(photo, "width", None) and getattr(photo, "height", None):
                    ratio = photo.width / photo.height
                    if ratio > 2.2 or ratio < 0.45:
                        aspect_warning = "\n⚠️ This image is an unusual shape — it may look stretched as a cover."

                if pending.get("edit_return"):
                    db.update_pending(
                        user_id, cover_file_id=photo.file_id, cover_msg_id=None,
                        cover_source_chat_id=update.effective_chat.id,
                        cover_source_message_id=msg.message_id,
                        stage="awaiting_confirm", edit_return=0,
                    )
                    await _send_preview(context, user_id)
                    return

                next_stage = "awaiting_tags" if pending.get("title") else "awaiting_title"
                db.update_pending(
                    user_id,
                    cover_file_id=photo.file_id,
                    cover_msg_id=None,
                    cover_source_chat_id=update.effective_chat.id,
                    cover_source_message_id=msg.message_id,
                    stage=next_stage,
                )
                if next_stage == "awaiting_tags":
                    await msg.reply_text(
                        f"🏷 *Step 3/4 · Tags* — send comma-separated tags, or tap Skip. "
                        f"(Using your default title.){aspect_warning}",
                        parse_mode="Markdown", reply_markup=_tags_kb(pending.get("category")),
                    )
                else:
                    await msg.reply_text(
                        f"🖼️ *Cover saved!*\n\n✍️ *Step 3/4 · Title* — send the title for this media.{aspect_warning}",
                        parse_mode="Markdown",
                    )
                return

            # If we're past the cover step, an unrelated image should not be
            # mistaken for bulk or silently overwrite the current form.
            return

        # No pending upload: a lone photo/image is itself a valid single catalog
        # item. The NEXT separate photo becomes its cover.
        db.start_pending(user_id, photo.file_id, None, None, None, getattr(photo, "file_size", None))
        db.update_pending(
            user_id,
            source_chat_id=update.effective_chat.id,
            source_message_id=msg.message_id,
            media_group_id=incoming_group,
            media_type="photo",
        )
        force_single = bool(context.user_data.pop("force_single_next", None))
        if force_single:
            next_stage = _apply_defaults(user_id)
            step_text, step_kb = _step_prompt_for(next_stage)
            await msg.reply_text("🎬 *Single mode* — continuing immediately. Storage/Backup happens only after Confirm & Save.", parse_mode="Markdown")
            await msg.reply_text(step_text, parse_mode="Markdown", reply_markup=step_kb)
        else:
            db.update_pending(user_id, stage="collecting")
            await msg.reply_text("⏳ Image received. I’ll wait a few seconds for another image/video.\n\n• Nothing else → *Single upload*\n• Another media → *Bulk upload* automatically ✨", parse_mode="Markdown")
            _schedule_single_wait(context, user_id)


def _category_label(category: str) -> str:
    category = db.normalize_category(category)
    try:
        counts = db.get_category_counts(visible_only=False)
        return f"📂 Category: {category} · {int(counts.get(category, 0))}"
    except Exception:
        return f"📂 Category: {category}"

def _subcategory_picker_kb(category: str = "Global", bulk: bool = False) -> InlineKeyboardMarkup:
    category = db.normalize_category(category)
    prefix = "bulk_subcategory_pick_" if bulk else "subcategory_pick_"
    rows=[]
    try: choices=db.get_subcategories(category, limit=8, visible_only=False)
    except Exception: choices=[]
    for idx,(name,count) in enumerate(choices):
        rows.append([InlineKeyboardButton(f"🏷 {name} · {count}", callback_data=f"{prefix}{idx}")])
    rows.append([InlineKeyboardButton("✍️ Type custom tags", callback_data="bulk_custom_tags" if bulk else "custom_tags")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="bulk_subcategory_back" if bulk else "subcategory_back")])
    return InlineKeyboardMarkup(rows)

def _tags_kb(category: str = "Global") -> InlineKeyboardMarkup:
    category=db.normalize_category(category)
    return InlineKeyboardMarkup([[InlineKeyboardButton(_category_label(category), callback_data="toggle_category")],
                                 [InlineKeyboardButton("🏷 Pick Existing Subcategory", callback_data="pick_subcategory")],
                                 [InlineKeyboardButton("⏭ Skip", callback_data="skip_tags")],[CANCEL_BTN]])

def _desc_kb(category: str = "Global") -> InlineKeyboardMarkup:
    category=db.normalize_category(category)
    return InlineKeyboardMarkup([[InlineKeyboardButton(_category_label(category), callback_data="toggle_category")],
                                 [InlineKeyboardButton("🏷 Pick Existing Subcategory", callback_data="pick_subcategory")],
                                 [InlineKeyboardButton("⏭ Skip", callback_data="skip_description")],[CANCEL_BTN]])

def _schedule_quick_kb(kind: str = "single") -> InlineKeyboardMarkup:
    pfx="sched_s_" if kind=="single" else "sched_b_"
    return InlineKeyboardMarkup([[InlineKeyboardButton("⚡ +2 hours", callback_data=f"{pfx}rel_2h"),InlineKeyboardButton("⚡ +6 hours", callback_data=f"{pfx}rel_6h")],
                                 [InlineKeyboardButton("🌅 Tomorrow 00:05", callback_data=f"{pfx}tom_0005"),InlineKeyboardButton("🌙 Tomorrow 18:00", callback_data=f"{pfx}tom_1800")],
                                 [InlineKeyboardButton("📅 Pick Date & Time", callback_data=f"{pfx}dates")],
                                 [InlineKeyboardButton("✍️ Type Date/Time", callback_data=f"{pfx}manual"),CANCEL_BTN]])

def _schedule_date_kb(kind: str = "single") -> InlineKeyboardMarkup:
    from datetime import datetime,timedelta
    pfx="sched_s_" if kind=="single" else "sched_b_"; now=datetime.now(config.TIMEZONE); rows=[]
    for i in range(7):
        d=now.date()+timedelta(days=i); label="Today" if i==0 else ("Tomorrow" if i==1 else d.strftime("%a %d %b"))
        rows.append([InlineKeyboardButton(f"📅 {label}",callback_data=f"{pfx}date_{d.isoformat()}")])
    rows.append([InlineKeyboardButton("✍️ Type Date/Time",callback_data=f"{pfx}manual"),CANCEL_BTN]); return InlineKeyboardMarkup(rows)

def _schedule_time_kb(kind: str, day_iso: str) -> InlineKeyboardMarkup:
    pfx="sched_s_" if kind=="single" else "sched_b_"
    times=[("00:05","0005"),("06:00","0600"),("12:00","1200"),("18:00","1800"),("21:00","2100")]
    rows=[[InlineKeyboardButton(times[0][0],callback_data=f"{pfx}time_{day_iso}_{times[0][1]}"),InlineKeyboardButton(times[1][0],callback_data=f"{pfx}time_{day_iso}_{times[1][1]}")],
          [InlineKeyboardButton(times[2][0],callback_data=f"{pfx}time_{day_iso}_{times[2][1]}"),InlineKeyboardButton(times[3][0],callback_data=f"{pfx}time_{day_iso}_{times[3][1]}")],
          [InlineKeyboardButton(times[4][0],callback_data=f"{pfx}time_{day_iso}_{times[4][1]}")],
          [InlineKeyboardButton("✍️ Type Date/Time",callback_data=f"{pfx}manual"),CANCEL_BTN]]
    return InlineKeyboardMarkup(rows)

def _schedule_manual_prompt(kind: str="single") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Quick Schedule",callback_data=("sched_s_quick" if kind=="single" else "sched_b_quick")),CANCEL_BTN]])

def _apply_subcategory_to_tags(existing: str|None,name: str) -> str:
    current=[x.strip() for x in (existing or "").split(",") if x.strip()]
    if not any(x.lower()==name.lower() for x in current): current.insert(0,name)
    return ", ".join(current)


def _tomorrow_005() -> str:
    """Publish just after midnight tomorrow in the configured local timezone."""
    from datetime import datetime, timedelta
    now = datetime.now(config.TIMEZONE)
    target = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return target.isoformat()


async def _process_stage_text(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, reply_fn):
    pending = db.get_pending(user_id)
    if not pending:
        return

    text = text.strip()
    edit_return = pending.get("edit_return")

    if pending["stage"] == "awaiting_number":
        if text.lower() in ("/auto", "auto"):
            db.update_pending(user_id, video_number=None, stage="awaiting_confirm", edit_return=0)
            await _send_preview(context, user_id)
            return
        try:
            number = int(text)
        except ValueError:
            await reply_fn("🔢 Send a positive whole number, or /auto for automatic numbering.")
            return
        ok, reason = db.video_number_available(number)
        if not ok:
            await reply_fn(f"❌ {reason}")
            return
        db.update_pending(user_id, video_number=number, stage="awaiting_confirm", edit_return=0)
        await _send_preview(context, user_id)
        return

    if pending["stage"] == "awaiting_title":
        if not text:
            await reply_fn("Title can't be empty — send some text.")
            return
        if edit_return:
            db.update_pending(user_id, title=text, stage="awaiting_confirm", edit_return=0)
            await _send_preview(context, user_id)
            return
        db.update_pending(user_id, title=text, stage="awaiting_tags")
        dupes = db.find_similar_titles(text)
        if dupes:
            names = "\n".join(f"• {d['title']} ({d['upload_date']})" for d in dupes)
            await reply_fn(f"⚠️ Similar title(s) already in the catalog:\n{names}\n\n(Continuing anyway.)")
        await reply_fn(
            "🏷 *Step 3/4 · Tags* — send comma-separated tags (e.g. `travel, vlog, tokyo`), "
            "or tap Skip.",
            parse_mode="Markdown", reply_markup=_tags_kb(pending.get("category")),
        )

    elif pending["stage"] == "awaiting_tags":
        tags = "" if text == "/skip" else text
        if edit_return:
            db.update_pending(user_id, tags=tags, stage="awaiting_confirm", edit_return=0)
            await _send_preview(context, user_id)
            return
        db.update_pending(user_id, tags=tags, stage="awaiting_description")
        await reply_fn(
            "📝 *Step 4/4 · Description* — send a short description, or tap Skip.",
            parse_mode="Markdown", reply_markup=_desc_kb(pending.get("category")),
        )

    elif pending["stage"] == "awaiting_subcategory":
        # Backward compatibility for drafts created before tags became the
        # sole subcategory source. Fold the value into Tags and clear legacy
        # subcategory state.
        value = "" if text.lower() == "/skip" else text[:200]
        db.update_pending(user_id, tags=value, subcategory=None, stage="awaiting_confirm", edit_return=0)
        await _send_preview(context, user_id)

    elif pending["stage"] == "awaiting_description":
        description = "" if text == "/skip" else text
        db.update_pending(user_id, description=description, stage="awaiting_confirm", edit_return=0)
        await _send_preview(context, user_id)

    elif pending["stage"] == "awaiting_schedule":
        parsed = db.parse_schedule_input(text)
        if not parsed:
            await reply_fn(
                "Couldn't understand that. Use `YYYY-MM-DD HH:MM` (24h) or relative "
                "like `+2h`, `+30m`, `+1d`.",
                parse_mode="Markdown",
            )
            return
        db.update_pending(user_id, scheduled_at=parsed, stage="awaiting_confirm", edit_return=0)
        await _send_preview(context, user_id)


async def _send_preview(context, user_id):
    p = db.get_pending(user_id)
    if not p:
        log.warning(f"_send_preview called for user {user_id} but pending state is gone")
        await context.bot.send_message(
            chat_id=user_id,
            text="⚠️ Something went wrong — the upload session was lost before the preview could be built. "
                 "Please send the video again.",
        )
        return

    if not p.get("cover_file_id") or not p.get("title"):
        log.warning(f"_send_preview: incomplete pending state for user {user_id}: {p}")
        await context.bot.send_message(
            chat_id=user_id,
            text="⚠️ The upload is missing required info (cover or title) — something interrupted it. "
                 "Please /cancel and send the video again.",
        )
        return

    tags = " ".join(f"#{t.strip()}" for t in (p.get("tags") or "").split(",") if t.strip()) or "—"
    tags = db.md_escape(tags)
    desc = db.md_escape(p.get("description")) or "—"
    meta = f"⏱ {fmt_duration(p.get('duration_seconds'))} · 🗄 {fmt_size(p.get('file_size_bytes'))}"
    backup_note = "\n☁️ Storage + backup: after Confirm & Save"
    scheduled_at = p.get("scheduled_at")
    schedule_note = f"\n⏰ Scheduled to appear: {scheduled_at}" if scheduled_at else ""
    video_number = p.get("video_number")
    if video_number is None:
        video_number_note = f"\n🔢 Video number: *#{db.next_video_number()}* (auto)"
    else:
        video_number_note = f"\n🔢 Video number: *#{int(video_number)}* (manual)"
    category = db.normalize_category(p.get("category"))
    access_tier = p.get("access_tier") or "free"
    access_labels = {"free":"🌍 Public", "ad":"📺 Ad unlock", "redeem":"🎟️ Redeem membership", "redeem_or_ad":"🎟️+📺 Redeem OR Ad", "members":"💎 Redeem membership only", "users":"👤 Specific users", "gated":"🎟️+📺 Redeem OR Ad"}
    access_note = f"\n🔐 Access: {access_labels.get(access_tier, access_tier)}"
    if p.get("access_redeem_code"):
        access_note += f" · `{db.md_escape(p['access_redeem_code'])}`"
    if p.get("access_user_ids"):
        access_note += f" · {len([x for x in p['access_user_ids'].split(',') if x.strip()])} user(s)"
    caption = (
        f"👀 *Ready to save?*\n\n"
        f"📌 *{db.md_escape(p['title'])}*\n"
        f"📂 *Category:* {category}\n"
        f"🏷 *Subcategory:* {db.md_escape(p.get('subcategory') or '—')}\n"
        f"🏷 *Tags:* {tags}\n"
        f"📝 {desc}\n"
        f"{meta}{backup_note}{schedule_note}{video_number_note}{access_note}"
    )
    schedule_row = (
        [InlineKeyboardButton("🔁 Reschedule", callback_data="edit_schedule"),
         InlineKeyboardButton("❌ Cancel Schedule", callback_data="clear_schedule")]
        if scheduled_at else
        [InlineKeyboardButton("🌅 Publish Tomorrow", callback_data="schedule_tomorrow"),
         InlineKeyboardButton("⏰ Custom Time", callback_data="edit_schedule")]
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm & Save", callback_data="confirm_save"),
            InlineKeyboardButton("❌ Discard", callback_data="confirm_discard"),
        ],
        [
            InlineKeyboardButton("✏️ Title", callback_data="edit_title"),
            InlineKeyboardButton("🏷 Tags", callback_data="edit_tags"),
        ],
        [
            InlineKeyboardButton("📝 Description", callback_data="edit_description"),
            InlineKeyboardButton("🖼 Cover", callback_data="edit_cover"),
        ],
        [InlineKeyboardButton("🔢 Video Number", callback_data="edit_number")],
        [InlineKeyboardButton("🔐 Access", callback_data="access_single")],
        schedule_row,
    ])
    # Default/random covers are stored as messages in Primary. Copying that
    # message is more reliable than reusing a possibly stale file_id. Manual
    # covers still use their original file_id until Confirm & Save copies them.
    try:
        cover_msg_id = p.get("cover_msg_id")
        if cover_msg_id:
            await asyncio.wait_for(
                context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=storage_config.primary(),
                    message_id=int(cover_msg_id),
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=kb,
                ),
                timeout=12,
            )
        else:
            await asyncio.wait_for(
                context.bot.send_photo(
                    chat_id=user_id, photo=p["cover_file_id"], caption=caption,
                    parse_mode="Markdown", reply_markup=kb,
                ),
                timeout=12,
            )
    except Exception as e:
        log.exception(f"Failed to send preview for user {user_id}")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"⚠️ Preview cover could not be loaded ({db.md_escape(str(e))}).\n\nThe upload itself is still safe. You can choose another cover or continue with Manual Cover.\n\n{caption}",
            parse_mode="Markdown", reply_markup=kb,
        )



def _save_progress_text(done: int, total: int, phase: str) -> str:
    total = max(int(total), 1)
    done = max(0, min(int(done), total))
    width = 12
    filled = int(round(width * done / total))
    bar = "█" * filled + "░" * (width - filled)
    pct = int(round(done * 100 / total))
    return f"📦 *Saving batch*\n\n[{bar}] *{pct}%*  ·  {done}/{total}\n🔄 {phase}\n\n⏳ Keep this chat open — I'm handling it safely."


async def _send_bulk_preview(context, user_id: int):
    items = context.user_data.get("bulk_items") or []
    title = (context.user_data.get("bulk_title") or "").strip()
    if not items or not title:
        await context.bot.send_message(chat_id=user_id, text="⚠️ Bulk session is incomplete. Use /cancel and try again.")
        return
    cover = context.user_data.get("bulk_cover_file_id") or db.get_setting("default_cover_file_id")
    tags = context.user_data.get("bulk_tags") or ""
    desc = context.user_data.get("bulk_description") or ""
    count = len(items)
    total_size = sum((i.get("file_size_bytes") or 0) for i in items)
    total_duration = sum((i.get("duration_seconds") or 0) for i in items)
    tag_text = " ".join(f"#{t.strip()}" for t in tags.split(",") if t.strip()) or "—"
    scheduled_at = context.user_data.get("bulk_scheduled_at")
    schedule_note = f"\n🌅 Publish: {scheduled_at}" if scheduled_at else "\n📅 Publish: Today (immediate)"
    start_number = context.user_data.get("bulk_start_number")
    if start_number is None:
        number_note = f"\n🔢 Numbers: *#{db.next_video_number()}–#{db.next_video_number() + count - 1}* (auto)"
    else:
        number_note = f"\n🔢 Numbers: *#{int(start_number)}–#{int(start_number) + count - 1}* (manual)"
    category = db.normalize_category(context.user_data.get("bulk_category"))
    subcategory = (context.user_data.get("bulk_subcategory") or "").strip()
    access_tier = context.user_data.get("bulk_access_tier") or "free"
    access_labels = {"free":"🌍 Public", "ad":"📺 Ad unlock", "redeem":"🎟️ Redeem membership", "redeem_or_ad":"🎟️+📺 Redeem OR Ad", "members":"💎 Redeem membership only", "users":"👤 Specific users", "gated":"🎟️+📺 Redeem OR Ad"}
    access_note = f"\n🔐 Access: {access_labels.get(access_tier, access_tier)}"
    if context.user_data.get("bulk_access_redeem_code"):
        access_note += f" · `{db.md_escape(context.user_data['bulk_access_redeem_code'])}`"
    if context.user_data.get("bulk_access_user_ids"):
        access_note += f" · {len([x for x in context.user_data['bulk_access_user_ids'].split(',') if x.strip()])} user(s)"
    caption = (
        f"👀 *Ready to save bulk batch?*\n\n"
        f"📌 *{db.md_escape(title)}*\n"
        f"📂 *Category:* {category}\n"
        f"🏷 *Subcategory:* {db.md_escape(subcategory or '—')}\n"
        f"🎞 {count} item(s) · {sum(1 for i in items if i.get('media_type') == 'video')} video(s) + {sum(1 for i in items if i.get('media_type') == 'photo')} photo(s)\n"
        f"🏷 *Subcategories / Tags:* {db.md_escape(tag_text)}\n"
        f"📝 {db.md_escape(desc) or '—'}\n"
        f"⏱ Total duration: {fmt_duration(total_duration)} · 🗄 {fmt_size(total_size)}"
        f"{schedule_note}{number_note}{access_note}"
    )
    schedule_buttons = ([InlineKeyboardButton("🔁 Reschedule", callback_data="bulk_schedule_custom"),
                          InlineKeyboardButton("❌ Clear", callback_data="bulk_schedule_clear")]
                         if scheduled_at else
                         [InlineKeyboardButton("🌅 Publish Tomorrow", callback_data="bulk_schedule_tomorrow"),
                          InlineKeyboardButton("⏰ Custom Time", callback_data="bulk_schedule_custom")])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(_category_label(category), callback_data="bulk_toggle_category")],
                               [InlineKeyboardButton("🏷 Pick Existing Subcategory", callback_data="bulk_pick_subcategory")],
                               [InlineKeyboardButton("🔐 Access", callback_data="access_bulk")],
                               [InlineKeyboardButton("🖼️ Change Cover", callback_data="bulk_change_cover")],
                               [InlineKeyboardButton("🔢 Set Starting Number", callback_data="bulk_edit_number")],
                               [InlineKeyboardButton("✅ Save Batch", callback_data="bulk_confirm")],
                               schedule_buttons,
                               [InlineKeyboardButton("❌ Discard Batch", callback_data="bulk_discard")]])
    try:
        if cover:
            await context.bot.send_photo(chat_id=user_id, photo=cover, caption=caption,
                                         parse_mode="Markdown", reply_markup=kb)
        else:
            await context.bot.send_message(chat_id=user_id, text=caption, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"{caption}\n\n⚠️ Preview image failed: {e}",
                                       parse_mode="Markdown", reply_markup=kb)


async def _handle_bulk_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    stage = context.user_data.get("bulk_stage")
    text = text.strip()
    if stage == "subcategory":
        # Legacy drafts: fold the old dedicated field into Tags.
        context.user_data["bulk_tags"] = "" if text.lower() == "/skip" else text[:200]
        context.user_data["bulk_stage"] = "confirm"
        _persist_bulk(context, update.effective_user.id)
        await _send_bulk_preview(context, update.effective_user.id)
        return

    if stage == "number":
        if text.lower() == "/auto" or text.lower() == "auto":
            context.user_data.pop("bulk_start_number", None)
            context.user_data["bulk_stage"] = "confirm"
            _persist_bulk(context, update.effective_user.id)
            await _send_bulk_preview(context, update.effective_user.id)
            return
        try:
            start = int(text)
        except ValueError:
            await update.message.reply_text("🔢 Send a positive starting number, or /auto.")
            return
        ok, reason = db.video_number_available(start, len(context.user_data.get("bulk_items") or []))
        if not ok:
            await update.message.reply_text(f"❌ {reason}")
            return
        context.user_data["bulk_start_number"] = start
        context.user_data["bulk_stage"] = "confirm"
        _persist_bulk(context, update.effective_user.id)
        await _send_bulk_preview(context, update.effective_user.id)
    elif stage == "cover":
        if text == "/skip":
            # If a global default cover exists, use it even when this batch has
            # no custom cover. Otherwise the batch preview can still be text-only.
            context.user_data["bulk_cover_file_id"] = db.get_setting("default_cover_file_id")
            default_msg = db.get_setting("default_cover_msg_id")
            if default_msg:
                context.user_data["bulk_cover_msg_id"] = int(default_msg)
            await _bulk_finish_or_prompt_title(update, context)
        else:
            await update.message.reply_text("📸 Please send a photo, or type /skip to use no separate cover.")
    elif stage == "title":
        if not text:
            await update.message.reply_text("Title can't be empty.")
            return
        context.user_data["bulk_title"] = text
        context.user_data["bulk_stage"] = "tags"
        _persist_bulk(context, update.effective_user.id)
        await update.message.reply_text("🏷 Send tags separated by commas, or /skip.")
    elif stage == "tags":
        context.user_data["bulk_tags"] = "" if text == "/skip" else text
        context.user_data["bulk_stage"] = "description"
        _persist_bulk(context, update.effective_user.id)
        await update.message.reply_text("📝 Send one description for the whole batch, or /skip.")
    elif stage == "description":
        context.user_data["bulk_description"] = "" if text == "/skip" else text
        context.user_data["bulk_stage"] = "confirm"
        _persist_bulk(context, update.effective_user.id)
        await _send_bulk_preview(context, update.effective_user.id)
    elif stage == "schedule":
        parsed = db.parse_schedule_input(text)
        if not parsed:
            await update.message.reply_text("Couldn't understand that. Use `YYYY-MM-DD HH:MM` or `+1d`.", parse_mode="Markdown")
            return
        context.user_data["bulk_scheduled_at"] = parsed
        context.user_data["bulk_stage"] = "confirm"
        _persist_bulk(context, update.effective_user.id)
        await _send_bulk_preview(context, update.effective_user.id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    user_id = update.effective_user.id
    async with _lock_for(user_id):
        frame_cfg = context.user_data.get("awaiting_frame_shots")
        if frame_cfg is not None:
            parsed = _parse_frame_custom(update.message.text)
            if not parsed:
                await update.message.reply_text("❌ Use `count interval`, e.g. `8 5`. Count 2–12, interval 1–300 seconds.", parse_mode="Markdown")
                return
            context.user_data.pop("awaiting_frame_shots", None)
            task = spawn(_generate_frame_shots(update, context, bool(frame_cfg.get("is_bulk")), parsed[0], parsed[1]), name=f"frame-shots-custom-{user_id}")
            _cover_tasks[user_id] = task
            return
        if context.user_data.get("awaiting_repair_video"):
            context.user_data.pop("awaiting_repair_video", None)
            v = _resolve_repair_video(update.message.text)
            if not v:
                context.user_data["awaiting_repair_video"] = True
                await update.message.reply_text("❌ Video not found. Send a valid video number (e.g. `190`) or video ID.")
                return
            await context.bot.send_message(
                chat_id=user_id,
                text=(f"🧰 *Video Recovery · #{v.get('video_number') or '—'}*\n\n"
                      f"🎬 *{db.md_escape(v.get('title') or v['id'])}*\n\n"
                      "Choose an action:"),
                parse_mode="Markdown",
                reply_markup=_repair_video_kb(str(v["id"])),
            )
            return

        if context.user_data.get("awaiting_recovery_channel"):
            raw = (update.message.text or "").strip()
            try:
                dest_id = int(raw)
            except (TypeError, ValueError):
                await update.message.reply_text("❌ That doesn't look like a channel ID. Send the numeric ID, e.g. `-1001234567890`.", parse_mode="Markdown")
                return
            if dest_id in {storage_config.primary(), storage_config.backup()}:
                await update.message.reply_text("❌ That channel is already configured as Primary or Backup. Add a brand-new recovery channel.")
                return
            try:
                info = await _channel_probe(context.bot, dest_id)
            except Exception as exc:
                await update.message.reply_text(
                    f"❌ I can't use that channel yet.\n\n{db.md_escape(str(exc))}\n\n"
                    "Add this bot as an administrator and send the channel ID again.",
                    parse_mode="Markdown",
                )
                return
            context.user_data.pop("awaiting_recovery_channel", None)
            context.user_data["recovery_dest_id"] = dest_id
            storage_config.set_recovery(dest_id)
            try:
                audit = await _service_bot_audit(context.bot, dest_id)
            except Exception as exc:
                audit = {"services": [], "error": str(exc)}
            context.user_data["recovery_dest_audit"] = audit
            audit_block = _service_audit_text(audit)
            await update.message.reply_text(
                "✅ *Recovery channel verified*\n\n"
                f"Destination: *{db.md_escape(info['title'])}*\n"
                f"Channel ID: `{dest_id}`\n\n"
                f"Source will be the current Backup: `{storage_config.backup()}`\n\n"
                "The migration will copy all recoverable media and preserve existing catalogue IDs/metadata. "
                "You can keep this as a saved standby, or mirror the catalogue into it. After setup it remains an optional 3rd content source; Primary/Backup are not changed automatically.\n\n"
                + audit_block,
                parse_mode="Markdown", reply_markup=_recovery_confirm_kb()
            )
            return

        access_input = context.user_data.pop("awaiting_access_input", None)
        if access_input:
            raw = update.message.text.strip()
            if access_input.startswith("redeem"):
                code = raw.upper()
                c = db.get_redeem_code(code)
                if not c or not c.get("active"):
                    context.user_data["awaiting_access_input"] = access_input
                    await update.message.reply_text("❌ That redeem code doesn't exist or is inactive. Send a valid active code.")
                    return
                if access_input == "redeem_bulk":
                    context.user_data["bulk_access_tier"] = "redeem"
                    context.user_data["bulk_access_redeem_code"] = code
                    context.user_data.pop("bulk_access_user_ids", None)
                    _persist_bulk(context, user_id)
                    await _send_bulk_preview(context, user_id)
                else:
                    db.update_pending(user_id, access_tier="redeem", access_redeem_code=code, access_user_ids=None)
                    await _send_preview(context, user_id)
                return
            if access_input.startswith("users"):
                ids = []
                for part in raw.replace(" ", ",").split(","):
                    if part.strip():
                        try:
                            ids.append(str(int(part.strip())))
                        except ValueError:
                            await update.message.reply_text("❌ User IDs must be numeric and comma-separated.")
                            context.user_data["awaiting_access_input"] = access_input
                            return
                if not ids:
                    context.user_data["awaiting_access_input"] = access_input
                    await update.message.reply_text("Send at least one Telegram user ID.")
                    return
                csv_ids = ",".join(dict.fromkeys(ids))
                if access_input == "users_bulk":
                    context.user_data["bulk_access_tier"] = "users"
                    context.user_data["bulk_access_user_ids"] = csv_ids
                    context.user_data.pop("bulk_access_redeem_code", None)
                    _persist_bulk(context, user_id)
                    await _send_bulk_preview(context, user_id)
                else:
                    db.update_pending(user_id, access_tier="users", access_redeem_code=None, access_user_ids=csv_ids)
                    await _send_preview(context, user_id)
                return

        if context.user_data.get("bulk_mode") and context.user_data.get("bulk_stage"):
            await _handle_bulk_text(update, context, update.message.text)
            return
        await _process_stage_text(context, user_id, update.message.text, update.message.reply_text)


async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow typed /skip too, in addition to the Skip button."""
    if not await guard(update):
        return
    user_id = update.effective_user.id
    async with _lock_for(user_id):
        if context.user_data.get("bulk_mode") and context.user_data.get("bulk_stage"):
            await _handle_bulk_text(update, context, "/skip")
            return
        await _process_stage_text(context, user_id, "/skip", update.message.reply_text)


async def _finish_preview_message(query, user_id: int, context, text: str):
    """Update the preview message to its final state (saved/discarded/expired).
    The preview is normally a photo (edit_message_caption), but if sending the
    cover failed earlier _send_preview falls back to a plain text message —
    editing "caption" on that throws. Try caption first, fall back to text,
    and as a last resort just send a fresh message so the admin always sees
    the result instead of an unhandled error."""
    try:
        await query.edit_message_caption(caption=text)
        return
    except Exception:
        log.debug("Preview caption edit was unavailable; trying text edit")
    try:
        await query.edit_message_text(text=text)
        return
    except Exception:
        log.exception(f"Could not update preview message for user {user_id}, sending fresh message instead")
        await context.bot.send_message(chat_id=user_id, text=text)


EDIT_PROMPTS = {
    "title": ("awaiting_title", "✍️ Send the new *title*.", None),
    "tags": ("awaiting_tags", "🏷 Send new *tags*, comma-separated, or tap Skip.", "tags"),
    "description": ("awaiting_description", "📝 Send a new *description*, or tap Skip.", "description"),
}




async def _safe_callback_answer(query, text: str | None = None):
    try:
        kwargs = {} if text is None else {"text": text}
        await asyncio.wait_for(query.answer(**kwargs), timeout=3)
    except Exception as exc:
        log.warning("Callback acknowledgement failed: %r", exc)

_confirm_tasks: dict[int, asyncio.Task] = {}
_bulk_confirm_tasks: dict[int, asyncio.Task] = {}
_cover_tasks: dict[int, asyncio.Task] = {}
_single_wait_tasks: dict[int, asyncio.Task] = {}
SINGLE_MEDIA_WAIT_SECONDS = max(3.0, float(os.getenv("STORAGE_SINGLE_MEDIA_WAIT_SECONDS", "6")))

def _frame_source_for_session(context, user_id: int, is_bulk: bool):
    if is_bulk:
        items = context.user_data.get("bulk_items") or []
        for item in items:
            if item.get("media_type") == "video" and item.get("source_chat_id") and item.get("source_message_id"):
                return item
        return None
    p = db.get_pending(user_id) or {}
    if p.get("media_type") != "video" or not p.get("video_file_id"):
        return None
    return p


def _frame_picker_settings_kb(prefix="cover"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("5 shots · 5s", callback_data=f"{prefix}_shots_5_5"),
         InlineKeyboardButton("8 shots · 5s", callback_data=f"{prefix}_shots_8_5")],
        [InlineKeyboardButton("10 shots · 10s", callback_data=f"{prefix}_shots_10_10"),
         InlineKeyboardButton("12 shots · 10s", callback_data=f"{prefix}_shots_12_10")],
        [InlineKeyboardButton("✍️ Custom count + interval", callback_data=f"{prefix}_shots_custom")],
        [InlineKeyboardButton("↩️ Back to Cover", callback_data=f"{prefix}_back")],
        [CANCEL_BTN],
    ])


def _parse_frame_custom(text: str):
    nums = [int(x) for x in re.findall(r"\d+", text or "")]
    if len(nums) < 2:
        return None
    count, interval = nums[0], nums[1]
    if not (2 <= count <= 12 and 1 <= interval <= 300):
        return None
    return count, interval


async def _download_video_for_frames(context, source: dict, temp_path: str):
    src = await context.bot.get_file(source.get("video_file_id")) if source.get("video_file_id") else None
    if not src:
        raise RuntimeError("Original video file reference is unavailable.")
    await src.download_to_drive(temp_path)


def _extract_frames(video_path: str, output_dir: str, count: int, interval: int, random_one: bool = False):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed on the server.")
    probe = shutil.which("ffprobe")
    duration = 0.0
    if probe:
        try:
            out = subprocess.check_output(
                [probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                text=True, stderr=subprocess.DEVNULL, timeout=20,
            ).strip()
            duration = float(out or 0)
        except Exception:
            duration = 0.0
    if duration <= 0:
        raise RuntimeError("Could not read video duration for frame generation.")
    margin = min(1.0, max(0.05, duration * 0.04))
    usable_end = max(margin, duration - margin)
    if random_one:
        ts = random.uniform(margin, usable_end)
        times = [ts]
    else:
        times = []
        t = margin
        while t < usable_end and len(times) < count:
            times.append(t)
            t += interval
        if len(times) < count:
            # Back-fill evenly near the end so the requested count is honored when possible.
            span = max(0.1, usable_end - margin)
            times = [margin + (span * i / max(1, count - 1)) for i in range(count)]
    paths = []
    for idx, ts in enumerate(times, 1):
        out = os.path.join(output_dir, f"shot_{idx:02d}.jpg")
        subprocess.run(
            [ffmpeg, "-y", "-ss", f"{ts:.3f}", "-i", video_path, "-frames:v", "1", "-q:v", "3", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=45,
        )
        paths.append((idx, ts, out))
        if random_one:
            break
    return paths, duration


def _frame_visual_score(path: str) -> float:
    """Lightweight local frame score: prefer sharp, detailed, non-flat frames.
    This is intentionally heuristic (not face/content recognition) and falls back safely.
    """
    if Image is None:
        return 0.0
    try:
        im = Image.open(path).convert("L").resize((320, 180))
        stat = ImageStat.Stat(im)
        contrast = float(stat.stddev[0])
        edges = im.filter(ImageFilter.FIND_EDGES)
        edge_stat = ImageStat.Stat(edges)
        detail = float(edge_stat.mean[0])
        # Penalize near-black / near-white flat frames.
        mean = float(stat.mean[0])
        exposure = 1.0 - min(1.0, abs(mean - 128.0) / 128.0)
        return (detail * 2.0) + contrast + (exposure * 18.0)
    except Exception:
        return 0.0


def _extract_smart_frames(video_path: str, output_dir: str, candidate_count: int = 8):
    """Sample candidates across the full usable timeline and rank them locally."""
    ffmpeg = shutil.which("ffmpeg")
    probe = shutil.which("ffprobe")
    if not ffmpeg or not probe:
        raise RuntimeError("ffmpeg/ffprobe is not installed on the server.")
    try:
        duration = float(subprocess.check_output(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            text=True, stderr=subprocess.DEVNULL, timeout=20).strip() or 0)
    except Exception as exc:
        raise RuntimeError("Could not read video duration for smart cover generation.") from exc
    if duration <= 0:
        raise RuntimeError("Video duration is unavailable.")
    margin = min(1.0, max(0.05, duration * 0.04))
    end = max(margin, duration - margin)
    count = max(4, min(int(candidate_count), 10))
    times = [margin + ((end - margin) * i / max(1, count - 1)) for i in range(count)]
    scored = []
    for idx, ts in enumerate(times, 1):
        path = os.path.join(output_dir, f"smart_{idx:02d}.jpg")
        subprocess.run(
            [ffmpeg, "-y", "-ss", f"{ts:.3f}", "-i", video_path, "-frames:v", "1", "-q:v", "3", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=45,
        )
        scored.append((_frame_visual_score(path), idx, ts, path))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0], duration, scored


async def _generate_smart_best_frame(update: Update, context: ContextTypes.DEFAULT_TYPE, is_bulk: bool):
    query = update.callback_query
    user_id = query.from_user.id
    source = _frame_source_for_session(context, user_id, is_bulk)
    if not source:
        await context.bot.send_message(chat_id=user_id, text="⚠️ A video is required for Smart Best Frame. For bulk, include at least one video.")
        return
    workdir = tempfile.mkdtemp(prefix="vault_smart_cover_")
    path = os.path.join(workdir, "video.bin")
    try:
        await context.bot.send_message(chat_id=user_id, text="🧠 Analyzing frames… I’ll pick a sharp, well-exposed frame automatically.")
        await _download_video_for_frames(context, source, path)
        best, duration, scored = await asyncio.to_thread(_extract_smart_frames, path, workdir, 8)
        score, idx, ts, shot_path = best
        with open(shot_path, "rb") as fh:
            sent = await context.bot.send_photo(
                chat_id=user_id,
                photo=fh,
                caption=f"🧠 Smart Best Frame · {ts:.1f}s / {duration:.1f}s\n8 candidate frames analyzed",
            )
        context.user_data["frame_shot_options"] = {"1": {"file_id": sent.photo[-1].file_id, "message_id": sent.message_id}}
        context.user_data["frame_shot_source"] = "smart"
        context.user_data["smart_frame_time"] = ts
        await context.bot.send_message(
            chat_id=user_id,
            text="⭐ *Smart frame selected*\n\nThe picker favors detail, contrast and usable exposure. Nothing is saved until you confirm the upload.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Use This Frame", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_shot_1")],
                [InlineKeyboardButton("🎬 Pick Shots Instead", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_frame_shots"),
                 InlineKeyboardButton("🎞️ Random", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_frame_random")],
            ]),
        )
    except Exception as exc:
        log.exception("Smart best-frame generation failed")
        await context.bot.send_message(chat_id=user_id, text=f"❌ Smart cover could not be generated: {db.md_escape(str(exc))}\n\nYou can still use Random, Default, Shots or Manual Cover.", parse_mode="Markdown")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _generate_random_frame_cover(update: Update, context: ContextTypes.DEFAULT_TYPE, is_bulk: bool):
    query = update.callback_query
    user_id = query.from_user.id
    source = _frame_source_for_session(context, user_id, is_bulk)
    if not source:
        await context.bot.send_message(chat_id=user_id, text="⚠️ A video is required for video-frame cover generation. For a bulk batch, include at least one video.")
        return
    workdir = tempfile.mkdtemp(prefix="vault_frame_")
    path = os.path.join(workdir, "video.bin")
    try:
        await context.bot.send_message(chat_id=user_id, text="🎞️ Generating a random frame…")
        await _download_video_for_frames(context, source, path)
        frames, duration = await asyncio.to_thread(_extract_frames, path, workdir, 1, 1, True)
        _, ts, shot_path = frames[0]
        with open(shot_path, "rb") as fh:
            sent = await context.bot.send_photo(chat_id=user_id, photo=fh, caption=f"🎞️ Random video frame · {ts:.1f}s / {duration:.1f}s")
        frame_file_id = sent.photo[-1].file_id
        context.user_data["frame_shot_options"] = {"1": {"file_id": frame_file_id, "message_id": sent.message_id}}
        context.user_data["frame_shot_source"] = "generated"
        await context.bot.send_message(chat_id=user_id, text="✅ Use this frame as the cover?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Use This Frame", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_shot_1")],
            [InlineKeyboardButton("🎬 Generate Shots Instead", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_frame_shots"),
             InlineKeyboardButton("✋ Manual Cover", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_manual")],
        ]))
    except Exception as exc:
        log.exception("Random frame generation failed")
        await context.bot.send_message(chat_id=user_id, text=f"❌ Could not generate a video frame: {db.md_escape(str(exc))}\n\nYou can still use Default/Random Default or Manual Cover.", parse_mode="Markdown")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _generate_frame_shots(update: Update, context: ContextTypes.DEFAULT_TYPE, is_bulk: bool, count: int, interval: int):
    query = update.callback_query
    user_id = query.from_user.id
    source = _frame_source_for_session(context, user_id, is_bulk)
    if not source:
        await context.bot.send_message(chat_id=user_id, text="⚠️ A video is required for shot generation. For a bulk batch, include at least one video.")
        return
    workdir = tempfile.mkdtemp(prefix="vault_shots_")
    path = os.path.join(workdir, "video.bin")
    try:
        await context.bot.send_message(chat_id=user_id, text=f"🎬 Creating {count} shots, about every {interval}s…")
        await _download_video_for_frames(context, source, path)
        frames, duration = await asyncio.to_thread(_extract_frames, path, workdir, count, interval, False)
        options = {}
        # Keep the picker compact: send the shots first, then a single button grid.
        for idx, ts, shot_path in frames:
            with open(shot_path, "rb") as fh:
                sent = await context.bot.send_photo(chat_id=user_id, photo=fh, caption=f"Shot {idx} · {ts:.1f}s / {duration:.1f}s")
            options[str(idx)] = sent.photo[-1].file_id
        context.user_data["frame_shot_options"] = options
        context.user_data["frame_shot_source"] = "generated"
        context.user_data["frame_shot_count"] = len(options)
        context.user_data["frame_shot_interval"] = interval
        rows = []
        row = []
        for idx in options:
            row.append(InlineKeyboardButton(f"✅ Shot {idx}", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_shot_{idx}"))
            if len(row) == 3:
                rows.append(row); row = []
        if row: rows.append(row)
        rows.append([InlineKeyboardButton("🔁 New Shot Set", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_frame_shots"),
                     InlineKeyboardButton("✋ Manual Cover", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_manual")])
        rows.append([InlineKeyboardButton("↩️ Back to Cover", callback_data=f"{'bulkcover' if is_bulk else 'cover'}_back")])
        await context.bot.send_message(chat_id=user_id,
            text=f"🎬 *Choose your cover shot*\n\n{len(options)} shots generated · {interval}s spacing. Tap exactly one.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
    except Exception as exc:
        log.exception("Frame shot generation failed")
        await context.bot.send_message(chat_id=user_id, text=f"❌ Could not generate shots: {db.md_escape(str(exc))}\n\nTry fewer shots, a longer interval, or use a manual/default cover.", parse_mode="Markdown")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _choose_generated_shot(update: Update, context: ContextTypes.DEFAULT_TYPE, shot_idx: str, is_bulk: bool):
    query = update.callback_query
    user_id = query.from_user.id
    opts = context.user_data.get("frame_shot_options") or {}
    selected = opts.get(str(shot_idx))
    if not selected:
        await context.bot.send_message(chat_id=user_id, text="⌛ Those shots have expired. Generate a new shot set.")
        return
    file_id = selected.get("file_id") if isinstance(selected, dict) else selected
    message_id = selected.get("message_id") if isinstance(selected, dict) else None
    if not file_id:
        await context.bot.send_message(chat_id=user_id, text="⌛ That shot is no longer available. Generate a new shot set.")
        return
    if is_bulk:
        context.user_data["bulk_cover_file_id"] = file_id
        context.user_data["bulk_cover_msg_id"] = None
        context.user_data["bulk_cover_source_chat_id"] = user_id
        context.user_data["bulk_cover_source_message_id"] = message_id
        context.user_data["bulk_stage"] = "title"
        _persist_bulk(context, user_id)
        await context.bot.send_message(chat_id=user_id, text=f"✅ *Shot {shot_idx} selected as the collection cover.*\n\n✍️ Now send ONE title for the batch.", parse_mode="Markdown")
    else:
        p = db.get_pending(user_id) or {}
        return_stage = "awaiting_tags" if p.get("title") else "awaiting_title"
        # No chat message id is needed here; the generated photo is already held by this bot as a file_id.
        db.update_pending(user_id, cover_file_id=file_id, cover_msg_id=None,
                          cover_source_chat_id=user_id, cover_source_message_id=message_id,
                          stage=return_stage, edit_return=0)
        await context.bot.send_message(chat_id=user_id, text=f"✅ *Shot {shot_idx} selected as the cover.*", parse_mode="Markdown")
        if return_stage == "awaiting_tags":
            await _send_preview(context, user_id)
        else:
            await context.bot.send_message(chat_id=user_id, text="✍️ Now send the title.")


async def _apply_cover_choice_background(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, is_bulk: bool):
    """Apply default/random cover without holding the callback update handler.

    The cover message is stored in Primary, so preview rendering can copy that
    Telegram message instead of depending on a possibly stale file_id.
    """
    query = update.callback_query
    user_id = query.from_user.id
    try:
        try:
            await asyncio.wait_for(
                context.bot.send_message(chat_id=user_id, text="⏳ Applying cover…"),
                timeout=5,
            )
        except Exception as exc:
            log.warning("Cover progress message failed: %r", exc)
        async with _lock_for(user_id):
            if is_bulk:
                if not context.user_data.get("bulk_items"):
                    await context.bot.send_message(chat_id=user_id, text="⌛ Bulk session expired. Start /bulk again.")
                    return
            else:
                pending = db.get_pending(user_id)
                if not pending:
                    await context.bot.send_message(chat_id=user_id, text="⌛ Upload session expired. Please send the video again.")
                    return

            chooser = db.choose_default_cover(randomize=(mode == "random"))
            if not chooser:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ No usable default cover is configured.\n\nUse /setdefaultcover or /adddefaultcover, or choose ✋ Manual Cover.",
                )
                return

            file_id = chooser.get("file_id")
            msg_id = chooser.get("msg_id")
            if not file_id or not msg_id:
                await context.bot.send_message(chat_id=user_id, text="⚠️ This cover entry is incomplete. Please set the default cover again.")
                return

            if is_bulk:
                context.user_data["bulk_cover_file_id"] = file_id
                context.user_data["bulk_cover_msg_id"] = int(msg_id)
                context.user_data["bulk_cover_source_chat_id"] = storage_config.primary()
                context.user_data["bulk_cover_source_message_id"] = int(msg_id)
                context.user_data["bulk_stage"] = "title"
                _persist_bulk(context, user_id)
                label = "🎲 Random default" if mode == "random" else "🖼️ Default"
                await context.bot.send_message(chat_id=user_id, text=f"{label} cover applied.\n\n✍️ Now send ONE title for the batch.")
            else:
                old_pending = db.get_pending(user_id) or {}
                return_stage = "awaiting_tags" if old_pending.get("title") else "awaiting_title"
                db.update_pending(
                    user_id,
                    cover_file_id=file_id,
                    cover_msg_id=int(msg_id),
                    cover_source_chat_id=storage_config.primary(),
                    cover_source_message_id=int(msg_id),
                    stage=return_stage,
                    edit_return=0,
                )
                await context.bot.send_message(
                    chat_id=user_id,
                    text=("🎲 Random default cover applied." if mode == "random" else "🖼️ Default cover applied.")
                    + ("\n\n✍️ Now send the title." if return_stage == "awaiting_title" else "\n\n👀 Preview refreshed."),
                )
                if return_stage == "awaiting_tags":
                    await _send_preview(context, user_id)
                else:
                    await context.bot.send_message(chat_id=user_id, text="✍️ Now send the title.")
    except Exception as exc:
        log.exception("Cover selection failed")
        try:
            await context.bot.send_message(chat_id=user_id, text=f"❌ Cover selection failed: {db.md_escape(str(exc))}\n\nNothing was lost. Try the cover button again or use Manual Cover.")
        except Exception:
            log.exception("Cover failure message could not be delivered")
    finally:
        _cover_tasks.pop(user_id, None)

async def _confirm_save_background(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run Confirm & Save outside the callback update handler."""
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await _safe_callback_answer(query)
        # Serialize confirmation so a double tap can never create two catalog items.
        async with _lock_for(user_id):
            pending = db.get_pending(user_id)
            # Give the admin immediate visual feedback. Telegram copy operations can
            # legitimately take several seconds for larger media; without this, the
            # button looks dead while the handler is still working.
            try:
                await asyncio.wait_for(
                    query.edit_message_reply_markup(reply_markup=None),
                    timeout=5,
                )
            except Exception as exc:
                log.warning("Could not clear Confirm & Save buttons: %r", exc)
            try:
                await asyncio.wait_for(
                    context.bot.send_message(
                        chat_id=user_id,
                        text="⏳ Saving… copying to Primary + Backup and finalizing the catalog entry.\n\nPlease don't tap Save again. 💗",
                    ),
                    timeout=5,
                )
            except Exception as exc:
                log.warning("Could not send saving progress message: %r", exc)
            if not pending:
                saved = context.user_data.get("last_saved_video_id")
                if saved:
                    await _finish_preview_message(query, user_id, context, f"✅ Already saved as `{saved}`. No duplicate was created. 💗")
                else:
                    await _finish_preview_message(query, user_id, context, "⌛ This upload session is no longer active. If it wasn't saved, send the media again.")
                return

            title_for_log = pending.get("title", "")
            scheduled_for_log = pending.get("scheduled_at")
            try:
                source_chat = pending.get("source_chat_id")
                source_msg = pending.get("source_message_id")
                existing = db.get_video_by_source(source_chat, source_msg)
                if existing:
                    db.clear_pending(user_id)
                    context.user_data["last_saved_video_id"] = existing["id"]
                    await _finish_preview_message(query, user_id, context, f"♻️ This source was already saved as `{existing['id']}`. I reused it instead of creating a duplicate. ✨")
                    return
                if not source_chat or not source_msg:
                    raise ValueError("Original upload message is missing; please re-upload this media.")

                primary_msg_id = pending.get("primary_msg_id")
                if primary_msg_id:
                    primary_message_id = int(primary_msg_id)
                else:
                    log.info("Confirm save: copying source %s/%s to primary for user %s", source_chat, source_msg, user_id)
                    primary_msg = await asyncio.wait_for(
                        context.bot.copy_message(
                            chat_id=storage_config.primary(),
                            from_chat_id=source_chat,
                            message_id=source_msg,
                        ),
                        timeout=30,
                    )
                    primary_message_id = primary_msg.message_id
                    db.update_pending(user_id, primary_msg_id=primary_message_id)

                backup_msg_id = pending.get("backup_msg_id")
                if not backup_msg_id:
                    try:
                        log.info("Confirm save: copying primary message %s to backup for user %s", primary_message_id, user_id)
                        backup_msg = await asyncio.wait_for(
                            context.bot.copy_message(
                                chat_id=storage_config.backup(),
                                from_chat_id=storage_config.primary(),
                                message_id=primary_message_id,
                            ),
                            timeout=20,
                        )
                        backup_msg_id = backup_msg.message_id
                        db.update_pending(user_id, backup_msg_id=backup_msg_id)
                    except Exception:
                        log.exception("Backup copy failed after final confirmation")

                cover_msg_id = pending.get("cover_msg_id")
                if not cover_msg_id and pending.get("cover_source_chat_id") and pending.get("cover_source_message_id"):
                    log.info("Confirm save: copying manual cover %s/%s for user %s", pending["cover_source_chat_id"], pending["cover_source_message_id"], user_id)
                    cover_primary = await asyncio.wait_for(
                        context.bot.copy_message(
                            chat_id=storage_config.primary(),
                            from_chat_id=pending["cover_source_chat_id"],
                            message_id=pending["cover_source_message_id"],
                        ),
                        timeout=20,
                    )
                    cover_msg_id = cover_primary.message_id
                    db.update_pending(user_id, cover_msg_id=cover_msg_id)

                db.update_pending(
                    user_id,
                    category=db.normalize_category(pending.get("category")),
                    subcategory=(pending.get("subcategory") or None),
                    primary_msg_id=primary_message_id,
                    backup_msg_id=backup_msg_id,
                    cover_msg_id=cover_msg_id,
                )
                vid = db.finalize_pending(user_id)
            except ValueError as exc:
                await _finish_preview_message(
                    query, user_id, context,
                    f"❌ Save blocked: {db.md_escape(str(exc))}\n\nYour upload is still safe. Fix the field and confirm again.",
                )
                return
            except Exception as exc:
                log.exception("Single save failed")
                await _finish_preview_message(
                    query, user_id, context,
                    f"❌ Save failed: {db.md_escape(str(exc))}\n\nNothing was added to the catalog. Please retry from the preview.",
                )
                return

            context.user_data["last_saved_video_id"] = vid
            context.user_data["last_saved_at"] = db.now_str()
            db.log_activity("storage_bot", "upload", f"{vid}: {title_for_log}")
            schedule_line = f"\n⏰ Scheduled to appear: {scheduled_for_log}" if scheduled_for_log else ""
            backup_line = "" if backup_msg_id else "\n⚠️ Backup copy is pending — the catalog save itself succeeded."
            await _finish_preview_message(
                query, user_id, context,
                f"🎉 *Saved!* `{vid}`\n\nCatalog entry is ready. No duplicate was created. ✨{schedule_line}{backup_line}",
            )
            return

    except asyncio.CancelledError:
        log.warning("Confirm save task cancelled for user %s", user_id)
        raise
    except Exception:
        log.exception("Unhandled Confirm & Save background failure for user %s", user_id)
        try:
            await asyncio.wait_for(
                context.bot.send_message(chat_id=user_id, text="❌ Save worker crashed unexpectedly. Check the bot log and retry."),
                timeout=5,
            )
        except Exception:
            log.exception("Save worker crash message could not be delivered")
    finally:
        _confirm_tasks.pop(user_id, None)




def _fmt_channel_id(cid: int) -> str:
    return str(cid) if cid else "Not configured"


def _recovery_menu_kb(job=None):
    buttons = []
    standby = storage_config.recovery()
    if job and job.get("status") in {"running", "paused", "stopped", "error"}:

        status = job.get("status")
        if status in {"paused", "stopped", "error"}:
            buttons.append([InlineKeyboardButton("▶️  Resume Recovery", callback_data="recovery_resume")])
        if status == "running":
            buttons.append([
                InlineKeyboardButton("⏸️  Pause", callback_data="recovery_pause"),
                InlineKeyboardButton("🛑  Stop", callback_data="recovery_stop"),
            ])
        if status in {"paused", "stopped", "running", "error"}:
            buttons.append([InlineKeyboardButton("❌  Cancel Recovery", callback_data="recovery_cancel")])
    if not job and standby:
        buttons.append([InlineKeyboardButton("🚀  Start from Saved Recovery Channel", callback_data="recovery_start_saved")])
        buttons.append([InlineKeyboardButton("🧪  Verify Saved Recovery Channel", callback_data="recovery_check_saved")])
        buttons.append([InlineKeyboardButton("🗑  Remove Standby", callback_data="recovery_clear")])
    buttons += [
        [InlineKeyboardButton("➕  Add / Change Recovery Channel", callback_data="recovery_add")],
        [InlineKeyboardButton("🔎  Check Bot Access", callback_data="recovery_check")],
        [
            InlineKeyboardButton("🔄  Refresh", callback_data="storage_menu_recovery"),
            InlineKeyboardButton("🏠  Storage Menu", callback_data="storage_menu_refresh"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def _recovery_confirm_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎  Check Bot Access", callback_data="recovery_check")],
        [InlineKeyboardButton("🚀  Start Recovery", callback_data="recovery_confirm")],
        [
            InlineKeyboardButton("✏️  Change", callback_data="recovery_add"),
            InlineKeyboardButton("↩️  Back", callback_data="storage_menu_recovery"),
        ],
    ])


def _recovery_progress_bar(done: int, total: int, width: int = 14) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round((done / total) * width)))
    return "█" * filled + "░" * (width - filled)


def _recovery_job_id(user_id: int) -> str:
    return f"sr_{user_id}_{int(asyncio.get_running_loop().time()*1000)}"


def _recovery_state_store():
    if not hasattr(_recovery_state_store, "tasks"):
        _recovery_state_store.tasks = {}
        _recovery_state_store.controls = {}
    return _recovery_state_store.tasks, _recovery_state_store.controls


async def _channel_probe(bot, chat_id: int):
    """Return a compact channel summary after verifying bot access/admin rights."""
    chat = await bot.get_chat(chat_id)
    member = await bot.get_chat_member(chat_id, bot.id)
    status = str(getattr(member, "status", ""))
    if status not in {"administrator", "creator"}:
        raise PermissionError("The bot must be an administrator in that channel.")
    if str(getattr(chat, "type", "")) != "channel":
        raise ValueError("Please add a Telegram channel, not a group or user chat.")
    title = getattr(chat, "title", None) or "Untitled channel"
    return {"id": int(chat.id), "title": title, "username": getattr(chat, "username", None), "status": status}



_service_bot_cache = {}


async def _service_bot_identity(role: str, token: str):
    """Resolve a configured service bot's Telegram identity without exposing its token."""
    if not token:
        return None, "Not configured"
    cached = _service_bot_cache.get(role)
    if cached:
        return cached, None
    probe = Bot(token=token)
    try:
        me = await asyncio.wait_for(probe.get_me(), timeout=8)
        info = {"id": int(me.id), "username": getattr(me, "username", None) or role}
        _service_bot_cache[role] = info
        return info, None
    except Exception as exc:
        return None, str(exc)
    finally:
        try:
            await probe.shutdown()
        except Exception:
            pass


async def _service_bot_audit(bot, chat_id: int):
    """Check whether Catalog/Delivery service bots can operate in a candidate storage channel.

    Telegram does not let one bot add another bot to a channel out of thin air.  We therefore
    verify membership/admin rights here, and opportunistically promote an already-present
    service bot when this Storage Bot itself has the required promote permission.
    """
    target = await bot.get_chat(chat_id)
    results = []
    service_defs = [
        ("Catalogue Bot", "catalogue", getattr(config, "CATALOG_BOT_TOKEN", "")),
        ("Delivery Bot · Hardcore", "delivery", getattr(config, "DELIVERY_BOT_TOKEN", "")),
    ]
    # Permanent delivery bots are managed in permanent_bot_store.json. Include
    # every active token in the readiness audit so a 3rd content channel isn't
    # left without one of the delivery workers.
    try:
        for i, row in enumerate(permanent_bot_store.active_delivery_bots()):
            token = str(row.get("token") or "").strip()
            if not token:
                continue
            username = str(row.get("username") or "permanent").lstrip("@")
            service_defs.append((f"Delivery Bot · Permanent · @{username}", f"permanent_delivery_{i}_{username}", token))
    except Exception as exc:
        log.warning("Could not enumerate permanent delivery bots for channel audit: %s", exc)
    storage_member = await bot.get_chat_member(chat_id, bot.id)
    storage_can_promote = bool(getattr(storage_member, "can_promote_members", False))

    for label, role, token in service_defs:
        info, identity_error = await _service_bot_identity(role, token)
        row = {"label": label, "role": role, "configured": bool(token), "ok": False, "status": "Not configured", "username": None, "auto_promoted": False, "error": None}
        if not token:
            results.append(row)
            continue
        if not info:
            row["status"] = "Token check failed"
            row["error"] = identity_error or "Unknown error"
            results.append(row)
            continue
        row["username"] = info.get("username")
        try:
            member = await bot.get_chat_member(chat_id, info["id"])
            status = str(getattr(member, "status", ""))
            if status in {"administrator", "creator"}:
                row["ok"] = True
                row["status"] = "Admin"
            elif status == "member" and storage_can_promote:
                try:
                    await bot.promote_chat_member(
                        chat_id=chat_id,
                        user_id=info["id"],
                        can_manage_chat=True,
                        can_post_messages=True,
                        can_edit_messages=True,
                        can_delete_messages=True,
                        can_invite_users=False,
                        can_restrict_members=False,
                        can_promote_members=False,
                        can_change_info=False,
                        can_pin_messages=False,
                        can_manage_video_chats=False,
                    )
                    row["ok"] = True
                    row["status"] = "Admin (auto-promoted)"
                    row["auto_promoted"] = True
                except Exception as exc:
                    row["status"] = "Member · needs admin"
                    row["error"] = str(exc)
            else:
                row["status"] = "Needs admin"
        except Exception as exc:
            row["status"] = "Not added"
            row["error"] = str(exc)
        results.append(row)

    return {"channel": {"id": int(target.id), "title": getattr(target, "title", None) or "Untitled channel"}, "storage_bot_admin": str(getattr(storage_member, "status", "")) in {"administrator", "creator"}, "storage_can_promote": storage_can_promote, "services": results}


def _service_audit_text(audit):
    if not audit:
        return "🧩 Bot integration check unavailable."
    lines = ["🧩 *Bot integration check*"]
    for item in audit.get("services", []):
        if not item.get("configured"):
            icon = "⚪"
        elif item.get("ok"):
            icon = "✅"
        else:
            icon = "⚠️"
        name = item.get("label", "Service Bot")
        user = item.get("username")
        suffix = f" · @{db.md_escape(user)}" if user else ""
        lines.append(f"{icon} {name}{suffix} — {db.md_escape(item.get('status') or 'Unknown')}")
    missing = [x["label"] for x in audit.get("services", []) if x.get("configured") and not x.get("ok")]
    if missing:
        lines.append("\n⚠️ Add/promote the missing bot(s) as channel admins, then tap *Check Bot Access*.")
    else:
        lines.append("\n✅ Catalogue/Delivery integration is ready for this channel.")
    return "\n".join(lines)


def _recovery_text(job, source_title="Backup", target_title="Recovery Channel"):
    if not job:
        return None
    total = int(job.get("total") or 0)
    next_index = int(job.get("next_index") or 0)
    copied = int(job.get("copied") or 0)
    failed = int(job.get("failed") or 0)
    status = str(job.get("status") or "paused")
    counts = db.storage_recovery_counts(job["id"])
    remaining = counts.get("pending", 0)
    pct = int((min(next_index, total) / total) * 100) if total else 0
    icons = {"running":"🟢", "paused":"⏸️", "stopped":"🛑", "error":"⚠️", "completed":"✅", "cancelled":"❌"}
    action = {
        "running":"Recovering media…",
        "paused":"Paused safely — progress is saved.",
        "stopped":"Stopped safely — progress is saved.",
        "error":"Recovery paused after an error — you can resume.",
        "completed":"Recovery completed.",
        "cancelled":"Recovery cancelled; copied videos were kept.",
    }.get(status, "Recovery")
    return (
        f"🛡️ *Storage Recovery*\n\n"
        f"{icons.get(status, '🛡️')} *{status.title()}* · {action}\n\n"
        f"📦 *Source*  ·  {db.md_escape(source_title)}\n"
        f"🆕 *Target*  ·  {db.md_escape(target_title)}\n\n"
        f"`{_recovery_progress_bar(next_index, total)}`  *{pct}%*\n\n"
        f"✅ Copied  ·  *{copied:,}*\n"
        f"⚠️ Failed  ·  *{failed:,}*\n"
        f"⏳ Remaining  ·  *{remaining:,}*\n"
        f"📦 Processed  ·  *{min(next_index,total):,} / {total:,}*\n\n"
        "🔐 Checkpoint saved after every video. Restarting the bot will not reset completed work."
    )


def _recovery_controls_for(job_id):
    tasks, controls = _recovery_state_store()
    return controls.setdefault(job_id, {"pause": False, "stop": False, "cancel": False})


def _recovery_normalize_job(job):
    """A process restart can leave a persisted job marked running with no live task.
    Make that state resumable rather than showing a dead-looking running job."""
    if not job:
        return None
    tasks, _ = _recovery_state_store()
    task = tasks.get(job.get("id"))
    if job.get("status") == "running" and (not task or task.done()):
        db.update_storage_recovery_job(job["id"], status="paused", last_error="Worker was interrupted; checkpoint is safe to resume.")
        job = db.get_storage_recovery_job(job["id"])
    return job


async def _recovery_run(update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str | None = None):
    """Crash-safe resumable recovery from surviving Backup to new destination."""
    query = update.callback_query
    user_id = query.from_user.id
    tasks, controls = _recovery_state_store()
    job = db.get_storage_recovery_job(job_id) if job_id else db.get_active_storage_recovery_job(user_id)
    if not job:
        await query.edit_message_text("❌ No recovery session was found. Open Storage Recovery and start again.", reply_markup=_recovery_menu_kb())
        return

    # Do not allow two workers for the same recovery job. The launcher stores
    # the task in `tasks` immediately after spawn(), so the worker can see its
    # own Task here. Treat the current task as valid; only reject a DIFFERENT
    # live worker for the same job. This fixes recovery sessions that stayed
    # at 0%/Paused after tapping Start.
    existing = tasks.get(job["id"])
    current_task = asyncio.current_task()
    if existing and existing is not current_task and not existing.done():
        return
    dest = int(job["dest_channel_id"])
    source = int(job["source_channel_id"])
    controls[job["id"]] = {"pause": False, "stop": False, "cancel": False}

    try:
        dest_info = await _channel_probe(context.bot, dest)
    except Exception as exc:
        db.update_storage_recovery_job(job["id"], status="error", last_error=str(exc))
        await query.edit_message_text(f"❌ Destination check failed.\n\n{db.md_escape(str(exc))}", parse_mode="Markdown", reply_markup=_recovery_menu_kb(db.get_storage_recovery_job(job["id"])))
        return

    rows = db.all_videos(limit=1000000, offset=0)
    # Recover every catalog item, not only rows with backup_msg_id. A stale/missing
    # Backup mapping can be repaired from Primary on a per-video basis.
    recoverable = [r for r in rows if (r.get("primary_msg_id") or r.get("backup_msg_id"))]
    recoverable.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("id"))))
    ids = [str(r["id"]) for r in recoverable]

    if not ids:
        db.update_storage_recovery_job(job["id"], status="error", total=0, last_error="No usable Primary/Backup message references")
        await query.edit_message_text("🛡️ *No recoverable Primary/Backup references found.*", parse_mode="Markdown", reply_markup=_recovery_menu_kb(db.get_storage_recovery_job(job["id"])))
        return

    # Keep total stable for the job. If this job was created before a DB restart, seed missing item rows.
    if int(job.get("total") or 0) != len(ids):
        db.update_storage_recovery_job(job["id"], total=len(ids))
        db.create_storage_recovery_job(job["id"], user_id, source, dest, len(ids), ids)
        db.update_storage_recovery_job(job["id"], status="running")
        job = db.get_storage_recovery_job(job["id"])
    else:
        # Ensure any missing per-item checkpoint rows exist.
        db.create_storage_recovery_job(job["id"], user_id, source, dest, len(ids), ids)
        db.update_storage_recovery_job(job["id"], status="running")
        job = db.get_storage_recovery_job(job["id"])

    await query.edit_message_text(_recovery_text(job, "Current Backup", dest_info.get("title") or "Recovery Channel"), parse_mode="Markdown", reply_markup=_recovery_menu_kb(job))

    for idx, row in enumerate(recoverable, start=1):
        # A completed checkpoint is never copied again.
        item = db.get_storage_recovery_item(job["id"], str(row["id"]))
        if item and item.get("status") == "copied":
            if item.get("dest_message_id") and not row.get("recovery_msg_id"):
                db.set_recovery_msg_id(str(row["id"]), int(item["dest_message_id"]))
            db.update_storage_recovery_job(job["id"], next_index=max(int(job.get("next_index") or 0), idx))
            job = db.get_storage_recovery_job(job["id"])
            continue

        ctl = controls[job["id"]]
        if ctl.get("cancel"):
            db.update_storage_recovery_job(job["id"], status="cancelled")
            break
        if ctl.get("pause"):
            db.update_storage_recovery_job(job["id"], status="paused")
            break
        if ctl.get("stop"):
            db.update_storage_recovery_job(job["id"], status="stopped")
            break

        # Prefer Backup, then automatically repair from Primary if Backup's
        # stored message is stale/missing.
        candidates = []
        if row.get("backup_msg_id") and storage_config.backup():
            candidates.append((int(storage_config.backup()), int(row["backup_msg_id"]), "Backup"))
        if row.get("primary_msg_id") and storage_config.primary():
            candidates.append((int(storage_config.primary()), int(row["primary_msg_id"]), "Primary"))
        msg = None
        used_source = None
        errors = []
        for src_chat, src_msg, src_label in candidates:
            try:
                msg = await context.bot.copy_message(chat_id=dest, from_chat_id=src_chat, message_id=src_msg)
                used_source = src_label
                break
            except Exception as exc:
                errors.append(f"{src_label}: {str(exc)[:180]}")
        if msg is not None:
            cover_message_id = None
            cover_status = "skipped"
            if row.get("cover_file_id"):
                try:
                    cover = await context.bot.send_photo(chat_id=dest, photo=row["cover_file_id"])
                    cover_message_id = int(cover.message_id)
                    cover_status = "copied"
                except Exception as cover_exc:
                    cover_status = "failed"
                    log.warning("Recovery cover copy failed for %s: %s", row["id"], cover_exc)
            db.set_storage_recovery_item(job["id"], str(row["id"]), "copied", int(msg.message_id), cover_status=cover_status, dest_cover_message_id=cover_message_id)
            db.set_recovery_msg_id(str(row["id"]), int(msg.message_id))
            db.recount_storage_recovery_job(job["id"])
            db.update_storage_recovery_job(job["id"], next_index=idx, status="running", last_error=None)
            if used_source and used_source != "Backup":
                log.info("Recovery repaired %s from %s because Backup reference failed", row["id"], used_source)
        else:
            error_text = " | ".join(errors) or "No usable Primary/Backup reference"
            db.set_storage_recovery_item(job["id"], str(row["id"]), "failed", error=error_text)
            db.recount_storage_recovery_job(job["id"])
            db.update_storage_recovery_job(job["id"], next_index=idx, status="running", last_error=error_text)

        job = db.get_storage_recovery_job(job["id"])
        if idx == 1 or idx % 10 == 0 or idx == len(recoverable):
            try:
                await context.bot.edit_message_text(chat_id=user_id, message_id=query.message.message_id, text=_recovery_text(job, "Current Backup", dest_info.get("title") or "Recovery Channel"), parse_mode="Markdown", reply_markup=_recovery_menu_kb(job))
            except Exception:
                pass

        # Yield to callback handlers so pause/stop/cancel taps are processed promptly.
        await asyncio.sleep(0)

    job = db.get_storage_recovery_job(job["id"])
    status = job.get("status")
    copied = int(job.get("copied") or 0)
    failed = int(job.get("failed") or 0)
    if status == "running":
        if copied >= int(job.get("total") or 0) - int(db.storage_recovery_counts(job["id"]).get("pending", 0)) and int(job.get("next_index") or 0) >= int(job.get("total") or 0):
            # A failed item does not block completion; it remains visible and retryable via Resume.
            if failed == 0:
                try:
                    final_audit = await _service_bot_audit(context.bot, dest)
                    missing_services = [x["label"] for x in final_audit.get("services", []) if x.get("configured") and not x.get("ok")]
                except Exception:
                    final_audit = {"services": []}
                    missing_services = []
                if missing_services:
                    # Media is safely mirrored, but don't switch the live source until service bots are ready.
                    db.update_storage_recovery_job(job["id"], status="stopped", last_error="Service bot admin access is still missing in the new channel.")
                else:
                    # Recovery channel is a permanent optional 3rd content source.
                    # Never silently replace the hardcore Primary/Backup pair.
                    db.update_storage_recovery_job(job["id"], status="completed")
            else:
                db.update_storage_recovery_job(job["id"], status="stopped", last_error="Some items failed. Resume will retry failed items using Backup → Primary.")
    job = db.get_storage_recovery_job(job["id"])
    integration_note = ""
    if job.get("status") == "completed":
        try:
            final_audit = await _service_bot_audit(context.bot, dest)
            missing = [x["label"] for x in final_audit.get("services", []) if x.get("configured") and not x.get("ok")]
            if missing:
                integration_note = "\n\n⚠️ *Integration pending:* " + ", ".join(missing) + " still need admin access in the new Primary channel.\nTap *Check Bot Access* after adding them."
            else:
                integration_note = "\n\n✅ *Catalogue + Delivery integration ready.* The new channel is ready as the optional 3rd content source for all active Delivery Bots."
        except Exception as exc:
            integration_note = f"\n\n⚠️ Integration check failed: {db.md_escape(str(exc))}"
        db.log_activity("storage_bot", "storage_recovery", f"recovery {job['id']} copied={copied} failed={failed} source={source} dest={dest}")
    try:
        final_text = _recovery_text(job, "Current Backup", dest_info.get("title") or "Recovery Channel") + integration_note
        await context.bot.edit_message_text(chat_id=user_id, message_id=query.message.message_id, text=final_text, parse_mode="Markdown", reply_markup=_recovery_menu_kb(job))
    except Exception:
        pass
    finally:
        tasks.pop(job["id"], None)
        controls.pop(job["id"], None)



def _resolve_repair_video(raw: str):
    raw = (raw or "").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if raw.isdigit():
        return db.get_video_by_number(int(raw))
    return db.get_video(raw)


def _repair_video_kb(video_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔧 Rebuild Mapping", callback_data=f"repair_video:{video_id}"),
         InlineKeyboardButton("📤 Send Replacement", callback_data=f"replace_video:{video_id}")],
        [InlineKeyboardButton("🏠 Storage Menu", callback_data="storage_menu_refresh")],
    ])


def _replacement_prompt_kb():
    return InlineKeyboardMarkup([[CANCEL_BTN]])


@_per_video_serialized
async def _replacement_video(context, user_id: int, video_id: str, message_id: int, source_chat_id: int):
    """Replace stored media while preserving the existing catalog identity.

    The new media is copied to every configured storage channel first. The live
    DB mapping changes only after all required copies succeed, so a partial
    Telegram failure cannot leave a mixed old/new fallback chain.
    """
    v = db.get_video(video_id)
    if not v:
        await context.bot.send_message(user_id, "❌ Video not found. The replacement session was cleared.")
        context.user_data.pop("replacement_video_id", None)
        return

    if db.get_pending(user_id) or context.user_data.get("bulk_mode"):
        context.user_data.pop("replacement_video_id", None)
        await context.bot.send_message(user_id, "⚠️ A normal Single/Bulk upload is already active. Finish it or /cancel before replacing a saved video.")
        return

    destinations = [("Primary", storage_config.primary(), "primary_msg_id"),
                    ("Backup", storage_config.backup(), "backup_msg_id")]
    recovery_channel = storage_config.recovery()
    if recovery_channel:
        destinations.append(("3rd Content", recovery_channel, "recovery_msg_id"))
    destinations = [(label, int(cid), key) for label, cid, key in destinations if cid]
    if not destinations:
        await context.bot.send_message(user_id, "❌ No storage channels are configured. Replacement cancelled.")
        return

    created = []
    try:
        primary_label, primary_channel, _ = destinations[0]
        primary_new = await asyncio.wait_for(
            context.bot.copy_message(
                chat_id=primary_channel, from_chat_id=int(source_chat_id), message_id=int(message_id)
            ),
            timeout=30,
        )
        created.append((primary_channel, int(primary_new.message_id)))
        mapping = {"primary_msg_id": int(primary_new.message_id)}

        for label, channel_id, key in destinations[1:]:
            try:
                copied = await asyncio.wait_for(
                    context.bot.copy_message(
                        chat_id=channel_id, from_chat_id=primary_channel, message_id=int(primary_new.message_id)
                    ),
                    timeout=30,
                )
            except Exception:
                for cleanup_chat, cleanup_mid in created:
                    try:
                        await context.bot.delete_message(chat_id=cleanup_chat, message_id=cleanup_mid)
                    except Exception:
                        pass
                raise
            created.append((channel_id, int(copied.message_id)))
            mapping[key] = int(copied.message_id)

        # All copies are confirmed before the existing catalog mapping is touched.
        # Commit all source IDs in one narrow transaction; do not use the generic
        # edit path here because replacement must never alter unrelated metadata.
        db.replace_video_mappings(
            video_id,
            mapping["primary_msg_id"],
            mapping.get("backup_msg_id"),
            mapping.get("recovery_msg_id"),
        )
        db.log_activity("storage_bot", "video_replacement", f"{video_id}: replacement mappings committed atomically")

        n = v.get("video_number") or "—"
        title = db.md_escape(v.get("title") or video_id)
        lines = [
            "✅ *Video replaced successfully*", "",
            f"🔢 #{n} · *{title}*",
            f"🗂 Video ID: `{video_id}`", "",
            f"✅ Primary → msg {mapping['primary_msg_id']}",
        ]
        if "backup_msg_id" in mapping:
            lines.append(f"✅ Backup → msg {mapping['backup_msg_id']}")
        elif storage_config.backup():
            lines.append("⚠️ Backup mapping was not created because the configured Backup destination was unavailable.")
        if "recovery_msg_id" in mapping:
            lines.append(f"✅ 3rd Content → msg {mapping['recovery_msg_id']}")
        lines += [
            "",
            "🔗 The Video ID is unchanged, so existing Catalogue + Mini App watch links remain valid.",
            "♻️ Their delivery target now resolves against the new storage mappings.",
        ]
        await context.bot.send_message(user_id, "\n".join(lines), parse_mode="Markdown", reply_markup=_repair_video_kb(video_id))
    except Exception as exc:
        await context.bot.send_message(
            user_id,
            f"❌ *Replacement failed*\n\nNo catalog mapping was changed. The previous Primary/Backup/3rd Content mappings remain active.\n\nReason: {db.md_escape(str(exc)[:500])}",
            parse_mode="Markdown", reply_markup=_repair_video_kb(video_id),
        )


@_per_video_serialized
async def _repair_video(context, user_id: int, video_id: str):
    """Diagnose one video across all configured content sources and rebuild only
    missing/broken copies from a source that can actually be copied.

    A tiny probe copy is sent to the admin chat and immediately deleted. This
    gives us a real Bot API reachability test for the stored message reference,
    without requiring an external Telegram client.
    """
    v = db.get_video(video_id)
    if not v:
        await context.bot.send_message(user_id, "❌ Video not found. Use its numeric video number or ID.")
        return
    sources = []
    for label, cid, key in (
        ("Primary", storage_config.primary(), "primary_msg_id"),
        ("Backup", storage_config.backup(), "backup_msg_id"),
        ("3rd Content", storage_config.recovery(), "recovery_msg_id"),
    ):
        if cid and v.get(key):
            sources.append((label, int(cid), int(v[key]), key))
    diagnostics=[]
    valid=[]
    for label,cid,msg_id,key in sources:
        probe=None
        try:
            probe=await context.bot.copy_message(chat_id=user_id, from_chat_id=cid, message_id=msg_id)
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=int(probe.message_id))
            except Exception:
                pass
            valid.append((label,cid,msg_id,key))
            diagnostics.append(f"✅ {label}: reachable (msg {msg_id})")
        except Exception as exc:
            diagnostics.append(f"❌ {label}: {str(exc)[:160]}")
    if not valid:
        text=(f"🧰 *Video Repair — #{v.get('video_number') or '—'}*\n\n"
              f"🎬 *{db.md_escape(v.get('title') or video_id)}*\n\n"
              "No configured source could deliver this stored message reference.\n\n"
              + "\n".join(diagnostics) + "\n\n"
              "💡 The next step is to find the video in a storage channel and repair its message mapping.")
        await context.bot.send_message(user_id,text,parse_mode="Markdown",reply_markup=_repair_video_kb(video_id))
        return
    # Prefer Primary, then Backup, then 3rd Content as the canonical source.
    source = valid[0]
    repaired=[]
    skipped=[]
    for label,cid,key in (
        ("Primary", storage_config.primary(), "primary_msg_id"),
        ("Backup", storage_config.backup(), "backup_msg_id"),
        ("3rd Content", storage_config.recovery(), "recovery_msg_id"),
    ):
        if not cid:
            continue
        current = v.get(key)
        current_valid = any(x[0]==label and x[2]==int(current) for x in valid) if current else False
        if current and current_valid:
            skipped.append(f"✅ {label}: existing copy healthy")
            continue
        if int(cid) == source[1]:
            # Same source channel is healthy; store its current message ID.
            if not current:
                if key == "primary_msg_id": db.set_primary_msg_id(video_id, source[2])
                elif key == "backup_msg_id": db.set_backup_msg_id(video_id, source[2])
                else: db.set_recovery_msg_id(video_id, source[2])
                repaired.append(f"🔗 {label}: mapping restored to msg {source[2]}")
            else:
                skipped.append(f"✅ {label}: source channel reachable")
            continue
        try:
            copied=await context.bot.copy_message(chat_id=int(cid),from_chat_id=source[1],message_id=source[2])
            mid=int(copied.message_id)
            if key == "primary_msg_id": db.set_primary_msg_id(video_id, mid)
            elif key == "backup_msg_id": db.set_backup_msg_id(video_id, mid)
            else: db.set_recovery_msg_id(video_id, mid)
            repaired.append(f"🛠 {label}: rebuilt as msg {mid}")
        except Exception as exc:
            repaired.append(f"⚠️ {label}: rebuild failed — {str(exc)[:150]}")
    text=(f"🧰 *Video Repair Complete*\n\n"
          f"🔢 #{v.get('video_number') or '—'} · *{db.md_escape(v.get('title') or video_id)}*\n\n"
          "*Source diagnostics*\n"+"\n".join(diagnostics)+"\n\n"
          "*Repair result*\n"+("\n".join(repaired) if repaired else "No rebuild needed.")+"\n\n"
          +("\n".join(skipped) if skipped else "")+
          "\n\n🎯 Delivery will now try: Primary → Backup → 3rd Content.")
    await context.bot.send_message(user_id,text,parse_mode="Markdown",reply_markup=_repair_video_kb(video_id))


async def repair_video_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if context.args:
        v = _resolve_repair_video(context.args[0])
        if not v:
            await update.message.reply_text("❌ Video not found. Send `/repair 190` or `/repair VIDEO_ID`.", parse_mode="Markdown")
            return
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=(f"🧰 *Video Recovery · #{v.get('video_number') or '—'}*\n\n"
                  f"🎬 *{db.md_escape(v.get('title') or v['id'])}*\n\n"
                  "Choose an action:"),
            parse_mode="Markdown",
            reply_markup=_repair_video_kb(str(v["id"])),
        )
        return
    context.user_data["awaiting_repair_video"] = True
    await update.message.reply_text(
        "🧰 *Video Recovery*\n\nSend the video number (e.g. `190`) or video ID.\n\n"
        "Then choose *Rebuild Mapping* or *Send Replacement*.",
        parse_mode="Markdown",
    )


async def _storage_health_text(context) -> str:
    """Read-only storage integrity snapshot. Never deletes or changes media."""
    try:
        recovery_id = storage_config.recovery()
    except Exception:
        recovery_id = 0
    channel_specs = [("Primary", storage_config.primary()), ("Backup", storage_config.backup())]
    if recovery_id:
        channel_specs.append(("3rd Content", recovery_id))
    channels = await check_channel_access(context.bot, channel_specs)
    # Scan in pages instead of using one hard 10k cap. Health must remain truthful
    # as the catalogue grows beyond 10,000 videos. This is still read-only and
    # intentionally does not probe Telegram message reachability (that belongs
    # to the targeted Repair flow, where an admin explicitly asks for it).
    total = missing_primary = missing_backup = missing_recovery = missing_cover = ready = 0
    offset = 0
    page_size = 500
    while True:
        page = db.all_videos(limit=page_size, offset=offset)
        if not page:
            break
        total += len(page)
        missing_primary += sum(1 for v in page if not v.get("primary_msg_id"))
        missing_backup += sum(1 for v in page if not v.get("backup_msg_id"))
        missing_recovery += sum(1 for v in page if recovery_id and not v.get("recovery_msg_id"))
        missing_cover += sum(1 for v in page if not v.get("cover_msg_id"))
        ready += sum(1 for v in page if v.get("primary_msg_id") and v.get("backup_msg_id"))
        if len(page) < page_size:
            break
        offset += page_size
    lines = ["🩺 *Storage Health*", "", f"📚 Catalog: *{total:,} videos*",
             f"🟢 Primary + Backup ready: *{ready:,}*",
             f"🖼️ Missing catalog cover: *{missing_cover:,}*" + (" ⚠️" if missing_cover else " ✅"),
             "", "*Storage copies*"]
    for label, _, status in channels:
        lines.append(f"{status}  {label}")
    primary_pct = (100 * (total - missing_primary) / total) if total else 100.0
    backup_pct = (100 * (total - missing_backup) / total) if total else 100.0
    ready_pct = (100 * ready / total) if total else 100.0
    lines += [f"⚠️ Missing Primary mapping: *{missing_primary:,}* · coverage *{primary_pct:.1f}%*" + (" ✅" if not missing_primary else ""),
              f"⚠️ Missing Backup mapping: *{missing_backup:,}* · coverage *{backup_pct:.1f}%*" + (" ✅" if not missing_backup else ""),
              f"🎯 Fully mirrored: *{ready:,}/{total:,}* · *{ready_pct:.1f}%*"]
    if recovery_id:
        lines.append(f"🟣 3rd source configured · missing mappings: *{missing_recovery:,}*" + ("" if missing_recovery else " ✅"))
    else:
        lines.append("⚪ 3rd source: not enabled")
    lines += ["", "ℹ️ This is a database/reference scan only. Nothing is deleted automatically."]
    return "\n".join(lines)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        return

    data = query.data

    # Confirm & Save is deliberately handled BEFORE query.answer(). The Telegram
    # acknowledgement itself has been observed to stall in this environment;
    # if we await it here, the whole update queue can look frozen.
    if data == "confirm_save":
        existing_task = _confirm_tasks.get(user_id)
        if existing_task and not existing_task.done():
            spawn(_safe_callback_answer(query, "⏳ Save is already running."), name=f"save-ack-{user_id}")
            return
        task = spawn(_confirm_save_background(update, context), name=f"confirm-save-{user_id}")
        _confirm_tasks[user_id] = task
        return

    # Normal callbacks can use the standard acknowledgement, but keep a hard
    # timeout so a bad Telegram connection cannot hold the update handler forever.
    try:
        await asyncio.wait_for(query.answer(), timeout=3)
    except Exception as exc:
        log.warning("Callback acknowledgement failed/timed out: %r", exc)

    if data == "storage_repair_video":
        context.user_data["awaiting_repair_video"] = True
        await query.edit_message_text(
            "🧰 *Repair Specific Video*\n\nSend the video number (e.g. `190`) or video ID.\n\n"
            "I’ll test Primary, Backup and 3rd Content, then rebuild only missing or broken copies.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="storage_menu_refresh")]])
        )
        return

    if data.startswith("replace_video:"):
        video_id = data.split(":", 1)[1].strip()
        v = db.get_video(video_id)
        if not v:
            await query.answer("Video not found", show_alert=True)
            return
        if db.get_pending(user_id) or context.user_data.get("bulk_mode"):
            await query.answer("Finish or /cancel the current single/bulk upload first.", show_alert=True)
            return
        context.user_data["replacement_video_id"] = str(video_id)
        context.user_data["replacement_processing"] = False
        await query.answer("Replacement mode armed ✅")
        await context.bot.send_message(
            chat_id=user_id,
            text=(f"📤 *Send Replacement Video*\n\n"
                  f"🔢 #{v.get('video_number') or '—'} · *{db.md_escape(v.get('title') or video_id)}*\n\n"
                  "Send exactly ONE replacement video now.\n"
                  "It will rebuild Primary + Backup + 3rd Content while keeping the same Video ID, catalogue entry and watch links.\n\n"
                  "⚠️ This mode is isolated from normal Single/Bulk upload."),
            parse_mode="Markdown", reply_markup=_replacement_prompt_kb(),
        )
        return

    if data.startswith("repair_video:"):
        video_id = data.split(":", 1)[1].strip()
        await _repair_video(context, user_id, video_id)
        return

    # Storage recovery UI — runtime channel IDs are persisted in SQLite, so no code/config edit is required.
    if data == "storage_menu_health":
        text = await _storage_health_text(context)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="storage_menu_health")],
            [InlineKeyboardButton("🧰 Repair Specific Video", callback_data="storage_repair_video")],
            [InlineKeyboardButton("📤 Upload / Replace", callback_data="storage_menu_upload")],
            [InlineKeyboardButton("🏠 Storage Menu", callback_data="storage_menu_refresh")],
        ]))
        return

    if data == "storage_menu_content":
        await query.answer(); await _send_content_overview(user_id, context); return
    if data == "storage_menu_daily":
        await query.answer(); await _send_daily_analysis(user_id, context); return
    if data.startswith("storage_daily_"):
        await query.answer(); await _send_daily_analysis(user_id, context, data[len("storage_daily_"):]); return
    if data == "storage_menu_recovery":
        primary = storage_config.primary()
        backup = storage_config.backup()
        job = _recovery_normalize_job(db.get_active_storage_recovery_job(user_id))
        recovery = storage_config.recovery()
        text = (
            "🛡️ *Storage Recovery Center*\n\n"
            "A safe failover path for rebuilding Primary from the surviving Backup.\n\n"
            f"🟢 *PRIMARY*\n`{_fmt_channel_id(primary)}`\n   ↳ live content source\n\n"
            f"🟡 *BACKUP*\n`{_fmt_channel_id(backup)}`\n   ↳ protected recovery source\n\n"
            f"🧊 *STANDBY / RECOVERY*\n`{_fmt_channel_id(recovery)}`\n   ↳ {'ready for migration' if recovery else 'not added yet'}\n\n"
            "━━━━━━━━━━━━━━\n"
            "⚡ *Safe flow*\n"
            "Add a fresh private channel → verify Storage Bot + service bots → optionally keep it as standby → run a resumable full mirror when needed.\n\n"
            "🧩 Catalogue + all active Delivery Bots are checked for the 3rd source. Primary/Backup stay unchanged.\n"
            "💾 Media and cover checkpoints are stored after each item.\n"
            "🔐 Pause · Stop · Resume · Cancel are safe; copied media is never deleted by Cancel.\n\n"
            "🔒 No channel link, username, or invite is required."
        )
        if job:
            text += "\n\n" + _recovery_text(job)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=_recovery_menu_kb(job))
        return

    if data == "recovery_add":
        context.user_data["awaiting_recovery_channel"] = True
        context.user_data.pop("recovery_dest_id", None)
        await query.edit_message_text(
            "➕ *Add Recovery Channel*\n\n"
            "*Step 1*  ·  Create a fresh private channel\n"
            "*Step 2*  ·  Add this bot as Administrator\n"
            "*Step 3*  ·  Send the numeric channel ID below\n\n"
            "Example\n`-1001234567890`\n\n"
            "🔒 No channel link, username or invite is needed.\n"
            "🧪 The bot will verify access before anything is copied.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️  Back to Recovery", callback_data="storage_menu_recovery")]])
        )
        return

    if data == "recovery_clear":
        storage_config.clear_recovery()
        context.user_data.pop("recovery_dest_id", None)
        await query.edit_message_text("🗑 *Standby channel removed.*\n\nNo Telegram media was deleted. The two configured storage channels are unchanged.", parse_mode="Markdown", reply_markup=_recovery_menu_kb())
        return

    if data == "recovery_check_saved":
        dest = storage_config.recovery()
        if not dest:
            await query.answer("No saved recovery channel.", show_alert=True)
            return
        try:
            info = await _channel_probe(context.bot, int(dest))
            audit = await _service_bot_audit(context.bot, int(dest))
        except Exception as exc:
            await query.edit_message_text(f"❌ *Saved standby check failed*\n\n{db.md_escape(str(exc))}", parse_mode="Markdown", reply_markup=_recovery_menu_kb())
            return
        context.user_data["recovery_dest_id"] = int(dest)
        context.user_data["recovery_dest_audit"] = audit
        await query.edit_message_text(
            "🧊 *Saved Standby Readiness*\n\n"
            f"📦 *Target* · {db.md_escape(info['title'])}\n`{int(dest)}`\n\n"
            + _service_audit_text(audit)
            + "\n\n✅ This channel is saved. It can be used immediately for a full recovery or kept ready for an emergency.",
            parse_mode="Markdown", reply_markup=_recovery_confirm_kb()
        )
        return

    if data == "recovery_start_saved":
        dest = storage_config.recovery()
        if not dest:
            await query.answer("No saved recovery channel.", show_alert=True)
            return
        context.user_data["recovery_dest_id"] = int(dest)
        # Reuse the exact confirmation path below so checks and safeguards stay identical.
        data = "recovery_confirm"

    if data == "recovery_check":
        dest = context.user_data.get("recovery_dest_id") or storage_config.recovery()
        if not dest:
            job = _recovery_normalize_job(db.get_active_storage_recovery_job(user_id))
            dest = job.get("dest_channel_id") if job else 0
        if not dest:
            await query.edit_message_text("➕ Add a Recovery Channel first.", reply_markup=_recovery_menu_kb())
            return
        try:
            info = await _channel_probe(context.bot, int(dest))
            audit = await _service_bot_audit(context.bot, int(dest))
        except Exception as exc:
            await query.edit_message_text(
                f"❌ *Channel integration check failed*\n\n{db.md_escape(str(exc))}",
                parse_mode="Markdown",
                reply_markup=_recovery_menu_kb(),
            )
            return
        context.user_data["recovery_dest_audit"] = audit
        text = (
            "🛡️ *Recovery Channel Readiness*\n\n"
            f"📦 *Target* · {db.md_escape(info['title'])}\n"
            f"`{int(dest)}`\n\n"
            + _service_audit_text(audit)
            + "\n\n💡 The Storage Bot automatically switches the runtime Primary after a successful migration; Catalogue/Delivery already read that runtime setting."
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=_recovery_confirm_kb())
        return

    if data == "recovery_confirm":
        if not (context.user_data.get("recovery_dest_id") or storage_config.recovery()):
            await query.answer("Add a recovery channel first.", show_alert=True)
            return
        existing = _recovery_normalize_job(db.get_active_storage_recovery_job(user_id))
        if existing:
            await query.edit_message_text(_recovery_text(existing), parse_mode="Markdown", reply_markup=_recovery_menu_kb(existing))
            return
        dest = int(context.user_data.get("recovery_dest_id") or storage_config.recovery())
        source = int(storage_config.backup())
        if not source:
            await query.answer("Backup channel is not configured.", show_alert=True)
            return
        try:
            await _channel_probe(context.bot, source)
            audit = await _service_bot_audit(context.bot, dest)
            context.user_data["recovery_dest_audit"] = audit
        except Exception as exc:
            audit = {"services": [], "error": str(exc)}
        missing_services = [x["label"] for x in audit.get("services", []) if x.get("configured") and not x.get("ok")]
        rows = [r for r in db.all_videos(limit=1000000, offset=0) if r.get("backup_msg_id")]
        ids = [str(r["id"]) for r in rows]
        if not ids:
            await query.edit_message_text("❌ No backup message references were found to recover.", reply_markup=_recovery_menu_kb())
            return
        import secrets
        job_id = f"sr_{secrets.token_hex(6)}"
        db.create_storage_recovery_job(job_id, user_id, source, dest, len(ids), ids)
        context.user_data["recovery_job_id"] = job_id
        task = spawn(_recovery_run(update, context, job_id), name=f"storage-recovery-{user_id}")
        tasks, _ = _recovery_state_store(); tasks[job_id] = task
        return

    if data in {"recovery_resume", "recovery_pause", "recovery_stop", "recovery_cancel"}:
        job = _recovery_normalize_job(db.get_active_storage_recovery_job(user_id))
        if not job:
            await query.edit_message_text("❌ No active recovery session found.", reply_markup=_recovery_menu_kb())
            return
        tasks, controls = _recovery_state_store()
        if data == "recovery_pause":
            ctl = _recovery_controls_for(job["id"]); ctl["pause"] = True
            db.update_storage_recovery_job(job["id"], status="paused")
            await query.edit_message_text(_recovery_text(db.get_storage_recovery_job(job["id"])), parse_mode="Markdown", reply_markup=_recovery_menu_kb(db.get_storage_recovery_job(job["id"])))
            return
        if data == "recovery_stop":
            ctl = _recovery_controls_for(job["id"]); ctl["stop"] = True
            db.update_storage_recovery_job(job["id"], status="stopped")
            await query.edit_message_text(_recovery_text(db.get_storage_recovery_job(job["id"])), parse_mode="Markdown", reply_markup=_recovery_menu_kb(db.get_storage_recovery_job(job["id"])))
            return
        if data == "recovery_cancel":
            task = tasks.get(job["id"])
            ctl = _recovery_controls_for(job["id"]); ctl["cancel"] = True
            db.update_storage_recovery_job(job["id"], status="cancelled")
            await query.edit_message_text(_recovery_text(db.get_storage_recovery_job(job["id"])), parse_mode="Markdown", reply_markup=_recovery_menu_kb(db.get_storage_recovery_job(job["id"])))
            return
        # Resume
        running = tasks.get(job["id"])
        if running and not running.done():
            await query.answer("Recovery is already running.")
            return
        # Recover from persisted state; do not reset checkpoints.
        task = spawn(_recovery_run(update, context, job["id"]), name=f"storage-recovery-resume-{user_id}")
        tasks[job["id"]] = task
        return

    # Start-menu UI
    if data == "storage_resume_bulk":
        if not _restore_bulk(context, user_id):
            await query.edit_message_text("⌛ No recoverable bulk draft was found.", reply_markup=_storage_start_kb())
            return
        items = context.user_data.get("bulk_items") or []
        stage = context.user_data.get("bulk_stage")
        if stage == "cover":
            text, kb = _cover_prompt_text(bulk=True)
            await query.edit_message_text(f"♻️ *Bulk resumed* · {len(items)} item(s) · {_media_summary(items)}\n\n{text}", parse_mode="Markdown", reply_markup=kb)
        elif stage in ("title", "tags", "description", "schedule", "number", "subcategory", "confirm"):
            await query.edit_message_text(f"♻️ *Bulk resumed* · {len(items)} item(s) · {_media_summary(items)}\n\nContinue from stage: *{stage}*.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👀 Open Preview", callback_data="bulk_open_preview")], [CANCEL_BTN]]))
        else:
            await query.edit_message_text(f"♻️ *Bulk resumed* · {len(items)} item(s) · {_media_summary(items)}\n\nSend more media or /bulkdone.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏁 Finish Batch", callback_data="storage_menu_bulkdone")], [CANCEL_BTN]]))
        return

    if data == "bulk_open_preview":
        if not _restore_bulk(context, user_id):
            await query.edit_message_text("⌛ Bulk draft expired.", reply_markup=_storage_start_kb())
            return
        if context.user_data.get("bulk_title") and context.user_data.get("bulk_stage") == "confirm":
            await _send_bulk_preview(context, user_id)
        else:
            await query.edit_message_text("🧭 This draft is not at the final preview yet. Continue the requested stage in chat, then the preview will appear.", reply_markup=InlineKeyboardMarkup([[CANCEL_BTN]]))
        return

    if data == "storage_menu_upload":
        await query.edit_message_text(
            "📤 *Upload Video*\n\nSend the video (or video document) now.\n\n"
            "I’ll copy it to Primary + Backup and then ask for cover, title, tags and description.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")],
                [InlineKeyboardButton("🏠 Storage Menu", callback_data="storage_menu_refresh")],
            ]),
        )
        return

    if data == "storage_menu_bulk":
        context.user_data["bulk_mode"] = True
        context.user_data["bulk_items"] = []
        context.user_data.pop("bulk_stage", None)
        await query.edit_message_text(
            "📦 *Bulk Upload Mode*\n\n"
            "Send your videos one by one. When finished, use /bulkdone.\n\n"
            "Each item will be copied to Primary + Backup only when you confirm the batch.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏁 Finish Batch", callback_data="storage_menu_bulkdone")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")],
            ]),
        )
        return

    if data == "storage_menu_bulkdone":
        _cancel_single_wait(user_id)
        items = context.user_data.get("bulk_items") or []
        if not items:
            await query.answer("No videos collected yet.", show_alert=True)
            return
        context.user_data["bulk_stage"] = "cover"
        text, kb = _cover_prompt_text(bulk=True)
        await query.edit_message_text(f"✅ {len(items)} item(s) collected.\n\n{text}", parse_mode="Markdown", reply_markup=kb)
        return

    if data == "storage_menu_defaults":
        await query.edit_message_text(
            "⚙️ *Default Settings*\n\n"
            "🖼 Default covers: `/defaults`\n"
            "📝 Default title: `/setdefaulttitle TITLE`\n"
            "🖼 Set default cover: `/setdefaultcover`\n"
            "➕ Add another default cover: `/adddefaultcover`\n"
            "🗑 Clear default covers: `/cleardefaultcovers`\n\n"
            "Use the commands above, then return to the Storage Menu.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Storage Menu", callback_data="storage_menu_refresh")]]),
        )
        return

    if data == "storage_menu_status":
        pending = db.get_pending(user_id)
        if context.user_data.get("bulk_mode"):
            n = len(context.user_data.get("bulk_items") or [])
            text = f"📊 *Status*\n\n📦 Bulk upload active\n🎬 Items collected: {n}"
        elif pending:
            label = STAGE_LABELS.get(pending.get("stage"), pending.get("stage", "Unknown"))
            text = f"📊 *Status*\n\n⏳ Current upload: {label}"
        else:
            text = "📊 *Status*\n\n✅ Nothing is currently in progress.\n\nSend a video whenever you’re ready."
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Storage Menu", callback_data="storage_menu_refresh")]]))
        return

    if data == "storage_menu_commands":
        await query.edit_message_text(
            "🛠 *Storage Commands*\n\n"
            "`/single` — explicitly start single mode\n"
            "`/bulk` — start bulk mode\n"
            "`/flow` — show smart upload rules\n"
            "`/bulkdone` — finish bulk collection\n"
            "`/status` — current upload status\n"
            "`/defaults` — default cover settings\n"
            "`/storagehealth` — storage/reference health scan\n"
            "`/editbulk ID` — edit a saved collection cover\n"
            "`/cancel` — cancel current upload/batch\n\n"
            "You can also use the buttons in the main menu.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Storage Menu", callback_data="storage_menu_refresh")]]),
        )
        return

    if data == "storage_menu_cancel":
        _cancel_single_wait(user_id)
        # Use the same complete upload-session reset as /cancel. Keeping a
        # hidden flag such as force_single_next/replacement state after a menu
        # cancel can change the behavior of the next unrelated upload.
        _reset_upload_session(context)
        db.clear_pending(user_id)
        db.clear_bulk_session(user_id)
        await query.edit_message_text("🗑 Current upload/batch cancelled.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Storage Menu", callback_data="storage_menu_refresh")]]))
        return

    if data == "storage_menu_refresh":
        await query.edit_message_text(_storage_start_text(), reply_markup=_storage_start_kb())
        return

    if data in ("convert_pending_to_bulk", "keep_pending_single"):
        if data == "keep_pending_single":
            context.user_data.pop("suggest_bulk_media", None)
            await query.edit_message_text("🎬 Keeping the first upload as a single item. Finish its preview or /cancel.")
            return
        pending = db.get_pending(user_id)
        suggested = context.user_data.pop("suggest_bulk_media", None)
        if not pending or not suggested:
            await query.edit_message_text("⌛ That conversion prompt has expired. Start /bulk and send the media again.")
            return
        first = {
            "source_chat_id": pending.get("source_chat_id"),
            "source_message_id": pending.get("source_message_id"),
            "duration_seconds": pending.get("duration_seconds"),
            "file_size_bytes": pending.get("file_size_bytes"),
            "media_type": pending.get("media_type") or "video",
            "media_group_id": pending.get("media_group_id"),
        }
        if not first["source_chat_id"] or not first["source_message_id"]:
            await query.edit_message_text("❌ The first upload is missing its source message. Please re-upload it with /bulk.")
            return
        db.clear_pending(user_id)
        context.user_data["bulk_mode"] = True
        context.user_data["bulk_items"] = [first, suggested]
        context.user_data["bulk_stage"] = None
        msg = await context.bot.send_message(
            chat_id=user_id,
            text="📦 *Converted to Bulk*\n\n"
                 "Your first upload + the new media are now one pending collection. "
                 "Send more video/photos, then /bulkdone. Nothing is copied to Primary/Backup until you confirm.",
            parse_mode="Markdown",
        )
        context.user_data["bulk_progress_msg_id"] = msg.message_id
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data in ("editbatch_cover_default", "editbatch_cover_random", "editbatch_cover_manual"):
        batch_id = context.user_data.get("edit_batch_id")
        batch = db.get_batch(batch_id) if batch_id else None
        if not batch:
            await query.edit_message_text("⌛ Bulk edit session expired. Run `/editbulk BATCH_ID` again.", parse_mode="Markdown")
            return
        mode = data.rsplit("_", 1)[1]
        if mode == "manual":
            context.user_data["awaiting_batch_cover"] = True
            await query.edit_message_text("✋ Send the new cover photo now. It will replace the shared cover for the whole collection.")
            return
        chooser = db.choose_default_cover(randomize=(mode == "random"))
        if not chooser:
            await query.edit_message_text("⚠️ No default covers are configured. Add a cover first or choose Manual.")
            return
        try:
            db.update_batch_cover(batch_id, chooser.get("file_id"), int(chooser["msg_id"]))
            label = "🎲 Random cover" if mode == "random" else "🖼️ Default cover"
            context.user_data.pop("edit_batch_id", None)
            await query.edit_message_text(f"✅ {label} applied to bulk `{batch_id}`.\n\nAll collection items now use the new cover.", parse_mode="Markdown")
        except Exception as e:
            log.exception("Bulk cover update failed")
            await query.edit_message_text(f"❌ Could not update bulk cover: {e}")
        return

    if data == "bulk_change_cover":
        context.user_data["bulk_stage"] = "cover"
        text, kb = _cover_prompt_text(bulk=True)
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", reply_markup=kb)
        return

    async def reply_fn(text, parse_mode=None, reply_markup=None):
        await context.bot.send_message(
            chat_id=user_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup
        )

    async with _lock_for(user_id):
        if data.startswith(("cover_", "bulkcover_")):
            is_bulk = data.startswith("bulkcover_")
            prefix = "bulkcover" if is_bulk else "cover"
            if data == f"{prefix}_frame_smart":
                task = spawn(_generate_smart_best_frame(update, context, is_bulk), name=f"frame-smart-{user_id}")
                _cover_tasks[user_id] = task
                return
            if data == f"{prefix}_frame_random":
                task = spawn(_generate_random_frame_cover(update, context, is_bulk), name=f"frame-random-{user_id}")
                _cover_tasks[user_id] = task
                return
            if data == f"{prefix}_frame_shots":
                await query.edit_message_text("🎬 *Video Shot Cover*\n\nHow many screenshots should I generate, and roughly how many seconds apart?", parse_mode="Markdown", reply_markup=_frame_picker_settings_kb(prefix))
                return
            if data == f"{prefix}_back":
                text, kb = _cover_prompt_text(bulk=is_bulk)
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
                return
            if re.match(rf"^{prefix}_shots_\d+_\d+$", data):
                _, _, count_s, interval_s = data.split("_")
                task = spawn(_generate_frame_shots(update, context, is_bulk, int(count_s), int(interval_s)), name=f"frame-shots-{user_id}")
                _cover_tasks[user_id] = task
                return
            if data == f"{prefix}_shots_custom":
                context.user_data["awaiting_frame_shots"] = {"is_bulk": is_bulk}
                await query.edit_message_text("✍️ Send *count + interval* like `8 5` for 8 shots, roughly 5 seconds apart.\n\nAllowed: 2–12 shots · 1–300 seconds.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data=f"{prefix}_frame_shots")], [CANCEL_BTN]]))
                return
            if re.match(rf"^{prefix}_shot_\d+$", data):
                await _choose_generated_shot(update, context, data.rsplit("_",1)[1], is_bulk)
                return
            mode = data.split("_", 1)[1]
            if mode in ("default", "random"):
                running = _cover_tasks.get(user_id)
                if running and not running.done():
                    spawn(_safe_callback_answer(query, "⏳ Cover is already being applied."), name=f"cover-ack-{user_id}")
                    return
                task = spawn(_apply_cover_choice_background(update, context, mode, is_bulk),
                             name=f"cover-{mode}-{user_id}")
                _cover_tasks[user_id] = task
                return
            # manual remains a simple state change; no Telegram media call here.
            if is_bulk:
                context.user_data["bulk_stage"] = "cover"
                _persist_bulk(context, user_id)
                await context.bot.send_message(chat_id=user_id, text="✋ Send the batch cover photo now.", reply_markup=InlineKeyboardMarkup([[CANCEL_BTN]]))
            else:
                pending_now = db.get_pending(user_id)
                db.update_pending(user_id, stage="awaiting_cover", edit_return=1 if pending_now and pending_now.get("stage") == "awaiting_confirm" else 0)
                await context.bot.send_message(chat_id=user_id, text="✋ Send the manual cover photo now.", reply_markup=InlineKeyboardMarkup([[CANCEL_BTN]]))
            return

        if data in ("access_single", "access_bulk"):
            context.user_data["access_target"] = "single" if data == "access_single" else "bulk"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌍 Public", callback_data="access_set_free"), InlineKeyboardButton("📺 Ad Unlock", callback_data="access_set_ad")],
                [InlineKeyboardButton("🎟️ Redeem Code", callback_data="access_set_redeem"), InlineKeyboardButton("🎟️+📺 Redeem OR Ad", callback_data="access_set_redeem_or_ad")],
                [InlineKeyboardButton("💎 Redeem Membership Only", callback_data="access_set_members")],
                [InlineKeyboardButton("👤 Specific Users", callback_data="access_set_users")],
                [InlineKeyboardButton("↩️ Back", callback_data="access_back")],
            ])
            await context.bot.send_message(chat_id=user_id, text="🔐 *Who should be able to receive this content?*\n\nChoose an access rule. You can change it again before saving.", parse_mode="Markdown", reply_markup=kb)
            return

        if data == "access_back":
            if context.user_data.get("access_target") == "bulk":
                await _send_bulk_preview(context, user_id)
            else:
                await _send_preview(context, user_id)
            return

        if data.startswith("access_set_"):
            choice = data[len("access_set_"):]
            target = context.user_data.get("access_target", "single")
            if choice == "redeem":
                context.user_data["awaiting_access_input"] = "redeem_bulk" if target == "bulk" else "redeem_single"
                await context.bot.send_message(chat_id=user_id, text="🎟️ Send the existing redeem code that should unlock this content.")
                return
            if choice == "users":
                context.user_data["awaiting_access_input"] = "users_bulk" if target == "bulk" else "users_single"
                await context.bot.send_message(chat_id=user_id, text="👤 Send Telegram user IDs separated by commas. Example: `123,456,789`", parse_mode="Markdown")
                return
            tier = {"free":"free", "ad":"ad", "redeem_or_ad":"redeem_or_ad", "members":"members"}.get(choice, "free")
            if target == "bulk":
                context.user_data["bulk_access_tier"] = tier
                context.user_data.pop("bulk_access_redeem_code", None)
                context.user_data.pop("bulk_access_user_ids", None)
                _persist_bulk(context, user_id)
                await _send_bulk_preview(context, user_id)
            else:
                db.update_pending(user_id, access_tier=tier, access_redeem_code=None, access_user_ids=None)
                await _send_preview(context, user_id)
            return

        if data == "bulk_confirm":
            # callback_router already owns this user's lock. Acquiring it again here
            # deadlocks the handler and makes Save Batch look completely silent.
            items = context.user_data.get("bulk_items") or []
            if not items:
                await _finish_preview_message(query, user_id, context, "🗑 Bulk session is empty or already committed.")
                return
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                log.debug("Bulk save: preview markup already unavailable")
            progress_message = await context.bot.send_message(
                chat_id=user_id,
                text=_save_progress_text(0, len(items), "Preparing batch…"),
            )
            context.user_data["bulk_save_progress_msg_id"] = progress_message.message_id

            unique_items = []
            seen_sources = set()
            for item in items:
                key = (item.get("source_chat_id"), item.get("source_message_id"))
                if key in seen_sources:
                    continue
                seen_sources.add(key)
                unique_items.append(item)
            items = unique_items
            total_items = len(items)
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id, message_id=progress_message.message_id,
                    text=_save_progress_text(0, total_items, "Starting Telegram copy…"),
                )
            except Exception:
                pass

            # Crash-safe resume: if Telegram/database work already completed for
            # these exact source messages, never copy them again. This is the
            # important second layer beyond the UI lock.
            existing_rows = [db.get_video_by_source(i.get("source_chat_id"), i.get("source_message_id")) for i in items]
            existing_rows = [r for r in existing_rows if r]
            if existing_rows and len(existing_rows) == len(items):
                batch_ids = {r.get("batch_id") for r in existing_rows if r.get("batch_id")}
                db.clear_bulk_session(user_id)
                _reset_upload_session(context)
                label = next(iter(batch_ids)) if len(batch_ids) == 1 else "already-saved"
                await _finish_preview_message(query, user_id, context, f"♻️ This collection was already saved ({len(existing_rows)} item(s)). No duplicate copies created.\n\n📦 Batch: `{label}`")
                return
            if existing_rows:
                await _finish_preview_message(query, user_id, context, f"⚠️ {len(existing_rows)} item(s) from this collection are already saved. I stopped before copying anything else, so you don't get a partial duplicate batch.\n\nUse /cancel and send only the unsaved media as a new batch.")
                return

            try:
                committed_items = []
                for index, item in enumerate(items):
                    primary_msg_id = item.get("primary_msg_id")
                    if not primary_msg_id:
                        primary_msg = await context.bot.copy_message(
                            chat_id=storage_config.primary(),
                            from_chat_id=item["source_chat_id"],
                            message_id=item["source_message_id"],
                        )
                        primary_msg_id = primary_msg.message_id
                        items[index]["primary_msg_id"] = primary_msg_id
                        context.user_data["bulk_items"] = items
                        _persist_bulk(context, user_id)
                    backup_msg_id = item.get("backup_msg_id")
                    if not backup_msg_id:
                        try:
                            backup_msg = await context.bot.copy_message(
                                chat_id=storage_config.backup(),
                                from_chat_id=storage_config.primary(),
                                message_id=primary_msg_id,
                            )
                            backup_msg_id = backup_msg.message_id
                            items[index]["backup_msg_id"] = backup_msg_id
                            context.user_data["bulk_items"] = items
                            _persist_bulk(context, user_id)
                        except Exception:
                            log.exception("Bulk backup copy failed after confirmation")

                    recovery_msg_id = item.get("recovery_msg_id")
                    recovery_channel = storage_config.recovery()
                    if recovery_channel and not recovery_msg_id:
                        try:
                            recovery_msg = await context.bot.copy_message(
                                chat_id=recovery_channel,
                                from_chat_id=storage_config.primary(),
                                message_id=primary_msg_id,
                            )
                            recovery_msg_id = recovery_msg.message_id
                            items[index]["recovery_msg_id"] = recovery_msg_id
                            context.user_data["bulk_items"] = items
                            _persist_bulk(context, user_id)
                        except Exception:
                            log.exception("Bulk 3rd content copy failed after confirmation")
                    committed_items.append({**item, "primary_msg_id": primary_msg_id, "backup_msg_id": backup_msg_id, "recovery_msg_id": recovery_msg_id})
                    done = len(committed_items)
                    try:
                        await context.bot.edit_message_text(
                            chat_id=user_id, message_id=progress_message.message_id,
                            text=_save_progress_text(done, total_items, f"Copied {done}/{total_items} media"),
                        )
                    except Exception:
                        pass

                cover_msg_id = context.user_data.get("bulk_cover_msg_id")
                cover_file_id = context.user_data.get("bulk_cover_file_id")
                if cover_file_id and not cover_msg_id:
                    cover_source_chat = context.user_data.get("bulk_cover_source_chat_id")
                    cover_source_msg = context.user_data.get("bulk_cover_source_message_id")
                    if cover_source_chat and cover_source_msg:
                        cover_primary = await context.bot.copy_message(
                            chat_id=storage_config.primary(),
                            from_chat_id=cover_source_chat,
                            message_id=cover_source_msg,
                        )
                        cover_msg_id = cover_primary.message_id
                        context.user_data["bulk_cover_msg_id"] = cover_msg_id
                        _persist_bulk(context, user_id)

                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id, message_id=progress_message.message_id,
                        text=_save_progress_text(total_items, total_items, "Writing catalog records…"),
                    )
                except Exception:
                    pass
                batch_id, video_ids = db.finalize_bulk(
                    user_id,
                    committed_items,
                    context.user_data.get("bulk_title", ""),
                    context.user_data.get("bulk_tags", ""),
                    context.user_data.get("bulk_description", ""),
                    cover_file_id,
                    cover_msg_id,
                    context.user_data.get("bulk_scheduled_at"),
                    context.user_data.get("bulk_access_tier", "free"),
                    context.user_data.get("bulk_access_redeem_code"),
                    context.user_data.get("bulk_access_user_ids"),
                    context.user_data.get("bulk_start_number"),
                    db.normalize_category(context.user_data.get("bulk_category")),
                    (context.user_data.get("bulk_subcategory") or None),
                )
                db.log_activity("storage_bot", "bulk_upload", f"{batch_id}: {len(video_ids)} items")
            except Exception as exc:
                log.exception("Bulk save failed")
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id, message_id=progress_message.message_id,
                        text=f"❌ Save stopped\n\n{db.md_escape(str(exc))}\n\nProgress is preserved. Fix the issue and tap Save Batch again.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                await _finish_preview_message(query, user_id, context, f"❌ Collection save failed: {db.md_escape(str(exc))}\n\nThe session is still open. Fix the issue and try Save Batch again.")
                return

            context.user_data["last_saved_video_id"] = batch_id
            context.user_data["last_saved_at"] = db.now_str()
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id, message_id=progress_message.message_id,
                    text=_save_progress_text(total_items, total_items, "Completed successfully ✓"),
                )
            except Exception:
                pass
            await _finish_preview_message(
                query,
                user_id,
                context,
                f"🎉 *Collection saved!*\n\n📦 {len(video_ids)} item(s) · {_media_summary(items)}\n🔗 Batch ID: `{batch_id}`\n\nCatalog will show ONE collection card and ONE Watch All link. ✨",
            )
            db.clear_bulk_session(user_id)
            _reset_upload_session(context)
            return


        # Friendly schedule picker: presets + 7-day date picker + typed input.
        if data in ("sched_s_quick", "sched_b_quick"):
            kind = "single" if data.startswith("sched_s_") else "bulk"
            if kind == "single": db.update_pending(user_id, stage="awaiting_schedule", edit_return=1)
            else: context.user_data["bulk_stage"]="schedule"; _persist_bulk(context,user_id)
            await query.edit_message_text("⏰ *Quick schedule*\n\nPick a preset, choose a date, or type your own time.",parse_mode="Markdown",reply_markup=_schedule_quick_kb(kind)); return
        if data.startswith("sched_s_") or data.startswith("sched_b_"):
            kind="single" if data.startswith("sched_s_") else "bulk"; tail=data[8:] if kind=="single" else data[8:]
            from datetime import datetime,timedelta
            if tail=="manual":
                if kind=="single": db.update_pending(user_id,stage="awaiting_schedule",edit_return=1)
                else: context.user_data["bulk_stage"]="schedule"; _persist_bulk(context,user_id)
                await context.bot.send_message(chat_id=user_id,text="✍️ Type the publish time as `YYYY-MM-DD HH:MM` or `+2h`, `+30m`, `+1d`.",parse_mode="Markdown",reply_markup=_schedule_manual_prompt(kind)); return
            if tail=="dates": await query.edit_message_text("📅 *Pick a publish date*",parse_mode="Markdown",reply_markup=_schedule_date_kb(kind)); return
            if tail.startswith("date_"): await query.edit_message_text(f"🕒 *Pick a time for {tail[5:]}*",parse_mode="Markdown",reply_markup=_schedule_time_kb(kind,tail[5:])); return
            if tail.startswith("time_"):
                _,day_iso,hhmm=tail.split("_",2); dt=datetime.strptime(f"{day_iso} {hhmm[:2]}:{hhmm[2:]}","%Y-%m-%d %H:%M").replace(tzinfo=config.TIMEZONE)
                if dt<=datetime.now(config.TIMEZONE): await query.answer("That time has already passed.",show_alert=True); return
            elif tail in ("rel_2h","rel_6h"): dt=datetime.now(config.TIMEZONE)+timedelta(hours=(2 if tail.endswith("2h") else 6))
            elif tail in ("tom_0005","tom_1800"):
                d=(datetime.now(config.TIMEZONE)+timedelta(days=1)).date(); hh,mm=(0,5) if tail.endswith("0005") else (18,0); dt=datetime.combine(d,datetime.min.time(),tzinfo=config.TIMEZONE).replace(hour=hh,minute=mm)
            else: dt=None
            if dt is not None:
                if kind=="single": db.update_pending(user_id,scheduled_at=dt.isoformat(),stage="awaiting_confirm",edit_return=0); await _send_preview(context,user_id)
                else: context.user_data["bulk_scheduled_at"]=dt.isoformat(); context.user_data["bulk_stage"]="confirm"; _persist_bulk(context,user_id); await _send_bulk_preview(context,user_id)
                await query.answer("Schedule set"); return
        if data == "schedule_tomorrow":
            db.update_pending(user_id, scheduled_at=_tomorrow_005(), stage="awaiting_confirm", edit_return=0)
            await _send_preview(context, user_id)
            return

        if data == "bulk_schedule_tomorrow":
            context.user_data["bulk_scheduled_at"] = _tomorrow_005()
            _persist_bulk(context, user_id)
            await _send_bulk_preview(context, user_id)
            return

        if data == "bulk_schedule_clear":
            context.user_data.pop("bulk_scheduled_at", None)
            _persist_bulk(context, user_id)
            await _send_bulk_preview(context, user_id)
            return

        if data == "bulk_schedule_custom":
            context.user_data["bulk_stage"] = "schedule"
            _persist_bulk(context, user_id)
            await context.bot.send_message(chat_id=user_id, text="⏰ *When should this collection appear?*\n\nPick a quick date/time or type `YYYY-MM-DD HH:MM` / `+1d`.", parse_mode="Markdown", reply_markup=_schedule_quick_kb("bulk"))
            return

        if data == "bulk_discard":
            for k in ["bulk_mode", "bulk_items", "bulk_stage", "bulk_cover_file_id", "bulk_cover_msg_id",
                      "bulk_title", "bulk_tags", "bulk_description", "bulk_scheduled_at",
                      "bulk_access_tier", "bulk_access_redeem_code", "bulk_access_user_ids", "bulk_progress_msg_id"]:
                context.user_data.pop(k, None)
            db.clear_bulk_session(user_id)
            await _finish_preview_message(query, user_id, context, "🗑 Bulk batch discarded. Channel copies remain until you remove them.")
            return

        if data == "cancel_upload":
            _cancel_single_wait(user_id)
            _reset_upload_session(context)
            db.clear_pending(user_id)
            db.clear_bulk_session(user_id)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                log.debug("Cancel: preview markup was already unavailable")
            await context.bot.send_message(chat_id=user_id, text="🗑 Cancelled. Send a new video whenever you're ready.")
            return

        if data == "retry_backup":
            pending = db.get_pending(user_id)
            if not pending:
                await context.bot.send_message(chat_id=user_id, text="This upload session has expired.")
                return
            if pending.get("backup_msg_id"):
                await context.bot.send_message(chat_id=user_id, text="✅ Backup copy already exists.")
                return
            try:
                backup_msg = await asyncio.wait_for(
                    context.bot.copy_message(
                        chat_id=storage_config.backup(),
                        from_chat_id=storage_config.primary(),
                        message_id=pending["primary_msg_id"],
                    ),
                    timeout=20,
                )
                db.update_pending(user_id, backup_msg_id=backup_msg.message_id)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    log.debug("Backup success: preview markup was already unavailable")
                await context.bot.send_message(chat_id=user_id, text="✅ Backup copy succeeded.")
            except Exception as e:
                log.exception("Retry backup failed")
                await context.bot.send_message(chat_id=user_id, text=f"❌ Retry failed: {e}")
            return

        if data in ("skip_tags", "skip_description"):
            await query.edit_message_reply_markup(reply_markup=None)
            await _process_stage_text(context, user_id, "/skip", reply_fn)
            return

        if data == "confirm_discard":
            db.clear_pending(user_id)
            await _finish_preview_message(query, user_id, context, "🗑 Discarded. Send a new video to start over.")
            return

        if data == "edit_number":
            db.update_pending(user_id, stage="awaiting_number", edit_return=1)
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🔢 Current number: #{db.get_pending(user_id).get('video_number') if db.get_pending(user_id).get('video_number') else 'AUTO'}\n\nSend a positive number to assign manually, or /auto to return to automatic numbering.",
                reply_markup=InlineKeyboardMarkup([[CANCEL_BTN]]),
            )
            return

        if data == "bulk_edit_number":
            context.user_data["bulk_stage"] = "number"
            _persist_bulk(context, user_id)
            await context.bot.send_message(
                chat_id=user_id,
                text="🔢 Send the starting video number for this batch.\n\nExample: `120` → videos become #120, #121, #122...\nSend /auto to use the next available numbers. Deleted numbers are never reused.",
                parse_mode="Markdown",
            )
            return

        if data == "pick_subcategory":
            pnd=db.get_pending(user_id)
            if not pnd: await query.answer("Upload session expired.",show_alert=True); return
            cat=db.normalize_category(pnd.get("category")); choices=db.get_subcategories(cat,limit=8,visible_only=False)
            if choices: await query.edit_message_text(f"🏷 *Existing subcategories · {cat}*\n\nTap one to prefill it.",parse_mode="Markdown",reply_markup=_subcategory_picker_kb(cat,False))
            else: await context.bot.send_message(chat_id=user_id,text=f"🏷 No existing subcategories for *{cat}* yet. Type custom tags.",parse_mode="Markdown",reply_markup=_tags_kb(cat))
            return
        if data == "custom_tags":
            db.update_pending(user_id,stage="awaiting_tags",edit_return=1); await context.bot.send_message(chat_id=user_id,text="🏷 Send comma-separated tags/subcategories.",reply_markup=InlineKeyboardMarkup([[CANCEL_BTN]])); return
        if data == "subcategory_back":
            pnd=db.get_pending(user_id); cat=db.normalize_category(pnd.get("category")) if pnd else "Global"; await query.edit_message_reply_markup(reply_markup=_tags_kb(cat)); return
        if data == "bulk_pick_subcategory":
            cat=db.normalize_category(context.user_data.get("bulk_category")); choices=db.get_subcategories(cat,limit=8,visible_only=False)
            if choices: await query.edit_message_text(f"🏷 *Existing subcategories · {cat}*\n\nTap one to use it for this collection.",parse_mode="Markdown",reply_markup=_subcategory_picker_kb(cat,True))
            else: context.user_data["bulk_stage"]="tags"; _persist_bulk(context,user_id); await context.bot.send_message(chat_id=user_id,text=f"🏷 No existing subcategories for *{cat}* yet. Type tags manually.",parse_mode="Markdown")
            return
        if data == "bulk_custom_tags":
            context.user_data["bulk_stage"]="tags"; _persist_bulk(context,user_id); await context.bot.send_message(chat_id=user_id,text="🏷 Send comma-separated tags/subcategories for this collection.",reply_markup=InlineKeyboardMarkup([[CANCEL_BTN]])); return
        if data == "bulk_subcategory_back": await _send_bulk_preview(context,user_id); return
        if data.startswith("subcategory_pick_"):
            pnd=db.get_pending(user_id); cat=db.normalize_category(pnd.get("category")) if pnd else "Global"; choices=db.get_subcategories(cat,limit=8,visible_only=False)
            try: name=choices[int(data.rsplit("_",1)[1])][0]
            except Exception: await query.answer("Subcategory unavailable.",show_alert=True); return
            db.update_pending(user_id,subcategory=name,tags=_apply_subcategory_to_tags(pnd.get("tags"),name),stage="awaiting_tags",edit_return=0); await query.answer(f"Subcategory: {name}"); await query.edit_message_reply_markup(reply_markup=_tags_kb(cat)); return
        if data.startswith("bulk_subcategory_pick_"):
            cat=db.normalize_category(context.user_data.get("bulk_category")); choices=db.get_subcategories(cat,limit=8,visible_only=False)
            try: name=choices[int(data.rsplit("_",1)[1])][0]
            except Exception: await query.answer("Subcategory unavailable.",show_alert=True); return
            context.user_data["bulk_subcategory"]=name; context.user_data["bulk_tags"]=_apply_subcategory_to_tags(context.user_data.get("bulk_tags"),name); context.user_data["bulk_stage"]="confirm"; _persist_bulk(context,user_id); await query.answer(f"Subcategory: {name}"); await _send_bulk_preview(context,user_id); return

        if data == "toggle_category":
            pending = db.get_pending(user_id)
            if not pending:
                await query.answer("Upload session expired.", show_alert=True)
                return
            new_cat = "Indian" if db.normalize_category(pending.get("category")) == "Global" else "Global"
            db.update_pending(user_id, category=new_cat)
            await query.answer(f"Category: {new_cat}")

            # During the metadata wizard, changing category must NOT jump to
            # the final preview. Keep the uploader on the same step and only
            # refresh the category button.
            stage = pending.get("stage")
            if stage == "awaiting_tags":
                await query.edit_message_reply_markup(reply_markup=_tags_kb(new_cat))
                return
            if stage == "awaiting_description":
                await query.edit_message_reply_markup(reply_markup=_desc_kb(new_cat))
                return

            # From the final preview, refresh the complete preview as before.
            await _send_preview(context, user_id)
            return

        if data == "bulk_toggle_category":
            current = db.normalize_category(context.user_data.get("bulk_category"))
            context.user_data["bulk_category"] = "Indian" if current == "Global" else "Global"
            _persist_bulk(context, user_id)
            await query.answer(f"Category: {context.user_data['bulk_category']}")
            await _send_bulk_preview(context, user_id)
            return

        if data == "edit_subcategory":
            pending=db.get_pending(user_id); cat=db.normalize_category(pending.get("category")) if pending else "Global"
            await context.bot.send_message(chat_id=user_id,text=f"🏷 *Subcategory · {cat}*\n\nChoose an existing subcategory or type your own.",parse_mode="Markdown",reply_markup=_subcategory_picker_kb(cat,False)); return

        if data == "bulk_edit_subcategory":
            # Legacy callback: tags are now the subcategory source.
            context.user_data["bulk_stage"] = "tags"
            _persist_bulk(context, user_id)
            await context.bot.send_message(
                chat_id=user_id,
                text="🏷 *Tags / Subcategories*\n\nSend comma-separated tags for the whole batch. These tags are used as subcategories in Catalogue.",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[CANCEL_BTN]]),
            )
            return

        if data == "edit_cover":
            db.update_pending(user_id, stage="awaiting_cover", edit_return=1)
            await context.bot.send_message(
                chat_id=user_id, text="🖼 Choose the new cover source.",
                reply_markup=_cover_choice_kb("cover"),
            )
            return

        if data == "edit_schedule":
            db.update_pending(user_id, stage="awaiting_schedule", edit_return=1)
            await context.bot.send_message(
                chat_id=user_id,
                text="⏰ *When should this appear?*\n\nUse a quick option below, or type your own `YYYY-MM-DD HH:MM` / `+2h` / `+1d`.",
                parse_mode="Markdown", reply_markup=_schedule_quick_kb("single"),
            )
            return

        if data == "clear_schedule":
            db.update_pending(user_id, scheduled_at=None, stage="awaiting_confirm", edit_return=0)
            await _send_preview(context, user_id)
            return

        if data.startswith("edit_"):
            field = data[len("edit_"):]
            if field in EDIT_PROMPTS:
                stage, prompt, skip_field = EDIT_PROMPTS[field]
                db.update_pending(user_id, stage=stage, edit_return=1)
                kb = _tags_kb() if skip_field == "tags" else (_desc_kb() if skip_field == "description" else InlineKeyboardMarkup([[CANCEL_BTN]]))
                await context.bot.send_message(chat_id=user_id, text=prompt, parse_mode="Markdown", reply_markup=kb)




async def _send_content_overview(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    c=db.content_dashboard()
    text=("📊 *Content Overview*\n\n"
          f"🎬 Total videos: *{c['total_videos']:,}*\n"
          f"📦 Collections: *{c['collections']:,}*\n"
          f"🟢 Live videos: *{c['live_videos']:,}*\n"
          f"⏰ Scheduled videos: *{c['scheduled_videos']:,}*\n"
          f"📦 Scheduled collections: *{c['scheduled_collections']:,}*\n"
          f"🗂 Scheduled collection items: *{c['scheduled_collection_items']:,}*")
    await context.bot.send_message(chat_id=chat_id,text=text,parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📈 Daily Analysis",callback_data="storage_menu_daily"),InlineKeyboardButton("🔄 Refresh",callback_data="storage_menu_content")],[InlineKeyboardButton("🏠 Storage Menu",callback_data="storage_menu_refresh")]]))

async def _send_daily_analysis(chat_id: int, context: ContextTypes.DEFAULT_TYPE, day_iso: str|None=None):
    from datetime import datetime,timedelta
    day_iso=day_iso or datetime.now(config.TIMEZONE).date().isoformat(); a=db.daily_content_analysis(day_iso); top=a.get("top_videos") or []
    text=(f"📈 *Daily Analysis · {day_iso}*\n\n📚 *Content*\n• New videos: *{a['new_videos']:,}*\n• New collections: *{a['new_collections']:,}*\n• Scheduled for this day: *{a['scheduled_videos']:,} videos* · *{a['scheduled_collections']:,} collections*\n• Published this day: *{a['published_videos']:,} videos*\n\n👥 *Audience*\n• Active users: *{a['active_users']:,}*\n• Deliveries: *{a['deliveries']:,}*\n• Searches: *{a['searches']:,}*\n• Ad unlocks: *{a['ad_unlocks']:,}* · Redeems: *{a['redemptions']:,}*")
    text += ("\n\n🏆 *Top Delivered*\n"+"\n".join(f"• {db.md_escape(r['title'])} — *{int(r['deliveries']):,}*" for r in top)) if top else "\n\n🏆 *Top Delivered*\n• No delivery events recorded."
    prev=(datetime.fromisoformat(day_iso).date()-timedelta(days=1)).isoformat(); nxt=(datetime.fromisoformat(day_iso).date()+timedelta(days=1)).isoformat()
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Previous",callback_data=f"storage_daily_{prev}"),InlineKeyboardButton("Today",callback_data="storage_menu_daily"),InlineKeyboardButton("Next ▶️",callback_data=f"storage_daily_{nxt}")],[InlineKeyboardButton("📊 Content Overview",callback_data="storage_menu_content"),InlineKeyboardButton("🔄 Refresh",callback_data=f"storage_daily_{day_iso}")],[InlineKeyboardButton("🏠 Storage Menu",callback_data="storage_menu_refresh")]])
    await context.bot.send_message(chat_id=chat_id,text=text,parse_mode="Markdown",reply_markup=kb)

async def daily_analysis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await _send_daily_analysis(update.effective_chat.id,context)

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Global fallback. Deliberately NEVER replies into update.effective_chat —
    for a channel-post update (which we receive because we're a channel admin)
    that chat IS the channel, and replying there can re-trigger the same crash,
    creating an infinite spam loop. Only ever notify the admin's own private
    chat, which is a completely different destination from anything that could
    have caused the error in the first place."""
    if boterror.is_transient_network_error(context.error):
        log.warning(f"Transient network hiccup (self-recovers): {context.error!r}")
        return
    log.error("Unhandled exception", exc_info=context.error)
    try:
        if config.ADMIN_USER_IDS:
            await context.bot.send_message(
                chat_id=config.ADMIN_USER_IDS[0],
                text=f"⚠️ storage_bot error: {context.error}",
            )
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
    storage_config.ensure_seeded()
    import hashlib
    token_key = hashlib.sha256(str(config.STORAGE_BOT_TOKEN or "").encode()).hexdigest()[:24]
    instance_lock = botutil.acquire_single_instance(f"polling_token_{token_key}")
    if instance_lock is None:
        return
    app = Application.builder().token(config.STORAGE_BOT_TOKEN).post_init(_startup_check).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("bulk", bulk_cmd))
    app.add_handler(CommandHandler("single", single_cmd))
    app.add_handler(CommandHandler("upload", single_cmd))
    app.add_handler(CommandHandler("workspace", start))
    app.add_handler(CommandHandler("flow", flow_cmd))
    app.add_handler(CommandHandler("bulkdone", bulkdone_cmd))
    app.add_handler(CommandHandler("editbulk", editbulk_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("storagehealth", storage_health_cmd))
    app.add_handler(CommandHandler("repair", repair_video_cmd))
    app.add_handler(CommandHandler("repairvideo", repair_video_cmd))
    app.add_handler(CommandHandler("dailyanalysis", daily_analysis_cmd))
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CommandHandler("defaults", defaults_cmd))
    app.add_handler(CommandHandler("setdefaultcover", setdefaultcover_cmd))
    app.add_handler(CommandHandler("adddefaultcover", adddefaultcover_cmd))
    app.add_handler(CommandHandler("cleardefaultcovers", cleardefaultcovers_cmd))
    app.add_handler(CommandHandler("cleardefaultcover", cleardefaultcover_cmd))
    app.add_handler(CommandHandler("setdefaulttitle", setdefaulttitle_cmd))
    app.add_handler(CommandHandler("cleardefaulttitle", cleardefaulttitle_cmd))
    # Use one media dispatcher so Telegram document-based images/videos cannot
    # fall through because their MIME type/filter classification differs from
    # the normal photo/video message type.  This is especially important for
    # rapid mixed bulk uploads (e.g. videos + photos in the same batch).
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.PHOTO | filters.Document.ALL,
        handle_media,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_error_handler(error_handler)

    log.info("Storage bot starting...")
    botutil.run_polling_resilient(app, "storage_bot")


if __name__ == "__main__":
    main()


