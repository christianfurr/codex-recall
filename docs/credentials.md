# Saved logins and approved administrator actions

Let Codex use a website login or a specific administrator action you have set up locally. Passwords are entered in your terminal and stored in your existing Linux keyring. Codex requests a saved alias; it receives an action result, never the password.

This optional companion is separate from the memory database. The base installation keeps its six memory tools. Enabling the companion registers `local_credentials` with five additional tools.

## Enable it

Use a Linux desktop with an existing **Linux Secret Service** default collection, unlocked through your normal desktop keyring application. The companion does not create a collection, open unlock prompts, or fall back to plaintext files. Website login also requires a working graphical session and Chromium's normal Linux runtime dependencies and sandbox.

From the repository:

```bash
./install.sh --credentials
.venv/bin/codex-recall-credentials status
codex mcp get local_credentials
```

The installer downloads the pinned optional Python dependencies and Playwright Chromium. It does not use sudo or install system packages. A healthy credential status reports `available: true` and `locked: false`. Unlock your keyring through the desktop if it is locked. Start a new Codex session to load the additional tools and guidance.

Start Codex from your desktop login session so it can reach the session keyring and display. The managed MCP registration forwards six desktop connection/data-directory variable names without saving their values: `DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`, and `XDG_DATA_HOME`. This uses Codex's [documented `env_vars` setting](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## Save a website login

Run this yourself in an interactive terminal:

```bash
.venv/bin/codex-recall-credentials add github --url https://github.com/login
```

The command prompts for the username and password with hidden input. There are no username or password flags. Keep secrets out of chat, shell arguments, environment variables, memory tools, and pasted logs.

Choose a simple alias such as `github` or `work-dashboard`: 1–64 lowercase letters, digits, underscores, or hyphens, starting with a letter. Register the exact HTTPS login page, including its path. URLs cannot contain embedded credentials, queries, fragments, or whitespace. The site origin is its scheme, host, and port.

Then ask Codex:

> Sign in to GitHub using my saved github login.

A dedicated, visible Chromium window opens with its own private persistent profile. The companion automates an ordinary top-level login form with one username field, one password field, one supported submit control, and a same-origin HTTPS POST action. It checks the form before filling and submission. Redirects, cross-origin forms, unusual multi-step pages, and unsupported controls may need your attention.

**Finish MFA, passkeys, or an unsupported flow yourself in that browser.** A `submitted` result means the form was submitted; `needs_attention` means automation could not complete. Both return `authentication_confirmed: false`. Confirm the signed-in state in the visible website. This companion exposes no general browser navigation, page-reading, or account-action tool.

The browser checks HTTP requests and blocks WebSockets. Before filling, it may allow unauthenticated HTTPS static resources from other origins; after filling begins, request routing is restricted to the saved origin. These runtime guards are not a comprehensive network firewall. JavaScript running in the login page, including scripts the site loads, can observe filled credentials. Register only origins you trust with that account.

## Save a sudo password and approve one action

Create the profile and register the exact command from your own interactive terminal:

```bash
.venv/bin/codex-recall-credentials add machine-sudo --sudo
.venv/bin/codex-recall-credentials allow machine-sudo check-host -- /usr/bin/id
```

The password prompt is hidden. The command after `--` is stored as a fixed executable and literal arguments. No arguments can be added through MCP. The executable and its resolved parent directories must be root-owned and protected against group/other writes. Shells, interpreters, and command dispatchers are rejected. Approval is checked again when the action runs.

Ask Codex:

> Run the approved check-host action using machine-sudo.

Codex calls `run_sudo` with `alias: "machine-sudo"` and `action: "check-host"`. Sudo receives the password through a private one-use askpass channel. Command input is disconnected, and stdout/stderr are discarded. The response reports `succeeded`, `failed`, `timed_out`, or `canceled`, with an exit code when available. For this example, a successful status confirms the approved command completed; its identity output is deliberately withheld.

Approve commands whose full behavior you understand, including arguments, files, configuration, and helpers they can load. An approved program retains its root capabilities; a fixed command approval is not a capability sandbox. The companion does not change sudo policy, configure passwordless sudo, or independently refresh sudo tickets. Having a saved password does not authorize unrelated changes or changes to security controls.

## Manage saved profiles

```bash
# Show aliases, origins, kinds, and approved action names; no usernames or passwords.
.venv/bin/codex-recall-credentials list

# Enter a replacement password locally. Sudo replacement preserves approved actions.
.venv/bin/codex-recall-credentials add machine-sudo --sudo --replace

# Revoke one action, retaining the password and other approvals.
.venv/bin/codex-recall-credentials revoke machine-sudo check-host

# Remove a profile and its password from this application's keyring namespace.
.venv/bin/codex-recall-credentials remove github
```

Use `add github --url https://github.com/login --replace` to replace a website profile. `allow ... --replace -- /absolute/executable fixed-arguments` replaces an existing named action. A profile's kind cannot be changed by replacement; remove it and create the new kind explicitly.

| MCP tool | What Codex can do |
| --- | --- |
| `credential_status` | Check whether the keyring exists and is locked. |
| `list_logins` | List aliases, site origins, kinds, and approved action names. |
| `sign_in` | Request the saved website login by `alias`. |
| `run_sudo` | Run a previously approved `alias` and `action`. |
| `close_login_browser` | Close the dedicated window for an `alias`, retaining browser files. |

No MCP tool adds credentials, changes approvals, or returns a stored password or username. Credential errors are sanitized, and browser pages and command output are withheld from the protocol.

## Disable or remove

```bash
./install.sh --no-credentials
```

This removes the managed credential registration and its global guidance. Restart Codex afterward. Memory stays enabled; saved keyring entries and browser files are retained. Running `./install.sh` without a credential flag preserves the existing enabled/disabled choice.

Use the credential CLI's `remove` command before uninstalling if you want to delete a saved keyring profile. Removing a profile does **not** delete website sessions or cookies. Closing a browser also retains its files. The dedicated profiles live under `~/.local/share/codex-recall/browser-profiles/` (or `$XDG_DATA_HOME/codex-recall/browser-profiles/`), with per-alias/origin hashed directory names. Close those browsers before clearing their session data locally. The full uninstall retains these files and keyring entries, including when `--delete-data` removes the memory database.

## Storage and trust

Credential payloads use the existing OS keyring, outside SQLite memory storage, memory backups, and JSON exports. Keyring encryption, recovery, and access policy belong to your provider. The keyring is not an isolation boundary from other programs running as your user or from privileged processes.

Browser cookies and website state are separate files protected by private directory/file permissions. Those permissions are not encryption, and cookies may keep an account signed in after its saved password is removed. The browser and sudo process must handle the secret locally while performing the requested action; memory copies and provider behavior are outside any secure-erasure guarantee.

See [Security](../SECURITY.md) for the trust boundaries and private vulnerability reporting.
