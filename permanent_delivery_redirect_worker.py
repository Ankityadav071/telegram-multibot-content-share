"""Legacy-link fallback for a disabled permanent Delivery Bot.

The bot is kept alive only to prevent old direct t.me links from becoming dead.
It does not run as part of the V9.3+ supervisor. Stable resolver links are now used for Watch actions. It remains only as an optional legacy compatibility worker.
"""
from __future__ import annotations
import os, random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
import permanent_bot_store
import botutil

BOT_ID = os.getenv("VV_PERMANENT_DELIVERY_ID", "unknown")
TOKEN = os.getenv("VV_PERMANENT_DELIVERY_TOKEN", "")


def _target(param: str):
    active = [b for b in permanent_bot_store.active_delivery_bots() if str(b.get("id")) != str(BOT_ID)]
    if not active: return None
    b = random.choice(active)
    return f"https://t.me/{str(b.get('username') or '').lstrip('@')}?start={param}" if b.get("username") else None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message: return
    param = context.args[0].strip() if context.args else ""
    if not (param.startswith("v_") or param.startswith("b_")):
        await update.effective_message.reply_text("🔄 This Delivery Bot is currently rerouting to an active Delivery Bot.")
        return
    url = _target(param)
    if not url:
        await update.effective_message.reply_text("⚠️ Delivery is temporarily unavailable. Please try again shortly.")
        return
    await update.effective_message.reply_text(
        "🔄 *Opening active Delivery Bot…*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Continue", url=url)]])
    )


def main():
    if not TOKEN: raise SystemExit("VV_PERMANENT_DELIVERY_TOKEN is required")
    import hashlib
    token_key = hashlib.sha256(TOKEN.encode()).hexdigest()[:24]
    lock = botutil.acquire_single_instance(f"delivery_token_{token_key}")
    if lock is None:
        return
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
