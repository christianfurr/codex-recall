# Changelog

## 1.1.0

- Optional `local_credentials` companion with five action tools, preserving the six memory tools and existing memory storage.
- Hidden local username/password entry into an existing unlocked Linux Secret Service keyring; no plaintext fallback or MCP password getter.
- Saved HTTPS website login aliases in dedicated visible Chromium profiles, with same-origin form checks, request guards, and owner-completed MFA/passkeys.
- Named fixed sudo commands approved locally by the owner, using a private one-use askpass channel and withholding command output.
- Opt-in installation, reversible credential registration, local profile removal/action revocation, and documented keyring, browser, and privileged-command trust limits.

## 1.0.0

- Six local MCP tools for selective memory, keyword recall, correction, deletion, browsing and health checks.
- SQLite FTS5 with transactional indexing, WAL concurrency, expiration, scope isolation and explicit decision supersession.
- Secret-pattern rejection, private files, consistent backups, coordinated restore and JSON export.
- Reversible Codex registration, concise global guidance and idempotent installation.
- A bounded starting brief loads recent global preferences and project decisions before keyword search, helping fresh Codex sessions reuse context when the task is worded differently.
