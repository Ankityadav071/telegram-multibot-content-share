# Telegram Video Vault V9.8.0 — Final Release Audit

## Final regression
- All Python modules compile successfully with Python 3.13.
- Netlify inline JavaScript syntax-checks successfully when extracted from `index.html`.
- No runtime secrets, `.env`, production DB, bot registry, or token files are included.
- Delivery resolver/routing policy remains unchanged.
- SQLite schema is not reset or destructively migrated by this release.
- Storage replacement/rebuild remains isolated from normal Single/Bulk upload state.
- Permanent Delivery Bot health/watchdog additions remain consistent with disabled/quarantined semantics.
- Catalogue persistent navigation and Mini App API/frontend contracts remain backward-compatible.
- Requested Mini App Featured dots overlay chrome is removed; the dots remain clickable.

## Release hygiene
- Removed Python `__pycache__` artifacts from the release package.
- Removed temporary deployment notes that are not required at runtime.
- Runtime/config data must be supplied separately on the VPS/Netlify environment.

## Known intentional behavior
- The resolver still prefers routable healthy bots and retains the existing degraded fallback behavior.
- Historical analytics/activity rows are retained when videos are deleted so existing cleanup/accounting behavior remains safe.
- Search temporarily hides the reply keyboard while waiting for free-form input and restores it afterward.

## Validation status
Static validation: PASS
Live Telegram production smoke test: NOT RUN (no production credentials/channels available in the build environment).
