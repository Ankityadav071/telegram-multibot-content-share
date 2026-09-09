"""Dynamic supervisor for Admin-managed temporary bots.

It watches temp_bots.json and launches/stops one isolated worker process per
enabled bot, so adding/removing bots never requires restarting the main bots.
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import time

import temp_bot_store
import botutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_runner_lock = botutil.acquire_single_instance("temp_bot_runner")
if _runner_lock is None:
    raise SystemExit(0)
workers = {}
failures = {}
stopping = False
last_spawn = 0.0
print("✨ Temp Bot Runner 9.4 online — health-aware self-healing", flush=True)


def spawn(row):
    env = os.environ.copy()
    env["VV_TEMP_BOT_ID"] = str(row["id"])
    env["VV_TEMP_BOT_TOKEN"] = str(row["token"])
    return subprocess.Popen([sys.executable, "-u", "temp_bot_worker.py"], cwd=BASE_DIR, env=env, stdin=subprocess.DEVNULL)


def stop_worker(bot_id):
    proc = workers.pop(str(bot_id), None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def stop_all(*_):
    global stopping
    stopping = True
    for bot_id in list(workers):
        stop_worker(bot_id)
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop_all)
signal.signal(signal.SIGINT, stop_all)

while not stopping:
    configured = {str(r["id"]): r for r in temp_bot_store.list_bots() if r.get("enabled") and r.get("token")}
    for bot_id in list(workers):
        if bot_id not in configured:
            stop_worker(bot_id)
    for bot_id, row in configured.items():
        proc = workers.get(bot_id)
        if proc is None:
            state = failures.get(bot_id, {"count": 0, "next": 0.0})
            if time.monotonic() < state.get("next", 0.0):
                continue
            try:
                if time.monotonic() - last_spawn < 2.0:
                    continue
                workers[bot_id] = spawn(row)
                last_spawn = time.monotonic()
                temp_bot_store.mark_start(bot_id)
            except Exception as exc:
                temp_bot_store.mark_failure(bot_id, f"runner spawn: {exc}")
        elif proc.poll() is not None:
            workers.pop(bot_id, None)
            state = failures.get(bot_id, {"count": 0, "next": 0.0})
            state["count"] += 1
            base = 60 if proc.returncode == -9 else 5
            state["next"] = time.monotonic() + min(600, base * (2 ** min(state["count"] - 1, 6)))
            failures[bot_id] = state
            exit_msg = f"worker exited with code {proc.returncode}; retry in {int(state['next']-time.monotonic())}s"
            temp_bot_store.mark_failure(bot_id, exit_msg)
            # Never silently disable a user-managed Temp Bot. The old
            # auto-disable-after-5-crashes behavior made bots appear to
            # randomly switch themselves OFF. Keep retrying with backoff and
            # show the exact startup error in Admin instead.
    time.sleep(3)
