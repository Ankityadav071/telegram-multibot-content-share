# V9.5.6 Mini App Direct t.me

- Today, Premium Today, Popular, and Premium Popular are separate server modes.
- Featured Spotlight is never used as the source of truth for those lists.
- List cards open delivery directly; no extra "Watch in Telegram" step is required.
- The Mini App asks the VPS API for a current healthy direct t.me target silently; the VPS URL is not user-facing.
- Cache namespace is versioned to prevent stale list JavaScript after deployment.
