"""Video Vault process supervisor.

V7: resilient multi-bot runner with graceful shutdown, missing-script safety,
crash-loop backoff and periodic child health logging. It never duplicates a
bot intentionally and does not change any bot's business logic.
"""
import collections
import os
import signal
import subprocess
import sys
import time

# web_api.py is deployed alongside the bot bundle on some VPS layouts.  The
# supervisor now skips it cleanly when this standalone bots package is used.
CANDIDATE_SCRIPTS = ["web_api.py", "permanent_delivery_runner.py", "catalog_bot.py", "admin_bot.py", "storage_bot.py", "temp_bot_runner.py"]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = [s for s in CANDIDATE_SCRIPTS if os.path.exists(os.path.join(BASE_DIR, s))]
import fcntl
_supervisor_lock_path = os.path.expanduser("~/.telegram_video_vault_supervisor.lock")
_supervisor_lock = open(_supervisor_lock_path, "a+")
try:
    fcntl.flock(_supervisor_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("❌ Another Video Vault supervisor is already running.", flush=True)
    raise SystemExit(0)
children = {}
_restart_history = collections.defaultdict(collections.deque)
_next_restart = {}
_stopping = False


def launch(script):
    print(f"🚀 Starting {script}…", flush=True)
    return subprocess.Popen(
        [sys.executable, "-u", script],
        cwd=BASE_DIR,
        stdin=subprocess.DEVNULL,
    )


def _restart_delay(script, code=None):
    now = time.monotonic()
    q = _restart_history[script]
    while q and now - q[0] > 600:
        q.popleft()
    q.append(now)
    count = len(q)
    if code == -9:
        delay = min(180, 60 * (2 ** min(max(count - 1, 0), 2)))
        print(f"🧠 {script}: resource kill (-9); cooling down {delay}s", flush=True)
        return delay
    if code == 75:
        delay = min(180, 90 * (2 ** min(max(count - 1, 0), 1)))
        print(f"⚠️ {script}: Telegram polling conflict; cooling down {delay}s", flush=True)
        return delay
    if count >= 5:
        delay = min(120, 3 * (2 ** min(count - 5, 5)))
        print(f"🔥 {script}: crash-loop protection ({count} restarts/10m), waiting {delay}s", flush=True)
        return delay
    return 3


def stop_all(*_):
    global _stopping
    if _stopping:
        return
    _stopping = True
    print("🛑 Shutting down Video Vault workers…", flush=True)
    for script, proc in list(children.items()):
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and any(p.poll() is None for p in children.values()):
        time.sleep(0.2)
    for proc in children.values():
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop_all)
signal.signal(signal.SIGINT, stop_all)

if not SCRIPTS:
    print("❌ No bot scripts found in this directory.", flush=True)
    raise SystemExit(1)

print(f"✨ Video Vault Supervisor online · {len(SCRIPTS)} worker(s)", flush=True)

# Stagger launches and schedule restarts without blocking the monitoring of
# other workers. This smooths RAM spikes on small VPS containers.
_next_initial = {script: (i * 2.0) for i, script in enumerate(SCRIPTS)}

while True:
    time.sleep(1)
    if _stopping:
        break
    now = time.monotonic()
    for script in SCRIPTS:
        if script in children:
            continue
        if now < _next_restart.get(script, _next_initial.get(script, 0.0)):
            continue
        try:
            import botutil
            ratio = botutil.memory_pressure_ratio()
        except Exception:
            ratio = None
        if ratio is not None and ratio >= 0.92:
            _next_restart[script] = now + 20
            continue
        children[script] = launch(script)
        _next_initial.pop(script, None)
        break

    for script, proc in list(children.items()):
        code = proc.poll()
        if code is not None:
            children.pop(script, None)
            delay = _restart_delay(script, code)
            _next_restart[script] = time.monotonic() + delay
            print(f"⚠️ {script} exited with code {code}; restarting after backoff…", flush=True)
