"""Supervisor for Admin-managed permanent Delivery Bots.

Every active bot runs in its own process. Stable Watch URLs are resolved at
click-time by the VPS, so inactive/deleted/banned bots do not own any link.
"""
from __future__ import annotations
import collections, os, subprocess, sys, time
from datetime import datetime, timedelta
import config
import permanent_bot_store

BASE_DIR=os.path.dirname(os.path.abspath(__file__))
import botutil
_runner_lock = botutil.acquire_single_instance("permanent_delivery_runner")
if _runner_lock is None:
    raise SystemExit(0)
workers={}
worker_tokens={}
next_restart={}
crashes=collections.defaultdict(collections.deque)
last_link_audit=0.0
last_worker_launch=0.0
HEARTBEAT_TIMEOUT=180.0

def seed_primary():
    token=getattr(config,"DELIVERY_BOT_TOKEN","") or ""
    username=getattr(config,"DELIVERY_BOT_USERNAME","") or ""
    if token: permanent_bot_store.ensure_primary(token, username)

def desired():
    return {str(b["id"]):b for b in permanent_bot_store.list_bots() if b.get("token")}

def launch(bot):
    env=os.environ.copy(); bid=str(bot["id"])
    env["VV_PERMANENT_DELIVERY_ID"]=bid
    env["VV_PERMANENT_DELIVERY_TOKEN"]=str(bot["token"])
    env["VV_PERMANENT_DELIVERY_USERNAME"]=str(bot.get("username") or "")
    print(f"🚀 Permanent Delivery @{str(bot.get('username') or bid).lstrip('@')} → worker", flush=True)
    return subprocess.Popen([sys.executable,"-u","permanent_delivery_worker.py"],cwd=BASE_DIR,env=env,stdin=subprocess.DEVNULL)

def terminate(proc):
    if proc and proc.poll() is None:
        try: proc.terminate()
        except Exception: pass

def crash_delay(key):
    now=time.monotonic(); q=crashes[key]
    while q and now-q[0]>600: q.popleft()
    q.append(now)
    return min(60,3*(2**min(max(0,len(q)-5),4)))

def stop_bot(bid):
    proc=workers.pop(bid,None)
    if proc: terminate(proc)
    worker_tokens.pop(bid,None); next_restart.pop(bid,None)

def start_bot(bid,row,now):
    try:
        permanent_bot_store.mark_starting(bid)
        workers[bid]=launch(row)
        worker_tokens[bid]=str(row.get("token") or "")
        next_restart[bid]=0
        permanent_bot_store.touch_status(bid,last_started_at=permanent_bot_store._now(),last_error=None)
    except Exception as exc:
        permanent_bot_store.record_failure(bid,f"runner launch: {str(exc)[:400]}",cooldown_seconds=30)
        next_restart[bid]=now+30

def _cooldown_active(row):
    cd=row.get("cooldown_until")
    if not cd: return False
    try: return datetime.fromisoformat(str(cd)).timestamp() > time.time()
    except Exception: return False

def reconcile():
    global last_worker_launch
    now=time.monotonic(); want=desired()
    # Stop deleted, retired and disabled workers immediately.
    for bid in list(workers):
        row=want.get(bid)
        if not row or not row.get("enabled") or row.get("retired"):
            stop_bot(bid)
    for bid,row in want.items():
        active=bool(row.get("enabled") and not row.get("retired"))
        if not active: continue
        proc=workers.get(bid)
        token=str(row.get("token") or "")
        if worker_tokens.get(bid) and worker_tokens[bid]!=token:
            stop_bot(bid); proc=None
        if proc is None:
            if str(row.get("health") or "starting") == "quarantined":
                continue
            if _cooldown_active(row):
                continue
            if now >= next_restart.get(bid,0) and now - last_worker_launch >= 2.0:
                start_bot(bid,row,now)
                last_worker_launch = now
            continue
        if proc.poll() is not None:
            code=proc.returncode
            workers.pop(bid,None); worker_tokens.pop(bid,None)
            delay = 90 if code == -9 else crash_delay(bid)
            next_restart[bid]=now+delay
            permanent_bot_store.touch_status(bid,last_error=f"worker exited with code {code}",health="degraded",cooldown_until=(datetime.now().astimezone()+timedelta(seconds=delay)).isoformat())
            print(f"⚠️ Permanent Delivery {bid} exited ({code}); retry in {delay}s",flush=True)
            continue

        # A worker can remain alive while its event loop/polling path is hung.
        # The dedicated heartbeat thread makes this distinguishable from a
        # normal Telegram/network cooldown. Restart only after a generous
        # timeout, and never restart quarantined/disabled bots.
        row_age = permanent_bot_store._heartbeat_age(row)
        if row_age is not None and row_age > HEARTBEAT_TIMEOUT and str(row.get("health") or "") not in {"quarantined", "disabled"}:
            print(f"⚠️ Permanent Delivery {bid} heartbeat stale ({row_age:.0f}s); watchdog restart",flush=True)
            permanent_bot_store.record_watchdog_restart(bid, f"Heartbeat stale for {row_age:.0f}s")
            stop_bot(bid)
            next_restart[bid]=time.monotonic()+15

seed_primary()
print("✨ Permanent Delivery Runner online",flush=True)
try:
    while True:
        reconcile()
        if time.monotonic() - last_link_audit >= 30:
            try:
                from delivery_link_migrator import audit_unroutable_link_owners
                audit_unroutable_link_owners()
            except Exception:
                pass
            last_link_audit = time.monotonic()
        time.sleep(2)
except KeyboardInterrupt:
    pass
finally:
    for p in list(workers.values()): terminate(p)
