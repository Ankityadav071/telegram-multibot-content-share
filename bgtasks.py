"""
Shared helper to keep asyncio background tasks alive.

asyncio.create_task() returns a Task object, but if nothing keeps a
reference to it, the event loop only holds a *weak* reference internally —
the task can be silently garbage collected mid-run, well before it
completes. This is a documented asyncio gotcha (see the "Important" note
under asyncio.create_task in the standard library docs), and it was the
real cause of auto-delete timers occasionally never firing: a 30-minute
asyncio.sleep() with its Task referenced nowhere is exactly the shape of
coroutine most likely to get collected during that long wait. The same
bug threatened Admin Bot's forever-running scheduler loop (auto-alert,
nightly verify, weekly backup) even more seriously, since a collected
scheduler task means those jobs just silently stop, with no error at all.

Route every fire-and-forget asyncio.create_task() through spawn() instead,
which keeps a strong reference in a module-level set until the task
finishes.
"""
import asyncio
import logging

log = logging.getLogger("bgtasks")

_tasks: set = set()


def spawn(coro, name: str = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)

    def _done(t: asyncio.Task):
        _tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.error(f"Background task {t.get_name()!r} raised", exc_info=exc)

    task.add_done_callback(_done)
    return task
