"""Single-purpose worker for an Admin-managed temporary bot."""
from __future__ import annotations
import os
import sys
import logging
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, Bot
from telegram.error import InvalidToken, Forbidden, RetryAfter, TimedOut, NetworkError, Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters

import temp_bot_store
import config
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s tempbot %(message)s")
log=logging.getLogger("temp_bot_worker")

BOT_ID = os.environ.get("VV_TEMP_BOT_ID", "").strip()
TOKEN = os.environ.get("VV_TEMP_BOT_TOKEN", "").strip()


def _config():
    default_link = str(getattr(config, "PRIMARY_CHANNEL_LINK", "") or "https://t.me/shivanifanbase").strip()
    cfg = temp_bot_store.get_bot_config(BOT_ID, default_link=default_link)
    return cfg["channel"], cfg["message"]


async def start(update, context: ContextTypes.DEFAULT_TYPE):
    # Temporary bots have one job: show the configurable welcome + main-channel button.
    # Use effective_message so the handler is resilient across Telegram update types.
    message_obj = update.effective_message
    if not message_obj:
        return
    channel, message = _config()
    link = channel.get("link", "").strip()
    if not link.startswith(("https://t.me/", "http://t.me/")):
        await message_obj.reply_text("⚠️ Main Channel is not configured yet.")
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(message["button_text"], url=link)]])
    text = message.get("text") or "✨ Welcome!\n\nTap below to join our Main Channel."
    image_path = message.get("image_path") or ""
    try:
        if image_path and os.path.isfile(image_path):
            with open(image_path, "rb") as photo:
                await message_obj.reply_photo(photo=photo, caption=text, reply_markup=keyboard)
        else:
            await message_obj.reply_text(text, reply_markup=keyboard)
        temp_bot_store.mark_success(BOT_ID)
    except RetryAfter as exc:
        temp_bot_store.mark_failure(BOT_ID, f"rate limited: retry after {getattr(exc, 'retry_after', '?')}s")
        log.warning("Temporary bot rate limited: %s", exc)
        return
    except (TimedOut, NetworkError, Conflict) as exc:
        temp_bot_store.mark_failure(BOT_ID, f"transient send error: {exc}")
        log.warning("Temporary bot transient send error: %s", exc)
        return
    except Forbidden as exc:
        # A user/channel-level block must not quarantine the whole bot.
        temp_bot_store.mark_failure(BOT_ID, f"send forbidden for recipient: {exc}")
        log.info("Temporary bot send forbidden for one recipient: %s", exc)
        return
    except Exception as exc:
        log.exception("Temporary bot start delivery failed")
        # Preserve the configured image and keep a text fallback for malformed media paths.
        try:
            await message_obj.reply_text(text, reply_markup=keyboard)
            temp_bot_store.mark_success(BOT_ID)
        except Exception as fallback_exc:
            temp_bot_store.mark_failure(BOT_ID, f"start delivery failed: {exc}; fallback: {fallback_exc}")
            return
    try: db.log_analytics("temp_start", user_id=(update.effective_user.id if update.effective_user else None), value=1, detail=BOT_ID)
    except Exception: pass

async def _heartbeat():
    while True:
        try:
            row = temp_bot_store.get_bot(BOT_ID)
            if row and row.get("enabled") and not row.get("quarantined"):
                # A heartbeat proves the isolated worker is alive; it is deliberately
                # separate from user delivery success metrics.
                temp_bot_store.touch_status(BOT_ID, last_seen_at=__import__('datetime').datetime.now().astimezone().isoformat(), status="HEALTHY")
        except Exception as exc:
            log.warning("Temporary bot heartbeat failed: %s", exc)
        await asyncio.sleep(60)


async def temp_join(update, context):
    # Kept for compatibility with older workers/configs. New buttons use a direct URL.
    query=update.callback_query
    if not query: return
    channel, _ = _config()
    link=channel.get("link", "").strip()
    try: db.log_analytics("temp_click", user_id=(query.from_user.id if query.from_user else None), value=1, detail=BOT_ID)
    except Exception: pass
    await query.answer()


async def ignore(*args, **kwargs):
    return


async def post_init(app):
    # Validate the token INSIDE python-telegram-bot's own event loop.
    # Do not call asyncio.run() before run_polling(): on Python 3.13+ that
    # closes/unsets the current loop and PTB 21.x can then crash before polling.
    try:
        me = await app.bot.get_me()
        if not me or not me.id or not me.username:
            raise RuntimeError("Telegram did not return a usable bot identity")
        await app.bot.set_my_commands([BotCommand("start", "Open main channel button")])
        temp_bot_store.touch_status(
            BOT_ID, username=me.username, first_name=me.first_name or '',
            last_started_at=__import__('datetime').datetime.now().astimezone().isoformat(),
            last_seen_at=__import__('datetime').datetime.now().astimezone().isoformat(),
            last_error=None,
        )
        temp_bot_store.mark_success(BOT_ID)
        app.create_task(_heartbeat(), name=f"tempbot-heartbeat-{BOT_ID}")
        log.info("Temporary bot ready: @%s (%s)", me.username, me.id)
    except InvalidToken as exc:
        temp_bot_store.mark_failure(BOT_ID, f"invalid/revoked token: {exc}", hard=True)
        raise
    except Exception as exc:
        temp_bot_store.mark_failure(BOT_ID, f"startup: {str(exc)[:400]}")
        raise


def main():
    if not BOT_ID or not TOKEN:
        raise SystemExit("Missing VV_TEMP_BOT_ID/VV_TEMP_BOT_TOKEN")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(temp_join, pattern=r"^tmpjoin:"))
    # Ignore everything else. No menus, search, delivery, or admin surface.
    app.add_handler(MessageHandler(filters.ALL, ignore))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
