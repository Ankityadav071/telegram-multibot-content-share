"""
Optional ad/shortlink integration used to gate specific videos behind an
ad-supported shortlink instead of (or alongside) a redeem code.

Pluggable by design: most shortlink/ad-lock services (GPLinks, ShrinkMe,
Linkpays, exe.io, and similar) share the same basic shape — a GET request
with your API key and the destination URL, returning either a JSON blob
with a "shortenedUrl"-style field, or the short URL as plain text. Point
config.SHORTLINK_API_URL / SHORTLINK_API_KEY at your provider and adjust
_extract_short_url() below if its response shape differs; everything else
here should work unchanged.
"""
import logging

import requests

import config

log = logging.getLogger("shortlink")


def _extract_short_url(response_text: str, response_json):
    if isinstance(response_json, dict):
        for key in ("shortenedUrl", "shortened_url", "short", "shortUrl", "url", "link"):
            val = response_json.get(key)
            if val:
                return val
    text = (response_text or "").strip()
    if text.startswith("http"):
        return text
    return None


def is_configured() -> bool:
    return bool(config.SHORTLINK_API_URL and config.SHORTLINK_API_KEY)


def shorten(long_url: str) -> str | None:
    """Return an ad-gated URL or ``None`` when the ad provider is unavailable.

    Ad access must fail closed: falling back to the raw Telegram deep-link
    would let a user bypass the intended ad step.
    """
    if not is_configured():
        return None
    try:
        resp = requests.get(
            config.SHORTLINK_API_URL,
            params={"api": config.SHORTLINK_API_KEY, "url": long_url},
            timeout=5,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            data = None
        short = _extract_short_url(resp.text, data)
        if not short:
            log.warning("Shortlink API returned an unrecognized response shape: %r", resp.text[:200])
        return short
    except Exception:
        log.exception("Shortlink API call failed; ad unlock is temporarily unavailable")
        return None

