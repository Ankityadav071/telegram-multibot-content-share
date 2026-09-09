# Video Vault V9.8.0 — Next Evolution Step 3: Catalogue UX

## Changes
- Unified the persistent catalogue keyboard through one helper.
- Put Categories and Tags directly on the main keyboard; they were defined but not consistently exposed.
- Added a consistent inline listing footer with Prev/Next, Back to Menu, and Web App.
- Added a cancellable Search Mode so users are never trapped after hiding the reply keyboard.
- Search cancellation clears only search state and restores the persistent menu.
- Existing Delivery Bot resolver/links, catalogue video IDs, batch links, and migration behavior are unchanged.

## Safety
- No DB schema changes.
- No resolver algorithm changes.
- No delivery mapping changes.
- Python compile check passed.
