V9.6.4 — Recovery integration hardening

• Recovery target now gets a preflight audit for Catalogue Bot and Delivery Bot admin access.
• If a service bot is already a member and Storage Bot has promotion rights, Storage Bot attempts to promote it automatically.
• Added “Check Bot Access” in Recovery UI.
• New recovered channel becomes the shared runtime Primary; Catalogue/Delivery/Admin already read storage_config.primary() dynamically.
• Missing service-bot admin rights are surfaced instead of silently leaving the new channel half-integrated.
• No channel link/username/invite is required; numeric channel ID remains the only recovery input.
• Existing resumable recovery/checkpoint behavior is preserved.


## V9.6.5 hardening
Persistent standby channel, safe pre-cutover checkpoints, cover migration when available, and integration-gated cutover.
