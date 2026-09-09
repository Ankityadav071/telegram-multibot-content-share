# Video Vault V9.7.4 — Audit Repair 4

## Scope
Admin callback/command routing, Catalogue navigation/state, cross-feature handler collisions, and regression verification.

## Finding fixed
### Duplicate `/dashboard` command registration
`admin_bot.py` registered `/dashboard` twice:
- first -> `menu_cmd`
- later -> `control_cmd`

python-telegram-bot uses the first matching command handler in the group, so the second registration was effectively unreachable. This was a stale handler collision and made the intended command map ambiguous.

**Repair:** retained the existing `/dashboard -> menu_cmd` alias and removed the unreachable duplicate registration. `/control` remains the explicit Control Center command.

## Handler collision audit
- Admin has one catch-all callback router after its specific mandatory-join callback.
- Storage has one catch-all callback router with explicit branch dispatch and a guarded confirm-save path.
- Catalogue callback patterns are disjoint: mandatory join, open menu, pagination, then calendar/category/tag family.
- Delivery callback patterns are disjoint for mandatory join, batch, overtake, retry, and reactions.
- No duplicate top-level Python function definitions found in the 22-module package.
- No unresolved cross-module imported call references found in the static scan.

## Catalogue state audit
Persistent ReplyKeyboard is enabled (`one_time_keyboard=False`, `is_persistent=True`). Search intentionally removes the keyboard while awaiting free-form search and restores the main menu after result/error paths. `Open Menu` and `Back` behavior remains separate from pagination callbacks.

## Regression checks
- Python compile: PASS for all 22 modules.
- Duplicate function scan: PASS.
- Duplicate command scan after repair: PASS for command names within admin.
- Resolver/routing code: unchanged in this repair.
- Database reset/migration: none added.

## Safety
This repair is intentionally small: only the unreachable duplicate `/dashboard` registration was removed. Existing command semantics are preserved.
