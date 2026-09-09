"""Run one permanent Delivery Bot instance with an Admin-managed token."""
from __future__ import annotations
import os
import asyncio
import threading
import time
import hashlib
import config
import storage_config
import botutil
from telegram import BotCommand, Bot
from telegram.error import RetryAfter, Conflict, NetworkError, TimedOut, InvalidToken, Forbidden

BOT_ID = os.getenv("VV_PERMANENT_DELIVERY_ID", "unknown")
TOKEN = os.getenv("VV_PERMANENT_DELIVERY_TOKEN", "")
USERNAME = os.getenv("VV_PERMANENT_DELIVERY_USERNAME", "")

if not TOKEN:
    raise SystemExit("VV_PERMANENT_DELIVERY_TOKEN is required")

config.DELIVERY_BOT_TOKEN = TOKEN
if USERNAME:
    config.DELIVERY_BOT_USERNAME = USERNAME.lstrip("@")

import permanent_bot_store
import delivery_link_migrator
import delivery_bot

_instance_lock = botutil.acquire_single_instance("permanent_delivery_token_" + hashlib.sha256(TOKEN.encode()).hexdigest()[:24])
if _instance_lock is None:
    raise SystemExit(0)


def _heartbeat(stop_event):
    while not stop_event.wait(20):
        try:
            permanent_bot_store.touch_status(BOT_ID, last_seen_at=permanent_bot_store._now())
        except Exception:
            pass

async def _preflight():
    bot=Bot(TOKEN)
    try:
        me=await bot.get_me()
        if not me or not me.id or not me.username:
            raise RuntimeError("Telegram did not return a usable bot identity")
        # A Delivery Bot is useful only when it can access at least one source
        # channel. This prevents a perfectly valid token with no channel access
        # from entering the resolver pool.
        channels=[]
        for label, cid in (("Primary Channel", storage_config.primary()), ("Backup Channel", storage_config.backup()), ("3rd Content Channel", storage_config.recovery())):
            if cid is not None and str(cid).strip() and int(cid or 0):
                channels.append((label,cid))
        access=await botutil.check_channel_access(bot, channels, "copying delivery media") if channels else []
        usable=any(str(status).startswith("✅") for _,_,status in access) if access else True
        if not usable:
            raise RuntimeError("Bot token is valid, but the bot has no usable source-channel admin access")
        permanent_bot_store.mark_ready(BOT_ID, username=me.username or USERNAME, first_name=me.first_name or "Delivery Bot")
        return me
    finally:
        try:
            await bot.close()
        except Exception:
            pass


def main():
    stop=threading.Event()
    try:
        asyncio.run(_preflight())
    except (InvalidToken, Forbidden) as exc:
        delivery_link_migrator.migrate_bot_links(BOT_ID, reason=f"Telegram rejected bot during preflight: {str(exc)[:220]}")
        permanent_bot_store.record_failure(BOT_ID, f"Telegram rejected this bot/token: {str(exc)[:400]}", fatal=True)
        stop.wait(60)
        return
    except Exception as exc:
        permanent_bot_store.record_failure(BOT_ID, f"Preflight failed: {str(exc)[:450]}", fatal=("no usable source-channel admin access" in str(exc).lower()), cooldown_seconds=60)
        stop.wait(60)
        return
    hb=threading.Thread(target=_heartbeat,args=(stop,),name=f"vv-hb-{BOT_ID}",daemon=True); hb.start()
    try:
        while not stop.is_set():
            try:
                result=delivery_bot.main()
                if result is False:
                    permanent_bot_store.record_failure(BOT_ID,"Another process is already polling this bot token.",cooldown_seconds=60)
                    stop.wait(60); continue
                break
            except (InvalidToken, Forbidden) as exc:
                delivery_link_migrator.migrate_bot_links(BOT_ID, reason=f"Telegram rejected bot/token: {str(exc)[:220]}")
                permanent_bot_store.record_failure(BOT_ID,f"Telegram rejected this bot/token: {str(exc)[:400]}",fatal=True)
                # Keep the worker alive but out of the resolver pool. Token Update
                # or re-enable by Admin causes the supervisor to relaunch it.
                stop.wait(60)
            except RetryAfter as exc:
                wait_for=max(30,int(getattr(exc,"retry_after",60))+5)
                permanent_bot_store.record_failure(BOT_ID,f"Flood control; retrying in {wait_for}s.",cooldown_seconds=wait_for)
                stop.wait(wait_for)
            except Conflict:
                permanent_bot_store.record_failure(BOT_ID,"Telegram polling conflict; another instance may be using this token.",cooldown_seconds=90)
                stop.wait(90)
            except (NetworkError, TimedOut) as exc:
                permanent_bot_store.record_failure(BOT_ID,f"Temporary network error: {str(exc)[:350]}",cooldown_seconds=30)
                stop.wait(30)
            except Exception as exc:
                permanent_bot_store.record_failure(BOT_ID,str(exc)[:500],cooldown_seconds=60)
                stop.wait(60)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
