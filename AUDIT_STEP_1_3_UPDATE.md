# Video Vault Audit & Repair — Audit Update

## Completed in this run

### Step 1 — Architecture / dependency audit
- Parsed all 22 Python modules successfully.
- Cross-checked imported-module attribute calls; no unresolved imported-module function references were found by static scan.
- Found and removed a duplicate top-level `db.set_setting()` definition that was silently shadowing the earlier definition.
- Audited handler registration counts and callback routing; no duplicate top-level function definitions remain.
- Compared V9.7.4 against V9.7.3 to isolate repair changes; resolver/routing code was not changed by this audit.

### Step 2 — State / concurrency hardening
- Replacement video handling is checked before normal Single/Bulk handling.
- Replacement photo/document-image input is now rejected while replacement mode is armed, preventing accidental cover/pending-upload state changes.
- Storage menu cancellation now uses the same complete upload-session reset as `/cancel`.
- `force_single_next` is explicitly cleared on cancellation, preventing a stale single-mode flag from changing a later unrelated upload.
- Permanent-bot retirement migration is now triggered after releasing the permanent-bot store lock. This avoids a lock re-entry/deadlock path because migration selects candidate bots from the same store.

### Step 3 — DB / mapping integrity
- Added a dedicated atomic `replace_video_mappings()` transaction for Primary/Backup/3rd Content message IDs.
- Replacement no longer uses the generic metadata editor for storage mappings.
- Generic `db.edit_video()` now has an explicit metadata whitelist and rejects storage mapping columns, reducing accidental cross-feature mutation.
- Verified with a temporary SQLite test database that mapping replacement commits all three IDs together and that `edit_video(primary_msg_id=...)` is rejected.

## Verification
- All 22 Python modules compile successfully with `py_compile`.
- No duplicate top-level function definitions remain.
- Replacement ordering/state isolation checks pass.
- Temporary SQLite mapping transaction test passes.
- No database reset or destructive migration was performed.
