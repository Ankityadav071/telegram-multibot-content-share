# V6 exception triage: intentionally swallowed exceptions in this module
# are limited to best-effort cleanup/compatibility fallbacks; user-visible or
# persistence failures are logged or surfaced by their surrounding handlers.
"""
Delivery Bot
------------
The only bot whose job is to actually hand over video content.
Reached via a deep link from Catalog Bot's "Watch" button:
    https://t.me/YourDeliveryBot?start=v_<video_id>

Sends the video with protect_content=True (no forward/save), tries the
Primary Channel first and falls back to Backup Channel if that fails.
Increments the view counter for whatever it successfully delivers.
"""
import logging
import asyncio
import random
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

import config
import storage_config
import db
import reactions
import shortlink
import bgtasks
import boterror
import botutil
import bot_ux
import admin_store

logging.basicConfig(level=logging.INFO)
from bgtasks import spawn
log = logging.getLogger("delivery_bot")

AUTO_DELETE_SECONDS = getattr(config, "DELIVERY_AUTO_DELETE_MINUTES", 0) * 60
# Delivery progress/status messages are intentionally short-lived and are
# cleaned after 30 minutes. Media deletion remains controlled by config.
STATUS_DELETE_SECONDS = 30 * 60

# Collection delivery uses exactly one transient status message per run.
# Keep message IDs here so a repeated completion callback cannot schedule the
# same status for deletion twice.
_SCHEDULED_STATUS_MESSAGES = set()
BOT_NAME = "delivery"

# Light per-user cooldown so rapid re-tapping "Watch" can't flood this bot.
RATE_LIMIT_SECONDS = 1.5
_last_action: dict[int, float] = {}
# Failed deliveries can be retried, but a Telegram user repeatedly tapping the
# button should not hammer the same broken content mapping.  This is deliberately
# local/in-memory: it is UX protection, not routing state.
_RETRY_COOLDOWN_SECONDS = 3.0
_last_retry: dict[tuple[int, str], float] = {}
_RETRY_DICT_MAX = 5000

def _pool_id():
    return os.getenv("VV_PERMANENT_DELIVERY_ID", "").strip()

def _pool_success():
    bid=_pool_id()
    if not bid: return
    try:
        import permanent_bot_store
        permanent_bot_store.record_delivery_success(bid)
    except Exception as exc:
        log.warning("Permanent bot success metric update failed: %r", exc)

def _pool_failure(error):
    bid=_pool_id()
    if not bid: return
    try:
        import permanent_bot_store
        permanent_bot_store.record_delivery_failure(bid, str(error)[:500])
    except Exception as exc:
        log.warning("Permanent bot failure metric update failed: %r", exc)
_batch_locks: dict[tuple[int, str], asyncio.Lock] = {}
_active_delivery_jobs: dict[int, dict] = {}


# --- Telegram Web App / Mini App configuration ---
WEBAPP_URL = os.getenv("TELEGRAM_WEBAPP_URL", "https://stellar-speculoos-1d5c7e.netlify.app/").strip()

def _webapp_button():
    if not WEBAPP_URL.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        return None
    return InlineKeyboardButton("🌐 Open Web App", web_app=WebAppInfo(url=WEBAPP_URL))
# --- END Telegram Web App configuration ---

def _pool_bot_url(param: str) -> str:
    try:
        import permanent_bot_store
        import random
        bots = permanent_bot_store.routable_delivery_bots(180)
        bot = random.choice(bots) if bots else None
        username = str(bot.get("username") or "").lstrip("@") if bot else ""
        if username and param:
            from urllib.parse import quote
            return f"https://t.me/{username}?start={quote(param)}"
    except Exception:
        pass
    return ""

def _batch_lock(user_id: int, batch_id: str) -> asyncio.Lock:
    key = (user_id, batch_id)
    lock = _batch_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _batch_locks[key] = lock
    return lock


def _active_job(user_id: int):
    job = _active_delivery_jobs.get(user_id)
    if not job or job.get("task") is None or job["task"].done():
        _active_delivery_jobs.pop(user_id, None)
        return None
    return job

def _register_job(user_id: int, kind: str, target_id: str):
    task = asyncio.current_task()
    if task is not None:
        _active_delivery_jobs[user_id] = {"task": task, "kind": kind, "target_id": target_id}

def _clear_job(user_id: int):
    job = _active_delivery_jobs.get(user_id)
    if job and job.get("task") is asyncio.current_task():
        _active_delivery_jobs.pop(user_id, None)

def _force_clear_job(user_id: int, task=None):
    """Remove a job registration after an intentional overtake/cancel."""
    job = _active_delivery_jobs.get(user_id)
    if not job:
        return
    if task is None or job.get("task") is task:
        _active_delivery_jobs.pop(user_id, None)

def _reply_target(update: Update):
    """Return the message object that can safely receive a reply.
    Deep-link / callback requests often have no update.message.
    """
    return update.message or (update.callback_query.message if update.callback_query else None)

async def _reply(update: Update, text: str, **kwargs):
    target = _reply_target(update)
    if target is not None:
        return await target.reply_text(text, **kwargs)
    return await update.effective_chat.send_message(text=text, **kwargs)

def _busy_kb(kind: str, target_id: str):
    button = (InlineKeyboardButton("⏩ Overtake & Start New Bulk", callback_data=f"ovb_{target_id}")
              if kind == "batch" else
              InlineKeyboardButton("⏩ Overtake & Watch This", callback_data=f"ovs_{target_id}"))
    return InlineKeyboardMarkup([[button], [InlineKeyboardButton("⏸️ Keep Current Delivery", callback_data="ovc_keep")]])

async def _ask_overtake(update: Update, job: dict, requested_kind: str, requested_id: str):
    target = update.message or update.callback_query.message
    current = "bulk delivery" if job.get("kind") == "batch" else "video delivery"
    progress = ""
    if job.get("kind") == "batch":
        p = db.get_batch_progress(update.effective_chat.id, job.get("target_id"))
        if p:
            progress = f"\n📍 Saved progress: Part {int(p['next_index']) + 1}"
    await target.reply_text(
        f"⏳ *{current.title()} is still running.*{progress}\n\n"
        "Your current delivery is safe — nothing will be lost.\n\n"
        "You can wait for it to finish, or use *Overtake* to pause it and start this request. "
        "Saved bulk progress can be resumed later.",
        parse_mode="Markdown", reply_markup=_busy_kb(requested_kind, requested_id))

_RATE_DICT_MAX = 5000  # public-facing bot, unbounded distinct users — cap growth

VIEW_MILESTONES = (100, 500, 1000, 5000, 10000, 50000)


def _rate_limited(chat_id: int) -> bool:
    import time
    now = time.monotonic()
    last = _last_action.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    if len(_last_action) >= _RATE_DICT_MAX:
        stale = [k for k, v in _last_action.items() if now - v >= RATE_LIMIT_SECONDS]
        for k in stale:
            _last_action.pop(k, None)
    _last_action[chat_id] = now
    return False


async def _check_milestone(bot, video_id: str, title: str):
    v = db.get_video(video_id)
    if not v:
        return
    count = v["view_count"]
    if count in VIEW_MILESTONES and config.ADMIN_USER_IDS:
        try:
            await bot.send_message(
                chat_id=config.ADMIN_USER_IDS[0],
                text=f"🎉 *{db.md_escape(title)}* just crossed {count} views!",
                parse_mode="Markdown",
            )
        except Exception:
            # Safe no-op: this secondary cleanup/notification failure must not mask the primary operation.
            pass


def schedule_delete(bot, chat_id, message_id, video_id: str = None):
    """Delete a delivered copy after the configured delay and leave a simple
    Watch Again action. The replacement is only a button; it never sends a
    second video automatically. Clicking it starts a fresh delivery request,
    so access and daily-limit checks run again normally."""
    if AUTO_DELETE_SECONDS <= 0:
        return
    row_id = db.add_scheduled_delete(
        BOT_NAME, chat_id, message_id, db.future_str(AUTO_DELETE_SECONDS),
        video_id=video_id,
    )
    _run_delete_job(bot, row_id, chat_id, message_id, AUTO_DELETE_SECONDS, video_id)


def _run_delete_job(bot, row_id, chat_id, message_id, delay_seconds, video_id: str = None):
    async def _job():
        await asyncio.sleep(max(0, delay_seconds))
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            log.debug("Scheduled message was already deleted or inaccessible")
        finally:
            db.remove_scheduled_delete(row_id)

        if video_id:
            v = db.get_video(video_id)
            if v and db.is_visible(v):
                try:
                    watch_again_url = botutil.direct_delivery_url(video_id=video_id)
                    kb = (InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Watch Again", url=watch_again_url)]])
                          if watch_again_url else None)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⏱️ *Video #{int(v['video_number']) if v.get('video_number') is not None else '?'} · {db.md_escape(v.get('title') or 'this video')} was removed.*\n\n"
                            "Tap below whenever you want to watch it again."
                        ),
                        parse_mode="Markdown",
                        reply_markup=kb,
                    )
                except Exception:
                    log.exception("Could not leave Watch Again action for video=%s", video_id)
    bgtasks.spawn(_job(), name=f"delivery-delete-{row_id}")


async def _resume_scheduled_deletes(app):
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
        _run_delete_job(app.bot, row["id"], row["chat_id"], row["message_id"], remaining, row.get("video_id"))
        resumed += 1
    log.info(f"Resumed {resumed} pending auto-delete(s) from before restart.")


check_channel_access = botutil.check_channel_access


async def _startup_check(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
    except Exception as exc:
        log.warning("Could not clear stale webhook before polling: %s", exc)
    log.info("Checking channel access...")
    channels = [("Primary Channel", storage_config.primary()), ("Backup Channel", storage_config.backup())]
    if storage_config.recovery():
        channels.append(("3rd Content Channel", storage_config.recovery()))
    await check_channel_access(app.bot, channels)
    await _resume_scheduled_deletes(app)
    await botutil.configure_bot_ui(app, "delivery")


async def _copy_from_content_pool(context, *, chat_id: int, video: dict, caption: str, reply_markup=None):
    """Per-video fallback: Primary → Backup → optional 3rd Content."""
    candidates = []
    if video.get("primary_msg_id") and storage_config.primary():
        candidates.append(("Primary", int(storage_config.primary()), int(video["primary_msg_id"])))
    if video.get("backup_msg_id") and storage_config.backup():
        candidates.append(("Backup", int(storage_config.backup()), int(video["backup_msg_id"])))
    if video.get("recovery_msg_id") and storage_config.recovery():
        candidates.append(("3rd Content", int(storage_config.recovery()), int(video["recovery_msg_id"])))
    errors = []
    for label, channel_id, message_id in candidates:
        try:
            sent = await context.bot.copy_message(
                chat_id=chat_id, from_chat_id=channel_id, message_id=message_id,
                protect_content=True, caption=caption, reply_markup=reply_markup,
            )
            return sent, label, errors
        except Exception as exc:
            errors.append(f"{label}: {str(exc)[:180]}")
            log.warning("%s delivery failed for %s: %s", label, video.get("id"), exc)
    return None, "", errors


def _video_kb(video_id: str, user_id: int) -> InlineKeyboardMarkup:
    """Back-to-catalog button plus live reaction rows. In a private chat,
    chat_id and user_id are the same value, which is what reactions are
    keyed on."""
    rows = [[InlineKeyboardButton("↩️ Back to Share Bot", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}")]]
    rows.extend(reactions.build_rows(video_id, user_id))
    return InlineKeyboardMarkup(rows)


def _access_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟️ Redeem Membership", url=(_pool_bot_url("redeem") or f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem")), _buy_membership_button()],
        [InlineKeyboardButton("📚 Open Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")],
    ])


_SHORTLINK_CACHE: dict[tuple[int, str], tuple[float, str]] = {}
_SHORTLINK_CACHE_TTL = 300

def _buy_membership_url():
    return getattr(config, "MEMBERSHIP_BUY_URL", "") or "https://t.me/deodrant0"

def _buy_membership_button(label="💳 Buy Membership"):
    return InlineKeyboardButton(label, url=_buy_membership_url())

async def _limit_kb(user_id: int, video_id: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if video_id and shortlink.is_configured():
        cache_key = (int(user_id), str(video_id))
        now = asyncio.get_running_loop().time()
        cached = _SHORTLINK_CACHE.get(cache_key)
        unlock_url = cached[1] if cached and cached[0] > now else None
        if not unlock_url:
            token = db.create_unlock_token(user_id, video_id)
            deep_link = f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=unlock_{token}"
            try:
                # The provider uses synchronous requests; never run it on the
                # Telegram event loop. A degraded provider must not freeze the bot.
                unlock_url = await asyncio.wait_for(
                    asyncio.to_thread(shortlink.shorten, deep_link),
                    timeout=6,
                )
            except asyncio.TimeoutError:
                log.warning("Shortlink provider timed out for user=%s video=%s", user_id, video_id)
                unlock_url = None
            except Exception:
                log.exception("Shortlink generation failed for user=%s video=%s", user_id, video_id)
                unlock_url = None
            if unlock_url:
                _SHORTLINK_CACHE[cache_key] = (now + _SHORTLINK_CACHE_TTL, unlock_url)
                if len(_SHORTLINK_CACHE) > 2000:
                    # Cheap bounded cleanup; this is intentionally approximate.
                    cutoff = now
                    for k, (expiry, _) in list(_SHORTLINK_CACHE.items())[:400]:
                        if expiry <= cutoff:
                            _SHORTLINK_CACHE.pop(k, None)
        if unlock_url:
            rows.append([InlineKeyboardButton("📺 Watch Ad — 24h Access", url=unlock_url)])
    rows.append([InlineKeyboardButton("🎟️ Redeem Membership", url=(_pool_bot_url("redeem") or f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem"))])
    rows.append([_buy_membership_button()])
    rows.append([InlineKeyboardButton("📚 Open Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")])
    return InlineKeyboardMarkup(rows)

def _quota_status(user_id: int, quota_type: str = "free") -> tuple[int | None, int | None, bool]:
    access = db.get_user_access_summary(user_id)
    limit = access.get("effective_limit")
    used = int(access.get("watch_count_today") or 0)
    privileged = bool(access.get("is_premium") or access.get("is_ad_member"))
    if privileged:
        return limit, None, True
    if limit is None:
        return None, None, False
    if access.get("watch_count_date") != db.today_str():
        used = 0
    return limit, max(0, limit - used), False


def _quota_type_for_video(user_id: int, v: dict) -> str:
    if db.is_premium(user_id):
        return "free"
    tier = (v.get("access_tier") or "free").lower()
    if tier == "ad" and db.has_ad_unlock(user_id, v["id"]):
        return "ad"
    if tier == "redeem_or_ad" and db.has_ad_unlock(user_id, v["id"]):
        return "ad"
    return "free"


def _limit_message(limit: int, remaining: int, used: int | None = None) -> str:
    if remaining <= 0:
        used = limit if used is None else used
        return (
            "⛔ *Daily watch limit reached*\n\n"
            f"📊 Today's usage: *{used}/{limit}*\n\n"
            "Your free watches reset tomorrow.\n\n"
            "📺 Watch an ad for *24-hour temporary member access*, 🎟️ redeem a membership, or 💳 buy membership."
        )
    used = max(0, limit - remaining) if used is None else used
    return (
        f"📊 *Today's usage: {used}/{limit}*\n\n"
        f"⚠️ *{remaining} watch{'' if remaining == 1 else 'es'} left today.*\n"
        "Normal videos stay free until the limit is reached."
    )


async def _video_access_allowed(bot, user_id: int, v: dict):
    """Return (allowed, reason, unlock_mode) for per-video access rules."""
    tier = (v.get("access_tier") or "free").lower()
    if tier == "free":
        return True, "", "free"
    if tier == "users":
        if db.has_user_video_access(v["id"], user_id):
            return True, "", "users"
        return False, "👤 *This content is reserved for selected users.*", "users"
    if tier == "members":
        # Membership is granted by redeeming a membership code.
        if db.is_premium(user_id):
            return True, "", "members"
        return False, (
            "💎 *Membership required.*\n\n"
            "This content is available to users with an active redeem membership.\n\n"
            f"{getattr(config, 'MEMBERSHIP_BUY_MESSAGE', '💳 To buy or renew membership, message @deodrant0 and ask for a membership redeem code.')}"
        ), "members"
    if tier == "redeem":
        code = v.get("access_redeem_code")
        if db.is_premium(user_id) or (code and db.has_redeemed_code(code, user_id)):
            return True, "", "redeem"
        return False, f"🎟️ *Redeem access required.*\n\nUse the code assigned to this content: `{db.md_escape(code or '—')}`", "redeem"
    if tier == "redeem_or_ad":
        code = v.get("access_redeem_code")
        if db.is_premium(user_id) or db.is_ad_member(user_id) or (code and db.has_redeemed_code(code, user_id)):
            return True, "", "redeem_or_ad"
        return False, (
            "🔒 *This content is locked.*\n\n"
            "Choose either option below to unlock it:\n\n📺 Watch an ad → 24-hour temporary member access\n🎟️ Redeem a membership → full member access"
        ), "redeem_or_ad"
    # Backward-compatible gated tier and explicit ad tier.
    if tier in ("ad", "gated"):
        if db.is_premium(user_id) or db.is_ad_member(user_id) or db.has_ad_unlock(user_id, v["id"]):
            return True, "", "ad"
        return False, "📺 *A quick ad unlock is required for this content.*", "ad"
    return True, "", tier


async def _send_access_prompt(update: Update, video_id: str, reason: str, mode: str):
    chat_id = update.effective_chat.id
    db.upsert_notification_user(chat_id)
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if mode == "ad":
        await _send_unlock_prompt(update, video_id)
        return
    if mode == "redeem_or_ad":
        # One-click choice: either watch an ad or use the content's redeem code.
        rows = []
        if shortlink.is_configured():
            token = db.create_unlock_token(update.effective_user.id, video_id)
            deep_link = f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=unlock_{token}"
            try:
                unlock_url = await asyncio.wait_for(asyncio.to_thread(shortlink.shorten, deep_link), timeout=6)
            except Exception:
                unlock_url = None
            if unlock_url:
                rows.append([InlineKeyboardButton("📺 Watch Ad — Unlock", url=unlock_url)])
        rows.append([InlineKeyboardButton("🎟️ Redeem Code", url=(_pool_bot_url("redeem") or f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem"))])
        rows.append([_buy_membership_button()])
        rows.append([InlineKeyboardButton("📚 Back to Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")])
        if target:
            await target.reply_text(reason, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return
    rows = []
    if mode in ("redeem", "members"):
        rows.append([InlineKeyboardButton("🎟️ Enter Redeem Code", url=(_pool_bot_url("redeem") or f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem"))])
        rows.append([_buy_membership_button()])
    rows.append([InlineKeyboardButton("📚 Open Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")])
    if target:
        await target.reply_text(reason, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def _mandatory_gate(update, context):
    if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        return False
    joined, link, title, error = await botutil.mandatory_join_status(context.bot, update.effective_user.id, config)
    if joined:
        return True
    context.user_data["pending_mandatory_start"] = " ".join(context.args or [])
    detail = "\n\n⚠️ Telegram couldn't verify membership. Make sure this bot is an admin in the mandatory channel, then tap Check again." if error else ""
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target is not None:
        await botutil.send_mandatory_join_prompt(target, title, link, detail)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🔐 Please join {title} and tap Check Again.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Join Channel", url=link), InlineKeyboardButton("🔄 Check Again", callback_data="mandatory_join_check")]]))
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
    await _maybe_membership_warning(update, context)
    if not update.effective_chat or update.effective_chat.type != "private":
        return  # ignore channel posts (we're a channel admin) — never process or reply to these
    args = context.args
    chat_id = update.effective_chat.id
    db.upsert_notification_user(chat_id)

    if _rate_limited(chat_id):
        return

    if not args:
        _rows = [
            [InlineKeyboardButton("📚 Open Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")],
            [InlineKeyboardButton("🎟️ Redeem Code", url=(_pool_bot_url("redeem") or f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem")), _buy_membership_button()],
        ]
        if _webapp_button():
            _rows.append([_webapp_button()])
        kb = InlineKeyboardMarkup(_rows)
        await _reply(update, 
            admin_store.get_template("delivery_start", "👋 Heyyy! Welcome in ✨\n\n🍿 Ready to pick something? Choose your vibe below 👇"),
            reply_markup=kb,
        )
        return

    param = args[0]

    if param.lower() == "redeem":
        await _reply(update, 
            "🎟️ *Got a Premium Code?* 🔥\n\n"
            "Paste your code using the format below:\n"
            "`/redeem YOURCODE`\n\n"
            "✨ Valid code? Boom — premium time is yours, with daily watch limits removed while it's active. 💎",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Buy Membership", url=_buy_membership_url())],
                [InlineKeyboardButton("📚 Open Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")],
            ]),
        )
        return

    if param.startswith("b_"):
        batch_id = param[2:]
        active = _active_job(chat_id)
        if active and active.get("target_id") != batch_id:
            await _ask_overtake(update, active, "batch", batch_id)
            return
        await _deliver_batch(update, context, batch_id)
        return

    if param.startswith("unlock_"):
        token = param[len("unlock_"):]
        video_id = db.resolve_unlock_token(token, chat_id)
        if not video_id:
            await _reply(update, 
                "😵‍💫 *Oops!* This unlock link is invalid, already used, or wasn't made for your account.\n\n🔁 Head back to the catalog and grab a fresh one."
            )
            return
        active = _active_job(chat_id)
        if active:
            await _ask_overtake(update, active, "video", video_id)
            return
        await _deliver_video(update, context, video_id)
        return

    if not param.startswith("v_"):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 Open Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")]
        ])
        await _reply(update, 
            "👀 This bot is the delivery room.\n\n📚 Pick your content from the catalog, then tap the watch button there. 😉",
            reply_markup=kb,
        )
        return

    video_id = param[2:]
    v = db.get_video(video_id)
    if not v or not db.is_visible(v):
        log.warning(f"Video not found or not yet visible for id={video_id!r} (deep link: {param!r}) — "
                    "if this happens on a user's very first tap but works on retry, "
                    "it's usually Telegram API rate-limiting from unrelated bot activity, "
                    "not a missing catalog entry.")
        await _reply(update, "😶‍🌫️ *This one has slipped away for now.*\n\n📚 Head back to the catalog and choose another upload.")
        return

    allowed, reason, mode = await _video_access_allowed(context.bot, chat_id, v)
    if not allowed:
        await _send_access_prompt(update, video_id, reason, mode)
        return

    active = _active_job(chat_id)
    if active:
        await _ask_overtake(update, active, "video", video_id)
        return
    await _deliver_video(update, context, video_id)


async def _send_unlock_prompt(update: Update, video_id: str):
    chat_id = update.effective_chat.id
    v = db.get_video(video_id)
    title = v["title"] if v else "This video"

    kb_rows = []
    if shortlink.is_configured():
        token = db.create_unlock_token(chat_id, video_id)
        deep_link = f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=unlock_{token}"
        unlock_url = shortlink.shorten(deep_link)
        if unlock_url:
            kb_rows.append([InlineKeyboardButton("📺 Watch Ad — 24h Access", url=unlock_url)])
    kb_rows.append([InlineKeyboardButton("🎟️ Redeem Premium Code", url=(_pool_bot_url("redeem") or f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem"))])
    kb_rows.append([InlineKeyboardButton("📚 Back to Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")])

    if len(kb_rows) == 2:
        text = (
            f"🔒 *{db.md_escape(title)}*\n\n"
            "Your free daily limit is finished.\n\n"
            "📺 Ad unlock is currently unavailable, so use a redeem membership code to continue."
        )
    else:
        text = (
            f"🔒 *{db.md_escape(title)}*\n\n"
            "Your free daily limit is finished. Choose one:\n\n"
            "• *Watch an ad* — get 24-hour temporary member access.\n"
            "• *Redeem a code* — get full member access while your membership is active."
        )
    target = _reply_target(update)
    if target is not None:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb_rows))


async def _deliver_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video_id: str):
    """Actually hands over the video. Assumes any access-tier gating has
    already been resolved by the caller — this only still enforces the
    per-user daily watch quota, which applies uniformly to every delivery,
    free or gated, so it can't be bypassed via the unlock_ deep link either."""
    chat_id = update.effective_chat.id
    active = _active_job(chat_id)
    if active and active.get("task") is not asyncio.current_task():
        await _ask_overtake(update, active, "video", video_id)
        return
    _register_job(chat_id, "video", video_id)
    task = asyncio.current_task()
    if task:
        task.add_done_callback(lambda _t: _clear_job(chat_id))
    v = db.get_video(video_id)
    if not v or not db.is_visible(v):
        await _reply(update, "😶‍🌫️ *This one has slipped away for now.*\n\n📚 Head back to the catalog and choose another upload.")
        _force_clear_job(chat_id, task)
        return

    quota_type = "free"
    can_watch, reason, limit, remaining = db.can_watch(chat_id)
    if not can_watch:
        await _reply(
            update,
            _limit_message(limit or 0, 0, limit or 0),
            parse_mode="Markdown",
            reply_markup=await _limit_kb(chat_id, video_id),
        )
        _force_clear_job(chat_id, task)
        return

    media_label = "🖼️ Image" if (v.get("media_type") or "video") == "photo" else "🎬 Video"
    number_label = "Item" if (v.get("media_type") or "video") == "photo" else "Video"
    number_line = f"🔢 {number_label} #{int(v['video_number'])}\n" if v.get("video_number") is not None else ""
    caption = f"{media_label} {v['title']}\n{number_line}\n🔒 Protected — this content can't be forwarded or saved."
    if AUTO_DELETE_SECONDS > 0:
        caption += f"\n🧹 This message auto-deletes in {AUTO_DELETE_SECONDS // 60} min (you can still watch it now)."
    back_kb = _video_kb(video_id, chat_id)

    sent = None
    status_msg = None
    target = _reply_target(update)
    try:
        if target is not None:
            status_msg = await target.reply_text(
                bot_ux.sending_text("image" if (v.get("media_type") or "video") == "photo" else "video"),
                parse_mode="HTML",
            )
        sent, delivery_source, delivery_errors = await _copy_from_content_pool(
            context, chat_id=chat_id, video=v, caption=caption, reply_markup=back_kb
        )
        if sent is None:
            _pool_failure(" | ".join(delivery_errors) or "All content sources failed")
            await _reply(
                update,
                f"❌ Couldn't retrieve this video from Primary, Backup, or the 3rd content source.\n\n🔢 Video #{v.get('video_number') or video_id}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Retry Delivery", callback_data=f"retry_video:{video_id}")]])
            )
            _force_clear_job(chat_id, task)
            return
    except Exception as exc:
        log.exception("Unexpected delivery error")
        _pool_failure(exc)
        await _reply(
            update,
            "❌ *Delivery failed temporarily.*\\n\\nPlease retry once. If it keeps failing, use Storage Bot → Repair / Rebuild Mapping for this item.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry Delivery", callback_data=f"retry_video:{video_id}")],
                [InlineKeyboardButton("📚 Back to Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")],
            ]),
        )
        _force_clear_job(chat_id, task)
        return

    # Telegram has already received the requested media, but keep the active
    # guard until quota registration and delivery bookkeeping finish. This is
    # important with multiple permanent Delivery Bot processes: releasing here
    # would allow a second process to pass can_watch() before this delivery has
    # atomically consumed its watch.
    _pool_success()
    if status_msg:
        try:
            await status_msg.edit_text(bot_ux.sending_text("image" if (v.get("media_type") or "video") == "photo" else "video", 1, 1), parse_mode="HTML")
        except Exception:
            pass

    registered, register_reason, used_limit, remaining_after = db.register_watch(chat_id)
    if not registered:
        # The quota can only change between pre-check and send if another
        # request raced this process. Do not silently grant a second watch.
        log.warning("Quota changed during delivery for user=%s video=%s: %s", chat_id, video_id, register_reason)
    db.increment_view(video_id)
    db.log_analytics("delivery", user_id=chat_id, video_id=video_id, value=1)
    await _check_milestone(context.bot, video_id, v["title"])
    if sent:
        schedule_delete(context.bot, chat_id, sent.message_id, video_id=video_id)

    limit, remaining, premium = _quota_status(chat_id, quota_type)
    if limit is not None and not premium and remaining is not None:
        used = max(0, limit - remaining)
        await _reply(
            update,
            _limit_message(limit, remaining, used),
            parse_mode="Markdown",
            reply_markup=await _limit_kb(chat_id, video_id) if remaining == 0 else None,
        )
    # All post-send accounting is now complete; release the per-process guard.
    _force_clear_job(chat_id, task)


async def _deliver_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, batch_id: str, force_resume: bool = False):
    chat_id = update.effective_chat.id
    target = update.message or update.callback_query.message
    active = _active_job(chat_id)
    if active and active.get("task") is not asyncio.current_task():
        await _ask_overtake(update, active, "batch", batch_id)
        return
    _register_job(chat_id, "batch", batch_id)
    task = asyncio.current_task()
    if task:
        task.add_done_callback(lambda _t: _clear_job(chat_id))

    lock = _batch_lock(chat_id, batch_id)
    if lock.locked():
        await target.reply_text("⏳ *Easyyy, one delivery at a time* 😌\n\nYour current batch is still working. A new request can wait safely — nothing will be lost.")
        _force_clear_job(chat_id, task)
        return

    async with lock:
        batch = db.get_batch(batch_id)
        videos = db.get_batch_videos(batch_id) if batch else []
        videos = [v for v in videos if db.is_visible(v)]
        if not batch or not videos:
            await target.reply_text("📦 *That batch is no longer available.*\n\nNo worries — jump back to the catalog and pick a fresh one. ✨")
            _force_clear_job(chat_id, task)
            return

        progress = db.get_batch_progress(chat_id, batch_id)
        start_index = int(progress["next_index"]) if progress else 0
        if start_index >= len(videos):
            db.clear_batch_progress(chat_id, batch_id)
            start_index = 0
            progress = None

        if progress and start_index > 0 and not force_resume:
            left = len(videos) - start_index
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔄 Resume ({left} left)", callback_data=f"br_{batch_id}")],
                [InlineKeyboardButton("▶️ Start From Beginning", callback_data=f"bs_{batch_id}")],
            ])
            await target.reply_text(
                f"⏸️ *Delivery paused*\n\n"
                f"You already received *{start_index}/{len(videos)}* videos from *{db.md_escape(batch['title'])}*.\n\n"
                f"Would you like to continue from Part {start_index + 1}?",
                parse_mode="Markdown", reply_markup=kb,
            )
            _force_clear_job(chat_id, task)
            return

        # One normal-user bulk request consumes exactly ONE daily watch for the
        # whole batch. A resume never consumes another watch. Premium redeem
        # members and successful 24h ad-members are unlimited.
        privileged = db.is_premium(chat_id) or db.is_ad_member(chat_id)
        quota_charged = bool(progress and start_index > 0)
        limit, remaining, premium = _quota_status(chat_id)
        if not privileged and not quota_charged:
            allowed, reason, limit, remaining = db.can_watch(chat_id)
            if not allowed:
                db.set_batch_progress(chat_id, batch_id, start_index, "paused")
                first_id = videos[start_index]["id"] if start_index < len(videos) else None
                await target.reply_text(
                    _limit_message(limit or 0, 0, limit or 0),
                    parse_mode="Markdown",
                    reply_markup=await _limit_kb(chat_id, first_id),
                )
                _force_clear_job(chat_id, task)
                return

        # Keep the original collection intro exactly as before. The ONLY
        # bulk-UX change is that the noisy per-item "Sending Part X/Y"
        # messages are removed.
        intro = (
            f"📦 *{db.md_escape(batch['title'])}* — {len(videos)} item(s)\n\n"
            f"Starting from Part {start_index + 1}…"
        )
        if not privileged and not quota_charged:
            intro += "\n\n📊 *This entire bulk delivery uses 1 daily watch.*"
        elif not privileged and quota_charged:
            intro += "\n\n📊 *This batch has already used today's 1 bulk watch.*"
        else:
            intro += "\n\n💎 *Unlimited member access.*"
        delivery_status = await target.reply_text(
            f"📦 <b>Delivery ready</b>\n\n{intro}",
            parse_mode="HTML",
        )

        i = start_index
        while i < len(videos):
            v = videos[i]
            access_ok, access_reason, access_mode = await _video_access_allowed(context.bot, chat_id, v)
            if not access_ok:
                db.set_batch_progress(chat_id, batch_id, i, "paused")
                await _send_access_prompt(
                    update, v["id"],
                    f"⏸️ *Batch paused at Part {i + 1}/{len(videos)}.*\n\n" + access_reason,
                    access_mode,
                )
                _force_clear_job(chat_id, task)
                return

            media_label = "🖼️ Image" if (v.get("media_type") or "video") == "photo" else "🎬 Video"
            number_label = "Item" if (v.get("media_type") or "video") == "photo" else "Video"
            caption = (
                f"{media_label} {v['title']}\n🔢 {number_label} #{int(v['video_number']) if v.get('video_number') is not None else '?'}\n\n📦 Part {i + 1}/{len(videos)}\n"
                "🔒 Protected — this content can't be forwarded or saved."
            )
            kind = "image" if (v.get("media_type") or "video") == "photo" else "video"
            sent, delivery_source, delivery_errors = await _copy_from_content_pool(
                context, chat_id=chat_id, video=v, caption=caption, reply_markup=_video_kb(v["id"], chat_id)
            )
            if sent is None:
                _pool_failure(" | ".join(delivery_errors) or "All content sources failed")
                db.set_batch_progress(chat_id, batch_id, i, "paused")
                await target.reply_text(
                    f"⏸️ *Delivery paused at Part {i + 1}/{len(videos)}.*\n\n"
                    "The video could not be delivered from Primary, Backup, or the 3rd content source. "
                    "Your progress is saved — tap Resume to retry.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Resume Delivery", callback_data=f"br_{batch_id}")]]),
                )
                _force_clear_job(chat_id, task)
                return

            _pool_success()

            # Charge once, after the first successful delivery. If a later
            # video fails, resume uses the saved progress and never charges
            # another daily watch.
            if not privileged and not quota_charged:
                registered, register_reason, _, _ = db.register_watch(chat_id)
                if not registered:
                    log.warning("Batch quota changed during first delivery for user=%s: %s", chat_id, register_reason)
                    db.set_batch_progress(chat_id, batch_id, i, "paused")
                    await target.reply_text(
                        "⏸️ *Daily limit changed while starting this batch.*\n\n"
                        "Your delivered video is kept, and the batch is paused. Unlock 24h access or redeem membership to continue.",
                        parse_mode="Markdown",
                        reply_markup=await _limit_kb(chat_id, v["id"]),
                    )
                    _force_clear_job(chat_id, task)
                    return
                quota_charged = True

            db.increment_view(v["id"])
            db.log_analytics("delivery", user_id=chat_id, video_id=v["id"], value=1)
            i += 1
            db.set_batch_progress(chat_id, batch_id, i, "running")
            await _check_milestone(context.bot, v["id"], v["title"])
            if sent:
                schedule_delete(context.bot, chat_id, sent.message_id, video_id=v["id"])

            # Once the final media item is sent, the delivery itself is done.
            # Release the busy guard before the final status message so a user
            # who immediately taps /start never sees a false "another delivery
            # is already running" prompt. Keep the guard for earlier parts.
            if i >= len(videos):
                _force_clear_job(chat_id, task)
            else:
                await asyncio.sleep(random.uniform(0.5, 1.0))

        db.clear_batch_progress(chat_id, batch_id)
        if privileged:
            usage_line = "💎 *Unlimited member access.*"
        else:
            current_limit, current_remaining, _ = _quota_status(chat_id)
            usage_line = (
                f"📊 *Today's usage: {max(0, (current_limit or 0) - (current_remaining or 0))}/{current_limit}*"
                if current_limit is not None else
                "📊 *Today's limit: unlimited.*"
            )
        # Keep the original final completion message. The old per-item Sending
        # messages are gone; completion remains a separate message.
        _force_clear_job(chat_id, task)
        try:
            complete_msg = await target.reply_text(
                f"✅ *Batch complete* — {len(videos)}/{len(videos)} video(s) delivered.\n\n"
                f"{usage_line}\n\n"
                "🎉 You can return to the catalog anytime to watch them again.",
                parse_mode="Markdown",
            )
            # Both the original intro and final status are transient status
            # messages. Clean each after 30 minutes without creating any
            # per-video status messages.
            for status_msg in (delivery_status, complete_msg):
                status_key = (int(chat_id), int(status_msg.message_id))
                if status_key in _SCHEDULED_STATUS_MESSAGES:
                    continue
                _SCHEDULED_STATUS_MESSAGES.add(status_key)
                row_id = db.add_scheduled_delete(
                    BOT_NAME, chat_id, status_msg.message_id,
                    db.future_str(STATUS_DELETE_SECONDS), video_id=None,
                )
                _run_delete_job(
                    context.bot, row_id, chat_id, status_msg.message_id,
                    STATUS_DELETE_SECONDS, None,
                )
        except Exception:
            log.debug("Could not finalize/auto-delete batch status message", exc_info=True)

async def _maybe_membership_warning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    days, expiry = db.membership_days_remaining(user_id)
    if not expiry or days is None or days > 3 or days <= 0:
        return
    warning_type = "1d" if days == 1 else "3d"
    premium_until = expiry.isoformat()
    if not db.should_send_membership_warning(user_id, warning_type, premium_until):
        return
    rows = []
    buy_url = getattr(config, "MEMBERSHIP_BUY_URL", "")
    if buy_url:
        rows.append([InlineKeyboardButton("💳 Renew Membership", url=buy_url)])
    rows.append([InlineKeyboardButton("🎟️ Enter Redeem Code", url=(_pool_bot_url("redeem") or f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem"))])
    if days == 1:
        text = f"🚨 *Your membership expires tomorrow!*\n\nRenew before it ends to keep your member access active.\n\n{getattr(config, 'MEMBERSHIP_BUY_MESSAGE', '💳 To buy or renew membership, message @deodrant0 and ask for a membership redeem code.')}"
    else:
        text = f"⚠️ *Your membership expires in about {days} days.*\n\nRenew early so you don't lose access to member-only content.\n\n{getattr(config, 'MEMBERSHIP_BUY_MESSAGE', '💳 To buy or renew membership, message @deodrant0 and ask for a membership redeem code.')}"
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        db.mark_membership_warning_sent(user_id, warning_type, premium_until)


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
    url=botutil.direct_delivery_url(video_id=video_id)
    for user_id in db.notification_recipients(500):
        if not db.notification_can_send(user_id,video_id,24): continue
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✨ *New drop is ready!*\\n\\n🎬 *{title}*\\n\\nTap below to watch it.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔥 Watch Now",url=url)]])
            )
            db.mark_notification_sent(user_id,video_id); sent+=1
        except Exception: failed+=1
    await update.message.reply_text(f"📣 Notification sent: *{sent}*\\n⚠️ Failed/skipped: *{failed}*",parse_mode="Markdown")

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    if not context.args:
        await update.message.reply_text("🎟️ *Redeem a code*\n\nUse: `/redeem CODE`\n\n💡 Got the code from an alert? Just copy-paste it here.", parse_mode="Markdown")
        return
    code = context.args[0].strip().upper()
    ok, message = db.redeem_code(code, update.effective_user.id)
    if ok:
        days, expiry = db.membership_days_remaining(update.effective_user.id)
        expiry_text = expiry.strftime("%d %b %Y, %I:%M %p") if expiry else "—"
        rows = [[InlineKeyboardButton("📚 Open Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")]]
        buy_url = getattr(config, "MEMBERSHIP_BUY_URL", "") or "https://t.me/deodrant0"
        rows.append([InlineKeyboardButton("💳 Renew / Buy Membership", url=buy_url)])
        await update.message.reply_text(
            f"✅ *Membership activated!*\n\n💎 Active until: *{expiry_text}*\n\nYour member-only access is now unlocked.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
        )
        db.log_activity("delivery_bot", "code_redeemed", f"{code} by {update.effective_user.id}")
    else:
        await update.message.reply_text(message)


async def batch_resume_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    data = query.data or ""
    if data.startswith("br_"):
        batch_id = data[3:]
        await query.edit_message_text("🔄 *Back in action!*\n\nPicking up your saved delivery from where we left off… 🍿")
        await _deliver_batch(update, context, batch_id, force_resume=True)
    elif data.startswith("bs_"):
        batch_id = data[3:]
        db.clear_batch_progress(update.effective_chat.id, batch_id)
        await query.edit_message_text("▶️ *Fresh start!*\n\nStarting this batch again from Part 1. 🚀")
        await _deliver_batch(update, context, batch_id, force_resume=True)


async def overtake_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Safely switch from the current delivery to the requested one.

    A callback update has no update.message, so the new delivery must reply
    through callback_query.message. The previous implementation also cancelled
    only batch jobs; cancelling a video job caused the new video to immediately
    see the old job as active and show the generic 'Tiny hiccup' / overtake loop.
    """
    query = update.callback_query
    await query.answer()
    if not update.effective_chat or update.effective_chat.type != "private":
        return

    data = query.data or ""
    user_id = update.effective_chat.id

    if data == "ovc_keep":
        await query.edit_message_text(
            "⏸️ *Current delivery protected!*\n\n"
            "Your new request is waiting safely. Nothing was interrupted. 💗",
            parse_mode="Markdown",
        )
        return

    job = _active_job(user_id)
    if not job:
        # The original job may have completed while the confirmation was on screen.
        # Treat the requested action as a fresh delivery instead of showing an error.
        if data.startswith("ovb_"):
            batch_id = data[4:]
            await query.edit_message_text(
                "▶️ *Starting your requested bulk delivery…* 🍿",
                parse_mode="Markdown",
            )
            await _deliver_batch(update, context, batch_id, force_resume=True)
        elif data.startswith("ovs_"):
            video_id = data[4:]
            await query.edit_message_text(
                "▶️ *Starting your requested video…* 🎬",
                parse_mode="Markdown",
            )
            await _deliver_video(update, context, video_id)
        return

    old_task = job.get("task")
    old_kind = job.get("kind")
    old_target = job.get("target_id")

    # Cancel BOTH batch and single-video jobs. The old code cancelled batches
    # only, which made "Overtake & Watch This" loop back into the busy guard.
    if old_task and not old_task.done() and old_task is not asyncio.current_task():
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Previous delivery task failed while being overtaken")

    _force_clear_job(user_id, old_task)

    if data.startswith("ovb_"):
        batch_id = data[4:]
        await query.edit_message_text(
            "⏩ *Switching delivery…* 🚀\n\n"
            "Current delivery paused safely. Starting the new request now…",
            parse_mode="Markdown",
        )
        await _deliver_batch(update, context, batch_id, force_resume=False)
    elif data.startswith("ovs_"):
        video_id = data[4:]
        await query.edit_message_text(
            "⏩ *Switching delivery…* 🚀\n\n"
            "Current delivery paused safely. Sending your requested video now… 🎬",
            parse_mode="Markdown",
        )
        await _deliver_video(update, context, video_id)


async def retry_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not update.effective_chat or update.effective_chat.type != "private":
        await query.answer()
        return
    try:
        await asyncio.wait_for(query.answer("Retrying…"), timeout=3)
    except Exception:
        pass
    video_id = (query.data or "").split(":", 1)[1].strip() if ":" in (query.data or "") else ""
    if not video_id:
        return
    if _retry_limited(update.effective_chat.id, video_id):
        try:
            await query.answer("Easy 😌 give the previous attempt a moment.", show_alert=False)
        except Exception:
            pass
        return
    try:
        await query.edit_message_text("🔄 *Retrying delivery…*\\n\\nChecking the stored content sources again. 🍿", parse_mode="Markdown")
    except Exception:
        pass
    await _deliver_video(update, context, video_id)


async def reaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not update.effective_chat or update.effective_chat.type != "private":
        await query.answer()
        return
    await reactions.handle_tap(query, rebuild_markup=_video_kb)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    rows=[[InlineKeyboardButton("📚 Open Catalog", url=f"https://t.me/{config.CATALOG_BOT_USERNAME}?start=today")],
          [InlineKeyboardButton("🎟️ Redeem Code", url=(_pool_bot_url("redeem") or f"https://t.me/{config.DELIVERY_BOT_USERNAME}?start=redeem")), _buy_membership_button()]]
    if _webapp_button():
        rows.append([_webapp_button()])
    await update.message.reply_text(bot_ux.brand_header("DELIVERY HUB", "Fast, tidy and resumable") + "\n\n🍿 Pick your next move below." + bot_ux.footer_hint("Open Catalog to choose content."), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    uid=update.effective_chat.id
    active=_active_job(uid)
    quota_type=_quota_type_for_video(uid, {})
    limit, used, unlimited=_quota_status(uid, quota_type)
    lines=["📊 *Your Delivery Status*", "", f"🎬 Active job: *{'yes' if active else 'none'}*"]
    if active:
        lines.append(f"• Type: `{active.get('kind')}`")
        lines.append(f"• Target: `{active.get('target_id')}`")
    if unlimited:
        lines.append("💎 Access: *unlimited*")
    elif limit is not None:
        remaining=max(0, limit-(used or 0))
        lines.append(f"🎟️ Free quota: *{remaining}/{limit}* remaining today")
    lines.append("", "Use /menu for shortcuts or /help for guidance.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mandatory_gate(update, context):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "✨ *Delivery Help*\n\n"
        "🎬 Open a video from the Catalog to start delivery.\n"
        "🎟️ Use /redeem CODE for premium access.\n"
        "🔄 If a delivery pauses, use the Resume button.\n"
        "💡 Having trouble? Open the Catalog again and grab a fresh link.\n\n"
        "Happy watching, baby 🍿💗",
        parse_mode="Markdown",
    )


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Global fallback. Public-facing, so a real private-chat user gets a
    generic message while the admin gets the real detail. Skips replying if
    effective_chat isn't a private chat — that could re-trigger the same
    crash and loop forever."""
    if boterror.is_transient_network_error(context.error):
        # A dropped read, connect timeout, etc. — PTB's own polling loop
        # already retries these; they resolve within a poll cycle or two.
        # Not worth logging as an error or paging the admin for.
        log.warning(f"Transient network hiccup (self-recovers): {context.error!r}")
        return
    log.error("Unhandled exception", exc_info=context.error)
    try:
        if (isinstance(update, Update) and update.effective_chat
                and update.effective_chat.type == "private"):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="🥲 *Tiny hiccup!*\n\nSomething went wrong on our side. Give it another tap in a moment. 🔄",
            )
    except Exception:
        log.warning("Could not notify user about delivery_bot error", exc_info=True)
    try:
        if config.ADMIN_USER_IDS:
            await context.bot.send_message(
                chat_id=config.ADMIN_USER_IDS[0],
                text=f"🐞 delivery_bot error: {context.error}",
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
    # Lock by TOKEN, not by the display role/name. This prevents the same
    # Telegram bot from being polled by both the permanent runner and an old
    # standalone delivery process, which is a common cause of 429 flood-control
    # errors and worker crash loops.
    import hashlib
    token_key = hashlib.sha256(str(config.DELIVERY_BOT_TOKEN or "").encode()).hexdigest()[:24]
    instance_lock = botutil.acquire_single_instance(f"delivery_token_{token_key}")
    if instance_lock is None:
        return False
    app = Application.builder().token(config.DELIVERY_BOT_TOKEN).post_init(_startup_check).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("redeem", redeem_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("hub", menu_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(mandatory_join_callback, pattern="^mandatory_join_check$"))
    app.add_handler(CallbackQueryHandler(batch_resume_callback, pattern="^b[rs]_"))
    app.add_handler(CallbackQueryHandler(overtake_callback, pattern="^ov"))
    app.add_handler(CallbackQueryHandler(retry_video_callback, pattern="^retry_video:"))
    app.add_handler(CallbackQueryHandler(reaction_callback, pattern="^rx_"))
    app.add_error_handler(error_handler)

    log.info("Delivery bot starting...")
    botutil.run_polling_resilient(app, "delivery_bot")
    return True


if __name__ == "__main__":
    main()
