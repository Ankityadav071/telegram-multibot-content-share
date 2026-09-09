"""
Shared startup helper used by all four bots.

Verifies the bot actually has the channel access it needs and logs a clear
✅/⚠️/❌ per channel — this is exactly the class of bug ("Chat not found",
copying silently failing) that keeps coming from a bot not being an admin
in a channel it needs, and it's much easier to diagnose from a startup log
line than from a confusing failure the first time someone tries to upload
or watch something.
"""
import logging
import html

log = logging.getLogger("botutil")


async def check_channel_access(bot, channels: list, verb: str = "posting/copying"):
    """channels: list of (label, chat_id) pairs.
    Returns [(label, chat_id, status_str), ...] for callers that want to
    display it (e.g. Admin Bot's /health and /ping) — every call also logs
    a line per channel regardless of whether the caller uses the result."""
    results = []
    for label, chat_id in channels:
        try:
            member = await bot.get_chat_member(chat_id, bot.id)
            if member.status in ("administrator", "creator"):
                log.info(f"✅ {label} ({chat_id}): admin access confirmed")
                results.append((label, chat_id, "✅ OK"))
            else:
                log.warning(f"⚠️ {label} ({chat_id}): bot is a member but NOT an admin — "
                            f"{verb} will fail. Add it as Administrator.")
                results.append((label, chat_id, "⚠️ Member, not admin"))
        except Exception as e:
            log.warning(f"❌ {label} ({chat_id}): can't access this channel at all — "
                        f"the bot likely hasn't been added there yet. ({e})")
            results.append((label, chat_id, "❌ No access"))
    return results


# --- single-instance guard -------------------------------------------------
def acquire_single_instance(name: str):
    """Keep one polling process per bot token. Returns a held file handle.
    A second Termux session cannot start the same bot until the first exits.
    """
    import os
    import fcntl
    path = os.path.expanduser(f"~/.telegram_video_bots_{name}.lock")
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("Another %s instance is already running; refusing to start a second polling process.", name)
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def direct_delivery_target(*, video_id=None, batch_id=None):
    """Choose a random healthy permanent Delivery Bot and return its direct
    Telegram URL plus bot metadata.  The caller can persist the selection so
    a later bot failure can migrate the exact catalogue link.
    """
    import random
    from urllib.parse import quote
    param=(f"b_{batch_id}" if batch_id else f"v_{video_id}" if video_id else "")
    if not param:
        return None
    try:
        import permanent_bot_store
        bots=permanent_bot_store.routable_delivery_bots(180)
    except Exception:
        bots=[]
    if not bots:
        return None
    bot=random.choice(bots)
    try:
        permanent_bot_store.record_selection(str(bot.get("id")))
    except Exception:
        pass
    username=str(bot.get("username") or "").lstrip("@")
    if not username:
        return None
    return {
        "bot_id": str(bot.get("id")),
        "username": username,
        "url": f"https://t.me/{username}?start={quote(param)}",
    }


def direct_delivery_url(*, video_id=None, batch_id=None):
    """Return a direct Telegram Delivery Bot URL from the live permanent pool.

    New catalogue messages use this direct t.me URL. If no permanent bot is
    routable, return an empty string so the system never creates a new link to
    a disabled/dead primary bot.
    """
    target=direct_delivery_target(video_id=video_id, batch_id=batch_id)
    return target["url"] if target else ""


def stable_watch_url(*, video_id=None, batch_id=None):
    """Return a stable public resolver URL that is independent of any one
    Delivery Bot. The resolver selects an active permanent Delivery Bot at
    click time, so disabling a bot does not invalidate existing catalogue
    buttons. VIDEO_VAULT_WATCH_RESOLVER_URL overrides the default base; the
    existing public API URL is used as the next fallback.
    """
    import os
    from urllib.parse import quote
    param = (f"b_{batch_id}" if batch_id else f"v_{video_id}" if video_id else "")
    if not param:
        return ""
    base = (os.getenv("VIDEO_VAULT_WATCH_RESOLVER_URL", "") or
            getattr(__import__("config"), "VIDEO_VAULT_WATCH_RESOLVER_URL", "") or
            os.getenv("VIDEO_VAULT_API_PUBLIC_URL", "") or
            getattr(__import__("config"), "VIDEO_VAULT_API_PUBLIC_URL", "") or
            "https://us1.visihost.in:5059").rstrip("/")
    if base.endswith("/watch"):
        return f"{base}?start={quote(param)}"
    return f"{base}/watch?start={quote(param)}"


# V6.1: bounded callback idempotency guard for rapid double-taps.
class CallbackGuard:
    def __init__(self, ttl_seconds=15):
        import time
        self.ttl_seconds = ttl_seconds
        self._items = {}
        self._time = time
    def acquire(self, key):
        now=self._time.monotonic()
        old=self._items.get(key)
        if old is not None and now-old < self.ttl_seconds:
            return False
        self._items[key]=now
        cutoff=now-self.ttl_seconds
        for k,t in list(self._items.items()):
            if t < cutoff:
                self._items.pop(k,None)
        return True
    def release(self,key):
        self._items.pop(key,None)
callback_guard=CallbackGuard()

async def guarded_background(callback_guard, guard_key, coro, callback_query=None):
    """Run a guarded job and release the guard only after it finishes."""
    if not callback_guard.acquire(guard_key):
        if callback_query is not None:
            try:
                await callback_query.answer("Already working on that…", show_alert=False)
            except Exception:
                # Safe no-op: this secondary cleanup/notification failure must not mask the primary operation.
                pass
        return False
    try:
        await coro
        return True
    finally:
        callback_guard.release(guard_key)


def guarded_handler(callback_guard, key_factory, busy_text="Already working on that…"):
    """Opt-in decorator for real Telegram handlers.

    The wrapped handler owns the guard lifecycle, so duplicate callback
    delivery cannot start the same operation twice.
    """
    from functools import wraps
    def deco(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            key = key_factory(*args, **kwargs)
            if not callback_guard.acquire(key):
                # Best-effort Telegram acknowledgement; never masks the guard.
                q = next((a for a in args if hasattr(a, "answer")), None)
                if q is not None:
                    try:
                        await q.answer(busy_text, show_alert=False)
                    except Exception:
                        pass
                return None
            try:
                return await fn(*args, **kwargs)
            finally:
                callback_guard.release(key)
        return wrapped
    return deco


# --- Mandatory audience-channel gate ---------------------------------------
DEFAULT_MANDATORY_JOIN_LINK = "https://t.me/shivanifanbase"
DEFAULT_MANDATORY_JOIN_ID = "@shivanifanbase"

def get_mandatory_join_config(config):
    saved_id = saved_link = None
    try:
        import db
        saved_id = db.get_setting("mandatory_join_channel_id")
        saved_link = db.get_setting("mandatory_join_channel_link")
    except Exception:
        pass
    raw_id = str(saved_id or getattr(config, "ALERT_CHANNEL_ID", "") or DEFAULT_MANDATORY_JOIN_ID).strip()
    link = str(saved_link or getattr(config, "ALERT_CHANNEL_LINK", "") or DEFAULT_MANDATORY_JOIN_LINK).strip()
    if raw_id.startswith(("https://t.me/", "http://t.me/")):
        tail = raw_id.rstrip("/").split("/")[-1]
        if tail and not tail.startswith("+"):
            raw_id = "@" + tail.lstrip("@")
    elif raw_id and not raw_id.startswith("@") and not raw_id.lstrip("-").isdigit():
        raw_id = "@" + raw_id.lstrip("@")
    return raw_id, link

async def mandatory_join_status(bot, user_id: int, config):
    chat_id, link = get_mandatory_join_config(config)
    try:
        chat = await bot.get_chat(chat_id)
        member = await bot.get_chat_member(chat.id, user_id)
        status = getattr(member, "status", "")
        joined = status in ("member", "administrator", "creator") or (status == "restricted" and bool(getattr(member, "is_member", False)))
        if getattr(chat, "username", None) and (not link or link == DEFAULT_MANDATORY_JOIN_LINK and chat.username != "shivanifanbase"):
            link = f"https://t.me/{chat.username}"
        return joined, link, getattr(chat, "title", None) or "Mandatory Join Channel", None
    except Exception as exc:
        log.warning("Mandatory join verification failed user=%s target=%s: %s", user_id, chat_id, exc)
        return False, link, "Mandatory Join Channel", str(exc)

MANDATORY_JOIN_IMAGE = __import__("os").path.join(__import__("os").path.dirname(__file__), "assets", "mandatory_join.jpg")
DEFAULT_MANDATORY_JOIN_MESSAGE = "🔒 One quick step\n\n👥 Join {channel} to continue.\n🔐 This check keeps access safe and automatic."

def get_mandatory_join_message(config, title: str = "Shivani's Fanbase"):
    try:
        import db
        saved = db.get_setting("mandatory_join_message")
    except Exception:
        saved = None
    template = str(saved or DEFAULT_MANDATORY_JOIN_MESSAGE)
    return template.replace("{channel}", title or "Shivani's Fanbase")

async def send_mandatory_join_prompt(message, title: str, link: str, detail: str = ""):

    """Send the branded mandatory-join gate as the supplied image + caption.
    Falls back to a normal text message if the bundled image cannot be sent.
    """
    from telegram import InputFile
    caption = html.escape(get_mandatory_join_message(__import__("config"), title)) + html.escape(detail or "")
    try:
        with open(MANDATORY_JOIN_IMAGE, "rb") as fh:
            return await message.reply_photo(
                photo=InputFile(fh, filename="mandatory_join.jpg"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=mandatory_join_keyboard(link),
            )
    except Exception as exc:
        log.warning("Mandatory join image send failed: %s", exc)
        return await message.reply_text(
            caption, parse_mode="HTML", reply_markup=mandatory_join_keyboard(link)
        )

def mandatory_join_keyboard(link: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢  Join Channel", url=link)],
        [InlineKeyboardButton("✅  I've Joined · Check", callback_data="mandatory_join_check")],
        [InlineKeyboardButton("↻  Check Again", callback_data="mandatory_join_check")],
    ])

# --- V7 premium bot experience ------------------------------------------------
# Centralized presentation/setup helpers. These only change Telegram UI copy,
# command labels and menu buttons; storage and delivery logic remain untouched.
BOT_COMMANDS = {
    "catalog": [
        ("start", "Open the video vault"),
        ("today", "See today's fresh uploads"),
        ("search", "Find a video by title, tag or ID"),
        ("browse", "Browse the catalogue"),
        ("refresh", "Refresh catalogue data"),
        ("tips", "Quick tips & shortcuts"),
        ("latest", "Open the latest uploads"),
        ("home", "Return to the catalogue home"),
    ],
    "delivery": [
        ("start", "Open a video delivery"),
        ("menu", "Open the delivery hub"),
        ("status", "Check current delivery status"),
        ("redeem", "Redeem an access code"),
        ("help", "How delivery works"),
        ("hub", "Open the delivery hub"),
    ],
    "storage": [
        ("start", "Open the upload workspace"),
        ("bulk", "Start a bulk upload"),
        ("status", "Check the current upload"),
        ("cancel", "Cancel the current flow"),
        ("upload", "Start a single upload"),
        ("workspace", "Open the upload workspace"),
    ],
    "admin": [
        ("start", "Open the admin console"),
        ("menu", "Open the admin dashboard"),
        ("health", "Check system health"),
        ("stats", "View catalogue statistics"),
        ("queue", "View the alert queue"),
        ("dashboard", "Open the admin dashboard"),
        ("control", "Open the control centre"),
    ],
}

async def configure_bot_ui(app, bot_kind: str):
    """Apply a consistent, polished Telegram command/menu experience."""
    commands = BOT_COMMANDS.get(bot_kind, [])
    if commands:
        try:
            from telegram import BotCommand, MenuButtonCommands
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in commands])
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as exc:
            log.warning("Bot UI setup skipped for %s: %s", bot_kind, exc)
    try:
        await app.bot.set_my_short_description({
            "catalog": "🎬 Video Vault · fresh uploads, search and browsing",
            "delivery": "🍿 Video Vault · fast, clean content delivery",
            "storage": "📦 Video Vault · upload and publish workspace",
            "admin": "🛠️ Video Vault · admin control centre",
        }.get(bot_kind, "✨ Video Vault"))
    except Exception:
        pass
    try:
        await app.bot.set_my_description(
            {
                "catalog": "🎬 Your video vault — discover fresh uploads, search by title or tags, and open any item in one tap.",
                "delivery": "🍿 Fast, tidy video delivery with resume support and clear progress updates.",
                "storage": "📦 Private upload workspace for covers, metadata, bulk batches and safe publishing.",
                "admin": "🛠️ Control centre for catalogue, queue, health, limits, backups and operations.",
            }.get(bot_kind, "✨ Video Vault")
        )
    except Exception:
        pass


def premium_panel(title: str, subtitle: str = "") -> str:
    """Small shared header used by bot screens that want the V7 visual style."""
    text = f"✨ <b>{title}</b>"
    if subtitle:
        text += f"\n<i>{subtitle}</i>"
    return text


def premium_divider() -> str:
    return "───────────────"


# --- Presentation compatibility aliases (V7.5.5) ---------------------------
# Older catalogue/delivery call sites referenced these helpers through
# botutil while the UX presentation layer owns their implementation.
# Keep aliases here so mixed-version deployments cannot crash at runtime.
import bot_ux as _bot_ux

def footer_hint(text: str = "") -> str:
    return _bot_ux.footer_hint(text)

def brand_header(title: str = "Video Vault", subtitle: str = "") -> str:
    return _bot_ux.brand_header(title, subtitle)


def memory_pressure_ratio():
    """Return cgroup memory usage ratio when available, otherwise None.
    Advisory only: never kills, deletes, disables, or removes a bot.
    """
    try:
        candidates = [
            ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
            ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        ]
        for used_path, limit_path in candidates:
            if not (os.path.exists(used_path) and os.path.exists(limit_path)):
                continue
            with open(used_path, "r", encoding="utf-8") as fh:
                used = int(fh.read().strip())
            with open(limit_path, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
            if raw == "max":
                return None
            limit = int(raw)
            if limit > 0 and limit < (1 << 60):
                return max(0.0, min(2.0, used / limit))
    except Exception:
        pass
    return None


def run_polling_resilient(app, role: str = "bot"):
    """Run PTB polling with a supervisor-friendly Conflict exit code.

    A Telegram 409 means another process is polling the same bot token. It is
    not useful to spin the process every few seconds: that creates a restart
    storm and wastes RAM. The supervisor recognizes exit code 75 and applies
    a long backoff while the existing owner can continue serving the bot.
    Other exceptions retain their normal behavior.
    """
    from telegram.error import Conflict
    try:
        return app.run_polling()
    except Conflict:
        log.error("%s: Telegram getUpdates conflict (another instance is polling this token); exiting with backoff code 75.", role)
        raise SystemExit(75)
