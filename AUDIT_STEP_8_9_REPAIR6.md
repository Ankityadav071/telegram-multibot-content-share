# Video Vault V9.7.4 — Audit Repair 6

## Step 8/9 deeper hardening
- Added per-video serialization for Storage `Rebuild Mapping` and `Send Replacement`. Two admins can no longer concurrently mutate the same video's live storage mappings inside the same Storage Bot process.
- Replacement and repair now share the same lock, so a repair cannot overwrite a replacement (or vice versa) midway through the operation.
- Hardened `set_backup_msg_id()` and `set_recovery_msg_id()` to raise on a missing video instead of silently reporting success. Optional mappings remain nullable.

## Preserved
- Normal Single/Bulk upload state is unchanged.
- Existing resolver/random healthy Delivery Bot selection is unchanged.
- No destructive DB migration or reset.
- Existing Video IDs and catalogue links remain stable during replacement.

## Verification
- All Python modules compile successfully.
- No duplicate top-level function definitions detected.
- Replacement/repair share a per-video lock.
