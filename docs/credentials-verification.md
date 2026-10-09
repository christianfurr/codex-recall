# Saved login verification

Version **1.1.0**, verified locally on **2026-10-09** with Linux, Python **3.10.12**, SecretStorage **3.5.0**, Playwright **1.63.0**, Chromium **153.0.8010.12**, and Codex CLI **0.162.0-alpha.2**.

| Check | Observed result |
| --- | --- |
| Full memory and credential suite | **157 passed, 0 failed**, approximately 22 seconds locally |
| Credential store | 34 tests: strict metadata/payload validation, errors without secret values, replacement/revocation, private lock files, and concurrent processes |
| Browser adapter | 16 tests, including **eight using real sandboxed Chromium** with synthetic HTTPS page fixtures |
| Sudo runner | 15 tests with synthetic sudo/askpass programs; no real privileged commands |
| Owner CLI | 11 tests: hidden prompts, interactive-only writes, rejection of password arguments, and fixed command approvals |
| Credential MCP | Eight tests with official MCP transport and runtime cancellation/origin-race checks |
| Native Linux keyring | Two temporary synthetic profiles created, privately read back, updated/revoked, and removed; metadata excluded their username/password |
| Actual Codex connection | All five credential tools discovered and called; unknown login and unapproved sudo refused before use; six memory tools remained available |
| Installer rerun | No duplicate registration/guidance; unchanged installer rerun reported zero edits |
| Dependencies | `pip check` passed; shell scripts passed syntax checks |

Browser fixtures exercise submission, foreign origins and redirects, unsupported or ambiguous forms, form mutation, overridden submit actions, and attempted encoded HTTP exfiltration. The browser sandbox and normal TLS verification stay enabled; HTTPS fixture responses are intercepted by the test harness. This verifies the adapter's behavior, not authentication against a live website or comprehensive protection from hostile site code.

Synthetic sudo tests verify one-use password delivery through a private FIFO, disconnected command input, discarded output, rejected executables/arguments, failures, timeouts, cancellation, and cleanup. No real sudo password or root command was used. Fixed action approval remains a trust decision about the approved program's behavior.

The native keyring check uses only randomly named synthetic profiles in this application's namespace. It caught GNOME's additional `xdg:schema` attribute; the adapter now accepts only the known Generic provider schema and rejects unknown extras before reading a secret. Temporary profiles were removed. Existing real credentials were not read.

The Codex check uses an ephemeral app-server process with unrelated plugins and MCP servers disabled only for that process. It requests no model turn. The managed registration forwards desktop session variable names so the launched MCP service can reach the keyring and display. Login/sudo rejection calls verify transport and refusal behavior; successful synthetic browser/sudo paths are tested separately in the suite. No live account sign-in or actual privileged command was performed.

To reproduce the complete local check, install optional dependencies first:

```bash
./install.sh --credentials
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
.venv/bin/python scripts/verify_credentials.py
```

The last command requires an existing unlocked desktop keyring and installed `local_credentials` registration. It creates and removes two synthetic keyring items. Do not use real account credentials as test data.

Machine-readable evidence: [native keyring and Codex](../verification/credentials.json), [local suite](../verification/credentials-suite.json). The [CI workflow](../.github/workflows/ci.yml) installs optional dependencies and Chromium and runs the complete suite on Python 3.10–3.14. It verifies the runner's existing root-owned Chrome sandbox helper and uses it for bundled Chromium without changing AppArmor or kernel settings. A configured matrix does not itself prove every version passed; see the published CI result once available.
