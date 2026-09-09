# V9.8.0 Step 8 — Advanced Discovery

Implemented additive audience discovery features:
- Private per-user Favorites stored in a dedicated table.
- Add/remove Favorite directly from each catalogue card.
- Recently Watched list derived from existing delivery analytics.
- Lightweight For You recommendations derived from tags of the user's recent deliveries.
- Favorites/Recent/For You added to the persistent catalogue menu.
- Video deletion cleans user favorite rows to avoid stale favorites.
- Delivery Bot links/resolver policy, storage mappings, quota, and existing catalogue delivery flow remain unchanged.
- No destructive DB reset; new tables are created through the existing additive init/migration path.
- All Python modules compile-tested successfully.
