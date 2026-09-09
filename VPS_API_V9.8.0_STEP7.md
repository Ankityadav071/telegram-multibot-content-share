# Video Vault V9.8.0 — Step 7: VPS API + Netlify Evolution

- VPS JSON API responses now use the explicit 9.8.0 API version header.
- Safe public read endpoints use short cache windows with stale-while-revalidate hints; resolver/status/watch remain live/no-store.
- Added defensive response headers (`nosniff`, `Referrer-Policy`, frame restriction).
- Fixed `/api/watch` exception handling referencing an undefined `path` variable.
- Netlify Mini App now stores successful catalogue responses in both sessionStorage and localStorage.
- If the API is temporarily unavailable, the Mini App can render a persistent cached catalogue for up to 6 hours.
- Resolver and resolver-status responses are explicitly excluded from persistent caching.
- Added missing `manifest.webmanifest` referenced by the frontend.
- Existing `/api/*` Netlify → VPS proxy remains unchanged.
- Delivery resolver/routing and DB schema were not changed.
