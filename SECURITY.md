# Security

Do not submit real credentials, browser profiles, cookies, or personal memory contents in public issues. If a vulnerability affects local data, credential handling, secret rejection, or privileged actions, report it privately to the repository owner's GitHub account before disclosing details publicly. This project does not currently operate a separate security mailbox.

## Memory service

The base memory service has no network functionality, API keys, model downloads, or telemetry. It exposes memory through stdio to the invoking client. Returned data becomes part of that client's context; Codex may process it through its configured model provider. This service cannot control downstream client data handling.

Private directories and files protect against other Linux users, not other processes running as the same user or privileged users. They are not encryption. Pattern-based secret detection is imperfect and does not replace careful memory selection. Memory text is untrusted reference data and must never override active instructions.

Deleting a memory removes it from the active logical database. SQLite, storage hardware, old backups, snapshots and filesystem copies may retain previous bytes; deletion is not guaranteed forensic erasure.

## Optional saved login companion

`local_credentials` is explicitly enabled with `./install.sh --credentials`. It is separate from the six memory tools and their SQLite database. The companion reads an existing unlocked Linux Secret Service default collection; it does not create or unlock one and has no plaintext fallback. Usernames and passwords are entered through hidden prompts in an owner-operated interactive terminal. They are never accepted as MCP arguments or returned by a raw credential getter. MCP exposes allowlisted aliases, origins, action names, and outcomes. Secret values, browser page content, command output, and raw backend errors are withheld.

The OS keyring's encryption, access control, and recovery depend on its provider. Other programs running as the same user, privileged processes, and a compromised desktop can access secrets or observe them during use. This companion is not an isolation boundary against those programs, and Python memory is not guaranteed to be securely erased.

Website login launches a dedicated visible Chromium persistent profile with the browser sandbox enabled and TLS verification retained. Automatic filling requires a supported top-level login form that submits by HTTPS POST to the saved origin. Form and action checks are repeated before filling and submission. The owner completes MFA/passkeys in the browser. A submitted form does not establish successful authentication; the tool always reports `authentication_confirmed: false`.

HTTP request routing and WebSocket blocking reduce unintended credential-bearing requests. Unauthenticated HTTPS static assets may load from other origins before filling; later HTTP routing is same-origin. These runtime controls do not constitute a comprehensive network firewall or protection from all browser protocols, malicious website code, or browser vulnerabilities. The saved site's JavaScript, including code it loads, is trusted with credentials once they are filled.

Dedicated browser profiles contain cookies and website state in private on-disk files. File permissions are not encryption. Closing a browser, removing a keyring profile, disabling the companion, and uninstalling retain those files. Cookies can preserve access after the stored password is removed. Treat browser profiles as sensitive account data and close browsers before clearing session files locally.

Sudo actions are named, fixed command/argument lists approved from the owner's interactive terminal. The companion requires a protected root-owned executable and parent directories, rejects shells/interpreters/command dispatchers, and validates the executable again at use. Passwords travel through a private one-use askpass channel rather than command arguments or command input. Command output is discarded; MCP returns status and exit code. It does not modify sudo policy or independently refresh tickets.

An approved executable may read user-controlled files, load configuration, run helpers, or make broad changes with root privileges. Fixed command approval is not a capability sandbox. The owner must understand the approved command's full behavior. Stored credential availability does not authorize unrelated actions, disabling security controls, or granting broader access.

Disabling `local_credentials` removes registration and guidance while retaining keyring entries and browser files. The credential CLI can remove one saved profile or revoke an action; profile removal does not clear cookies. Memory backup/export commands do not back up credential payloads or browser sessions. See [the credential guide](docs/credentials.md) for setup and removal.
