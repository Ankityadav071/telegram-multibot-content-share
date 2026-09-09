"""
Shared error-handling helper for all four bots.

Long-polling over a real-world (often mobile) connection routinely hits
transient network hiccups — a dropped read, a connect timeout, a reset
connection. python-telegram-bot's own polling loop already retries these
on its own; they resolve within a poll cycle or two and need no action.
Treating every one of them as an "unhandled exception" and paging the
admin for each — which is what was happening — just trains you to ignore
the alerts, drowning out the ones that are actually worth seeing.
"""
import telegram.error

try:
    import httpx
    _HTTPX_TRANSIENT = (
        httpx.ReadError, httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectTimeout,
        httpx.WriteError, httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError,
    )
except ImportError:  # pragma: no cover - httpx ships with python-telegram-bot
    _HTTPX_TRANSIENT = ()

_PTB_TRANSIENT = (telegram.error.NetworkError, telegram.error.TimedOut)

_TRANSIENT = _PTB_TRANSIENT + _HTTPX_TRANSIENT


def is_transient_network_error(err: BaseException) -> bool:
    return isinstance(err, _TRANSIENT)
