import bot_ux
# V6 exception triage: intentionally swallowed exceptions in this module
# are limited to best-effort cleanup/compatibility fallbacks; user-visible or
# persistence failures are logged or surfaced by their surrounding handlers.
import os
"""
Catalog Bot (audience-facing)
------------------------------
Uses Telegram's persistent bottom keyboard (ReplyKeyboardMarkup) as the main
interface — a "Today's Content" row that expands into title buttons, an
"Global" row for everything else, plus Search and Browse.

Tapping a title button sends that video's cover + details + a "Watch" button
(inline) that deep-links out to Delivery Bot, which is the only bot that
actually sends video content.

Commands still work too: /start, /menu, /today, /search <text>, /browse.
"""
import logging
import random
import asyncio
import calendar
from datetime import date, datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, WebAppInfo, MenuButtonWebApp
)
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
import delivery_link_migrator

logging.basicConfig(level=logging.INFO)
from bgtasks import spawn
log = logging.getLogger("catalog_bot")

AUTO_DELETE_SECONDS = getattr(config, "CATALOG_AUTO_DELETE_MINUTES", 0) * 60
BOT_NAME = "catalog"



# --- Telegram Web App / Mini App configuration ---
WEBAPP_URL = os.getenv("TELEGRAM_WEBAPP_URL", "https://stellar-speculoos-1d5c7e.netlify.app/").strip()

def _webapp_button():
    if not WEBAPP_URL.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        return None
    return InlineKeyboardButton("🌐 Open Web App", web_app=WebAppInfo(url=WEBAPP_URL))
# --- END Telegram Web App configuration ---

def schedule_delete(bot, chat_id, message_id):
    """Delete this message after AUTO_DELETE_SECONDS, just to keep the chat
    tidy. The recipient can already open/watch anything sent — this never
    blocks or delays delivery, only tidies up afterward. The schedule is
    persisted to the database, so a restart mid-wait reschedules (or fires
    immediately, if overdue) instead of losing track of it."""
    if AUTO_DELETE_SECONDS <= 0:
        return
    row_id = db.add_scheduled_delete(BOT_NAME, chat_id, message_id, db.future_str(AUTO_DELETE_SECONDS))
    _run_delete_job(bot, row_id, chat_id, message_id, AUTO_DELETE_SECONDS)


def _run_delete_job(bot, row_id, chat_id, message_id, delay_seconds):
    async def _job():
        await asyncio.sleep(max(0, delay_seconds))
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            # Safe no-op: this secondary cleanup/notification failure must not mask the primary operation.
            pass  # already deleted, too old, or no permission — fine to ignore
        finally:
            db.remove_scheduled_delete(row_id)

    bgtasks.spawn(_job(), name=f"catalog-delete-{row_id}")


async def _resume_scheduled_deletes(app):
    """On startup, pick back up any deletes a previous run didn't get to —
    overdue ones fire immediately, the rest resume with their remaining time."""
    pending = db.get_pending_deletes(BOT_NAME)
    if not pending:
        return
    now = datetime.now(config.TIMEZONE)
    resumed = 0
    for row in pending:
        try:
            delete_at = datetime.fromisoformat(row["delete_at"])
        except Exception:
            db.remove_scheduled_delete(row["id"])
            continue
        remaining = (delete_at - now).total_seconds()
        _run_delete_job(app.bot, row["id"], row["chat_id"], row["message_id"], remaining)
        resumed += 1
    log.info(f"Resumed {resumed} pending auto-delete(s) from before restart.")


check_channel_access = botutil.check_channel_access


async def _startup_check(app):
    log.info("Checking channel access...")
    await check_channel_access(app.bot, [("Primary Channel", storage_config.primary())])
    await _resume_scheduled_deletes(app)

PAGE_SIZE = max(5, int(getattr(config, "PAGE_SIZE", 6)))
SEND_DELAY = 0.15  # small pause between rapid sends to the same chat, avoids Telegram flood limits

BTN_TODAY = "📅 TODAY'S CONTENT"
BTN_SEARCH = "🔍 SEARCH"
BTN_BROWSE = "🗓 BROWSE"
BTN_TOP = "🔥 TOP VIDEOS"
BTN_TAGS = "🏷 TAGS"  # retained as a subcategory feature
BTN_CATEGORIES = "📂 CATEGORIES"
BTN_INDIAN = "🇮🇳 INDIAN"
BTN_OTHERS = "🌍 GLOBAL"
BTN_RANDOM = "🎲 RANDOM"
BTN_BACK = "🔙 BACK"
BTN_PREV = "◀️ PREV"
BTN_NEXT = "NEXT ▶️"


def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


# Per-chat lock: if someone taps "Today's Content" (or any listing button)
# multiple times quickly, without this each tap starts its own show_list
# and they all send their cards in parallel — flooding the chat with
# duplicates. This serializes them so a rapid second tap waits for the
# first listing to finish instead of overlapping it.
_chat_locks: dict[int, asyncio.Lock] = {}


def _lock_for(chat_id: int) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


# Light per-user cooldown so mashing buttons can't flood this bot or trip
# Telegram's own rate limits. Not a security measure — just politeness.
RATE_LIMIT_SECONDS = 1.0
_last_action: dict[int, float] = {}
_RATE_DICT_MAX = 5000  # this bot is public-facing with no bound on distinct users;
                        # without a cap this dict grows forever over the bot's lifetime


def _rate_limited(chat_id: int) -> bool:
    import time
    now = time.monotonic()
    last = _last_action.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    if len(_last_action) >= _RATE_DICT_MAX:
        # Cheap eviction: drop everything already past the cooldown window
        # rather than tracking real LRU order.
        stale = [k for k, v in _last_action.items() if now - v >= RATE_LIMIT_SECONDS]
        for k in stale:
            _last_action.pop(k, None)
    _last_action[chat_id] = now
    return False


BTN_WEBAPP = KeyboardButton("🌐 Web App", web_app=WebAppInfo(url=WEBAPP_URL)) if WEBAPP_URL else None

def main_reply_kb() -> ReplyKeyboardMarkup:
    # Keep the primary discovery actions one tap away.  This is deliberately
    # generated from one helper so listings and the home screen cannot drift
    # into different menus.
    rows = [
        [BTN_TODAY],
        [BTN_SEARCH, BTN_BROWSE],
        [BTN_TOP, BTN_RANDOM],
        [BTN_CATEGORIES, BTN_TAGS],
        [BTN_INDIAN, BTN_OTHERS],
    ]
    if BTN_WEBAPP:
        rows.append([BTN_WEBAPP])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )

def _listing_footer_markup(has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton(BTN_PREV, callback_data="catalog_prev"))
    if has_next:
        nav.append(InlineKeyboardButton(BTN_NEXT, callback_data="catalog_next"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("📋 Back to Menu", callback_data="catalog_open_menu")])
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton("🌐 Open Web App", web_app=WebAppInfo(url=WEBAPP_URL))])
    return InlineKeyboardMarkup(rows)

def _search_cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✖️ Cancel Search", callback_data="catalog_cancel_search")]
    ])


def open_menu_markup() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("☰ Open Menu", callback_data="catalog_open_menu")]]
    if WEBAPP_URL:
        rows[0].append(InlineKeyboardButton("🌐 Open", web_app=WebAppInfo(url=WEBAPP_URL)))
    return InlineKeyboardMarkup(rows)

def fmt_duration(seconds) -> str:
    if not seconds:
        return None
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def fmt_size(num_bytes) -> str:
    if not num_bytes:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _video_caption(v: dict) -> str:
    """Compact SHIVANI'S FANBASE catalogue caption.

    Keep the Telegram message short and clean: the preview image stays in its
    original aspect ratio, while the caption carries only the useful metadata.
    """
    from html import escape

    def esc(value):
        return escape(str(value or ""), quote=False)

    title = esc((v.get("title") or "Untitled").strip())
    category = esc(db.normalize_category(v.get("category")))
    access_labels = {
        "free": "🌍 Public",
        "ad": "📺 Ad Unlock",
        "redeem": "🎟️ Redeem",
        "redeem_or_ad": "🎟️ + 📺 Redeem OR Ad",
        "members": "💎 Members Only",
        "users": "👤 Private",
        "gated": "🎟️ + 📺 Redeem OR Ad",
    }
    access = esc(access_labels.get((v.get("access_tier") or "free").lower(), "🌍 Public"))
    number = v.get("video_number")

    tags = " ".join(f"#{t.strip()}" for t in (v.get("tags") or "").split(",") if t.strip())
    tags = esc(tags)

    lines = [
        "╭━━━━━━━༺✨༻━━━━━━━╮",
        "      ✨ <b>SHIVANI'S FANBASE</b> ✨",
        "╰━━━━━━━༺✨༻━━━━━━━╯",
        "",
        f"🎬 <b>{title}</b>",
    ]

    meta = f"📂 {category}"
    if number is not None:
        meta += f"  •  🔢 #{int(number)}"
    lines.append(meta)
    lines.append(f"🔐 {access}")

    if tags:
        lines.append(f"🏷 {tags}")

    count = int(v.get("batch_count") or 0)
    if count > 1:
        lines += [
            "",
            f"📦 <b>Collection · {count} items</b>",
            "🔗 One link · Watch the full set",
        ]

    lines += [
        "",
        "╭───────༺🍿༻───────╮",
        "      🔗 <b>Tap below to watch ✨</b>",
        "╰───────༺🍿༻───────╯",
    ]
    return "\n".join(lines)


def _catalog_items(videos):
    """Collapse bulk-upload rows into one audience card per batch.

    A batch/collection is represented by its first row in catalogue listings,
    with the total item count attached for the collection-style card.
    Single videos pass through unchanged.
    """
    out = []
    seen = set()
    for v in videos:
        bid = v.get("batch_id")
        if not bid:
            out.append(v)
            continue
        key = str(bid)
        if key in seen:
            continue
        seen.add(key)
        first = dict(v)
        try:
            first["batch_count"] = db.get_batch_count(bid)
        except Exception:
            # Catalogue rendering should never crash if an older DB schema
            # lacks the batch-count helper. Fall back to one item.
            first["batch_count"] = 1
        out.append(first)
    return out



def _delivery_target(*, video_id=None, batch_id=None):
    return botutil.direct_delivery_target(video_id=video_id, batch_id=batch_id)


def _delivery_link(video_id: str) -> str:
    target = _delivery_target(video_id=video_id)
    return target["url"] if target else ""


def _batch_delivery_link(batch_id: str) -> str:
    target = _delivery_target(batch_id=batch_id)
    return target["url"] if target else ""


def _register_catalog_delivery_link(target, *, target_type: str, target_id: str, chat_id: int, message_id: int, button_label: str):
    if not target or not target.get("bot_id") or not target.get("url"):
        return
    try:
        db.register_delivery_link(
            target_type=target_type, target_id=str(target_id),
            delivery_bot_id=str(target["bot_id"]),
            delivery_username=str(target.get("username") or ""),
            delivery_url=str(target["url"]), chat_id=int(chat_id),
            message_id=int(message_id), button_label=button_label,
        )
    except Exception:
        log.warning("Could not persist catalogue delivery link target=%s:%s", target_type, target_id, exc_info=True)


async def _delivery_link_update_loop(app):
    # Permanent-bot failure is detected in another process. This catalog bot
    # loop applies the already-persisted migrations using the catalog bot's own
    # token, so existing Telegram messages get a fresh direct t.me button.
    while True:
        try:
            result = await delivery_link_migrator.apply_pending_catalog_updates(app.bot, limit=100)
            if result.get("updated") or result.get("stale") or result.get("failed"):
                log.info("Delivery link updater: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Delivery link updater failed", exc_info=True)
        await asyncio.sleep(10)


async def _send_cover(bot, chat_id, v, caption=None, reply_markup=None):
    """Send a reliable catalogue preview image without modifying the image.

    Priority:
      1) video's explicit cover_file_id / cover_msg_id
      2) configured default/random cover file_id / msg_id

    The previous implementation only considered msg_id values when selecting
    the random/default cover. If the database had a valid default_cover_file_id
    but an empty/stale pool, the function returned None and the caller fell
    back to text-only output. Keep file_id and message-id paths independent so
    a valid preview can never disappear just because the pool metadata differs.
    """
    candidates = []

    def add_candidate(file_id=None, msg_id=None, source="cover"):
        if not file_id and not msg_id:
            return
        key = (str(file_id or ""), str(msg_id or ""))
        if any((str(c.get("file_id") or ""), str(c.get("msg_id") or "")) == key for c in candidates):
            return
        candidates.append({"file_id": file_id, "msg_id": msg_id, "source": source})

    # Explicit per-video cover always wins.
    add_candidate(v.get("cover_file_id"), v.get("cover_msg_id"), "video")

    # Then configured random/default cover. Prefer the selected pool item,
    # but always fall back to the canonical default setting.
    try:
        selected = db.choose_default_cover(randomize=True)
    except Exception:
        selected = None
    if selected:
        add_candidate(selected.get("file_id"), selected.get("msg_id"), "random")

    add_candidate(
        db.get_setting("default_cover_file_id"),
        db.get_setting("default_cover_msg_id"),
        "default",
    )

    # Finally add a few pool alternatives for stale/deleted message ids.
    try:
        for item in db.get_default_cover_pool():
            add_candidate(item.get("file_id"), item.get("msg_id"), "pool")
    except Exception:
        pass

    if not candidates:
        log.warning("No catalogue cover configured for video=%s", v.get("id"))
        return None

    async def _send_candidate(candidate):
        file_id = candidate.get("file_id")
        msg_id = candidate.get("msg_id")
        # Telegram file_ids are bot-specific. A cover_file_id saved by Storage
        # or Admin may therefore be unusable by Catalog Bot. Prefer it when it
        # works, but ALWAYS fall back to the portable PRIMARY_CHANNEL message
        # from the same candidate before trying another cover.
        if file_id:
            try:
                return await asyncio.wait_for(
                    bot.send_photo(
                        chat_id=chat_id,
                        photo=file_id,
                        caption=caption,
                        parse_mode="HTML" if caption else None,
                        reply_markup=reply_markup,
                    ),
                    timeout=12,
                )
            except Exception:
                if not msg_id:
                    raise
                log.debug("Catalog cover file_id is not portable; falling back to msg_id=%s", msg_id)
        if not msg_id:
            raise RuntimeError("Cover candidate has neither file_id nor msg_id")
        return await asyncio.wait_for(
            bot.copy_message(
                chat_id=chat_id,
                from_chat_id=storage_config.primary(),
                message_id=int(msg_id),
                caption=caption,
                parse_mode="HTML" if caption else None,
                reply_markup=reply_markup,
            ),
            timeout=12,
        )

    for candidate in candidates[:8]:
        try:
            # IMPORTANT: do not crop, resize, frame, or otherwise rewrite the
            # Telegram preview. The original cover image must remain untouched.
            msg = await _send_candidate(candidate)
            return msg

        except asyncio.TimeoutError:
            log.warning("Catalogue cover timed out: video=%s", v.get("id"))
            continue
        except Exception as exc:
            text = str(exc).lower()
            stale = candidate.get("msg_id") and any(x in text for x in (
                "message to copy not found", "message not found", "message_id_invalid",
            ))
            if stale:
                try:
                    db.remove_default_cover(candidate.get("msg_id"))
                except Exception:
                    pass
                continue
            log.warning("Catalogue cover candidate failed video=%s: %s", v.get("id"), exc)
            continue

    return None


async def send_single_video_card(chat_id, context, video_id):
    v = db.get_video(video_id)
    if not v or not db.is_visible(v):
        await context.bot.send_message(chat_id=chat_id, text="Video not found.")
        return
    link_target = None
    link_type = "video"
    link_id = v["id"]
    if v.get("batch_id") and db.get_batch_count(v["batch_id"]) > 1:
        count = db.get_batch_count(v["batch_id"])
        link_type = "batch"
        link_id = v["batch_id"]
        link_target = _delivery_target(batch_id=v["batch_id"])
        link_url = link_target["url"] if link_target else ""
        button_label = f"🔥 Watch All · {count}"
        rows = []
        if link_url:
            rows.append([InlineKeyboardButton(button_label, url=link_url)])
        kb = InlineKeyboardMarkup(rows) if rows else None
        caption = _video_caption(v)
    else:
        label = "🖼️ Open Image" if (v.get("media_type") or "video") == "photo" else "🔥 Watch Now"
        link_target = _delivery_target(video_id=v["id"])
        link_url = link_target["url"] if link_target else ""
        button_label = label
        rows = []
        if link_url:
            rows.append([InlineKeyboardButton(label, url=link_url)])
        kb = InlineKeyboardMarkup(rows) if rows else None
        caption = _video_caption(v)
    msg = await _send_cover(context.bot, chat_id, v, caption=caption, reply_markup=kb)
    if msg is None:
        msg = await context.bot.send_message(
            chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=kb
        )
    _register_catalog_delivery_link(
        link_target, target_type=link_type, target_id=link_id,
        chat_id=chat_id, message_id=msg.message_id, button_label=button_label,
    )
    if not link_target:
        log.warning("No routable permanent Delivery Bot was available while rendering catalogue target=%s:%s", link_type, link_id)
    schedule_delete(context.bot, chat_id, msg.message_id)

    # Deliberately do not append automatic "related videos" here. A single
    # preview should lead to exactly one requested delivery; users can return
    # to the catalogue when they want another video. This also prevents a
    # completed delivery from appearing to trigger a second video request.


# ---------- list building for kind: today / others / search / day ----------

def _videos_for(kind: str, extra):
    if kind == "today":
        videos = db.get_videos_by_date(db.today_str())
    elif kind == "others":
        today = db.today_str()
        videos = [v for v in db.all_videos(limit=10000) if v["upload_date"] != today]
    elif kind == "search":
        videos = db.search_videos(extra or "", visible_only=True)
    elif kind == "day":
        videos = db.get_videos_by_date(extra)
    elif kind == "top":
        videos = db.top_videos(30, visible_only=True)
    elif kind == "category":
        videos = db.get_videos_by_category(extra, visible_only=True)
    elif kind == "subcategory":
        cat, sub = extra
        videos = db.get_videos_by_category(cat, sub, visible_only=True)
    elif kind == "random":
        ids = list(extra or [])
        videos = [db.get_video(v_id) for v_id in ids]
        videos = [v for v in videos if v]
    else:
        return []
    # Scheduled-for-later uploads stay hidden from every audience-facing list
    # until their publish time arrives.
    return _catalog_items([v for v in videos if db.is_visible(v)])


def _header_for(kind: str, extra) -> str:
    if kind == "today":
        return f"🎬 *Today's Uploads* ({db.today_str()})"
    if kind == "others":
        return "📁 *Other Uploads*"
    if kind == "search":
        return f"🔎 *Results for* _{db.md_escape(extra)}_"
    if kind == "day":
        return f"📅 *Uploads on {extra}*"
    if kind == "top":
        return "🔥 *Top Videos*"
    if kind == "category":
        return f"📂 *{db.normalize_category(extra)} Content*"
    if kind == "subcategory":
        cat, sub = extra
        return f"📂 *{db.normalize_category(cat)}* · 🏷 *{db.md_escape(sub)}*"
    if kind == "random":
        return "🎲 *Random Picks*"
    return "Results"


async def show_list(chat_id, context, user_data, kind: str, extra=None, page: int = 0):
    lock = _lock_for(chat_id)
    if lock.locked():
        # A listing is already being sent to this chat — a rapid repeat tap
        # gets ignored instead of queuing up and flooding once the first
        # one finishes.
        return
    async with lock:
        await _show_list_impl(chat_id, context, user_data, kind, extra, page)


async def _show_list_impl(chat_id, context, user_data, kind: str, extra, page: int):
    videos = _videos_for(kind, extra)
    total = len(videos)
    header = _header_for(kind, extra)

    if total == 0:
        user_data.pop("active_kind", None)
        user_data.pop("active_extra", None)
        user_data.pop("active_page", None)
        empty_lines = {
            "today": "Nothing uploaded yet today — check back soon, or tap 🔥 TOP VIDEOS for what's popular.",
            "others": "No older uploads yet — everything's still in Today's Content.",
            "search": "No matches — try a different keyword, or tap 🏷 TAGS to browse by topic instead.",
            "day": "Nothing was uploaded that day — try 📅 TODAY'S CONTENT for the latest.",
            "top": "No views yet — check 📅 TODAY'S CONTENT to be the first to watch something.",
            "random": "No videos available for a random pick yet.",
        }
        empty_text = empty_lines.get(kind, "Nothing here yet.")
        msg = await context.bot.send_message(
            chat_id=chat_id, text=f"{header}\n\n{empty_text}",
            parse_mode="Markdown", reply_markup=main_reply_kb(),
        )
        schedule_delete(context.bot, chat_id, msg.message_id)
        return

    start = page * PAGE_SIZE
    page_videos = videos[start:start + PAGE_SIZE]
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    user_data["active_kind"] = kind
    user_data["active_extra"] = extra
    user_data["active_page"] = page

    header_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"{header}\n\n✨ *{total} result(s)* · Page {page + 1}/{total_pages}\nTap a card below to open it.",
        parse_mode="Markdown",
        reply_markup=main_reply_kb(),
    )
    schedule_delete(context.bot, chat_id, header_msg.message_id)

    # Send every video on this page as a full preview card right away —
    # no extra tap needed to see what's there.
    for i, v in enumerate(page_videos):
        count = int(v.get("batch_count") or db.get_batch_count(v["batch_id"])) if v.get("batch_id") else 1
        link_target = None
        link_type = "video"
        link_id = v["id"]
        if v.get("batch_id") and count > 1:
            link_type = "batch"
            link_id = v["batch_id"]
            link_target = _delivery_target(batch_id=v["batch_id"])
            link_url = link_target["url"] if link_target else ""
            button_label = f"🔥 Watch All · {count}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(button_label, url=link_url)]]) if link_url else None
            caption = _video_caption(v)
        else:
            link_target = _delivery_target(video_id=v["id"])
            link_url = link_target["url"] if link_target else ""
            button_label = "🔥 Watch Now"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(button_label, url=link_url)]]) if link_url else None
            caption = _video_caption(v)
        card_msg = await _send_cover(context.bot, chat_id, v, caption=caption, reply_markup=kb)
        if card_msg is None:
            card_msg = await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=kb)
        _register_catalog_delivery_link(
            link_target, target_type=link_type, target_id=link_id,
            chat_id=chat_id, message_id=card_msg.message_id, button_label=button_label,
        )
        if not link_target:
            log.warning("No routable permanent Delivery Bot was available while rendering catalogue target=%s:%s", link_type, link_id)
        schedule_delete(context.bot, chat_id, card_msg.message_id)
        if i < len(page_videos) - 1:
            await asyncio.sleep(SEND_DELAY)

    has_prev = page > 0
    has_next = start + PAGE_SIZE < total

    # Inline navigation belongs to the listing footer; the persistent bottom
    # keyboard remains the primary catalogue navigation surface.
    kb = _listing_footer_markup(has_prev, has_next)
    # Keep the navigation hint at the very end of the listing, after every
    # video card.  It is intentionally part of the same footer message as
    # the auto-delete notice, so it never appears before the actual content.
    if has_next:
        footer = "➡️ *More videos are available* — tap `NEXT ▶️` below."
    else:
        footer = "💫 *You've reached the end.*"
    if AUTO_DELETE_SECONDS > 0:
        footer += f"\n\n🧹 Messages here auto-clean after {AUTO_DELETE_SECONDS // 60} min."
    footer_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=footer,
        parse_mode="Markdown",
        reply_markup=kb,
    )
    schedule_delete(context.bot, chat_id, footer_msg.message_id)


# ---------- /start (handles deep links) ----------

async def notify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if uid not in getattr(config, "ADMIN_USER_IDS", []): return
    if not context.args:
        await update.message.reply_text("Usage: /notify <video_id>")
        return
    video_id=context.args[0].strip()
    v=db.get_video(video_id)
    if not v:
        await update.message.reply_text("❌ Video not found."); return
    sent=failed=0
    title=db.md_escape(v.get("title") or "New content")
    url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=v_{video_id}"
    for user_id in db.notification_recipients(500):
        if not db.notification_can_send(user_id,video_id,24): continue
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✨ *New drop is ready!*\\n\\n🎬 *{title}*\\n\\nOpen the catalogue to check it out.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📚 Open Catalogue",url=url)]])
            )
            db.mark_notification_sent(user_id,video_id); sent+=1
        except Exception: failed+=1
    await update.message.reply_text(f"📣 Notification sent: *{sent}*\\n⚠️ Failed/skipped: *{failed}*",parse_mode="Markdown")

async def _mandatory_gate(update, context):
    if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        return False
    joined, link, title, error = await botutil.mandatory_join_status(context.bot, update.effective_user.id, config)
    if joined:
        return True
    context.user_data["pending_mandatory_start"] = " ".join(context.args or [])
    detail = "\n\n⚠️ Telegram couldn't verify membership. Make sure this bot is an admin in the mandatory channel, then tap Check again." if error else ""
    await botutil.send_mandatory_join_prompt(update.message, title, link, detail)
    return False

async def mandatory_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        await query.answer(); return
    joined, link, title, error = await botutil.mandatory_join_status(context.bot, update.effective_user.id, config)
    if not joined:
        await query.answer("Not verified yet — join the channel, then check again. 🔐", show_alert=True); return
    await query.answer("Verified ✓")
    try:
        await query.edit_message_caption(caption=bot_ux.delivery_state_text("verified", title), parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    args = context.user_data.pop("pending_mandatory_start", "")
    context.args = args.split() if args else []
    try: await query.edit_message_reply_markup(reply_markup=None)
    except Exception: pass
    await start_cmd(update, context, _skip_gate=True)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, _skip_gate=False):
    if not _skip_gate and not await _mandatory_gate(update, context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return  # ignore channel posts (we're a channel admin) — never process or reply to these
    args = context.args
    chat_id = update.effective_chat.id
    db.upsert_notification_user(chat_id)
    db.log_analytics("start", user_id=chat_id, value=1)

    target_message = update.effective_message
    if target_message is None:
        return

    if not args:
        start_text = admin_store.get_template("catalog_start", botutil.premium_panel("Welcome to Video Vault", "Fresh drops · smart browsing · one-tap watch" ) + "\n\n🍿 Pick a section below and let's find something good." + bot_ux.footer_hint("New here? Start with Today or Browse."))
        start_buttons = []
        if WEBAPP_URL.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            start_buttons.append([InlineKeyboardButton("🌐 Open Web App", web_app=WebAppInfo(url=WEBAPP_URL))])
        await target_message.reply_text(
            start_text,
            reply_markup=open_menu_markup(),
        )
        return

    param = args[0]
    if param == "today":
        await show_list(chat_id, context, context.user_data, "today", page=0)
    elif param.startswith("v_"):
        await send_single_video_card(chat_id, context, param[2:])
        await target_message.reply_text("Browse more below 👇", reply_markup=main_reply_kb())
    elif param in ("cat_indian", "cat_global"):
        await send_subcategory_menu(chat_id, context, "Indian" if param.endswith("indian") else "Global")
    else:
        await target_message.reply_text("Unrecognized link.", reply_markup=main_reply_kb())


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    await update.message.reply_text(
        "📋 *Video Vault Catalogue*\n\n✨ Today, search, browse, top picks and categories.\n\n_Use the buttons below or /menu anytime._", parse_mode="Markdown", reply_markup=open_menu_markup()
    )


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    await update.message.reply_text("✨ Freshening the catalogue… give me a sec, cutie. 🔄")
    await show_list(update.effective_chat.id, context, context.user_data, "today")


async def tips_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "💡 *Catalogue Tips*\n\n"
        "🔎 Search by title or keyword.\n"
        "🏷 Browse tags when you don't know the exact title.\n"
        "🔥 Top shows what people are watching most.\n"
        "📦 Collections have a single *Watch All* link.\n"
        "🌐 The Web App is the prettiest way to browse. 😌✨",
        parse_mode="Markdown", reply_markup=main_reply_kb()
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    await show_list(update.effective_chat.id, context, context.user_data, "today", page=0)


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    if not context.args:
        context.user_data["awaiting_search"] = True
        await update.message.reply_text(
            "🔍 *Search mode*\n\nType a video number, title, or tag. Example: `125` or `movie name`.",
            parse_mode="Markdown", reply_markup=_search_cancel_markup()
        )
        return
    keyword = " ".join(context.args)
    db.log_analytics("search", user_id=update.effective_user.id, value=1, detail=keyword[:120])
    await show_list(update.effective_chat.id, context, context.user_data, "search", keyword, page=0)


async def send_categories_menu(chat_id, context):
    counts = db.get_category_counts(visible_only=True)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🇮🇳 Indian · {counts.get('Indian',0)}", callback_data="cat_Indian"),
        InlineKeyboardButton(f"🌍 Global · {counts.get('Global',0)}", callback_data="cat_Global"),
    ]])
    await context.bot.send_message(chat_id=chat_id, text="📂 *Choose a category*\n\nThen pick a subcategory below.", parse_mode="Markdown", reply_markup=kb)

async def send_subcategory_menu(chat_id, context, category):
    category = db.normalize_category(category)
    subs = db.get_subcategories(category, 12, visible_only=True)
    rows = [[InlineKeyboardButton(f"📚 All {category}", callback_data=f"catall_{category}")]]
    for name, count in subs:
        rows.append([InlineKeyboardButton(f"🏷 {name} · {count}", callback_data=f"subcat_{category}_{name[:45]}")])
    rows.append([InlineKeyboardButton("🔙 Categories", callback_data="categories_menu")])
    await context.bot.send_message(chat_id=chat_id, text=f"📂 *{category}*\n\n🏷 *Subcategories*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))

# ---------- /browse (inline calendar, coexists with the bottom keyboard) ----------

def _build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    cal = calendar.monthcalendar(year, month)
    rows = [[InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="ignore")]]
    rows.append([InlineKeyboardButton(d, callback_data="ignore") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                day_iso = date(year, month, day).isoformat()
                row.append(InlineKeyboardButton(str(day), callback_data=f"day_{day_iso}"))
        rows.append(row)
    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year
    rows.append([
        InlineKeyboardButton("◀️", callback_data=f"cal_{prev_year}_{prev_month}"),
        InlineKeyboardButton("▶️", callback_data=f"cal_{next_year}_{next_month}"),
    ])
    return InlineKeyboardMarkup(rows)


async def browse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    today = datetime.now(config.TIMEZONE).date()
    await update.message.reply_text(
        "🗓 Pick a date:", reply_markup=_build_calendar(today.year, today.month)
    )


async def send_tags_menu(chat_id, context):
    tags = db.get_popular_tags(8)
    if not tags:
        await context.bot.send_message(chat_id=chat_id, text="No tags yet.", reply_markup=main_reply_kb())
        return
    rows = [[InlineKeyboardButton(f"{t} ({c})", callback_data=f"tag_{t}")] for t, c in tags]
    await context.bot.send_message(
        chat_id=chat_id, text="🏷 *Popular Tags* — tap one to browse.",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows),
    )


async def catalog_open_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()
    try:
        await query.message.reply_text(
            "📋 *Main Menu*\n\nChoose a section below:",
            parse_mode="Markdown",
            reply_markup=main_reply_kb(),
        )
    except Exception:
        log.warning("Could not open catalogue menu", exc_info=True)


async def catalog_cancel_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message:
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await query.answer()
        return
    context.user_data.pop("awaiting_search", None)
    await query.answer("Search cancelled.")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(
        "📋 Back to the catalogue menu.", reply_markup=main_reply_kb()
    )

async def catalog_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message:
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await query.answer(); return
    await query.answer()
    action = query.data
    data = context.user_data
    kind = data.get("active_kind")
    extra = data.get("active_extra")
    page = int(data.get("active_page") or 0)
    if not kind:
        await query.message.reply_text("📋 Open the menu to choose a section.", reply_markup=open_menu_markup())
        return
    if action == "catalog_prev":
        page = max(0, page - 1)
    elif action == "catalog_next":
        page += 1
    await show_list(query.message.chat_id, context, data, kind, extra, page=page)


async def calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        await query.answer(); return
    joined, link, title, error = await botutil.mandatory_join_status(context.bot, update.effective_user.id, config)
    if not joined:
        await query.answer("Please join the mandatory channel first. 💗", show_alert=True); return

    if data == "ignore":
        await query.answer()
        return

    if data.startswith("cal_"):
        await query.answer()
        _, year, month = data.split("_")
        await query.edit_message_reply_markup(reply_markup=_build_calendar(int(year), int(month)))
        return

    if data.startswith("day_"):
        await query.answer()
        day_iso = data[4:]
        await show_list(query.message.chat_id, context, context.user_data, "day", day_iso, page=0)
        return

    if data == "categories_menu":
        await query.answer()
        await send_categories_menu(query.message.chat_id, context)
        return

    if data.startswith("cat_"):
        await query.answer()
        await send_subcategory_menu(query.message.chat_id, context, data[4:])
        return

    if data.startswith("catall_"):
        await query.answer()
        await show_list(query.message.chat_id, context, context.user_data, "category", data[7:], page=0)
        return

    if data.startswith("subcat_"):
        await query.answer()
        raw = data[7:]
        if "_" not in raw:
            return
        cat, sub = raw.split("_", 1)
        await show_list(query.message.chat_id, context, context.user_data, "subcategory", (cat, sub), page=0)
        return

    if data.startswith("tag_"):
        await query.answer()
        tag = data[4:]
        await show_list(query.message.chat_id, context, context.user_data, "search", tag, page=0)
        return

    await query.answer()


# ---------- bottom keyboard text handler ----------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type != "private":
        return  # ignore channel posts — never process or reply to these
    if not await _mandatory_gate(update, context):
        return
    chat_id = update.effective_chat.id
    db.upsert_notification_user(chat_id)
    text = update.message.text.strip()
    ud = context.user_data

    # Buttons that trigger a listing are rate-limited; plain typed searches
    # and menu navigation stay responsive even if someone's mid-search.
    if text in (BTN_TODAY, BTN_TOP, BTN_TAGS, BTN_CATEGORIES, BTN_INDIAN, BTN_OTHERS, BTN_RANDOM, BTN_PREV, BTN_NEXT) and _rate_limited(chat_id):
        return

    if text == BTN_TODAY:
        await show_list(chat_id, context, ud, "today", page=0)
        return

    if text == BTN_SEARCH:
        ud["awaiting_search"] = True
        await update.message.reply_text(
            "🔍 *Search mode*\n\nType a video number, title, or tag. Example: `125` or `movie name`.",
            parse_mode="Markdown", reply_markup=_search_cancel_markup()
        )
        return

    if text == BTN_BROWSE:
        today = datetime.now(config.TIMEZONE).date()
        await update.message.reply_text("🗓 Pick a date:", reply_markup=_build_calendar(today.year, today.month))
        return

    if text == BTN_TOP:
        await show_list(chat_id, context, ud, "top", page=0)
        return

    if text == BTN_TAGS:
        await send_tags_menu(chat_id, context)
        return

    if text == BTN_RANDOM:
        pool = [v for v in db.all_videos(limit=10000) if db.is_visible(v)]
        if not pool:
            await update.message.reply_text("No videos yet — check back soon.", reply_markup=main_reply_kb())
            return
        random.shuffle(pool)
        extra_ids = [str(v["id"]) for v in pool[:min(30, len(pool))]]
        await show_list(chat_id, context, ud, "random", extra=extra_ids, page=0)
        return

    if text == BTN_CATEGORIES:
        await send_categories_menu(chat_id, context)
        return

    if text == BTN_INDIAN:
        await send_subcategory_menu(chat_id, context, "Indian")
        return

    if text == BTN_OTHERS:
        await send_subcategory_menu(chat_id, context, "Global")
        return

    if text == BTN_BACK:
        ud.pop("active_map", None)
        ud.pop("active_kind", None)
        ud.pop("active_extra", None)
        ud.pop("active_page", None)
        await update.message.reply_text("📋 Main Menu", reply_markup=main_reply_kb())
        return

    if text in (BTN_PREV, BTN_NEXT):
        kind = ud.get("active_kind")
        extra = ud.get("active_extra")
        page = ud.get("active_page", 0)
        if not kind:
            await update.message.reply_text("📋 Main Menu", reply_markup=main_reply_kb())
            return
        new_page = page - 1 if text == BTN_PREV else page + 1
        await show_list(chat_id, context, ud, kind, extra, page=max(0, new_page))
        return

    # Otherwise, if we're waiting on a search keyword, treat this as it
    if ud.get("awaiting_search"):
        ud["awaiting_search"] = False
        keyword = text
        db.log_analytics("search", user_id=update.effective_user.id, value=1, detail=keyword[:120])
        await show_list(chat_id, context, ud, "search", keyword, page=0)
        return

    # Unrecognized free text — nudge back to the menu
    await update.message.reply_text(
        "Not sure what that means — use the menu below.", reply_markup=main_reply_kb()
    )


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Global fallback. This bot is public-facing, so a real private-chat user
    gets a generic friendly message, while the admin gets the real detail in
    their own private chat. Deliberately skips replying if effective_chat isn't
    a private chat (e.g. a channel post) — that reply could itself trigger the
    same crash again, creating an infinite spam loop."""
    if boterror.is_transient_network_error(context.error):
        log.warning(f"Transient network hiccup (self-recovers): {context.error!r}")
        return
    log.error("Unhandled exception", exc_info=context.error)
    try:
        if (isinstance(update, Update) and update.effective_chat
                and update.effective_chat.type == "private"):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Something went wrong — please try again.",
            )
    except Exception:
        log.warning("Could not notify user about catalog_bot error", exc_info=True)
    try:
        if config.ADMIN_USER_IDS:
            await context.bot.send_message(
                chat_id=config.ADMIN_USER_IDS[0],
                text=f"🐞 catalog_bot error: {context.error}",
            )
    except Exception:
        # Safe no-op: this secondary cleanup/notification failure must not mask the primary operation.
        pass


async def _post_init(app):
    await _startup_check(app)
    await botutil.configure_bot_ui(app, "catalog")
    bgtasks.spawn(_delivery_link_update_loop(app), name="catalog-delivery-link-updater")
    await app.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Open",
            web_app=WebAppInfo(
                url=WEBAPP_URL
            ),
        )
    )


def main():
    # Python 3.14 removed automatic event-loop creation (PEP 719); some versions
    # of python-telegram-bot still expect it, so create/set one manually here.
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    db.init_db()
    instance_lock = botutil.acquire_single_instance("catalog")
    if instance_lock is None:
        return
    app = Application.builder().token(config.CATALOG_BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
    app.add_handler(CommandHandler("help", menu_cmd))
    app.add_handler(CommandHandler("latest", today_cmd))
    app.add_handler(CommandHandler("home", menu_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("refresh", refresh_cmd))
    app.add_handler(CommandHandler("tips", tips_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("browse", browse_cmd))
    app.add_handler(CallbackQueryHandler(mandatory_join_callback, pattern="^mandatory_join_check$"))
    app.add_handler(CallbackQueryHandler(catalog_open_menu_callback, pattern="^catalog_open_menu$"))
    app.add_handler(CallbackQueryHandler(catalog_cancel_search_callback, pattern="^catalog_cancel_search$"))
    app.add_handler(CallbackQueryHandler(catalog_pagination_callback, pattern="^catalog_(prev|next)$"))
    app.add_handler(CallbackQueryHandler(calendar_callback, pattern="^(cal_|day_|tag_|cat_|subcat_|catall_|categories_menu|ignore)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    log.info("Catalog bot starting...")
    botutil.run_polling_resilient(app, "catalog_bot")


if __name__ == "__main__":
    main()



