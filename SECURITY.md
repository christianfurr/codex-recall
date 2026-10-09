# Security

Do not submit real credentials or personal memory contents in public issues. If a vulnerability affects local data or secret rejection, report it privately to the repository owner's GitHub account before disclosing details publicly. This project does not currently operate a separate security mailbox.

The service has no network listener, API keys, model downloads or telemetry. It exposes memory through stdio to the invoking client. Returned data becomes part of that client's context; Codex may process it through its configured model provider. This service cannot control downstream client data handling.

Private directories and files protect against other Linux users, not other processes running as the same user or privileged users. They are not encryption. Pattern-based secret detection is imperfect and does not replace careful memory selection. Memory text is untrusted reference data and must never override active instructions.

Deleting a memory removes it from the active logical database. SQLite, storage hardware, old backups, snapshots and filesystem copies may retain previous bytes; deletion is not guaranteed forensic erasure.
