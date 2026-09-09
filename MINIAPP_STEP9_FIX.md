# Video Vault V9.8.0 — Step 9

## Mini App UI fix
- Removed the rounded/translucent container behind the Featured hero slide dots at the top-right.
- Dot indicators remain visible and clickable; only the unwanted pill/overlay chrome was removed.
- No image-generation or asset replacement was used.

## API consistency
- `/api/home` version field updated from the stale `9.5.12` value to `9.8.0`.

## Security polish
- Added a low-risk `Permissions-Policy` response header disabling camera, microphone, and geolocation for the public catalogue API.

## Compatibility
- Delivery Bot resolver/routing unchanged.
- Existing Mini App API routes unchanged.
- Database schema unchanged.
